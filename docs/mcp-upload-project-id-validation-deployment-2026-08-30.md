# MCP 图片上传参数校验加固 — 服务端处理需求（2026-08-30）

> 状态：**代码已改完，未提交、未重启**，需要服务端执行验证 + 重启。
> 影响服务：`openmontage-mcp.service`（Python MCP Server，端口 8900）
> 代码路径：`/opt/OpenMontage_Voicebox/`（即 SMB 共享 `\\192.168.20.173\voicebox\`）
> 改动文件：
> - `tools/asset_upload_chunk.py`
> - `mcp_server.py`
> - `tests/test_asset_upload_chunk.py`

---

## TL;DR（30 秒读完）

2026-08-30 04:53 有客户端通过 MCP 上传 8 张图片，**全部失败**，日志里只有一句
`project_id must be a safe basename`，客户端侧表现为「图片不显示」。

根因：调用方没传 `project_id`，而工具的 schema 里 `project_id` 不在必填列表，
协议层放行，到运行时才炸；失败发生在三段式上传的第一步 `start`，后续 `append`/`complete`
一次都没发，服务端什么都没落盘，所以客户端无图可显示。

本次改动**不改变任何正确用法的行为**，只做三件事：

1. 运行时按 operation 校验必填参数，缺什么明确报什么（不再是那句看不懂的 basename 报错）
2. 客户端看到的工具描述里写清每个 operation 的必填参数
3. `operation` 从 `str` 收紧成枚举 `Literal["start","append","complete"]`

**注意：本次修复只是让错误可诊断，不能让那个客户端自动传对参数。**
上线后需要用真实客户端复跑一次，看新报错判断它是漏传还是参数名写错。

---

## ⚠️ 动手前必读：改动目前是「未提交」状态

三个文件目前只是工作区改动，**没有 commit**。

因此：

- **不要直接跑 `scripts/update_frameflow_server.sh`（不带 `--no-pull`）** —— 它会 `git pull`，
  未提交的改动可能被覆盖或导致 pull 失败。
- 要么先 commit（推荐），要么全程用 `--no-pull`。

建议服务端先执行：

```bash
cd /opt/OpenMontage_Voicebox
git -c safe.directory='*' status --short
# 期望看到 3 个 M：
#  M mcp_server.py
#  M tests/test_asset_upload_chunk.py
#  M tools/asset_upload_chunk.py
```

确认改动在位后再往下走。

---

## 背景：04:53 那次失败的完整链路

```
[04:53:58] upload_asset_chunk dispatch operation=start request_id=2adff168... session_hash=459bfe72...
           project_id=None upload_hash=None total_bytes=1486629 offset=None chunk_b64_chars=0
[04:53:58] upload_asset_chunk completed operation=start ... success=False elapsed_ms=5
           error=project_id must be a safe basename
```

- 客户端 IP `192.168.20.168`，共 8 次调用，**全部是 `operation=start`**，没有一次 `append` 或 `complete`
- 每次都声明了 `total_bytes`（1.07~1.66 MB，8 张不同大小的图），但 `chunk_b64_chars=0`
- 服务端 1~7 ms 就返回失败，没写任何文件

对比成功案例（08-29 22:31，同一个工具）：

| | 成功 | 04:53 失败 |
|---|---|---|
| project_id | `frameflow-batch-batch-<24hex>` | `None` |
| 流程 | start → append×17 → complete | 只有 start ×8 |
| chunk 数据 | append 带 `chunk_b64_chars=1398104` | 无 |
| 结果 | success=True | success=False |

全量统计（当天 `mcp_server.log`）：441 次调用，431 成功、10 失败。
失败只有两类，本次 8 次全是第一类：

- `project_id must be a safe basename` — 8 次（就是这次）
- `MCP session batch is currently rendering` — 2 次（08-29 21:27，complete 撞上渲染，与本次无关）

### 为什么能确定是「没传」而不是「传了非法值」

`mcp_server.py` 打日志前会把 project_id 做一次 sanitize：

```python
project_log = re.sub(r"[^A-Za-z0-9._-]", "_", str(project_id))[:128]
```

如果传的是非法字符串（比如 `my project`），日志会打出 `my_project`。
实际打出的是 `None`，说明 Python 对象就是 `None` —— 即调用方压根没传这个参数，
或者用了 `project` / `projectId` 这类参数名被 `inputs.get("project_id")` 取成 None。

---

## 根因：三层 schema 里，真正生效的那层没兜住

这个仓库里工具的「入参定义」实际有三层，很容易搞混：

| 层 | 位置 | 作用 |
|---|---|---|
| ① 目录元数据 | `tools/*.py` 类里的 `input_schema` | 只喂给 `get_tool_info`（`tools/base_tool.py:420`），**不参与校验** |
| ② MCP 契约 | MCP SDK 从 `mcp_server.py` 的 Python 签名生成 | 客户端 `tools/list` 看到的就是这个，**这层才校验** |
| ③ 运行时兜底 | `Tool.execute()` | 最后一道 |

原代码的问题：

- ② 里 `upload_asset_chunk` 的签名是 `project_id: Optional[str] = None` —— 可选
- ① 里 `required` 只有 `["operation"]` —— 也没标
- ③ 里 start 分支直接用正则校验，缺失时报的是格式错误，而不是「你没传」

顺带一个不一致：`upload_asset`（一次性上传）的签名是 `project_id: str` **必填**，
`upload_asset_chunk`（分块上传）却是可选。两个工具对同一字段的要求不一样。

### 关于「条件必填」的说明

`project_id` 只在 `start` 时必填，`append`/`complete` 靠 `upload_id`，所以不能简单加进 `required`。
而 MCP SDK 1.29 是从 Python 类型注解生成 schema 的，`@mcp.tool()` 不接受自定义 schema 覆盖，
**条件必填无法在 ② 表达**。因此本次的做法是：

- ② 能做的：把 `operation` 收紧成枚举，并把必填说明写进 `description`（LLM 驱动的客户端读这个决定传什么）
- ① 补齐：`input_schema` 加 `allOf` + `if/then` 条件必填，让目录信息准确
- ③ 兜住：运行时校验，这是真正拦住的地方

---

## 改动内容

### 1. `tools/asset_upload_chunk.py`（+58/-8）

新增模块级常量与辅助函数：

```python
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROJECT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

_REQUIRED_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "start": ("project_id", "filename", "total_bytes"),
    "append": ("upload_id", "offset", "chunk_base64"),
    "complete": ("upload_id",),
}

def _is_absent(value: Any) -> bool:
    """Treat None and blank strings as missing; 0 and False are valid values."""
    return value is None or (isinstance(value, str) and not value.strip())
```

`execute()` 开头、任何写盘之前插入校验：

```python
operation = inputs.get("operation")
if operation not in _REQUIRED_BY_OPERATION:
    raise ValueError("operation must be start, append, or complete")
required = _REQUIRED_BY_OPERATION[operation]
missing = [name for name in required if _is_absent(inputs.get(name))]
if missing:
    raise ValueError(
        f"operation={operation} is missing required argument(s): {', '.join(missing)}. "
        f"Required for operation={operation}: {', '.join(required)}."
    )
```

要点：

- `_is_absent` 只把 `None` 和空串当缺失。**`offset=0` 是合法值**（第一个分片），不能被误判为缺失，
  已有 round-trip 用例覆盖。
- project_id 格式错误的信息带上了规则本身，便于自助排查：
  `project_id must be a safe basename: 1-128 chars, start with a letter or digit, then letters, digits, '.', '_' or '-' only (e.g. 'mclaw-demo')`
- **顺带修掉一个崩溃隐患**：原先 `operation=None` 会走到 `_state_paths(None)`，
  `re.fullmatch` 抛 `TypeError`，而 `except` 只接 `OSError/ValueError/KeyError/JSONDecodeError`，
  `TypeError` 会直接穿透。现在在入口就被拦住。
- `input_schema` 加 `allOf` + `if/then` 条件必填，并给关键字段补 `description`。

### 2. `mcp_server.py`（+16/-2）

- `operation: str` → `operation: Literal["start", "append", "complete"]`
  （客户端 schema 里从 `{"type":"string"}` 变成带 `enum`）
- docstring 重写，写清每个 operation 的必填参数 + project_id 的格式规则。
  这段 docstring 就是客户端看到的 `description`，LLM 驱动的客户端靠它决定传什么参数。

### 3. `tests/test_asset_upload_chunk.py`（+66）

新增 9 个用例（原 8 个，共 17 个）：

| 用例 | 覆盖 |
|---|---|
| `test_chunk_start_reports_every_missing_argument` | **04:53 事故的精确复现**：只传 operation+total_bytes，断言报错含 `missing required argument` 且点名 `project_id`、`filename`，且没写任何 `.uploads` 状态 |
| `test_chunk_start_rejects_unsafe_project_id`（7 组参数） | `my project` / `../evil` / `/abs/path` / `with/slash` / `项目A` / `-leading` / 129 个 a |
| `test_chunk_upload_rejects_unknown_operation` | `operation="bogus"` 被拦，不再崩 |

> 注意：`scripts/update_frameflow_server.sh` 的测试门禁里本来就跑
> `pytest -q tests/test_asset_upload_chunk.py`，所以这次新增用例会自动进入部署门禁。

---

## 服务端执行清单

### 步骤 0：确认代码到位 + 服务实际运行路径

```bash
cd /opt/OpenMontage_Voicebox
git -c safe.directory='*' status --short

# 确认 systemd 里 MCP 是从哪个目录起的（部署脚本里 REPO 默认是 /opt/OpenMontage，
# 但共享目录对应的是 /opt/OpenMontage_Voicebox/，两者需核对一致）
sudo systemctl show openmontage-mcp.service -p WorkingDirectory -p ExecStart --value
```

如果 `WorkingDirectory` 不是 `/opt/OpenMontage_Voicebox`，以实际值为准执行后续步骤。

### 步骤 1：用真实 venv 跑测试

仓库目标 Python 是 `3.10.12`（见 `.python-version`），部署脚本实际用的是
`/root/.pyenv/versions/3.11.8/bin/python3`。**不要**用与目标版本差异过大的解释器凑合，
例如不要直接拿系统 python3 跑（`mcp>=1.29` 等依赖可能不在里面）。

```bash
cd /opt/OpenMontage_Voicebox

# 首选：仓库自带 venv（pyvenv.cfg 里 home = /root/.pyenv/versions/3.10.12/bin）
./.venv/bin/python -m pytest tests/test_asset_upload_chunk.py -v
# 等价写法：./.venv/bin/pytest tests/test_asset_upload_chunk.py -v

# 若 .venv/bin/python 缺失，退回部署脚本的选法
PY=/root/.pyenv/versions/3.11.8/bin/python3
[[ -x "$PY" ]] || PY=python3
"$PY" -m pytest -q tests/test_asset_upload_chunk.py
```

> 注：从 Windows 通过 SMB 看 `.venv/bin/` 看不到 `python`/`python3` 符号链接
> （只能看到 `pytest`），这是 Samba 的显示问题，Linux 侧文件是在的
> （`.venv/bin/pytest` 的 shebang 就是 `#!/opt/OpenMontage_Voicebox/.venv/bin/python`）。
> **在服务端执行不用管这条。**

**期望：17 passed。**（8 个原有 + 9 个新增）

顺带把相关套件一起跑一遍，确认没有连带影响：

```bash
./.venv/bin/python -m pytest -q \
  tests/test_asset_upload_chunk.py \
  tests/test_read_session_asset.py \
  tests/test_resolve_session_asset_path.py \
  tests/test_session_asset_concurrency.py
```

**期望：34 passed。**

> 说明：我这边的验证是在 Windows + Python 3.13 上做的（`lib/workbuddy_session.py` 依赖 POSIX
> 的 `fcntl`，Windows 下需要打桩才能跑），34 passed。但**仓库目标是 3.10.12，
> 请在服务端用真实 venv 复跑确认**，这是本次最需要服务端补的一步。

### 步骤 2：重启 MCP 服务

```bash
sudo systemctl restart openmontage-mcp.service
sleep 3
sudo systemctl is-active openmontage-mcp.service     # 期望: active
sudo journalctl -u openmontage-mcp.service --no-pager -n 30
```

如果 MCP 跑在 tmux/docker 而非 systemd，按实际方式重启（参考
`docs/frameflow-asset-404-deployment-2026-08-19.md` 的 1.4 节）。

### 步骤 3：验证新代码已生效（关键）

这条 curl 是**判定改动是否上线的唯一标准** —— 故意不传 `project_id`，看报错是不是新的。

```bash
cd /opt/OpenMontage_Voicebox
TOKEN="$(grep -E '^MCP_API_TOKEN=' .env | cut -d= -f2-)"
TOKEN="${TOKEN%\"}"; TOKEN="${TOKEN#\"}"     # 去掉 .env 里可能的包裹引号
[[ -n "$TOKEN" ]] || { echo "未取到 MCP_API_TOKEN"; exit 1; }
MCP="http://127.0.0.1:8900/mcp"

# 3.1 initialize，拿 Mcp-Session-Id
RESP=$(curl -sS -m 5 -i -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1"}}}')
SID=$(echo "$RESP" | grep -i "Mcp-Session-Id:" | head -1 | awk '{print $2}' | tr -d '\r')
echo "sid: ${SID:0:12}..."

# 3.2 复现 04:53 的调用：start 且不传 project_id
curl -sS -m 10 -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"upload_asset_chunk","arguments":{"operation":"start","total_bytes":1486629}}}' \
  | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('result',{}), ensure_ascii=False, indent=2)[:600])"
```

**重启前（旧代码）会看到：**

```
project_id must be a safe basename
```

**重启后（新代码）期望看到：**

```
operation=start is missing required argument(s): project_id, filename.
Required for operation=start: project_id, filename, total_bytes.
```

看到新文案 = 改动已生效。看到旧文案 = 代码没到位或没重启，回头查步骤 0。

顺带确认 `operation` 已经是枚举：

```bash
curl -sS -m 10 -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}' \
  | python3 -c "
import json,sys
t=[x for x in json.load(sys.stdin)['result']['tools'] if x['name']=='upload_asset_chunk'][0]
print('operation enum:', t['inputSchema']['properties']['operation'].get('enum'))
print('required      :', t['inputSchema'].get('required'))
print()
print(t['description'])
"
```

**期望：** `operation enum: ['start', 'append', 'complete']`，且 description 里
有「Required arguments per operation」段落。

### 步骤 4：确认没有连带影响（可选但建议）

用仓库自带的探测脚本跑一次完整的分块上传，确认正常路径没被改坏：

```bash
cd /opt/OpenMontage_Voicebox
# 先确认有 MCP session（没有的话先跑一次 init）
python3 scripts/mcp_helper.py init

# 找一张小图跑完整 start -> append -> complete
python3 om_mcp_probe.py chunkupload /path/to/a/small.jpg -p mclaw-demo -v
```

**期望：** 三步都成功，日志末尾有 `chunk_upload complete: {... "success": true ...}`。

---

## 验收标准

- [ ] `git status` 显示 3 个文件已改动（若已 commit 则显示已提交）
- [ ] `pytest tests/test_asset_upload_chunk.py` → **17 passed**
- [ ] 4 个相关测试文件 → **34 passed**
- [ ] MCP 服务重启后 `is-active` 为 `active`
- [ ] 步骤 3.2 返回**新**的 `missing required argument` 报错
- [ ] `tools/list` 里 `operation` 带 enum
- [ ] `om_mcp_probe.py chunkupload` 完整跑通

---

## 回滚方案

改动是纯校验加固，不改任何正确用法的行为，风险很低。真要回滚：

```bash
cd /opt/OpenMontage_Voicebox

# 情况 A：还没 commit —— 直接丢掉工作区改动
git -c safe.directory='*' checkout -- \
  tools/asset_upload_chunk.py mcp_server.py tests/test_asset_upload_chunk.py

# 情况 B：已经 commit —— revert 那个 commit
git -c safe.directory='*' revert --no-edit <commit>

sudo systemctl restart openmontage-mcp.service
```

回滚后步骤 3.2 应重新返回 `project_id must be a safe basename`。

---

## 遗留问题（不阻塞本次上线，但建议跟进）

### 1. 那个客户端到底为什么没传 project_id —— 要靠新报错定位

本次只解决「报错看不懂」。上线后用真实客户端复跑一次：

- 若新报错点名 `project_id` → 确实是漏传，改客户端即可
- 若客户端坚称传了 → 大概率用了 `project` / `projectId` 之类的参数名，被
  `inputs.get("project_id")` 取成 None

### 2. `session_video.log` 里的 `relative_path` 问题（另一条链路）

```
workflow_failed stage=validation asset_count=2
error=session asset has no relative_path; cannot resolve a filesystem location
  File "mcp_server.py", line 1292, in _resolve_session_asset_path
```

来自 `mcp_server.py:1291-1292`，是 `create_remotion_video_share` 的校验链路，
project_id 是 `demo`、时间在 08-29 14:17，看着像历史/drain 数据，**与本次上传失败不是一回事**。
但如果修好 project_id 后图片仍不显示，这条值得一起查。

### 3. split-host 部署下的缩略图 404（`commit 22b852c` 已处理，需确认部署）

`docs/frameflow-asset-404-deployment-2026-08-19.md` 记录的问题：BFF 与 MCP 分机部署时，
BFF 在自己磁盘上找文件必然 404，前端是静默裂图。该修复已合入，
**但需确认 BFF 侧也已部署**（两端都要更新才生效）。

### 4. `upload_asset` 与 `upload_asset_chunk` 字段要求不一致

前者 `project_id` 必填，后者可选。这次没动（改签名会影响所有现有客户端），
但如果需要彻底统一，得单独评估。

---

## 附录：给调用方的正确用法

三段式，每步都要带 `project_id`：

```
start    → { operation, project_id, filename, total_bytes, mime_type?, sha256? }  返回 upload_id
append   → { operation, project_id, upload_id, offset, chunk_base64 }             每片 ≤ 1 MiB
complete → { operation, project_id, upload_id }
```

- `offset` 用**二进制字节偏移**，不是 base64 字符偏移（踩过坑，见 `om_mcp_probe.py:222-228` 注释）
- `project_id` 规则：1~128 字符，首字符字母或数字，之后可用 `A-Za-z0-9._-`；
  合法示例 `mclaw-demo`、`frameflow-default`、`frameflow-batch-batch-<24hex>`
- 一次上传的三次调用必须用**同一个** `project_id`
- 仓库内的可运行参考：`om_mcp_probe.py chunkupload FILE -p PROJECT`
  （`--project` 默认 `mclaw-demo`）
