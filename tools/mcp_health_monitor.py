#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenMontage MCP Health Monitor — 5-minute cron probe + email→SMS alerting
=======================================================================

Why this exists
---------------
The remote MCP server (`lanes.ymxt.top:8900/mcp`, Streamable-HTTP, Bearer-auth)
is the only upstream the FrameFlow BFF can talk to. Today (2026-08-19) a
render job was orphaned because the upstream process restarted and the
submitter only learned about it after the fact — the BFF's 5-second
`/api/render-queue` poll is per-user, not a system-level canary.

This script is a system-level canary: every 5 minutes it runs two probes
and, on anomaly, emails `18218401359@139.com` (139.com mailbox → SMS).

Probes
------
1. **Initialize handshake** — shells out to `om_mcp_probe.py status`, which
   does a real JSON-RPC `initialize` with Bearer / Accept / Content-Type.
   Reuses existing probe so we don't reinvent protocol plumbing.

2. **Business tool call** — inline JSON-RPC against the same endpoint:
   `initialize` → `notifications/initialized` → `tools/call get_render_status`
   for a known-published sentinel job_id (`d75622b7...`).
   Confirms the upstream is not just alive but actually serving business
   traffic. The JSON-RPC + `Mcp-Session-Id` rotation pattern is copied
   from `om_mcp_probe.py:104-209` (we don't import it because that file
   runs as `__main__` and importing would re-trigger its CLI).

Severity / thresholds
---------------------
- `WARN_LATENCY` (default 8 s) — slow but functional. Logged as WARN.
- `CRIT_LATENCY` (default 15 s) — too slow for BFF's 5 s poll cadence.
  Logged as FAULT.
- `PROBE_TIMEOUT` (default 10 s) — hard cap, kills the probe.

Cooldown
--------
- Same `FAULT[<tag1>+<tag2>]` key: at most one email per
  `ALERT_COOLDOWN_SEC` (default 30 min).
- Recovery (`OK` after a fault): always emails a RECOVERED notice (no
  cooldown — recovery is rare and important).

Configuration
-------------
Reads SMTP creds from `/opt/OpenMontage/.env` (existing lines 69-74):
    sender="975762756@qq.com"
    receiver=['7284045@qq.com','18218401359@139.com']
    smtpserver="smtp.qq.com"
    username="975762756@qq.com"
    passwd="klrnzevlkbirbceg"

MCP base URL + Bearer come from `/opt/OpenMontage/frameflow/bff/.env`:
    MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp
    MCP_API_TOKEN=h6LQUTVPA5...

Run modes
---------
  python3 mcp_health_monitor.py            # normal cron tick
  python3 mcp_health_monitor.py --dry-run # probe but don't email / update state
  python3 mcp_health_monitor.py --test-alert # force a TEST email

Cron entry — /etc/cron.d/openmontage-mcp-monitor:
  */5 * * * * root /usr/bin/flock -n /var/lock/openmontage-mcp-monitor.lock \
      /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py \
      >> /var/log/openmontage/mcp_monitor.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
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

#: Path to OpenMontage repo root. SMTP creds are read from `<repo>/.env`.
REPO_ROOT = Path(os.environ.get("OPENMONTAGE_REPO", "/opt/OpenMontage"))

#: Path to BFF (where `MCP_BASE_URL` and `MCP_API_TOKEN` live).
BFF_ENV = REPO_ROOT / "frameflow/bff/.env"

#: SMTP creds live in `<repo>/.env` (NOT in BFF .env).
OM_ENV = REPO_ROOT / ".env"

#: Sentinel job_id for the business probe — known to be `published` in the
#: local BFF SQLite and on the upstream MCP. If this is ever garbage-collected
#: by the upstream we'll fall back to the most-recent published row.
SENTINEL_JOB_ID = "d75622b7d77b4ce392514c8c20beeccd"

#: Where the upstream MCP probe (`om_mcp_probe.py`) lives.
PROBE_PY = REPO_ROOT / "om_mcp_probe.py"

#: Where `om_mcp_probe.py` writes its own WARNING/ERROR output. We point it
#: at its native location (kept since Aug 19 06:06) so its noisy ERROR
#: lines ("上游探测失败", etc.) don't pollute our `SCRIPT_LOG` and confuse
#: operators into thinking the monitor itself is probing the wrong target.
PROBE_LOG = REPO_ROOT / "om_mcp_probe.log"

#: Default alert recipient (overridable via env). 139.com mailbox → SMS.
ALERT_TO = os.environ.get("MONITOR_ALERT_TO", "18218401359@139.com")

#: Hardcoded thresholds (seconds).
WARN_LATENCY = float(os.environ.get("MONITOR_WARN_LATENCY", "8"))
CRIT_LATENCY = float(os.environ.get("MONITOR_CRIT_LATENCY", "15"))
PROBE_TIMEOUT = float(os.environ.get("MONITOR_PROBE_TIMEOUT", "10"))
ALERT_COOLDOWN_SEC = int(os.environ.get("MONITOR_COOLDOWN_SEC", "1800"))

#: State and log paths.
STATE_FILE = Path(os.environ.get(
    "MONITOR_STATE_FILE", "/var/lib/openmontage/mcp_monitor_state.json"))
SCRIPT_LOG = Path(os.environ.get(
    "MONITOR_LOG_FILE", "/var/log/openmontage/mcp_monitor.log"))

# --------------------------------------------------------------------------- #
# Logging — single source of truth for the operator
# --------------------------------------------------------------------------- #


def setup_logging() -> logging.Logger:
    """Configure a logger that writes to stdout only.

    Cron redirects stdout to ``SCRIPT_LOG`` via ``>> $SCRIPT_LOG 2>&1``,
    so we deliberately do NOT add a second FileHandler — that would cause
    every line to be written twice when run from cron. Manual invocation
    (``./mcp_health_monitor.py``) just prints to stdout; if the operator
    wants a file copy they can ``tee`` or redirect themselves.
    """
    log = logging.getLogger("mcp_health_monitor")
    if log.handlers:  # idempotent (cron re-invokes the same script)
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


LOG = setup_logging()


# --------------------------------------------------------------------------- #
# Config parsing — read two .env files with simple line-by-line regex
# --------------------------------------------------------------------------- #


_KV_RE = re.compile(
    r'^\s*(?:export\s+)?(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*'
    r'(?P<val>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\[[^\]]*\]|[^\s#]+)'
    r'\s*(?:#.*)?$',
)


def _strip_quotes(v: str) -> str:
    """Strip matching surrounding quotes; for `receiver=[...]` keep brackets."""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def parse_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser — handles `KEY=VAL`, `KEY="VAL"`, `KEY='VAL'`,
    `KEY=[...]`, comments, blank lines. No variable expansion."""
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
    """Read SMTP creds from `<repo>/.env`."""
    cfg = parse_env_file(OM_ENV)
    missing = [k for k in ("sender", "smtpserver", "username", "passwd")
               if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"{OM_ENV} missing SMTP keys: {missing}. "
            f"Expected: sender / smtpserver / username / passwd")
    # ALERT_TO comes from `receiver` list — make sure our pinned value is in
    # the list (sanity check; doesn't filter it out).
    receiver = cfg.get("receiver", "")
    if ALERT_TO not in receiver:
        LOG.warning("ALERT_TO %s not in receiver list %s — proceeding anyway",
                    ALERT_TO, receiver)
    return cfg


def load_mcp_config() -> tuple[str, str]:
    """Read MCP base URL + Bearer from `<repo>/frameflow/bff/.env`."""
    cfg = parse_env_file(BFF_ENV)
    base = cfg.get("MCP_BASE_URL", "").rstrip("/")
    token = cfg.get("MCP_API_TOKEN", "")
    if not base or not token:
        raise RuntimeError(
            f"{BFF_ENV} missing MCP_BASE_URL or MCP_API_TOKEN")
    return base, token


# --------------------------------------------------------------------------- #
# Probe 1 — initialize handshake via existing om_mcp_probe.py status
# --------------------------------------------------------------------------- #


def probe_initialize(base_url: str, token: str) -> dict:
    """Run `om_mcp_probe.py status` against the MCP URL and parse its output.

    Returns dict with keys: ok (bool), elapsed_ms (int), tags (list[str]),
    stderr (str), raw_stdout_tail (str). Tags include 'upstream_down' /
    'upstream_slow' when the probe reports a problem.
    """
    t0 = time.monotonic()
    # `--token` is a top-level arg on `om_mcp_probe.py` (line 1265), so it
    # must come BEFORE the `status` subcommand. `--cpu-warn` / `--mem-warn`
    # / `--disk-warn` raise the resource-check thresholds so the probe
    # doesn't flag this host's CPU/mem/disk — we only care about upstream.
    cmd = [
        sys.executable, str(PROBE_PY),
        "--token", token,
        "--quiet",
        "--log", str(PROBE_LOG),  # probe's own log; keeps monitor.log clean
        "status",
        "--target", base_url,
        "--role", "bff",
        "--cpu-warn", "100",
        "--mem-warn", "100",
        "--disk-warn", "100",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "elapsed_ms": int(PROBE_TIMEOUT * 1000),
                "tags": ["init_timeout"], "stderr": "",
                "raw_stdout_tail": "<probe killed after %ds>" % PROBE_TIMEOUT}
    except OSError as exc:
        return {"ok": False, "elapsed_ms": int((time.monotonic()-t0)*1000),
                "tags": ["init_exec_fail"], "stderr": str(exc),
                "raw_stdout_tail": ""}

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    tags: list[str] = []
    ok = (proc.returncode == 0)
    stdout_tail = "\n".join(proc.stdout.splitlines()[-15:])
    # Probe's own logger writes to its --log file, but it also surfaces
    # curl errors to stderr (`curl: (52) Empty reply from server`, etc.).
    # Concatenate both streams so we can pattern-match the failure mode.
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")

    # Parse PROBLEM_TAGS= line (om_mcp_probe.py:1240) and VERDICT= (line1239).
    for line in proc.stdout.splitlines():
        if line.startswith("PROBLEM_TAGS="):
            raw = line.split("=", 1)[1].strip()
            if raw and raw != "-":
                tags = [t for t in raw.split(",") if t]
        if line.startswith("VERDICT=") and "PROBLEMS_FOUND" in line:
            ok = False

    # Map probe stderr / log substrings → our tag vocabulary. This catches
    # the "Empty reply from server" / curl-fallback cases where om_mcp_probe
    # fails but doesn't print a clean PROBLEM_TAGS line.
    #
    # Only apply when ok=False — otherwise harmless noise (e.g. the
    # `notifications/initialized` notification that some servers reply to
    # with a 4xx) would create spurious `init_http_4xx` tags on every tick.
    if not ok:
        for pattern, tag in _INIT_ERR_PATTERNS:
            if pattern.search(blob):
                tag_name = tag if tag not in tags else None
                if tag_name:
                    tags.append(tag_name)

    if elapsed_ms / 1000.0 > CRIT_LATENCY:
        if "init_latency_crit" not in tags:
            tags.append("init_latency_crit")
        ok = False
    elif elapsed_ms / 1000.0 > WARN_LATENCY:
        if "init_latency_warn" not in tags:
            tags.append("init_latency_warn")

    # PROBLEMS_FOUND from om_mcp_probe can flag non-init issues
    # (port_down, cpu_high, etc.). For OUR purpose — "is the MCP serving
    # traffic?" — those don't matter. If we ended up with zero `init_*`
    # tags, the JSON-RPC handshake path is healthy regardless of the
    # probe's overall verdict.
    if not any(t.startswith("init_") for t in tags):
        ok = True
    return {"ok": ok, "elapsed_ms": elapsed_ms, "tags": tags,
            "stderr": proc.stderr[-500:], "raw_stdout_tail": stdout_tail}


#: curl / uvicorn error substrings → our `init_*` tag vocabulary.
#: Order matters only insofar as the first match wins per pattern; we keep
#: each pattern distinct so multiple tags can stack (e.g. `init_empty_reply
#: +init_http_5xx` if both apply).
_INIT_ERR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"empty reply from server", re.I), "init_empty_reply"),
    (re.compile(r"connection refused", re.I), "init_conn_refused"),
    (re.compile(r"couldn'?t connect|failed to connect|could not connect"
                r"|connection (?:reset|aborted|closed)|broken pipe",
                re.I), "init_conn_fail"),
    (re.compile(r"timed?\s*out|timeout exceeded", re.I), "init_timeout"),
    (re.compile(r"ssl|certificate|tls|handshake", re.I), "init_tls"),
    (re.compile(r"\b401\b|unauthorized|missing or invalid bearer", re.I),
     "init_auth"),
    (re.compile(r"\b5\d\d\b", re.I), "init_http_5xx"),
    # Tightened: only fire on HTTP 4xx in HTTP response context (`HTTP/1.1
    # 4xx`, `HTTP/2 4xx`). The previous loose `\b4\d\d\b` over-matched
    # innocent port-listen output like `":443 -> OK"` (HTTPS listen) when
    # the probe was running with `role=bff` and the BFF was misbound to
    # a non-8080 port. That path produced `port_down:8080` (real) +
    # `init_http_4xx` (false-positive from the regex) every probe tick.
    (re.compile(r"\bHTTP/[\d.]+\s+4\d\d\b", re.I), "init_http_4xx"),
]


# --------------------------------------------------------------------------- #
# Probe 2 — business tool call (inline JSON-RPC)
# --------------------------------------------------------------------------- #


class MCPClient:
    """Minimal MCP Streamable-HTTP client. Pattern copied from
    `om_mcp_probe.py:104-209` (don't import — that file is a CLI, not a
    library). Rotates `Mcp-Session-Id` after every response, which the
    server requires."""

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
                       "clientInfo": {"name": "mcp_health_monitor",
                                      "version": "1.0"}}})
        if "error" in res:
            raise RuntimeError(f"initialize error: {res['error']}")
        # Server may rotate SID; sid_from_response is already saved.
        return res

    def notify_initialized(self) -> None:
        # `notifications/initialized` — server should accept silently.
        # Some servers return -32602 here; that's a known harmless quirk.
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


def probe_business(base_url: str, token: str) -> dict:
    """Run a real business tool call end-to-end. Returns ok/tags/elapsed."""
    t0 = time.monotonic()
    tags: list[str] = []
    try:
        client = MCPClient(base_url, token, PROBE_TIMEOUT)
        client.initialize()
        client.notify_initialized()
        res = client.call_tool(
            "get_render_status", {"render_job_id": SENTINEL_JOB_ID})
        content = res.get("result", {}).get("content", [])
        if not content or content[0].get("type") != "text":
            raise RuntimeError("unexpected tool response shape")
        inner = json.loads(content[0]["text"])
        # MCP returns success=False / an `error` field when the sentinel job
        # is no longer in upstream state (typically because the upstream
        # restarted and rebuilt its job→session index without this baseline
        # row — see SENTINEL_JOB_ID docstring). That's an operations issue
        # with the *sentinel reference*, NOT a fault against the upstream's
        # ability to serve business traffic. Tag as `biz_sentinel_missing`
        # so run_once() can keep the run classified OK and stop the
        # `biz_unexpected_status:none` 30-min 139-SMS storm.
        if inner.get("success") is False or "error" in inner:
            LOG.warning("sentinel %s missing from upstream (%s)",
                        SENTINEL_JOB_ID,
                        (inner.get("error") or "success=False")[:160])
            tags.append("biz_sentinel_missing")
        else:
            status = (inner.get("status") or "").lower()
            share = inner.get("share_url") or ""
            if status != "published":
                tags.append(f"biz_unexpected_status:{status or 'none'}")
            elif not share:
                tags.append("biz_missing_share_url")
    except Exception as exc:  # noqa: BLE001
        tags.append("biz_fail")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "elapsed_ms": elapsed_ms, "tags": tags,
                "error": str(exc)[:300]}

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if elapsed_ms / 1000.0 > CRIT_LATENCY:
        tags.append("biz_latency_crit")
    elif elapsed_ms / 1000.0 > WARN_LATENCY:
        tags.append("biz_latency_warn")
    return {"ok": not tags, "elapsed_ms": elapsed_ms, "tags": tags,
            "error": ""}


# --------------------------------------------------------------------------- #
# State + cooldown
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    """Load cooldown state. Returns a fresh dict if file is missing or invalid."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_status": "OK", "faults": {}}


def save_state(state: dict) -> None:
    """Atomic-ish state save: write to .tmp then rename."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(STATE_FILE)
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


# --------------------------------------------------------------------------- #
# Email — QQ SMTP via smtplib.SMTP_SSL, port 465
# --------------------------------------------------------------------------- #


def send_email(smtp_cfg: dict, subject: str, body: str,
               to_addr: str = ALERT_TO) -> None:
    """Send a single plain-text email via QQ SMTP. Raises on failure."""
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
# Main
# --------------------------------------------------------------------------- #


def fmt_run_summary(init_res: dict, biz_res: dict, state_key: str) -> str:
    """Plain-text body for fault / recovery emails. ~30 lines, fits in SMS."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"OpenMontage MCP Health Monitor — {now}",
        f"state: {state_key}",
        "",
        "[init probe]",
        f"  ok      : {init_res['ok']}",
        f"  elapsed : {init_res['elapsed_ms']} ms",
        f"  tags    : {init_res.get('tags') or '-'}",
    ]
    if init_res.get("stderr"):
        lines.append(f"  stderr  : {init_res['stderr']}")
    if init_res.get("raw_stdout_tail"):
        lines += ["  --- probe stdout tail ---",
                  init_res["raw_stdout_tail"]]
    lines += [
        "",
        "[business probe]",
        f"  ok      : {biz_res['ok']}",
        f"  elapsed : {biz_res['elapsed_ms']} ms",
        f"  tags    : {biz_res.get('tags') or '-'}",
    ]
    if biz_res.get("error"):
        lines.append(f"  error   : {biz_res['error']}")
    lines += [
        "",
        f"thresholds (s)  warn={WARN_LATENCY}  crit={CRIT_LATENCY}",
        f"cooldown        {ALERT_COOLDOWN_SEC}s per (FAULT[tags]) key",
        "",
        "Reply with `ok` to silence for 1h, or check upstream.",
    ]
    return "\n".join(lines)


def run_once(smtp_cfg: dict, base_url: str, token: str,
             dry_run: bool = False, test_alert: bool = False) -> int:
    """One cron tick. Returns shell exit code (0=clean, 1=fault)."""
    if test_alert:
        now_hms = datetime.now().strftime("%H:%M:%S")
        subject = f"[MCP] TEST ALERT @ {now_hms}"
        body = ("This is a test alert from OpenMontage MCP Health Monitor.\n"
                "If you got this on SMS, email→139.com forwarding works.\n")
        if dry_run:
            LOG.info("dry-run: %s | %s", subject, body.replace("\n", " | "))
            return 0
        try:
            send_email(smtp_cfg, subject, body)
        except Exception as exc:  # noqa: BLE001
            LOG.error("test-alert email failed: %s", exc)
            return 1
        return 0

    LOG.info("probing %s", base_url)
    init_res = probe_initialize(base_url, token)
    biz_res = probe_business(base_url, token)

    tags: list[str] = []
    tags += [t for t in init_res.get("tags", []) if t.startswith("init_")
             or t == "upstream_down"]
    tags += [t for t in biz_res.get("tags", []) if t.startswith("biz_")]
    # `biz_sentinel_missing` is OK at the run level — see probe_business:
    # sentinel reference is stale, but the upstream actually served the
    # JSON-RPC round trip + tool execution. Treating it as FAULT would
    # fire a 139-SMS every ALERT_COOLDOWN_SEC for a sentinel that we
    # never rotated.
    biz_ok = biz_res["ok"] or "biz_sentinel_missing" in biz_res.get("tags", [])
    state_key = "OK" if (init_res["ok"] and biz_ok) else f"FAULT[{','.join(tags) or 'unknown'}]"

    LOG.info("init: ok=%s elapsed_ms=%d tags=%s",
             init_res["ok"], init_res["elapsed_ms"], init_res["tags"])
    LOG.info("biz:  ok=%s elapsed_ms=%d tags=%s",
             biz_res["ok"], biz_res["elapsed_ms"], biz_res["tags"])
    LOG.info("state_key: %s", state_key)

    state = load_state()
    prev = state.get("last_status", "OK")
    now_epoch = time.time()

    if state_key == "OK":
        if prev.startswith("FAULT"):
            # Recovery — always email, no cooldown.
            LOG.info("recovery detected (was %s)", prev)
            subject = (f"[MCP] RECOVERED @ "
                       f"{datetime.now().strftime('%H:%M:%S')}")
            body = fmt_run_summary(init_res, biz_res, state_key)
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

    # state_key is FAULT[...]
    if dry_run:
        LOG.info("dry-run: fault detected, no email sent (state not saved)")
        return 1

    send, _entry = should_alert(state, state_key, now_epoch)
    if send:
        subject = (f"[MCP] FAULT: "
                   f"{','.join(tags) or 'unknown'} @ "
                   f"{datetime.now().strftime('%H:%M:%S')}")
        body = fmt_run_summary(init_res, biz_res, state_key)
        try:
            send_email(smtp_cfg, subject, body)
        except Exception as exc:  # noqa: BLE001
            LOG.error("alert email failed: %s", exc)
            # still save state — don't lose track of the fault
    state["last_status"] = state_key
    save_state(state)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="Probe but don't send email or persist state.")
    ap.add_argument("--test-alert", action="store_true",
                    help="Send a single test email and exit.")
    args = ap.parse_args()

    try:
        smtp_cfg = load_smtp_config()
        base_url, token = load_mcp_config()
    except Exception as exc:  # noqa: BLE001
        LOG.error("config error: %s", exc)
        return 2

    return run_once(smtp_cfg, base_url, token,
                    dry_run=args.dry_run, test_alert=args.test_alert)


if __name__ == "__main__":
    sys.exit(main())