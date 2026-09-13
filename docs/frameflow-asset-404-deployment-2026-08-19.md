# FrameFlow 图片 404 修复 — 部署指南（2026-08-19）

> 修复提交：`22b852c fix(frameflow): route ServeAsset through MCP so 404 thumbnails get served`
> 影响服务：`frameflow-bff`（Go）+ `mcp_server.py`（Python，承载新工具）+ `openmontage-mcp-proxy`（如果分开部署）
> 关联文档：`docs/frameflow-asset-404-2026-08-19.md`（根因分析，待补）

---

## TL;DR（10 秒读完）

「创建视频」页面的图片缩略图 404，根因是 BFF 和 MCP 跑在不同机器、上传落到 MCP 主机、BFF 在自己磁盘上 `os.Stat` 永远找不到。这次修复让 BFF 通过 MCP 读文件 + 加诊断日志 + 跨进程 flock 保护上传元数据。

**必须在 BFF 和 MCP 两端都部署新代码才生效。** 只重启 BFF → 仍然 404（BFF 调到 MCP 拿不到新工具）。只重启 MCP → BFF 不会调新工具。

---

## 部署顺序（严格按此顺序）

1. **MCP 服务端先部署**（承载 `read_session_asset` 工具）
2. **BFF 再部署**（开始代理读 MCP）
3. **可选清理**：脏 batch 的 DB 修正

如果反过来：BFF 重启后会反复打日志 `[session-asset] mcp_read_error err="read_session_asset tool is not registered"`，但不致命；前端 404 → fall back 到 local-fs → 仍然 404。**不会损坏数据**，只是 404 多持续几分钟。

---

## 步骤 1：部署 MCP 服务端

### 1.1 拉代码
```bash
cd /opt/OpenMontage   # 或 MCP 实际部署路径
git pull
git log --oneline -1   # 确认拿到 22b852c
```

### 1.2 确认新工具能被加载
```bash
# 在 MCP venv 下
python3 -c "
from tools.asset.read_session_asset import ReadSessionAsset
t = ReadSessionAsset()
print('name:', t.name, 'tier:', t.tier.value, 'stability:', t.stability.value)
"
# 期望输出: name: read_session_asset tier: core stability: production
```

### 1.3 跑单测
```bash
./.venv/bin/python -m pytest tests/test_read_session_asset.py -v
./.venv/bin/python -m pytest tests/test_session_asset_concurrency.py -v
```
**期望**：8 + 5 = 13 个测试全部通过。

### 1.4 重启 MCP 进程
如果 MCP 跑在 systemd：
```bash
# 找 MCP 的 service 文件
ls /etc/systemd/system/ | grep -iE "mcp|openmontage"
# 假设是 openmontage-mcp-proxy.service（具体名字按实际）
sudo systemctl restart openmontage-mcp-proxy.service
```

如果 MCP 跑在 tmux / docker：
```bash
# tmux
tmux send-keys -t mcp C-c
sleep 2
tmux send-keys -t mcp "./start_mcp_server.sh" Enter

# docker
docker restart <mcp-container-name>
```

### 1.5 验证 MCP 注册了新工具
```bash
TOKEN="h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"  # 跟 .env 一致
MCP="http://127.0.0.1:8900/mcp"

# initialize
RESP=$(curl -sS -m 5 -i -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1"}}}')
SID=$(echo "$RESP" | grep -i "Mcp-Session-Id:" | head -1 | awk '{print $2}' | tr -d '\r')
echo "got sid: $SID"

# list_tools 过滤 read_session_asset
curl -sS -m 5 -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); names=[t['name'] for t in r['result']['tools']]; print('read_session_asset present:', 'read_session_asset' in names); print('total tools:', len(names))"
```
**期望**：`read_session_asset present: True`，total ≥ 之前 + 1。

---

## 步骤 2：部署 BFF

### 2.1 拉代码 + 编译
```bash
cd /opt/OpenMontage
git pull
cd frameflow/bff
go build -o frameflow-bff .
```

### 2.2 跑 BFF 单测
```bash
cd /opt/OpenMontage/frameflow/bff
go test -count=1 ./...
```
**期望**：所有 `frameflow-bff/handlers`、`internal/mcp`、`internal/limits` 等包 `ok`。新加的 `TestReconcile*` 和 `TestDecodeMcpAssetResponse*` 必须出现在 PASS 列表里。

### 2.3 替换 + 重启
```bash
cd /opt/OpenMontage/frameflow/bff
ls -lh frameflow-bff                  # 现有
cp frameflow-bff frameflow-bff.bak-$(date +%s)   # 备份旧 binary
mv frameflow-bff.new frameflow-bff 2>/dev/null   # 上一条 build 出来的（如有）

# 用上面 go build 产物替换
systemctl restart frameflow-bff.service
sleep 2
systemctl status frameflow-bff.service --no-pager -n 3
journalctl -u frameflow-bff --no-pager -n 10
```

### 2.4 验证 binary 含新代码
```bash
strings /opt/OpenMontage/frameflow/bff/frameflow-bff | grep -E "read_session_asset|session-asset.*404|reconcile summary"
```
**期望**：全部命中（至少 4-5 行）。

---

## 步骤 3：端到端验证

### 3.1 通过 BFF 拿一个真实缩略图
```bash
# 在 SPA 里上传一张图（走 upload_asset → MCP 写盘 → BFF 代理读）
# 然后 DevTools Network 看 /api/assets?rel=... 的响应：

# 期望: status=200, Content-Type=image/png, body 是真实 PNG 字节
# 期望 journalctl 有: GET "/api/assets" with elapsed < 200ms (MCP roundtrip)
journalctl -u frameflow-bff --no-pager -n 50 | grep -E "session-asset.*serve|GET.*api/assets"
```

### 3.2 验证 404 仍能返回 JSON body（防御）
```bash
# 故意打一个不存在的相对路径
curl -sS -m 5 -i -b "ff_sid=<任意>" \
  "http://127.0.0.1:8080/api/assets?rel=projects%2Ffake-batch%2Fassets%2F_sessions%2Ffakehash%2Fghost.png"
```
**期望**：返回 403（whitelist 不过）或 404 JSON body `{"error":"asset_not_found","reason":...,"abs":...,"rel":...}`，**而不是空 404 或 SPA HTML**。

### 3.3 验证 stale_count
```bash
# 在 SPA 里刷新素材列表，看 /api/session/assets 响应：
curl -sS -m 5 -b "ff_sid=<你的会话cookie>" http://127.0.0.1:8080/api/session/assets | python3 -m json.tool
```
**期望**：响应里出现 `stale_count`（数字，可能为 0）和 `total_count`（数字）。

---

## 步骤 4（强烈建议）：修复历史脏数据

> ⚠️ 这条 2026-08-19 11:49–11:51 之间短暂上线过一个 buggy BFF binary，向 `image_batches.session_id` 写了**原始 `ff_sid`**（不是哈希后的 `renderQueueOwnerID(sid)`）。当前 BFF 按哈希 scope 查 → 永远找不到那个 batch → 用户提交渲染时拿到 `image batch not found`。

### 4.1 找脏行
```bash
DB=/opt/OpenMontage/frameflow/bff/data/frameflow.db
sqlite3 "$DB" "
  SELECT b.id, b.session_id, b.status, b.asset_count, b.created_at
  FROM image_batches b
  WHERE b.status IN ('collecting', 'queued', 'rendering')
  ORDER BY b.created_at DESC;
"
```
找出**原始 ff_sid 长度 = 32 hex**（如 `88ce568db2539f8776d73605927c0e8a`）的 batch，那些就是脏行。**预期是 0 行**（如果有，再走 4.2）。

### 4.2 修正 session_id（如果需要）
```sql
-- 把脏行的 session_id 改成哈希后的 scope
-- ⚠️ 必须先确认 hash 正确：
-- sha256("wechat:<openid>") = expected_scope
-- 或 sha256("session:<raw_ff_sid>") = expected_scope（取决于 renderQueueOwnerID 怎么算）

-- 备份
sqlite3 frameflow.db ".backup frameflow.db.bak-$(date +%s)"

-- 修正
UPDATE image_batches
SET session_id = '<正确的哈希scope>'
WHERE id = '<脏行 batch id>';
```

### 4.3 但请注意
脏行 batch 的 6 张图实际归属的 upstream MCP session 是冷 init 的、**从未持久化**。即使改了 `image_batches.session_id`，`create_remotion_video_share` 也会作为新 session 跑、找不到那 6 张图 → 渲染失败。

**实际建议**：脏行 batch 标记为 `failed` 让用户重新创建：
```sql
UPDATE image_batches SET status='failed', error='orphaned by buggy binary 22b852c-1; please retry'
WHERE id = '<脏行 batch id>';
```

---

## 回滚方案（如果出问题）

每个组件独立可回滚：

### 回滚 BFF
```bash
# 旧 binary 在 frameflow-bff.bak-<timestamp>
ls /opt/OpenMontage/frameflow/bff/frameflow-bff.bak-*
cp /opt/OpenMontage/frameflow/bff/frameflow-bff.bak-<timestamp> /opt/OpenMontage/frameflow/bff/frameflow-bff
systemctl restart frameflow-bff.service
```
**结果**：BFF 回到读本地 fs → 与本次修复前一样（404 会回来，但系统不挂）。

### 回滚 MCP（更危险，建议先回滚 BFF 再回滚 MCP）
```bash
cd /opt/OpenMontage
git checkout f18c7db -- mcp_server.py tools/asset_upload_chunk.py lib/workbuddy_session.py tools/asset/
# 然后重启 MCP 服务
```
**结果**：`read_session_asset` 工具消失；BFF 调用会失败 → 自动 fall back 到本地 fs → 行为同部署前。

---

## 关键文件路径速查

| 文件 | 角色 |
|------|------|
| `frameflow/bff/handlers/session_assets.go` | BFF ServeAsset + SessionAssets 主逻辑 |
| `frameflow/bff/handlers/session_assets_*_test.go` | BFF 单测（5+3 个） |
| `tools/asset/read_session_asset.py` | 新 MCP BaseTool |
| `mcp_server.py` | `@mcp.tool(read_session_asset)` 包装 |
| `lib/workbuddy_session.py` | flock + replace_asset_by_sha |
| `tools/asset_upload_chunk.py` | chunk 上传 dedup-promote |
| `tests/test_read_session_asset.py` | 8 个工具单测 |
| `tests/test_session_asset_concurrency.py` | 5 个 flock 单测 |
| `data/frameflow.db` | SQLite 批次库（步骤 4 用） |

---

## 部署后 24 小时监控

```bash
# BFF 日志：stale / mcp_proxy_err / serve_404 都不应该高频出现
journalctl -u frameflow-bff --since "24 hours ago" | grep -E "session-asset.*stale_entry|session-asset.*mcp_proxy_err|session-asset.*serve_404" | wc -l

# MCP 日志：read_session_asset 错误率应该 < 1%
grep -c "read_session_asset" /var/log/openmontage-mcp-proxy/proxy.log
grep -c "tool.read_session_asset.*err" /var/log/openmontage-mcp-proxy/proxy.log
```

如果 24h 内 `mcp_proxy_err` 占比 > 5% → 优先排查网络 / MCP 鉴权问题；如果 `stale_entry` 持续增加 → 检查 MCP 端是否在批量清理 session state 但没清文件。

---

## 联系人

- 实现 + 文档：Claude (MiniMax-M3)
- 提交：`22b852c` on `main`
- 测试覆盖：13 Python + 8 Go 新单测，全部通过