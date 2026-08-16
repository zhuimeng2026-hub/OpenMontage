#!/usr/bin/env python3
"""Zero-dependency Linux monitor for FrameFlow/Remotion load tests.

Prints a compact live status line and optionally writes machine-readable JSONL.
It reads /proc only, so it can run on a minimal Ubuntu host without psutil.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import time
from pathlib import Path


TRACKED = ("remotion", "chrome", "ffmpeg", "mcp", "bff", "node")
DISK_RE = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def cpu_totals() -> tuple[int, int]:
    fields = read_text("/proc/stat").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def memory() -> dict[str, float]:
    values: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        try:
            values[key] = int(raw.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "total_gb": total / 2**30,
        "available_gb": available / 2**30,
        "used_percent": ((total - available) / total * 100) if total else 0,
        "swap_used_gb": (swap_total - swap_free) / 2**30,
    }


def disk_totals() -> tuple[int, int, int]:
    read_sectors = write_sectors = io_ms = 0
    for line in read_text("/proc/diskstats").splitlines():
        fields = line.split()
        if len(fields) < 14 or not DISK_RE.match(fields[2]):
            continue
        read_sectors += int(fields[5])
        write_sectors += int(fields[9])
        io_ms += int(fields[12])
    return read_sectors, write_sectors, io_ms


def network_totals() -> tuple[int, int]:
    received = sent = 0
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        if name.strip() == "lo":
            continue
        fields = raw.split()
        received += int(fields[0])
        sent += int(fields[8])
    return received, sent


def process_group(cmdline: str) -> str | None:
    lower = cmdline.lower()
    if "remotion" in lower and "render" in lower:
        return "remotion"
    if "chrome" in lower or "chromium" in lower:
        return "chrome"
    if "ffmpeg" in lower:
        return "ffmpeg"
    if "mcp_server.py" in lower or "uvicorn" in lower:
        return "mcp"
    if "frameflow-bff" in lower:
        return "bff"
    if re.search(r"(^|/)node(?:\s|$)", lower):
        return "node"
    return None


def processes() -> tuple[dict[str, dict[str, float]], dict[int, tuple[str, int]]]:
    groups = {name: {"count": 0, "rss_mb": 0.0, "cpu_ticks": 0} for name in TRACKED}
    by_pid: dict[int, tuple[str, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            group = process_group(cmdline)
            if not group:
                continue
            raw = (entry / "stat").read_text()
            rest = raw[raw.rfind(")") + 2 :].split()
            ticks = int(rest[11]) + int(rest[12])
            rss_pages = int(rest[21])
        except (OSError, ValueError, IndexError):
            continue
        groups[group]["count"] += 1
        groups[group]["rss_mb"] += rss_pages * os.sysconf("SC_PAGE_SIZE") / 2**20
        groups[group]["cpu_ticks"] += ticks
        by_pid[pid] = (group, ticks)
    return groups, by_pid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 runs until Ctrl-C")
    parser.add_argument("--output", type=Path, help="optional JSONL output path")
    args = parser.parse_args()
    if args.interval < 0.2:
        parser.error("--interval must be at least 0.2 seconds")

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    output = args.output.open("a", encoding="utf-8", buffering=1) if args.output else None
    started = time.monotonic()
    previous_time = started
    previous_cpu = cpu_totals()
    previous_disk = disk_totals()
    previous_net = network_totals()
    _, previous_pids = processes()
    peaks = {"cpu_percent": 0.0, "memory_percent": 0.0, "swap_used_gb": 0.0, "load1": 0.0}
    clock_ticks = os.sysconf("SC_CLK_TCK")
    cpu_count = os.cpu_count() or 1

    print("timestamp cpu% mem% availGB swapGB load1 diskR/W(MB/s) netR/W(MB/s) processes(count/rssMB/cpu%)", flush=True)
    try:
        while not stop and (not args.duration or time.monotonic() - started < args.duration):
            time.sleep(args.interval)
            now = time.monotonic()
            elapsed = max(0.001, now - previous_time)
            current_cpu = cpu_totals()
            total_delta = current_cpu[0] - previous_cpu[0]
            idle_delta = current_cpu[1] - previous_cpu[1]
            cpu_percent = (100 * (total_delta - idle_delta) / total_delta) if total_delta else 0
            mem = memory()
            load1, load5, load15 = os.getloadavg()

            current_disk = disk_totals()
            read_mbps = (current_disk[0] - previous_disk[0]) * 512 / elapsed / 2**20
            write_mbps = (current_disk[1] - previous_disk[1]) * 512 / elapsed / 2**20
            io_busy_percent = min(100.0, max(0.0, (current_disk[2] - previous_disk[2]) / (elapsed * 10)))
            current_net = network_totals()
            net_rx_mbps = (current_net[0] - previous_net[0]) / elapsed / 2**20
            net_tx_mbps = (current_net[1] - previous_net[1]) / elapsed / 2**20

            groups, current_pids = processes()
            for group in TRACKED:
                delta = 0
                for pid, (pid_group, ticks) in current_pids.items():
                    if pid_group == group and pid in previous_pids:
                        delta += max(0, ticks - previous_pids[pid][1])
                groups[group]["cpu_percent"] = delta / clock_ticks / elapsed / cpu_count * 100
                groups[group]["rss_mb"] = round(groups[group]["rss_mb"], 1)
                groups[group].pop("cpu_ticks", None)

            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "elapsed_seconds": round(now - started, 3),
                "cpu_percent": round(cpu_percent, 1),
                "memory": {key: round(value, 2) for key, value in mem.items()},
                "load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
                "disk": {"read_mbps": round(read_mbps, 2), "write_mbps": round(write_mbps, 2), "busy_percent": round(io_busy_percent, 1)},
                "network": {"rx_mbps": round(net_rx_mbps, 2), "tx_mbps": round(net_tx_mbps, 2)},
                "processes": groups,
            }
            for key, value in (("cpu_percent", cpu_percent), ("memory_percent", mem["used_percent"]), ("swap_used_gb", mem["swap_used_gb"]), ("load1", load1)):
                peaks[key] = max(peaks[key], value)
            proc_text = " ".join(
                f"{name}:{int(groups[name]['count'])}/{groups[name]['rss_mb']:.0f}/{groups[name]['cpu_percent']:.0f}"
                for name in TRACKED if groups[name]["count"]
            ) or "-"
            print(
                f"{record['timestamp']} {cpu_percent:5.1f} {mem['used_percent']:5.1f} {mem['available_gb']:6.2f} "
                f"{mem['swap_used_gb']:6.2f} {load1:5.2f} {read_mbps:5.1f}/{write_mbps:5.1f} "
                f"{net_rx_mbps:5.1f}/{net_tx_mbps:5.1f} {proc_text}",
                flush=True,
            )
            if output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
            previous_time, previous_cpu, previous_disk, previous_net, previous_pids = now, current_cpu, current_disk, current_net, current_pids
    finally:
        summary = {"type": "summary", "samples_duration_seconds": round(time.monotonic() - started, 2), "peaks": {key: round(value, 2) for key, value in peaks.items()}}
        print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
        if output:
            output.write(json.dumps(summary, ensure_ascii=False) + "\n")
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
