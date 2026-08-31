"""Dedicated health monitor for the mcp-decompose-and-recompose skill.

Runs three probes on a schedule to catch the failure modes specific to the
decompose path (half-finished runs, workspace-contract violations, and the
scene_detect tool itself going down):

  - Probe A — probe_scene_detect:  round-trip scene_detect via MCP JSON-RPC
  - Probe B — probe_decompose_log_tail:  byte-offset scan of logs/decompose.log
  - Probe C — probe_workspace_contract:  walk projects/ root for violations

Cron entry — /etc/cron.d/openmontage-decompose-monitor:
  */5 * * * * root /usr/bin/flock -n /var/lock/openmontage-decompose-monitor.lock \
      /usr/bin/python3 /opt/OpenMontage_Voicebox/tools/decompose_health_monitor.py \
      >> /var/log/openmontage/decompose_monitor.log 2>&1

Operator step (post-merge):
  install -m 644 /etc/cron.d/openmontage-decompose-monitor \
      /etc/cron.d/openmontage-decompose-monitor

Usage:
  python3 tools/decompose_health_monitor.py            # normal cron tick
  python3 tools/decompose_health_monitor.py --dry-run   # probe but don't email / update state
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers  # RotatingFileHandler
import os
import re
import smtplib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import format_datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration (defaults overridable via env)
# --------------------------------------------------------------------------- #

#: Path to OpenMontage repo root.
REPO_ROOT = Path(os.environ.get("OPENMONTAGE_REPO", "/opt/OpenMontage_Voicebox"))

#: Decompose log file.
LOG_DIR = REPO_ROOT / "logs"
DECOMPOSE_LOG = LOG_DIR / "decompose.log"

#: Projects root (where the workspace contract is enforced).
PROJECTS_DIR = REPO_ROOT / "projects"

#: Default alert recipient.
ALERT_TO = os.environ.get("DECOMPOSE_ALERT_TO", "18218401359@139.com")

#: Cooldown between repeated alerts for the same fault (seconds).
ALERT_COOLDOWN_SEC = int(os.environ.get("DECOMPOSE_COOLDOWN_SEC", "1800"))

#: How far back Probe B scans the log tail (minutes).
LOG_TAIL_MINUTES = int(os.environ.get("DECOMPOSE_LOG_TAIL_MINUTES", "10"))

#: State file for cooldown tracking.
STATE_FILE = Path(os.environ.get(
    "DECOMPOSE_STATE_FILE", "/var/lib/openmontage/decompose_monitor_state.json"))

#: SMTP creds live in `<repo>/.env`.
OM_ENV = REPO_ROOT / ".env"

#: BFF env for MCP credentials.
BFF_ENV = REPO_ROOT / "frameflow/bff/.env"

PROBE_TIMEOUT = float(os.environ.get("DECOMPOSE_PROBE_TIMEOUT", "10"))

# --------------------------------------------------------------------------- #
# Logging — single source of truth for the operator
# --------------------------------------------------------------------------- #


def setup_logging() -> logging.Logger:
    """Configure a rotating file logger + stderr (idempotent on cron re-invoke)."""
    log = logging.getLogger("decompose_health_monitor")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Rotating file handler — 10 MB × 5 backups, same semantics as mcp_health.log
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(  # type: ignore[attr-defined]
            str(DECOMPOSE_LOG), maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8")
    except (PermissionError, OSError):
        fh = logging.NullHandler()
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Also emit to stderr so cron redirects it to the cron log file.
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


LOG = setup_logging()


# --------------------------------------------------------------------------- #
# Config parsing — tiny .env parser (mirrors mcp_health_monitor.py)
# --------------------------------------------------------------------------- #

_KV_RE = re.compile(
    r'^\s*(?:export\s+)?(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*'
    r'(?P<val>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\[[^\]]*\]|[^\s#]+)'
    r'\s*(?:#.*)?$',
)


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        LOG.warning("cannot read %s: %s", path, exc)
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        out[m.group("key")] = _strip_quotes(m.group("val"))
    return out


def load_smtp_config() -> dict[str, str]:
    """Read SMTP creds from <repo>/.env."""
    cfg = parse_env_file(OM_ENV)
    missing = [k for k in ("sender", "smtpserver", "username", "passwd")
               if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"{OM_ENV} missing SMTP keys: {missing}. "
            f"Expected: sender / smtpserver / username / passwd")
    return cfg


def load_mcp_config() -> tuple[str, str]:
    """Read MCP base URL + Bearer token.

    Primary: <repo>/frameflow/bff/.env (same path as mcp_health_monitor.py).
    Fallback: <repo>/.env if the BFF path does not exist.
    """
    bff_cfg = parse_env_file(BFF_ENV)
    base = bff_cfg.get("MCP_BASE_URL", "").rstrip("/")
    token = bff_cfg.get("MCP_API_TOKEN", "")

    # Fallback: read directly from <repo>/.env (used in worktrees / minimal setups).
    if not base or not token:
        root_cfg = parse_env_file(OM_ENV)
        base = base or root_cfg.get("MCP_BASE_URL", "").rstrip("/") or "http://localhost:8900/mcp"
        token = token or root_cfg.get("MCP_API_TOKEN", "")

    if not base or not token:
        raise RuntimeError(
            f"Neither {BFF_ENV} nor {OM_ENV} has MCP_BASE_URL / MCP_API_TOKEN")
    return base, token


# --------------------------------------------------------------------------- #
# Probe A — scene_detect round-trip via inline MCP JSON-RPC
# --------------------------------------------------------------------------- #


class MCPClient:
    """Minimal MCP Streamable-HTTP client — copied verbatim from
    mcp_health_monitor.py:355-423. Do NOT import mcp_health_monitor
    (module-level side effects)."""

    def __init__(self, base_url: str, token: str, timeout: float):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.sid: str | None = None

    def _post(self, body: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.base_url, data=json.dumps(body).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.sid = r.headers.get("Mcp-Session-Id") or self.sid
                data = r.read().decode()
                if data.startswith("event:"):
                    payload = ""
                    for line in data.splitlines():
                        if line.startswith("data:"):
                            payload += line[5:].strip()
                    return json.loads(payload)
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"transport: {exc.reason}") from exc

    def initialize(self) -> dict:
        res = self._post({
            "jsonrpc": "2.0", "id": str(time.time()),
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {},
                       "clientInfo": {"name": "decompose_health_monitor",
                                      "version": "1.0"}}})
        if "error" in res:
            raise RuntimeError(f"initialize error: {res['error']}")
        return res

    def notify_initialized(self) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception as exc:  # noqa: BLE001
            LOG.debug("notifications/initialized ignored: %s", exc)

    def call_tool(self, name: str, arguments: dict) -> dict:
        res = self._post({
            "jsonrpc": "2.0", "id": str(time.time()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        if "error" in res:
            raise RuntimeError(f"tool {name} error: {res['error']}")
        return res


def _build_probe_asset() -> Path:
    """Create a minimal 8-byte MP4 probe file at /tmp/decompose_probe.mp4."""
    path = Path("/tmp/decompose_probe.mp4")
    # A valid MP4 header (tiny but parseable by ffprobe / scene_detect)
    path.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70")
    return path


def probe_scene_detect(base_url: str, token: str) -> dict:
    """Probe A — scene_detect round-trip via MCP JSON-RPC.

    Creates a minimal 8-byte MP4 probe file, calls scene_detect via the
    inline MCPClient, and returns ok=True iff the tool call succeeds.
    """
    t0 = time.monotonic()
    tags: list[str] = []
    error = ""

    # Probe file — regenerate each tick so scene_detect doesn't cache
    probe_path = _build_probe_asset()

    try:
        client = MCPClient(base_url, token, timeout=PROBE_TIMEOUT)
        client.initialize()
        client.notify_initialized()

        result = client.call_tool("scene_detect", {
            "input_path": str(probe_path),
            "method": "content",
            "threshold": 0.3,
            "min_scene_length_seconds": 2.0,
            "output_path": "/tmp/decompose_probe_scenes.json",
        })
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # scene_detect with a tiny/bogus file returns success=True but zero
        # scenes — the probe is only checking that the tool IS callable.
        scenes = (
            result.get("result", {})
            .get("structuredContent", {})
            .get("data", {})
            .get("scene_count", 0)
        )
        ok = True  # tool was reachable; scene_count is informational
        tags = [f"scene_count={scenes}"]
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ok = False
        error = str(exc)[:200]
        tags = ["scene_detect_error"]

    return {
        "ok": ok,
        "elapsed_ms": elapsed_ms,
        "tags": tags,
        "error": error or None,
    }


# --------------------------------------------------------------------------- #
# Probe B — byte-offset scan of logs/decompose.log
# --------------------------------------------------------------------------- #


def probe_decompose_log_tail() -> dict:
    """Probe B — byte-offset scan of logs/decompose.log.

    Opens the file, seeks to max(0, end - 1MB), scans backwards for
    ``state=finish`` lines within the trailing LOG_TAIL_MINUTES window,
    and returns ok=True iff at least one ``event=decompose_run state=finish``
    line is found.

    Does NOT use file mtime — a file touched only by ``state=start`` with
    no following ``state=finish`` must NOT pass.
    """
    t0 = time.monotonic()
    tags: list[str] = []
    last_finish_epoch: float | None = None
    error = ""

    try:
        if not DECOMPOSE_LOG.exists():
            return {
                "ok": False,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "tags": ["log_file_missing"],
                "error": f"{DECOMPOSE_LOG} does not exist",
                "last_finish_epoch": None,
            }

        cutoff = time.time() - (LOG_TAIL_MINUTES * 60)
        finish_count = 0

        with open(DECOMPOSE_LOG, "rb") as f:
            # Seek to ~1 MB before EOF to find the tail window.
            f.seek(max(0, f.seek(0, 2) - 1 * 1024 * 1024))

            # Read to end, collect all lines.
            tail = f.read().decode("utf-8", errors="replace")

        for raw_line in reversed(tail.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            if "event=decompose_run state=finish" not in line:
                continue

            # Parse epoch from line — look for "ts=..." or "timestamp=..."
            ts_match = re.search(r'(?:ts|timestamp)=(\d+(?:\.\d+)?)', line)
            if ts_match:
                try:
                    ts_val = float(ts_match.group(1))
                    # If ts is in seconds (wallclock), compare with cutoff.
                    # If it looks like a unix epoch, use it directly.
                    if ts_val > 1_000_000_000:
                        # Already a unix epoch in seconds.
                        if ts_val >= cutoff:
                            finish_count += 1
                            if last_finish_epoch is None or ts_val > last_finish_epoch:
                                last_finish_epoch = ts_val
                    else:
                        # Floating seconds from some relative clock — treat as
                        # valid if any finish line exists in the tail.
                        finish_count += 1
                        if last_finish_epoch is None:
                            last_finish_epoch = ts_val
                except ValueError:
                    # Couldn't parse epoch; count the line anyway as a fallback.
                    finish_count += 1
                    if last_finish_epoch is None:
                        last_finish_epoch = 0.0
            else:
                # No timestamp field — count it as a valid finish line.
                finish_count += 1
                if last_finish_epoch is None:
                    last_finish_epoch = 0.0

        ok = finish_count > 0
        if not ok:
            tags = ["no_finish_event_in_tail"]
        else:
            tags = [f"finish_count={finish_count}"]

    except Exception as exc:
        ok = False
        error = str(exc)[:200]
        tags = ["log_tail_scan_error"]

    return {
        "ok": ok,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "tags": tags,
        "error": error or None,
        "last_finish_epoch": last_finish_epoch,
    }


# --------------------------------------------------------------------------- #
# Probe C — workspace-contract scan of projects/ root
# --------------------------------------------------------------------------- #

# Files that are expected at projects/ root (not violations).
_KNOWN_JUNK_TOP_FILES = {"events.jsonl", "README.md"}


def _decompose_event_safe(event: str, **fields: Any) -> None:
    """Write one line to logs/decompose.log without importing mcp_server."""
    try:
        parts = [f"event={event}"]
        parts += [f"{k}={v}" for k, v in fields.items() if v is not None]
        line = " ".join(parts)
        with open(DECOMPOSE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def probe_workspace_contract() -> dict:
    """Probe C — walk projects/ root and flag unexpected files.

    Allow-lists:
      - directories: _scratch/, _analysis/
      - dotfiles
      - _KNOWN_JUNK_TOP_FILES (events.jsonl, README.md)

    Every other file at projects/ root gets a ``workspace_violation:<name>`` tag.
    Caps at 10 tags; if truncated, appends ``workspace_violation_truncated``.
    """
    t0 = time.monotonic()
    tags: list[str] = []
    error = ""

    try:
        if not PROJECTS_DIR.exists():
            return {
                "ok": True,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "tags": [],
                "error": None,
            }

        violations: list[str] = []
        allow_dirs = {"_scratch", "_analysis"}

        for entry in PROJECTS_DIR.iterdir():
            name = entry.name

            # Allow-listed: directories in allow_dirs
            if entry.is_dir() and name in allow_dirs:
                continue
            # Allow-listed: dotfiles
            if name.startswith("."):
                continue
            # Allow-listed: known junk top files
            if name in _KNOWN_JUNK_TOP_FILES:
                continue

            # Everything else is a violation.
            if entry.is_file():
                violations.append(name)

        if violations:
            tags = [f"workspace_violation:{n}" for n in violations[:10]]
            if len(violations) > 10:
                tags.append("workspace_violation_truncated")

        ok = len(violations) == 0

    except Exception as exc:
        ok = False
        error = str(exc)[:200]
        tags = ["workspace_scan_error"]

    # Write one scan line per probe run.
    try:
        scan_tag = "ok" if ok else f"violations={len(violations)}"
        _decompose_event_safe("decompose_monitor_workspace_scan", ok=ok, tag=scan_tag)
        if tags:
            files_csv = ",".join(violations[:10])
            _decompose_event_safe(
                "decompose_monitor_workspace_violation",
                files=files_csv if violations else None,
            )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": ok,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "tags": tags,
        "error": error or None,
    }


# --------------------------------------------------------------------------- #
# Alerting — cooldown + email (mirrors mcp_health_monitor.py)
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except OSError:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        LOG.warning("could not save state to %s: %s", STATE_FILE, exc)


def should_alert(state: dict, fault_key: str, now: float) -> tuple[bool, dict]:
    """Return (should_send, updated_fault_entry)."""
    faults = state.setdefault("faults", {})
    entry = faults.get(fault_key, {})
    last_alerted = entry.get("last_alerted_epoch", 0.0)
    age = now - last_alerted
    if not last_alerted or age >= ALERT_COOLDOWN_SEC:
        entry = {
            "first_seen": entry.get("first_seen") or
                          datetime.fromtimestamp(now,
                              tz=timezone.utc).isoformat(),
            "last_alerted_epoch": now,
            "alert_count": entry.get("alert_count", 0) + 1,
        }
        faults[fault_key] = entry
        return True, entry
    LOG.info("alert suppressed (cooldown): fault=%s age=%.0fs < %ds",
             fault_key, age, ALERT_COOLDOWN_SEC)
    return False, entry


def send_email(smtp_cfg: dict, subject: str, body: str,
               to_addr: str = ALERT_TO) -> None:
    """Send a single plain-text email via SMTP (QQ / port 465)."""
    from_addr = smtp_cfg["sender"]
    smtp_host = smtp_cfg["smtpserver"]
    smtp_user = smtp_cfg["username"]
    smtp_pass = smtp_cfg["passwd"]
    smtp_port = int(os.environ.get("MONITOR_SMTP_PORT", "465"))

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = format_datetime(datetime.now(timezone.utc))

    LOG.info("smtp connect: %s:%d as %s", smtp_host, smtp_port, smtp_user)
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=PROBE_TIMEOUT) as s:
        s.login(smtp_user, smtp_pass)
        s.sendmail(from_addr, [to_addr], msg.as_string())
    LOG.info("email sent to %s: %s", to_addr, subject)


# --------------------------------------------------------------------------- #
# Run summary formatter (3-probe layout)
# --------------------------------------------------------------------------- #


def fmt_run_summary(scene_detect_res: dict, log_tail_res: dict,
                    workspace_res: dict, state_key: str) -> str:
    """Plain-text body for fault / recovery emails (3-probe layout)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"OpenMontage Decompose Monitor — {now}",
        f"state: {state_key}",
        "",
        "[Probe A — scene_detect]",
        f"  ok      : {scene_detect_res['ok']}",
        f"  elapsed : {scene_detect_res['elapsed_ms']} ms",
        f"  tags    : {scene_detect_res.get('tags') or '-'}",
    ]
    if scene_detect_res.get("error"):
        lines.append(f"  error   : {scene_detect_res['error']}")
    lines += [
        "",
        "[Probe B — log_tail]",
        f"  ok      : {log_tail_res['ok']}",
        f"  elapsed : {log_tail_res['elapsed_ms']} ms",
        f"  tags    : {log_tail_res.get('tags') or '-'}",
    ]
    if log_tail_res.get("last_finish_epoch"):
        lines.append(f"  last_finish: {datetime.fromtimestamp(log_tail_res['last_finish_epoch'], tz=timezone.utc).isoformat()}")
    lines += [
        "",
        "[Probe C — workspace_contract]",
        f"  ok      : {workspace_res['ok']}",
        f"  elapsed : {workspace_res['elapsed_ms']} ms",
        f"  tags    : {workspace_res.get('tags') or '-'}",
    ]
    if workspace_res.get("error"):
        lines.append(f"  error   : {workspace_res['error']}")
    lines += [
        "",
        f"cooldown        {ALERT_COOLDOWN_SEC}s per FAULT key",
        "",
        "Reply with `ok` to silence for 1h, or check upstream.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def run_once(smtp_cfg: dict, base_url: str, token: str,
             dry_run: bool = False) -> int:
    """One cron tick. Returns shell exit code (0=clean, 1=fault)."""
    LOG.info("probing decompose path at %s", base_url)

    scene_detect_res = probe_scene_detect(base_url, token)
    log_tail_res = probe_decompose_log_tail()
    workspace_res = probe_workspace_contract()

    tags: list[str] = []
    tags += [t for t in scene_detect_res.get("tags", [])
             if t.startswith("scene_") or "error" in t]
    tags += [t for t in log_tail_res.get("tags", [])
             if t.startswith("log_") or "error" in t or t == "no_finish_event_in_tail"]
    tags += [t for t in workspace_res.get("tags", [])
             if t.startswith("workspace_")]

    all_ok = scene_detect_res["ok"] and log_tail_res["ok"] and workspace_res["ok"]
    state_key = "OK" if all_ok else f"FAULT[{','.join(tags) or 'unknown'}]"

    LOG.info("scene_detect: ok=%s elapsed_ms=%d tags=%s",
             scene_detect_res["ok"], scene_detect_res["elapsed_ms"],
             scene_detect_res["tags"])
    LOG.info("log_tail:    ok=%s elapsed_ms=%d tags=%s",
             log_tail_res["ok"], log_tail_res["elapsed_ms"],
             log_tail_res["tags"])
    LOG.info("workspace:    ok=%s elapsed_ms=%d tags=%s",
             workspace_res["ok"], workspace_res["elapsed_ms"],
             workspace_res["tags"])
    LOG.info("state_key: %s", state_key)

    state = load_state()
    prev = state.get("last_status", "OK")
    now_epoch = time.time()

    if state_key == "OK":
        if prev.startswith("FAULT"):
            LOG.info("recovery detected (was %s)", prev)
            subject = f"[Decompose] RECOVERED @ {datetime.now().strftime('%H:%M:%S')}"
            body = fmt_run_summary(scene_detect_res, log_tail_res,
                                   workspace_res, state_key)
            if not dry_run:
                try:
                    send_email(smtp_cfg, subject, body)
                except Exception as exc:  # noqa: BLE001
                    LOG.error("recovery email failed: %s", exc)
            state["faults"] = {}
        state["last_status"] = state_key
        if not dry_run:
            save_state(state)
        return 0

    # FAULT
    if dry_run:
        LOG.info("dry-run: fault detected, no email sent (state not saved)")
        return 1

    send, _entry = should_alert(state, state_key, now_epoch)
    if send:
        subject = (f"[Decompose] FAULT: "
                  f"{','.join(tags) or 'unknown'} @ "
                  f"{datetime.now().strftime('%H:%M:%S')}")
        body = fmt_run_summary(scene_detect_res, log_tail_res,
                               workspace_res, state_key)
        try:
            send_email(smtp_cfg, subject, body)
        except Exception as exc:  # noqa: BLE001
            LOG.error("alert email failed: %s", exc)
    state["last_status"] = state_key
    save_state(state)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="Probe but don't send email or persist state.")
    args = ap.parse_args()

    try:
        smtp_cfg = load_smtp_config()
        base_url, token = load_mcp_config()
    except Exception as exc:  # noqa: BLE001
        LOG.error("config error: %s", exc)
        return 2

    return run_once(smtp_cfg, base_url, token, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
