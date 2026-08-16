# FrameFlow 远端观测与压测交接单

本文档交给 `192.168.20.173` 上的运维大模型执行。目标是让开发端 `192.168.20.246` 能在不取得 SSH 和生产密钥的情况下，只读查看实时性能指标及脱敏日志，并据此逐级压测 Remotion 并发。

## 执行边界

- 不输出或提交 `.env`、微信密钥、`MCP_API_TOKEN`、cookie、TLS 私钥。
- 不开放任意文件浏览、shell 执行或写入接口。
- 观测端口 `9910` 只允许来源 `192.168.20.246/32`，不得映射到公网。
- 先修复单任务上传与渲染，再进行并发压测；禁止直接满载。
- 修改服务前记录现状；不确定真实服务名或安装目录时先探测，不要猜测覆盖。

## 1. 更新与现状采集

在实际仓库目录执行；若不是 `/opt/OpenMontage`，后续路径同步替换：

```bash
cd /opt/OpenMontage
git status --short
git pull --ff-only origin main
git rev-parse --short HEAD

systemctl list-units --type=service --all | grep -Ei 'frameflow|openmontage|mcp'
sudo systemctl status frameflow-bff openmontage-mcp --no-pager -l || true
sudo ss -lntp | grep -E ':(8080|8900|9910)\b' || true
```

如果仓库有未提交改动，先报告文件列表，不得强制覆盖或 reset。

## 2. 修正合并部署回环地址

Web/BFF/MCP/Remotion 位于同一台 `192.168.20.173` 时，BFF 必须使用本机回环地址：

```dotenv
MCP_BASE_URL=http://127.0.0.1:8900/mcp
MCP_PROGRESS_URL=http://127.0.0.1:8900/render-progress
```

通过 `systemctl cat <BFF真实服务名>` 找到真实 `EnvironmentFile`，备份后只修改以上两项。不要把环境文件内容回显到聊天或日志。随后：

```bash
sudo systemctl daemon-reload
sudo systemctl restart <BFF真实服务名>
sudo systemctl status <BFF真实服务名> --no-pager -l
```

此前远程验证结果：MCP `initialize` 和 `get_render_status` 正常，但 BFF 通过 `192.168.20.173:8900` 调用 `upload_asset_chunk` 时等待响应头超过 120 秒，291 字节 PNG 直传也会卡住。修改回环地址后必须先复测小文件上传；若仍卡住，采集 MCP 同时段日志并定位上传处理函数，不能将其判断为性能不足。

## 3. 创建最小权限观测用户

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin frameflow-observer 2>/dev/null || true
sudo usermod -aG systemd-journal,adm frameflow-observer
sudo install -d -o frameflow-observer -g frameflow-observer -m 0750 /var/log/frameflow-observer
sudo install -d -o root -g frameflow-observer -m 0750 /etc/frameflow-observer
```

生成独立观测令牌。该令牌只能读取指标/脱敏日志，不得复用任何生产密钥：

```bash
OBSERVER_TOKEN="$(openssl rand -hex 32)"
printf 'FRAMEFLOW_OBSERVER_TOKEN=%s\n' "$OBSERVER_TOKEN" | sudo tee /etc/frameflow-observer/observer.env >/dev/null
unset OBSERVER_TOKEN
sudo chown root:frameflow-observer /etc/frameflow-observer/observer.env
sudo chmod 0640 /etc/frameflow-observer/observer.env
```

令牌需通过安全渠道交给操作人一次，不得写进仓库、普通日志或本文档。

## 4. 安装性能监控服务

创建 `/etc/systemd/system/frameflow-perf-monitor.service`：

```ini
[Unit]
Description=FrameFlow performance monitor
After=network.target

[Service]
Type=simple
User=frameflow-observer
Group=frameflow-observer
WorkingDirectory=/opt/OpenMontage
ExecStart=/usr/bin/python3 /opt/OpenMontage/scripts/frameflow_perf_monitor.py --interval 1 --output /var/log/frameflow-observer/metrics.jsonl
Restart=always
RestartSec=3
StandardOutput=append:/var/log/frameflow-observer/monitor.log
StandardError=append:/var/log/frameflow-observer/monitor.log
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/frameflow-observer

[Install]
WantedBy=multi-user.target
```

## 5. 安装只读观测接口

先把下面的 `<BFF真实服务名>`、`<MCP真实服务名>` 换成第 1 步探测到的名称。创建 `/etc/systemd/system/frameflow-observer.service`：

```ini
[Unit]
Description=FrameFlow authenticated read-only observer
After=network.target frameflow-perf-monitor.service
Requires=frameflow-perf-monitor.service

[Service]
Type=simple
User=frameflow-observer
Group=frameflow-observer
SupplementaryGroups=systemd-journal adm
EnvironmentFile=/etc/frameflow-observer/observer.env
WorkingDirectory=/opt/OpenMontage
ExecStart=/usr/bin/python3 /opt/OpenMontage/scripts/frameflow_observer.py --host 0.0.0.0 --port 9910 --allow-cidr 192.168.20.246/32 --metrics-file /var/log/frameflow-observer/metrics.jsonl --monitor-log /var/log/frameflow-observer/monitor.log --bff-unit <BFF真实服务名> --mcp-unit <MCP真实服务名>
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/var/log/nginx /var/log/journal /run/log/journal

[Install]
WantedBy=multi-user.target
```

启动并限制防火墙：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now frameflow-perf-monitor frameflow-observer
sudo ufw allow from 192.168.20.246 to any port 9910 proto tcp
sudo systemctl status frameflow-perf-monitor frameflow-observer --no-pager -l
```

如果未使用 UFW，应在现有防火墙中添加等价的单源地址规则，不能为了方便关闭防火墙。

## 6. 验证接口

在服务端本机读取令牌进行验证，命令和返回内容中不得打印令牌：

```bash
set -a
. /etc/frameflow-observer/observer.env
set +a
curl -fsS http://127.0.0.1:9910/health
curl -fsS -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" http://127.0.0.1:9910/v1/metrics/latest
curl -fsS -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" 'http://127.0.0.1:9910/v1/logs?source=mcp&lines=20'
unset FRAMEFLOW_OBSERVER_TOKEN
```

允许的只读接口：

| 接口 | 用途 |
| --- | --- |
| `/health` | 进程健康，不含系统数据 |
| `/v1/metrics/latest` | 最新一秒性能样本 |
| `/v1/metrics/tail?limit=300` | 最近 N 个性能样本，最大 1000 |
| `/v1/logs?source=bff&lines=200` | BFF journald 日志 |
| `/v1/logs?source=mcp&lines=200` | MCP journald 日志 |
| `/v1/logs?source=nginx-access&lines=200` | nginx access log |
| `/v1/logs?source=nginx-error&lines=200` | nginx error log |
| `/v1/logs?source=monitor&lines=200` | 性能监控自身日志 |

除了 `/health`，所有接口都要求 Bearer token。日志返回前会对常见 token、key、secret 字段二次脱敏。

## 7. 日志保留

创建 `/etc/logrotate.d/frameflow-observer`：

```text
/var/log/frameflow-observer/*.log /var/log/frameflow-observer/*.jsonl {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su frameflow-observer frameflow-observer
}
```

确认 journald 已持久化。如果现有平台已有集中日志策略，沿用现有策略；否则创建 `/var/log/journal` 并将 BFF/MCP 日志至少保留 14 天、总量限制在合理范围。不要无限保留渲染日志。

## 8. 回传给开发端的信息

完成后只回传以下内容：

1. 实际仓库路径和当前 commit。
2. BFF/MCP 的真实 systemd 服务名。
3. `http://192.168.20.173:9910/health` 是否可由 `192.168.20.246` 访问。
4. 观测令牌通过安全渠道单独交付。
5. `metrics/latest` 是否持续更新。
6. 小文件上传是否在 10 秒内完成；若失败，给出已脱敏的 MCP/BFF 同时段日志。
7. 当前 8080、8900、9910 的监听地址和防火墙来源限制。

完成这些项目后不要自行开始 4–6 并发压测。开发端会先跑 1 个基线任务，再按 2、4、5、6 并发递增，同时从观测接口记录 CPU、内存、swap、load、磁盘 busy、网络和进程组资源。

每一级的 E2E JSON 报告与同期监控 JSONL 可用以下命令生成机器判定；退出码 `0` 表示该级稳定、`2` 表示停止加压并诊断：

```bash
python3 scripts/frameflow_capacity_analyzer.py \
  --report perf/e2e-jobs-N.json \
  --metrics perf/metrics-jobs-N.jsonl \
  --output perf/assessment-jobs-N.json
```

## 9. 故障与回滚

观测服务异常时只需：

```bash
sudo systemctl disable --now frameflow-observer frameflow-perf-monitor
sudo ufw delete allow from 192.168.20.246 to any port 9910 proto tcp
```

观测组件与渲染业务解耦，停止它们不应影响 BFF/MCP/Remotion。不要为了修复观测服务重启整台生产机。
