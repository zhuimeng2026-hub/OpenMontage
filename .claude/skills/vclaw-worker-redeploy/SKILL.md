---
name: vclaw-worker-redeploy
description: Sync vclaw master, rebuild the Go worker binary, restart control-plane-worker systemd service with verified PID swap. Use when the user says "重启 vclaw worker", "重新构建 vclaw worker", "升级 vclaw", "sync and rebuild vclaw", "deploy vclaw fix", or after pulling a vclaw upstream change that touches cmd/worker/ or internal/store/.
license: MIT
compatibility: Linux host with control-plane-worker.service running under systemd; Go toolchain installed (matches /opt/vclaw/go.mod); git remote `origin` pointing at gitee.com/webheat/vclaw.git.
metadata: {"openclaw": {"requires": {"service": "control-plane-worker.service", "repo": "/opt/vclaw"}}}
---

# vclaw-worker-redeploy

Sync `/opt/vclaw` master, rebuild the Go worker binary, swap it under `/opt/vclaw/bin/`, restart the systemd service, verify the PID swap, and probe a healthcheck job through the queue. Same shape as `om-mcp-redeploy` but for the Go half of the stack.

## When to invoke this skill

Trigger phrases (Chinese / English):
- "重启 vclaw worker" / "重新构建 vclaw worker" / "升级 vclaw"
- "sync and rebuild vclaw" / "deploy vclaw fix" / "vclaw worker 升级"
- After a `git log upstream/master` shows new commits under `cmd/worker/`, `internal/store/`, or `handler/`
- Worker has been up long enough that "is it still alive?" is a fair question (no built-in health probe — only `journalctl -u control-plane-worker -f`)

Do NOT invoke for:
- First-time setup on a fresh host
- vclaw server (`control-plane-server.service`) restart — same pattern, different unit; copy/adapt this skill
- OpenMontage MCP restart — use `om-mcp-redeploy`
- Diagnosing *why* a specific job failed — that's an investigation, not a redeploy
- Editing vclaw source and committing locally — this skill only consumes upstream changes

## Pre-flight (always run)

```bash
# 1. 旧 PID + RSS — 回滚或事后对比
OLD_PID=$(systemctl show control-plane-worker --property=MainPID --value)
OLD_RSS=$(ps -p "$OLD_PID" -o rss= 2>/dev/null | tr -d ' ')
echo "OLD_PID=$OLD_PID  RSS=${OLD_RSS}KB  uptime=$(ps -p $OLD_PID -o etime= 2>/dev/null | tr -d ' ')"
echo "$OLD_PID" > /tmp/vclaw_redeploy_old_pid

# 2. 工作树必须干净 (Go build 不会覆盖但 pull 会冲突)
cd /opt/vclaw
git status --short
# 若非空: 停下问用户 "stash / commit / 强 pull?"

# 3. Go 工具链 sanity
go version
# 必须 >= go.mod 指定的版本 (通常是 go 1.21+)
```

## Step 1 — Sync code

```bash
cd /opt/vclaw
git fetch --all
git pull --ff-only
git log --oneline -5   # 确认新 commit 落到 HEAD
```

**Stop condition:** 如果本地有未推送 commit, `--ff-only` 失败。停下来告诉用户:
- 本地多了哪些 commit (`git log --oneline origin/master..HEAD`)
- 选项: (a) 先 push 再 pull, (b) `git pull --rebase`, (c) 跳过 sync

**Stop condition:** 如果 `git pull` 报 "Already up to date", 仍然走完下面的 build + restart — PID swap 是这个 skill 的核心目标, 跟有没有新代码无关。

## Step 2 — Build worker binary

vclaw worker 是 Go 编译产物, **改完源代码必须 build 才能让 systemd 加载新代码**——不像脚本语言 reload 文件就行。最常见的失败模式是 `git pull` 完忘 build, systemd 还在跑老 binary。

```bash
cd /opt/vclaw
go build -o /tmp/control-plane-worker.new ./cmd/worker/
ls -la /tmp/control-plane-worker.new
# 大小应该跟 /opt/vclaw/bin/control-plane-worker 接近 (差几 KB 对应源码改动量)
```

**Stop condition:** build 失败 (缺依赖等). 停下来查 `go.mod` / `go.sum`. 常见原因:
- 升级 Go 版本后 toolchain 不匹配
- `internal/` 加了 import 但 `go mod tidy` 没跑
- GOPROXY 不通 (本机如果用 goproxy.cn, 跟 mihomo 代理要走 NO_PROXY bypass)

## Step 3 — Swap binary + restart

```bash
# 1. 备份老 binary — 留底以防回滚
cp /opt/vclaw/bin/control-plane-worker /opt/vclaw/bin/control-plane-worker.bak-$(date +%Y%m%d)

# 2. 替换
mv /tmp/control-plane-worker.new /opt/vclaw/bin/control-plane-worker
chmod +x /opt/vclaw/bin/control-plane-worker
ls -la /opt/vclaw/bin/control-plane-worker   # mtime 应该是当下
```

**千万别忘 chmod**, 否则 systemd 拉不起 ExecStart 会报 "Permission denied"。

```bash
# 3. 重启 systemd service
time systemctl restart control-plane-worker
```

systemd 会在 TERM 后启新进程; 老进程如果有 goroutine 在跑会被打断 (Go 默认不优雅退出, 但 worker 定期 poll, 丢失一个 poll 周期没关系).

## Step 4 — Wait for ready

vclaw worker 没有 HTTP `/health` 端点. 看 journal 确认启动三段日志都出现:

```bash
for i in $(seq 1 10); do
  if journalctl -u control-plane-worker --since "5 seconds ago" --no-pager -o short-iso 2>&1 \
    | grep -q "worker started"; then
    echo "ready in ${i}s"
    break
  fi
  sleep 1
done

# 验证三条标志性日志
journalctl -u control-plane-worker --since "10 seconds ago" --no-pager -o short-iso 2>&1 \
  | grep -E "openmontage client ready|openclaw client ready|worker started"
```

期望看到三条:
- `openmontage client ready` (worker 连上 OM MCP :8900)
- `openclaw client ready` (worker 连上 OpenClaw 网关 :7700)
- `worker started pid=<NEW> concurrency=4 poll_interval=3s`

**Worker 日志走 journald**, 不写 `/opt/vclaw/logs/cp-worker.log` (那个文件是 systemd 切换前 ad-hoc 跑时留的, 已不再更新, 别被骗).

## Step 5 — Verify PID swap (核心: 旧进程必须真的死)

```bash
SAVED=$(cat /tmp/vclaw_redeploy_old_pid)
NEW_PID=$(systemctl show control-plane-worker --property=MainPID --value)
NEW_RSS=$(ps -p "$NEW_PID" -o rss= 2>/dev/null | tr -d ' ')

if kill -0 "$SAVED" 2>/dev/null; then
  echo "❌ OLD_PID $SAVED still alive"
  ps -p "$SAVED" -o pid,etime,rss,cmd
  systemctl kill --signal=KILL control-plane-worker
  sleep 1
  if kill -0 "$SAVED" 2>/dev/null; then
    echo "❌ OLD_PID refuses to die — manual intervention needed"
    exit 1
  fi
fi
echo "✅ OLD_PID $SAVED exited (was ${OLD_RSS}KB)"

if [[ "$NEW_PID" != "$SAVED" ]] && kill -0 "$NEW_PID" 2>/dev/null; then
  echo "✅ NEW_PID $NEW_PID alive (${NEW_RSS}KB RSS, fresh binary)"
else
  echo "❌ NEW_PID unchanged or dead"
  journalctl -u control-plane-worker --since "30 seconds ago" --no-pager | tail -20
  exit 1
fi
```

**预期:** Go worker 一般 ~30-40MB RSS. 如果新 PID >1GB, 说明重启前没真正退出, 要再 kick 一次.

## Step 6 — Smoke test: 健康探测 job

vclaw worker 没有 HTTP 端点可 ping. 最直接的健康探测是塞一条 test job 进 `job_queue`, 看 worker 是否在下一个 poll 周期内 drain:

```bash
JOB_ID="healthcheck-$(date +%Y%m%d%H%M%S)"
sqlite3 /opt/vclaw/controlplane.db <<SQL
INSERT INTO job_queue (id, tenant_id, job_type, payload, status, attempts, created_at, updated_at)
VALUES (
  '$JOB_ID',
  '20260830-3df5fc346a9e',
  'poll_render',
  json_object('production_job_id','probe-$JOB_ID','video_project_id','probe-$JOB_ID'),
  'pending',
  0,
  strftime('%Y-%m-%d %H:%M:%f','now') || ' +0800 HKT',
  strftime('%Y-%m-%d %H:%M:%f','now') || ' +0800 HKT'
);
SQL

sleep 4   # worker poll_interval=3s, 留 1s 余量

sqlite3 -header /opt/vclaw/controlplane.db \
  "SELECT status, attempts FROM job_queue WHERE id='$JOB_ID';"
# 期望: status=done, attempts=0
# (payload 是 probe, worker 在 production_jobs 里查不到对应 job, 立即标 done)

# 同时 journal 应该有这三段:
#   claimed jobs count=1
#   processing job job_id=$JOB_ID
#   job done duration_ms=5 (左右)

# 清理
sqlite3 /opt/vclaw/controlplane.db "DELETE FROM job_queue WHERE id='$JOB_ID';"
```

**期望:** 4 秒内 status 从 pending 变成 done, journal 里 "claimed jobs" / "processing job" / "job done" 三条齐全.

如果想进一步压一下 OM (让 worker 真的去 call tools/call 而不是短路), 构造完整的 video_projects + production_jobs 三件套 — 但这只在改 worker 行为时需要.

## Step 7 — Summary report

```
✅ vclaw worker redeploy complete
   pulled:   <N> new commits from origin/master
   old PID:  <OLD_PID>  RSS=<OLD_RSS>KB
   new PID:  <NEW_PID>  RSS=<NEW_RSS>KB
   ready in: <N>s (openmontage + openclaw + worker_started 都齐)
   healthcheck: probe job drained in <4s
```

## Rollback (新代码坏了)

```bash
# 1. 停服务, 避免 Restart=on-failure 把坏 binary 又拉起来
sudo systemctl stop control-plane-worker

# 2. 还原 binary
LATEST_BAK=$(ls -t /opt/vclaw/bin/control-plane-worker.bak-* | head -1)
cp "$LATEST_BAK" /opt/vclaw/bin/control-plane-worker

# 3. (可选) 回滚代码
cd /opt/vclaw
git revert HEAD              # 安全: 产生新 commit, 不改历史
git reset --hard HEAD~1      # 危险: 改写历史, 仅当本地 commit 还没 push 时

# 4. 启服务
sudo systemctl start control-plane-worker

# 5. 走 Step 4-6 验证
```

如果连 backup binary 也坏了:

```bash
cd /opt/vclaw
git checkout HEAD -- bin/control-plane-worker   # 注意: bin/ 通常是 .gitignore, 这条会失败
go build -o bin/control-plane-worker ./cmd/worker/
```

(实际开发机上 `bin/` 不在 git 里, 最后 resort 是用上游 release tag 重新 build.)

## Latent bugs 这个 skill 自动避开

1. **`production_jobs.external_run_id` 不能 NULL** — Go 的 `sql.Scan` 在 strict 模式下把 NULL 转到 string 会 panic, 报 `Scan error on column index 5, name "external_run_id": converting NULL to string is unsupported`. 所有插入 production_jobs 的测试/迁移/seed 都要写 `''` 而不是 NULL.
2. **`payload` 不能是 JSON string** — `store.Enqueue` 期望 `payload any` 是 struct/map, 传 `string` 会被 `json.Marshal` 双层编码成 `"\"...\""`, 下次 poll `json.Unmarshal` 失败. 已修: `9b8d5ac fix(worker): pass decoded struct on poll_render re-enqueue`. 这个 skill 的 healthcheck 会顺带验证 retry 路径 payload 干净.

## What this skill does NOT do

- 不 push 到 origin (项目约定 "push only when asked")
- 不重启 `control-plane-server` (不同 unit, 同样可以适配)
- 不跑 `go test ./...` / 不 lint
- 不改 systemd unit 文件 / `/opt/vclaw/config.yaml`
- 不动 `/opt/vclaw/controlplane.db` schema (migrations 是单独的事)

## Why this skill exists

vclaw worker 的二进制是 Go 编译产物, **改完源代码必须 `go build` + 替换 binary 才能生效**——不像脚本语言 reload 文件就行. 这个 skill 把常见失败模式打包成一条龙:

1. `git pull` 后忘了 build, systemd 还是跑老 binary (最常见)
2. `go build` 报缺依赖 (go.sum 没更新 / GOPROXY 不通)
3. binary 替换后忘了 `chmod +x`, systemd 拉不起来
4. 不验证 PID 切换, 不知道 systemd 是自动重启还是手工起了第二个进程
5. `journalctl -u control-plane-worker` 没确认启动三段日志就宣布成功, 但其实 worker 连 OM 都连不上

Reproduced 2026-09-08: 拉到上游 `9b8d5ac fix(worker): pass decoded struct on poll_render re-enqueue`, 走完这条 skill 全程, PID 1103574, healthcheck probe job 4 秒内 drain.

## Reference

- Unit file: `/etc/systemd/system/control-plane-worker.service`
- Source root: `/opt/vclaw` (git repo, origin → gitee.com/webheat/vclaw)
- Worker main: `/opt/vclaw/cmd/worker/main.go`
- Store: `/opt/vclaw/internal/store/store.go` (Enqueue / GetJobByID)
- Build cmd: `go build -o bin/control-plane-worker ./cmd/worker/`
- Live binary: `/opt/vclaw/bin/control-plane-worker`
- Backup dir: `/opt/vclaw/bin/control-plane-worker.bak-YYYYMMDD`
- Logs: `journalctl -u control-plane-worker -f` (worker stdout/stderr 走 journald, 不写 `/opt/vclaw/logs/cp-worker.log`)
- Queue DB: `/opt/vclaw/controlplane.db` (`job_queue` 表 + `production_jobs` + `video_projects`)
- 旧 PID 临时文件: `/tmp/vclaw_redeploy_old_pid`

Sibling skill: `om-mcp-redeploy` — 同样的 PID-swap pattern, 适用于 OM MCP server. 如果要做 vclaw-server (control-plane-server) 启停, 复制这个 skill 把 unit 名和 binary 路径改一下即可.
