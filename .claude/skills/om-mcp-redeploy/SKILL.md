---
name: om-mcp-redeploy
description: Sync upstream OpenMontage code and cleanly restart the openmontage-mcp systemd service with verified PID swap. Use when the user says "sync and redeploy", "更新后重启", "pull and restart", "更新部署", mentions that upstream has new commits, or wants the MCP server to pick up new code without leaking the old process.
license: MIT
compatibility: Linux host with openmontage-mcp.service running under systemd; git remote `upstream` pointing at github.com/zhuimeng2026-hub/OpenMontage.
metadata: {"openclaw": {"requires": {"service": "openmontage-mcp.service"}}}
---

# om-mcp-redeploy

Sync `upstream/OpenMontage_Voicebox` and restart the MCP server with old-PID/new-PID verification. The defining feature of this skill: **it asserts the running PID changed and the old one is gone**, instead of assuming `systemctl restart` succeeded.

## When to invoke this skill

Trigger phrases (Chinese / English):
- "同步代码并重启 MCP" / "更新后重新部署" / "拉新代码重启"
- "sync and redeploy" / "pull and restart" / "redeploy the MCP"
- "upstream has new commits, deploy them"
- "MCP server has been up too long, restart it" (memory-hygiene)

Do NOT invoke for:
- First-time setup on a fresh host → use Makefile `make setup`
- Tweak sidecar (`:8901`) restart → different unit, different command
- vclaw control-plane (`:8080` on `/opt/vclaw`) restart → different host, different skill
- Diagnosing *why* a download hangs → that's a different bug class (see memory: `om-mcp-server-huggingface-proxy-requirement`, `voicebox-blocked-kokoro-escape-hatch`)

## Pre-flight (always run, before touching anything)

```bash
# 1. 记下旧 PID + RSS — 回滚或事后对比有用
OLD_PID=$(systemctl show openmontage-mcp --property=MainPID --value)
OLD_RSS=$(ps -p "$OLD_PID" -o rss= 2>/dev/null | tr -d ' ')
echo "OLD_PID=$OLD_PID  OLD_RSS=${OLD_RSS}KB  uptime=$(ps -p $OLD_PID -o etime= 2>/dev/null | tr -d ' ')"
echo "$OLD_PID" > /tmp/om_redeploy_old_pid

# 2. 工作树必须干净 (否则 pull --ff-only 会拒)
git status --short
# 若非空: 停下问用户 "stash / commit / 强 pull?"
```

## Step 1 — Sync code

```bash
# fast-forward only. 本地若有未推送 commit, --ff-only 会失败 — 停下来问用户
# 而不是悄悄 --rebase (会改写历史, 违反 "push only when asked" 项目约定)
git fetch upstream
git pull --ff-only
git log --oneline -5   # 确认新 commit 已落到 HEAD
```

**Stop condition:** 如果 `git pull --ff-only` 报错 "Not possible to fast-forward", 说明本地有未推送 commit。停下来告诉用户：
- 本地多了哪些 commit (`git log --oneline upstream/OpenMontage_Voicebox..HEAD`)
- 选项: (a) 先 push 再 pull, (b) `git pull --rebase`, (c) 跳过这次 sync

**Stop condition:** 如果 `git pull --ff-only` 报 "Already up to date", 仍然走完下面的 restart — 因为 PID swap 是这个 skill 的核心目标, 跟有没有新代码无关。

## Step 2 — Restart service

```bash
time systemctl restart openmontage-mcp
```

systemd 会: TERM 老进程 → 等 graceful shutdown timeout → KILL (如果需要) → 启新进程。比手动 `kill OLD_PID && start_mcp_server.sh &` 干净得多 (后者不会清掉孤儿 yt-dlp / ffmpeg 子进程)。

## Step 3 — Wait for ready

The TCP socket binds before FastMCP finishes initializing — poll until `initialize` returns HTTP 200 (或 401 for missing auth; both mean HTTP layer is up).

```bash
for i in $(seq 1 15); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 -X POST http://127.0.0.1:8900/mcp \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ready-probe","version":"1.0"}},"id":1}')
  if [[ "$CODE" == "200" || "$CODE" == "401" ]]; then
    echo "ready in ${i}s (HTTP $CODE)"
    break
  fi
  echo "  ${i}s: HTTP $CODE"
  sleep 1
done
```

## Step 4 — Verify PID swap (核心: 旧进程必须真的死)

```bash
SAVED=$(cat /tmp/om_redeploy_old_pid)
NEW_PID=$(systemctl show openmontage-mcp --property=MainPID --value)
NEW_RSS=$(ps -p "$NEW_PID" -o rss= 2>/dev/null | tr -d ' ')

# 旧 PID 必须在 5s 内自然退出 (systemd 给的 graceful shutdown timeout)
if kill -0 "$SAVED" 2>/dev/null; then
  echo "❌ OLD_PID $SAVED still alive after restart"
  ps -p "$SAVED" -o pid,etime,rss,cmd
  echo "Forcing cleanup..."
  systemctl kill --signal=KILL openmontage-mcp
  sleep 1
  if kill -0 "$SAVED" 2>/dev/null; then
    echo "❌ OLD_PID $SAVED refuses to die — manual intervention needed"
    exit 1
  fi
fi
echo "✅ OLD_PID $SAVED exited (was ${OLD_RSS}KB)"

# 新 PID 必须绑住 :8900 且不等于旧 PID
if [[ "$NEW_PID" != "$SAVED" ]] && kill -0 "$NEW_PID" 2>/dev/null; then
  echo "✅ NEW_PID $NEW_PID alive (${NEW_RSS}KB RSS, fresh process)"
else
  echo "❌ NEW_PID unchanged or dead: $NEW_PID"
  journalctl -u openmontage-mcp --since "30 seconds ago" --no-pager | tail -20
  exit 1
fi

# 端口确认 (防止 systemd 报 active 但 socket 没起来)
ss -tlnp | grep ":8900 " | head -1
```

**预期:** 旧 PID RSS 通常累积到几 GB (会话久了内存泄漏明显), 新 PID 一上来是 ~100MB。如果新 PID 也 >1GB, 说明重启前没真正退出, 需要再 kick 一次。

## Step 5 — Smoke test (推荐)

```bash
MCP_TOKEN=$(grep MCP_API_TOKEN /opt/OpenMontage_Voicebox/.env | cut -d= -f2 | tr -d '"')
SID=$(curl -sS -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}},"id":1}' \
  -D /tmp/h.txt 2>&1 >/dev/null && grep -i "mcp-session-id" /tmp/h.txt | awk -F': ' '{print $2}' | tr -d '\r\n')

COUNT=$(curl -sS -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}' | \
  python -c "import sys,json; print(len(json.loads(sys.stdin.read())['result']['tools']))")
echo "MCP tools visible: $COUNT  (expect ~37 — registry has ~139 BaseTools but MCP surface is curated)"
```

注: `video_downloader` / `video_analyzer` 等不在 MCP surface, 走 `execute_tool(name=...)` dispatcher — 这是设计如此, 不是回归。

## Step 6 — Summary report

把以下信息一次性打印:

```
✅ Sync + restart complete
   pulled:   <N> new commits from upstream
   old PID:  <OLD_PID>  RSS=<OLD_RSS>KB  uptime=<etime>
   new PID:  <NEW_PID>  RSS=<NEW_RSS>KB  uptime=<etime>
   ready in: <N>s
   tools:    <COUNT> visible
```

## Rollback (新代码坏了)

```bash
# 1. 停服务, 避免 Restart=on-failure 把坏代码又拉起来
sudo systemctl stop openmontage-mcp

# 2. 回滚到上一个好 commit (选择其中一种, 用户批准)
git revert HEAD              # 安全: 产生新 commit, 不改历史
git reset --hard HEAD~1      # 危险: 改写历史, 仅当本地 commit 还没 push 时

# 3. 启服务
sudo systemctl start openmontage-mcp

# 4. 走 Step 3-5 同样验证
```

## What this skill does NOT do

- 不 push 到 upstream (项目约定 "push only when asked")
- 不改 .env、systemd unit、proxy 配置 (这些是单独的运维动作)
- 不跑测试 / `make test` / pytest
- 不碰 vclaw (`/opt/vclaw`)、tweak-sidecar (`:8901`)、Remotion composer

## Why this skill exists

OpenMontage MCP server 是长驻进程。手动 `kill OLD_PID && nohup ./start_mcp_server.sh &` 模式下:
1. PID 文档化不全 — 容易两个进程同时跑撞 :8900
2. 没有 supervisor — 崩了不会自动拉起
3. 子进程 (yt-dlp / ffmpeg) 在主进程死后可能孤儿, 占着 FD 不放

切到 systemd 后, `Restart=on-failure` + `RestartSec=5` 解决 2/3; 这个 skill 解决 1 (PID swap 显式断言)。Memory hygiene 顺手解决: 重启前 12.2GB → 重启后 103MB 是常见收益 (复现于 2026-09-08)。

## Reference

- Unit file: `/etc/systemd/system/openmontage-mcp.service`
- Startup script (含 NO_PROXY 兜底): `start_mcp_server.sh`
- Health log: `logs/mcp_health.log` (heartbeat, `event=heartbeat status=ok executor_threads=2`)
- Server log: `logs/mcp_server.log` (per-call trace, `execute_tool called/done/response`)
- 旧 PID 临时文件: `/tmp/om_redeploy_old_pid` (skill 自己写, 不依赖外部)
