# OpenMontage MCP Proxy 验证指引

## 配置

代理 `.env` 必须包含：

    UPSTREAM_MCP_URL=http://lanes.ymxt.top:8900/mcp
    UPSTREAM_MCP_TOKEN=<上游 Token>
    PROXY_CLIENT_TOKEN=<独立客户端 Token>
    PORT=8080

两个 Token 必须不同；`.env` 不得提交 Git。若使用旧配置，可用 `mcp_key` 代替 `UPSTREAM_MCP_TOKEN`。

## 编译与本机检查

    gofmt -d main.go
    go test ./...
    go build -o openmontage-mcp-proxy .
    curl -i http://127.0.0.1:8080/health

预期测试通过，`gofmt -d` 无输出，`/health` 返回 HTTP 200 且不显示 Token。

## 公网检查

客户端先设置 `PROXY_CLIENT_TOKEN`，然后执行：

    curl -i --max-time 20 https://dw.aixifs.com/health
    curl -i --max-time 20 https://dw.aixifs.com/mcp

预期 `/health` 为 200；无 Token 的 `/mcp` 必须为 401。连接失败或 TLS 错误时，检查 DNS、443、证书、Nginx/Caddy 和云安全组。

## MCP 初始化

    curl -i --max-time 30 -X POST https://dw.aixifs.com/mcp \
      -H "Authorization: Bearer \${PROXY_CLIENT_TOKEN}" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"proxy-verifier","version":"1.0"}}}'

预期 HTTP 200。406 通常表示缺少 `Accept: text/event-stream`；502 表示代理到上游连接或上游服务有问题。

## 工具和 SSE

初始化后使用返回的 `Mcp-Session-Id` 调用 `tools/list`，携带客户端 Token、`MCP-Protocol-Version: 2025-03-26` 和 `Accept: application/json, text/event-stream`。结果应包含 `list_tools`、`execute_tool`、`upload_asset`。

SSE 检查：

    curl -N --max-time 30 -H "Authorization: Bearer \${PROXY_CLIENT_TOKEN}" -H 'Accept: text/event-stream' https://dw.aixifs.com/mcp

连接不应立即断开；代理使用 `FlushInterval: -1` 转发 SSE。

## 验收标准

- [ ] `go test ./...` 通过；
- [ ] `/health` 为 200；
- [ ] 无 Token 的 `/mcp` 为 401；
- [ ] initialize 为 200；
- [ ] tools/list 返回 OpenMontage 工具；
- [ ] SSE 可保持；
- [ ] 上游故障返回 502，不泄露 Token；
- [ ] `.env` 未被 Git 跟踪。

故障顺序：DNS/443/TLS → Nginx/Caddy → `/health` → 客户端鉴权(401) → 上游连接(502) → MCP initialize/tools/list。
