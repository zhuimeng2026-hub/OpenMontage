# FrameFlow MCP 健康监控 — 部署指南

> 引入提交：`c2d9c26 feat(tools): add MCP health monitor cron probe + email→SMS alerting`
> 监控目标：`lanes.ymxt.top:8900/mcp`（FrameFlow BFF 的唯一上游，Streamable-HTTP + Bearer）
> 告警通道：`975762756@qq.com` (QQ SMTP) → `18218401359@139.com` (139.com 邮箱 → SMS)

---

## TL;DR（10 秒读完）

每 5 分钟由 system cron 触发一次 `tools/mcp_health_monitor.py`，做两个探测：

1. **Initialize 握手** —— 通过现有 `om_mcp_probe.py status` 探活
2. **业务工具调用** —— 完整 JSON-RPC：`initialize → notifications/initialized → tools/call get_render_status(sentinel_job_id)`

阈值：`WARN=8s`、`CRIT=15s`、`PROBE_TIMEOUT=10s`。同一 `FAULT[tag1+tag2]` 故障 30 分钟内最多告警一次；恢复（OK 之前是 FAULT）**总是**发邮件，不走冷却。

---

## 部署前清单

| 项 | 期望值 | 检查命令 |
|----|--------|----------|
| Python 版本 | ≥ 3.10 | `python3 --version` |
| `tools/mcp_health_monitor.py` 存在且可执行 | mode 755 | `ls -l tools/mcp_health_monitor.py` |
| `om_mcp_probe.py` 存在 | 顶部有 `om_mcp_probe.py:104-209` 的 JSON-RPC 模式 | `head -1 om_mcp_probe.py` |
| `/opt/OpenMontage/.env` 含 SMTP 凭据 | `sender / smtpserver / username / passwd` 四项非空 | `grep -E '^(sender\|smtpserver\|username\|passwd)=' /opt/OpenMontage/.env` |
| `/opt/OpenMontage/frameflow/bff/.env` 含 MCP 凭据 | `MCP_BASE_URL`、`MCP_API_TOKEN` | `grep -E '^MCP_' /opt/OpenMontage/frameflow/bff/.env` |
| 锁文件父目录存在 | `/var/lock` 可写 | `ls -ld /var/lock` |
| 日志父目录存在或可创建 | `/var/log/openmontage/` | `ls -ld /var/log/openmontage` |

> **注意**：监控脚本必须以 `root` 身份运行（写 `/var/log`、`/var/lib` 不需要 sudo），所以用 **system cron**（`/etc/cron.d/`），不要 `crontab -e`。

---

## 部署步骤

### 步骤 1：拉代码并确认文件就位

```bash
cd /opt/OpenMontage
git pull
git log --oneline -1   # 确认拿到 c2d9c26
ls -l tools/mcp_health_monitor.py   # mode 755
```

如果文件不可执行：
```bash
chmod 755 tools/mcp_health_monitor.py
```

### 步骤 2：创建运行时目录

```bash
sudo mkdir -p /var/log/openmontage /var/lib/openmontage
sudo chown root:root /var/log/openmontage /var/lib/openmontage
sudo chmod 755 /var/log/openmontage /var/lib/openmontage
```

### 步骤 3：安装 cron 文件

把以下内容写到 `/etc/cron.d/openmontage-mcp-monitor`（owner `root:root`，mode `644`）：

```cron
# OpenMontage MCP Health Monitor
# Probes lanes.ymxt.top:8900/mcp every 5 min; emails 18218401359@139.com on anomaly.
# flock -n prevents overlap if a probe stalls past 5 min (next tick just skips).
# Output (stdout+stderr) → /var/log/openmontage/mcp_monitor.log
*/5 * * * * root /usr/bin/flock -n /var/lock/openmontage-mcp-monitor.lock /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py >> /var/log/openmontage/mcp_monitor.log 2>&1
```

```bash
sudo tee /etc/cron.d/openmontage-mcp-monitor > /dev/null <<'EOF'
# OpenMontage MCP Health Monitor
# Probes lanes.ymxt.top:8900/mcp every 5 min; emails 18218401359@139.com on anomaly.
# flock -n prevents overlap if a probe stalls past 5 min (next tick just skips).
# Output (stdout+stderr) → /var/log/openmontage/mcp_monitor.log
*/5 * * * * root /usr/bin/flock -n /var/lock/openmontage-mcp-monitor.lock /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py >> /var/log/openmontage/mcp_monitor.log 2>&1
EOF
sudo chown root:root /etc/cron.d/openmontage-mcp-monitor
sudo chmod 644 /etc/cron.d/openmontage-mcp-monitor
```

### 步骤 4：dry-run 自检（不发邮件、不写状态）

```bash
cd /opt/OpenMontage
python3 tools/mcp_health_monitor.py --dry-run
```

期望输出：
```
2026-08-20 11:30:00 INFO probing http://lanes.ymxt.top:8900/mcp
2026-08-20 11:30:00 INFO init: ok=True elapsed_ms=... tags=[]
2026-08-20 11:30:00 INFO biz:  ok=True elapsed_ms=... tags=[]
2026-08-20 11:30:00 INFO state_key: OK
```

`state_key: OK` → 监控自身 + 上游都健康。如果 `init` 或 `biz` 有 `tags=` 内容，先解决再继续。

### 步骤 5：测试告警链路（QQ SMTP → 139.com → SMS）

```bash
cd /opt/OpenMontage
python3 tools/mcp_health_monitor.py --test-alert
```

期望：
1. 终端打印 `email sent to 18218401359@139.com: [MCP] TEST ALERT @ HH:MM:SS`
2. 1–2 分钟内手机收到 139.com 转发的短信

**不发短信就继续排查**，别跳到下一步。

### 步骤 6：等待下一次 cron tick（或手动触发一次）

```bash
# 等到下一个 */5 分钟边界（最多等 5 分钟）；或手动模拟 cron 环境：
sudo /usr/bin/flock -n /var/lock/openmontage-mcp-monitor.lock \
    /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py \
    >> /var/log/openmontage/mcp_monitor.log 2>&1
```

### 步骤 7：检查 cron 真的在跑

```bash
# 1) 确认 cron daemon 装了这个文件
sudo run-parts --test /etc/cron.d/   # 应列出 openmontage-mcp-monitor
# 2) 看 cron 日志里有没有这条命令的痕迹
sudo journalctl -u cron -n 50 | grep -E 'mcp_health_monitor\|flock' || \
    grep -i 'mcp_health_monitor' /var/log/cron.log 2>/dev/null
# 3) 看我们的日志有最新一行
tail -3 /var/log/openmontage/mcp_monitor.log
```

---

## 验证（部署后 5 分钟内）

| 检查项 | 命令 | 期望 |
|--------|------|------|
| 状态文件已创建 | `cat /var/lib/openmontage/mcp_monitor_state.json` | JSON 含 `last_status` |
| 日志正常增长 | `wc -l /var/log/openmontage/mcp_monitor.log` | 每 5 分钟 +12 行左右 |
| 锁文件无残留 | `ls -l /var/lock/openmontage-mcp-monitor.lock` | 探测结束后自动释放 |
| 上次跑的状态 | `tail -1 /var/log/openmontage/mcp_monitor.log` | `state_key: OK` |

---

## 手工操作手册

### 强制发一次测试告警（不依赖故障）

```bash
cd /opt/OpenMontage
python3 tools/mcp_health_monitor.py --test-alert
```

### 不告警地跑一次（确认行为但不发邮件）

```bash
cd /opt/OpenMontage
python3 tools/mcp_health_monitor.py --dry-run
```

### 重置冷却（已经告警过，想立刻再测一遍）

```bash
sudo rm /var/lib/openmontage/mcp_monitor_state.json
# 下一次 cron tick 会重建；手动触发：
sudo /usr/bin/flock -n /var/lock/openmontage-mcp-monitor.lock \
    /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py
```

### 调整阈值（不修改代码）

通过环境变量覆盖默认值（写在 `/etc/cron.d/openmontage-mcp-monitor` 里或 `/etc/environment.d/`）：

| 变量 | 默认 | 含义 |
|------|------|------|
| `MONITOR_WARN_LATENCY` | `8` | 超过 → WARN tag |
| `MONITOR_CRIT_LATENCY` | `15` | 超过 → CRIT tag + 算作 FAULT |
| `MONITOR_PROBE_TIMEOUT` | `10` | 单次探测硬超时（秒） |
| `MONITOR_COOLDOWN_SEC` | `1800` | 同 FAULT key 重发告警的最小间隔 |
| `MONITOR_ALERT_TO` | `18218401359@139.com` | 收件人 |
| `MONITOR_SMTP_PORT` | `465` | QQ SMTP SSL 端口 |
| `OPENMONTAGE_REPO` | `/opt/OpenMontage` | 仓库根目录 |
| `MONITOR_STATE_FILE` | `/var/lib/openmontage/mcp_monitor_state.json` | 状态文件 |
| `MONITOR_LOG_FILE` | `/var/log/openmontage/mcp_monitor.log` | 日志文件 |

> cron 里改环境变量要在命令前加 `KEY=VAL`，例如：
> ```cron
> */5 * * * * root MONITOR_CRIT_LATENCY=20 /usr/bin/flock -n ...
> ```

---

## 回滚

按从浅到深的顺序，任选一步即可：

| 层级 | 命令 | 效果 |
|------|------|------|
| 停监控（保留文件） | `sudo rm /etc/cron.d/openmontage-mcp-monitor` | 下个 tick 起不再运行；文件、日志、状态保留 |
| 同时清掉锁 | `sudo rm /etc/cron.d/openmontage-mcp-monitor /var/lock/openmontage-mcp-monitor.lock` | 同上 + 解锁 |
| 完全回滚（含状态和日志） | `sudo rm -rf /etc/cron.d/openmontage-mcp-monitor /var/lib/openmontage/mcp_monitor_state.json /var/log/openmontage/mcp_monitor.log` | 完全干净 |
| 代码层回滚 | `cd /opt/OpenMontage && git revert c2d9c26` | 从仓库移除脚本 |

> **回滚不需要重启任何服务**。cron 是无状态的——删掉文件，下一次 tick 就停了。

---

## 故障排查

### 监控自己挂了（不发邮件也不写日志）

```bash
# 1) cron 在跑吗？
systemctl status cron
# 2) 文件还在吗？
ls -l /etc/cron.d/openmontage-mcp-monitor
# 3) 手动跑看错误
sudo -u root /usr/bin/python3 /opt/OpenMontage/tools/mcp_health_monitor.py --dry-run
```

### 持续 `init_auth` 告警

`MCP_API_TOKEN` 失效或过期。检查并更新 `/opt/OpenMontage/frameflow/bff/.env`，**无需重启 cron** —— 下个 tick 重新读 `.env`。

### 持续 `biz_unexpected_status:none` 或 `biz_missing_share_url`

sentinel job (`d75622b7d77b4ce392514c8c20beeccd`) 在上游被回收。脚本头部 `SENTINEL_JOB_ID` 注释里说会"fall back to most-recent published row" —— 这是 TODO，目前不会自动 fallback。要么：
- 换一个新的 sentinel job_id（找一个仍在 published 状态的 job 写入 `tools/mcp_health_monitor.py:105`）
- 或者接受这个告警作为"上游数据异常"信号（其实也算正常）

### QQ SMTP 报 `authentication failed`

QQ 邮箱授权码（不是登录密码）过期。去 QQ 邮箱设置 → 账户 → POP3/SMTP 服务 → 生成新授权码，更新 `/opt/OpenMontage/.env` 的 `passwd`。

### `flock` 报锁未释放 / 探测一直堆叠

`flock -n` 是非阻塞的，不会堆叠。如果看到日志时间戳不前进，可能是：
- `PROBE_TIMEOUT` 设得太大（> 5 min），下一 tick 来时锁还被占。脚本默认 10 s，正常。
- Python 进程僵死：`pgrep -af mcp_health_monitor` 看进程，`kill -9` 重置。

---

## 24 小时后续观察

部署后第一个 24h 检查清单：

- [ ] **第 1 小时**：每 15 分钟看一眼日志，确认 `state_key: OK` 出现 12 次
- [ ] **第 6 小时**：检查 `mcp_monitor_state.json` 的 `last_status` 字段保持 `OK`
- [ ] **第 24 小时**：检查日志无意外告警；如收到过告警，确认对应的恢复邮件也到了

如果 24h 内收到 ≥ 1 次误告警，调整 `MONITOR_*` 环境变量（见上表）；如果 ≥ 3 次，停下来重新审视阈值和 sentinel 选择。
