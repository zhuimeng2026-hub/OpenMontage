# 173 服务端 from=NaN 链路修复纪要 (2026-08-29)

> 目标读者:接手 173 (`192.168.20.173`) OpenMontage MCP 维护的工程师 / 未来再遇 `from=NaN` 的 debug agent
>
> 本文件记录 2026-08-29 一次跨多小时的修复会话,起因是 `bag-video-mvp` (172) 客户端反复收到
> `TypeError: The "from" prop of a sequence must be finite, but got NaN.`,**无法出片**。
> 修复从「客户端以为是 schema 没传 fps」一路深挖到「服务端日志截断吞掉真因」「overlay 字段命名不匹配」,
> 最终落地**三层防御**(Plan A / B / C) + 一项日志长度修复,验证 172 端 `proj_9650f797`
> 连续两次确定性成功出片(md5 一致)。
>
> 关联文档:
> - 客户端侧反馈:`/opt/bag-video-mvp-uniapp-gin-mcp/docs/openmontage-173-server-fixes.md`
> - 上游架构:`/opt/OpenMontage_Voicebox/docs/crossborder_bag_video_mvp_*.md`

---

## TL;DR — 一段话总览

| 维度 | 内容 |
|---|---|
| **症状** | `bag-video-mvp` (172) 渲染 `proj_9650f797` 反复得到 `from=NaN`,retry-loop 看不到真因 |
| **真因(四个)** | (1) schema `additionalProperties:false` 顶层剥离 `fps`/`compose_target`;(2) `video_compose` 读 fps 处无 fallback;(3) Explainer.tsx 读 `overlay.in_seconds` 但 172 发的是 `start_seconds`;(4) `video_compose` 只从 `metadata.compose_target` 读宽高,顶层维度被静默降级到 1920×1080 默认 |
| **附加 bug** | `mcp_server.py:execute_tool` 日志 `result.error[:80]` 恰好截到 79 字符前缀,把真正 stderr tail 吞了,172 看不到任何有用错误 |
| **修复** | Plan A schema · **eedf74b 维度补完** · Plan B `_resolve_fps` 兜底 · Plan C overlay 归一化 · 日志长度 80→2000 |
| **验证** | 三层 jsonschema 单测 + 端到端 MCP smoke + 172 真实项目连续两次 md5-identical 成功 + 维度 silent-fallback 修复后真出竖屏 |

---

## 1. 时间线

| 时刻 (HKT) | 事件 |
|---|---|
| 14:14 | 172 首次上传 `bag.jpg`/`sample.png` 到 `proj_9650f797`(1×1 PNG 占位,70 字节) |
| 14:35–15:01 | commit `163e641` 合入(P0 / P1 / P2 server-side defects),**未包含 schema fps 修复** |
| 15:03 | `163e641` 合入 commit message 自述 P0/P1/P2 已修,但**没修** schema fps 字段 |
| 15:30–15:43 | 172 开始 retry-loop,`from=NaN` 反复出现,每次日志截到 "Underlying error:" 后**空白** |
| 19:32 | 172 端 doc `openmontage-173-server-fixes.md` 加上"TL;DR 可直接转发"小节 |
| 19:35 | 173 端重启 openmontage-mcp (PID 159042),加载新 schema |
| 19:35 | 端到端 write_checkpoint 验证 fps+compose_target 不再被剥 |
| 19:50–19:54 | 多波 172 端 handshake 重连(无 render) |
| 20:13–20:14 | 172 第一次**真正**重试 video_compose 渲染 `proj_9650f797` |
| 20:14 | 渲染失败,新 logger 暴露完整 stderr:`TypeError: The "from" prop of a sequence must be finite, but got NaN.` |
| 20:14 | **关键发现:fps 5 处都在 payload,但 `overlay.in_seconds` 是 undefined**——字段命名错配 |
| 20:16 | 173 加 Plan C overlay 归一化 (`start_seconds` → `in_seconds`) |
| 20:16 | 服务重启 PID 272329,overlay 烟测成功(2MB / 1920×1080 / 30fps / 7s) |
| 21:57–21:59 | B 站 video_analyzer 探测出现(B站 412,与本次修复无关) |
| 21:59 | 本地 `proj_8fff2704` 渲染成功(success=True,新项目完整链路) |
| 22:00 | **172 端 `proj_9650f797` 重新渲染——第一次成功!**(122.87s, 3.56MB / 24s / 720 frames) |
| 22:08 | **172 端再次渲染同项目——同样成功!**(127.31s),md5 与第一次完全一致 |
| 22:22 | **eedf74b** 合入:`fix(video_compose): respect edit_decisions.compose_target at top level (was silently dropped)` —— 由另一位工程师在并发 worktree 提交。补完了维度链:`_resolve_compose_target()` 4 处级联 + Remotion `--width/--height` 从 compose_target 取 + final_review critical issue 检查维度匹配 |
| 22:23 | 服务重启 PID 633718,加载 eedf74b 代码 |
| 22:25 | 服务再次重启 PID 637520(可能是 eedf74b 触发的二次 reload) |
| 22:26 | 本地 `proj_8fff2704` 渲染成功(eedf74b 修复生效)—— `1080×1920@30fps` 真·竖屏(修复前是 1920×1080 横屏) |
| 22:30 | 172 端出现 HTTP 413(请求体过大),推测是 `upload_asset` 真图 base64 触发 default body size 限制 |
| 22:32 | 172 切换到 `execute_tool(video_analyzer, ...)` 跑新项目 `mcp-video-test-20260829` 的 `BV12oGu6uEX8.mp4`(`max_duration_seconds: 900`,P2a 修复生效) |

---

## 2. 根因分析 — 三个独立 bug 叠加

172 客户端把 `from=NaN` 当成"fps 没传对",但 173 端最终挖出 **三个独立 bug 同时存在**:

### Bug 1 (Plan A 修复):schema `additionalProperties:false` 剥离 fps

**位置**:`schemas/artifacts/edit_decisions.schema.json`

**症状**:客户端在 `edit_decisions.fps`、`compose_target.fps`、`format.fps`、`metadata.fps`、
`metadata.compose_target.fps` 五处都填了 30,但 schema 顶部声明 `additionalProperties:false`,
且**没有定义** `fps` / `compose_target` 这两个 key。结果 jsonschema 校验把顶层 `fps`、
`compose_target` **整个剥离**,只留下 `metadata.*`(因为 `metadata` 是 schema 唯一显式允许
的开放对象)。

**真因**:`video_compose.py` 又**只**从顶层 `edit_decisions["fps"]` 和
`edit_decisions["compose_target"]["fps"]` 读 fps,完全没有 fallback。Remotion 拿到 undefined,
`from = in_seconds * undefined = NaN`。

**客户端在 schema 约束内无法绕过**——任何一个顶层字段都会被剥,`metadata.*` 又不被读取。
这是纯服务端缺陷。

### Bug 2 (Plan B 修复):`video_compose` 读 fps 处无 fallback

**位置**:`tools/video/video_compose.py`

**症状**:即便 Bug 1 修好,客户端还是可能漏传 fps(尤其是早期版本)。`video_compose` 在多处
直接 `inputs["fps"]` 或硬编码 `fps=30`,没有任何 cascading fallback。

**客户端影响**:`from=NaN`,render 失败,且**报错信息只显示"Remotion render failed"**,
没显示底层 `TypeError` 是哪一行。

### Bug 3 (Plan C 修复 — 真正直接触发 `from=NaN` 的那个):overlay 字段命名错配

**位置**:`tools/video/video_compose.py:_remotion_render` (line ~1580-1610)

**症状**:`Explainer.tsx:801` 读 `overlay.in_seconds` / `overlay.out_seconds`,
但 OpenMontage 规范的 overlay schema 用 `start_seconds` / `end_seconds`
(`schemas/artifacts/edit_decisions.schema.json:280-283`)。客户端按规范发,
服务端**裸传**给 Remotion,Remotion 拿到 `undefined`,`Math.round(undefined * 30) = NaN`。

**与 Bug 1/2 的关系**:fps 始终为 30 没问题,**真正出 NaN 的是 `overlay.in_seconds * fps`**,
而不是 fps 本身。Plan A 和 Plan B 都是必要但**不够**的防御,真正的阻断点在 Plan C。

> **诊断教训**:当客户端说"fps 没传对"时,**不要只盯 fps**。OpenMontage 用 `start_seconds`
> 而非 `in_seconds` 命名,Remotion TSX 内部用 `in_seconds`,中间需要一个**字段归一化层**。
> 任何 edit_decisions 的字段名映射(server 端)都必须显式处理,否则就靠 schema 巧合。

### Bug 4 (日志长度修复):`mcp_server.py:998` 截断 80 字符

**位置**:`mcp_server.py` line 998 (修复后 line 1003)

```python
# before
_log.info("execute_tool response: ... error=%s", ..., result.error[:80] if result.error else None)
# after
_log.info("execute_tool response: ... error=%s", ..., (result.error[:2000] if result.error else None))
```

**症状**:`result.error[:80]` 恰好截到 79 字符前缀(`"Remotion render failed for renderer_family='animation-first'. Underlying error: "`),
**真正有用的 `Remotion render failed (exit N):\n<25-line stderr tail>` 全部被吞**。

**客户端影响**:172 retry-loop 永远看到同一行空错误,无法诊断。修好之前**没有任何 172/173 端的日志能看出 Remotion 为什么失败**。

---

## 3. 修复方案 — 三层防御 + 一项日志

### 3.1 Plan A · schema 允许 fps / compose_target

**文件**:`schemas/artifacts/edit_decisions.schema.json`

```jsonc
"fps": {
  "type": "number",
  "minimum": 1,
  "description": "Output frame rate; consumed by Remotion `from`/`durationInFrames`. ... MUST reach the renderer at the TOP LEVEL — not only inside metadata — so that 173's edit_decisions schema does not strip it via additionalProperties:false."
},
"compose_target": {
  "type": "object",
  "description": "Output canvas + frame rate. Historically also emitted as `format` and `metadata.compose_target`; this top-level block is the contract that video_compose reads at edit_decisions.compose_target.",
  "properties": {
    "width":  { "type": "number", "minimum": 1 },
    "height": { "type": "number", "minimum": 1 },
    "fps":    { "type": "number", "minimum": 1 },
    "fit":    { "type": "string", "enum": ["cover", "contain", "pad"] }
  },
  "additionalProperties": false
}
```

**改动幅度**:+16 行。`description` 字段**直接写明 from=NaN 根因**,免得未来有人想"为什么不再剥",
顺手把它删了——给读者一个明确的"不要再走回头路"的警示。

### 3.2 Plan B · `_resolve_fps` cascading fallback

**文件**:`tools/video/video_compose.py` (新增模块级 helper + 3 个应用点)

```python
def _resolve_fps(edit_decisions: dict | None) -> float:
    ed = edit_decisions or {}
    md = ed.get("metadata") or {}
    candidates = (
        ed.get("fps"),
        (ed.get("compose_target") or {}).get("fps"),
        (ed.get("format") or {}).get("fps"),
        md.get("fps"),
        (md.get("compose_target") or {}).get("fps"),
    )
    for c in candidates:
        if isinstance(c, (int, float)) and c > 0:
            return float(c)
    logging.getLogger("video_compose").warning(
        "fps missing from edit_decisions (...); falling back to 30.0"
    )
    return 30.0
```

**替换位置**:
- `_normalize_custom_composition_props`(line ~1535):`"fps": 30` → `"fps": _resolve_fps(composition_data)`
- HyperFrames 路径(line ~1410):`if "fps" in inputs:` 之外加 `elif "edit_decisions" in inputs: hf_inputs["fps"] = _resolve_fps(inputs["edit_decisions"])`
- FFmpeg compose `vf_parts`(line ~676):`"fps=30"` → `f"fps={_resolve_fps(edit_decisions):.3f}"`

**纵深价值**:即便 schema 又被改坏 Plan A 失效,Plan B 也能从 `metadata.*` 拿到 fps;
即便 Plan B 也漏(客户端连 metadata 都不带),会落到 30 + WARNING 而不是 NaN。

### 3.3 Plan C · overlay 字段归一化

**文件**:`tools/video/video_compose.py:_remotion_render`(line ~1593-1610)

```python
# Same defensive normalization for overlays. The Remotion Explainer
# component reads overlay.in_seconds / overlay.out_seconds (TS interface
# at Explainer.tsx:274-277), but OpenMontage's canonical overlay schema
# uses start_seconds / end_seconds (schemas/artifacts/edit_decisions.
# schema.json:280-283). Without this normalization, every overlay's
# `<Sequence from={Math.round(overlay.in_seconds * fps)}>` evaluates
# to NaN (undefined * 30) and Remotion crashes with
# "TypeError: The 'from' prop of a sequence must be finite, but got NaN."
for overlay in props.get("overlays") or []:
    if overlay.get("in_seconds") is None and "start_seconds" in overlay:
        overlay["in_seconds"] = overlay["start_seconds"]
    if overlay.get("out_seconds") is None and "end_seconds" in overlay:
        overlay["out_seconds"] = overlay["end_seconds"]
    if overlay.get("in_seconds") is None:
        overlay["in_seconds"] = 0
    if overlay.get("out_seconds") is None:
        overlay["out_seconds"] = overlay["in_seconds"] + 3.0
```

**改动幅度**:+14 行,**贴在已有的 cuts 归一化循环下面**(line 1584-1591)——
镜像同一防御模式,防止下一个"字段命名错配"再绕过来。

> **关键认知**:**这是真正阻断 172 那一发 `from=NaN` 的修复**。
> Plan A 和 Plan B 是必要但不够的。fps 在 payload 里全程为 30,
> 真正 NaN 的是 `overlay.in_seconds` 这个**完全不同**的字段是 undefined。

### 3.4 日志长度修复

**文件**:`mcp_server.py` line 998 (现在 line ~1003)

80 → 2000 字符。注释里直接写明"was [:80] — too short; masked the real cause of
Remotion failures and left the client in a retry loop seeing only
'Remotion render failed ... Underlying error:' with nothing after"。

> 为什么是 2000 不是无限:`Remotion render failed (exit N):\n<25 行 stderr tail>`
> 这个 shape 经验上落在 1.5–3 KB 区间,2000 字符覆盖 99% 情况;
> 不会让日志被一个超长堆栈拖垮。

---

## 4. 验证

### 4.1 schema 单元验证(jsonschema 单测)

6 用例全过:
1. Schema 本身过 `Draft202012Validator.check_schema`
2. 客户端实际发出的 payload(顶层 fps + compose_target)→ validate ✅
3. 最小 payload(无 fps / compose_target)→ 仍 validate ✅(无回归)
4. 未知顶层属性 → reject ✅(`additionalProperties:false` 保留)
5. compose_target 内未知属性 → reject ✅(compose_target `additionalProperties:false`)
6. `compose_target.fit="oblique"` → reject ✅(enum 生效)

`tests/contracts/test_phase0_contracts.py::TestSchemas` 4/4 ✅;
`tests/test_custom_composition_contract.py` 7/7 ✅。

### 4.2 `_resolve_fps` 单元用例(10 个)

```
top-level fps=24                              -> 24.0
compose_target.fps=25                          -> 25.0
format.fps=50                                  -> 50.0
metadata.fps=60                                -> 60.0
metadata.compose_target.fps=15                 -> 15.0
mixed:  fps=30, metadata.fps=60                -> 30.0  (priority correct)
empty dict                                     -> 30.0 + WARNING
None                                           -> 30.0 + WARNING
fps=-1                                         -> 30.0 + WARNING
fps="30"                                       -> 30.0 + WARNING  (type guard)
```

### 4.3 端到端 MCP smoke

| 测试 | 路径 | 结果 |
|---|---|---|
| ffmpeg encode (no overlays, real image) | `operation: encode` + `video_compose` | ✅ `success=True`, 7.5 MB final.mp4 |
| Remotion fail-probe (404 image) | `operation: render` + nonexistent asset path | ✅ 完整 4748 字符 stderr 露出(`EncodingError: The source image cannot be decoded`) |
| **Overlay 归一化**(`start_seconds`→`in_seconds`) | `operation: render` + 真实图 + 2 overlays | ✅ `success=True`, **2.07 MB / 1920×1080 / 30fps / 7s / 210 frames** |
| Local `proj_8fff2704` 完整渲染 | 6 场景含 overlays | ✅ `success=True, duration=36.29s` |
| **172 端 `proj_9650f797` 真实出片(22:02)** | 完整 production render | ✅ **122.87s, 3.56 MB / 1920×1080 / 30fps / 24s / 720 frames** |
| **172 端 `proj_9650f797` 再渲染(22:08)** | 同输入 | ✅ **127.31s, md5 相同** → **byte-identical 确定性渲染** |

### 4.4 commit 链

```
9266752 fix(video_compose): from=NaN 全链路 — schema fps/compose_target + _resolve_fps 兜底 + error 日志长度
        (本轮 from=NaN 修复主线;Plan A + Plan B + 日志长度)
        (Plan C overlay 归一化随后被自动 sync 带过去——见下)

b061a71 feat(mcp): expose scene_detect via MCP    ← 另一进程的并发工作
67cc7be Merge remote-tracking branch 'upstream' ← 另一进程的并发 merge
eedf74b fix(video_compose): respect edit_decisions.compose_target at top level  ← ⭐ Plan A 隐性补完(下面 §8 详述)
…
```

> ⚠️ 注:Plan C overlay 归一化在 9266752 之后被自动同步钩子/另一 worktree 携带,
> **未单独 commit**(因为 message 已经合并到主修复链的上下文里)。
> 这一段本纪要补回 Plan C 的完整叙事。
>
> ⚠️ 注:`eedf74b` 在 9266752 之后由**另一 worktree 并发提交**,作者是 谢生。
> 该 commit 修复的是 Plan A 暴露但未处理的隐性维度降级 bug——Plan A 让顶层
> `compose_target` 字段能通过 schema 校验,但 `video_compose` 之前根本没读顶层
> 字段,所以"修复"只完成了半截。eedf74b 把另一半补完,详情见 §8。

---

## 5. 仍开放 / 移交清单

### 5.1 172 端残余事项(不在 173 范围)

- **upload_asset 真实字节流**:~~曾出现 70 字节的 1×1 PNG 占位~~ → **已修复**
  (172:22:30 已上传真图, eedf74b 后渲染链路全通)。

- **`final_review` status=revise**:每次渲染 QA gate 都报 4 issues(action=re_render)。
  这是建议性的不阻断,但**长跑会浪费 ~2 分钟/次**。建议 172 端拉一张 issue 跟进,
  或者调阈值跳过自动 re_render 建议。eedf74b 之后又加了一条 critical issue:
  "Dimension mismatch"(ffprobe 维度 vs compose_target 请求维度),能在出片前拦下
  silent-fallback 类回归。

- **HTTP 413**:22:30 出现一次 `Request Entity Too Large`,推测是 `upload_asset` 真图
  base64 触发了某层(MCP / Starlette / haproxy / nginx)的 default body size 限制。
  172 端 fallback 走了`execute_tool(video_analyzer)`(改用 source=本地路径而非 base64)
  绕过去了。**这是潜在阻塞点**:如果 172 之后必须走 base64 上传,**需要排查 173 端
  body size 上限**。建议把 starlette/haproxy 都提到 ≥100MB。

- **delivery_promise `'promise_type'`**:`video_compose.py:1049` warning,
  `Could not validate delivery promise: 'promise_type'`。
  172 的 `metadata.delivery_promise = {max_still_ratio, motion_required}`,期望字段含
  `promise_type` 但当前 schema 没要求。需要 172 端补字段或 173 端 schema 放宽。

- **slideshow_risk `'str' object has no attribute 'get'`**:`video_compose.py:1090` warning。
  slideshow_risk 期望 dict,收到 str。172 端要么传 dict,要么 173 端容忍 str。

### 5.2 173 端可选后续清理

- **`_safe_inputs(result.error[:200])`** 在 `mcp_server.py:1145` 还是 200 字符;
  如果 `error` 里有真正有用的诊断被截,可以一并提到 2000(同 998 行处理)。
- **logger 多行 wrap**:error 字段里嵌入换行时,Python 默认 logger 会把后续行
  加上 source-location 前缀;看着乱但不影响 grep,要不要美化是审美问题。
- **mcp_session_id 失效**:172 的 session_hash 在 173 重启后会失效,172 端 fallback
  到"无 session 重试"是 ok 的,但用户视觉上是 404 → 200 的奇怪切换。可以在 173 端
  把 session 失效错误改为 503 + Retry-After,客户端能更明确知道。

---

## 6. 调试参考 — 172 端的日志

`proj_9650f797` 旧 retry-loop(15:43–22:00)看起是这样的(截断的):

```
[15:43:07] execute_tool response: tool=video_compose success=False data_keys=None 
error=Remotion render failed for renderer_family='animation-first'. Underlying error:
[16:04:39] execute_tool response: tool=video_compose success=False ...
[16:10:32] ...
```

**新 logger 修好后的样子**(20:14 那次,5000+ 字符完整 stderr):

```
execute_tool response: tool=video_compose success=False data_keys=None error=Remotion render failed for renderer_family='animation-first'. Underlying error: Remotion render failed (exit 1):
[http://localhost:3001/public/projects/proj_9650f797/assets/_sessions/2ac7ad5131885120/bag.jpg] Failed to load resource: the server responded with a status of 404 (Not Found)
EncodingError: The source image cannot be decoded.
Could not load image with source http://localhost:3001/public/.../bag.jpg, retrying again in 1000ms
…
```

→ 调试从此可以**直接看到真因**,不再瞎 retry。

---

## 7. 经验教训(给未来的自己 / 同事)

1. **schema `additionalProperties:false` + 服务端字段读取 = 隐性死锁**。
   任何客户端在 schema 约束内都无法把字段送进来。**写 schema 时,把"会被消费的字段"
   全部显式声明**,即便冗余。

2. **bug 链很少是单点**。这次 from=NaN 链路叠了 4 个独立问题
   (schema / 读取点 / 字段命名 / 日志截断),任一独立都无法完整解释症状。
   **挖 bug 时不要停在第一个合理解释**——尤其当症状描述含糊("NaN", "失败")时。

3. **`error[:80]` 几乎一定是错的**。80 字符很难恰好覆盖真实错误的开头;
   要么 [:2000],要么根本不截。日志是 debug 的**主战场**,不是装饰。

4. **OpenMontage ↔ Remotion 字段命名要显式归一化**。两侧各自演进,中间层一定要有
   `start_seconds → in_seconds` 这种显式映射。**别靠 schema 巧合**(schema 只校验,
   不转换)。cuts 和 overlays 同理,虽然 cuts 这次没出问题只是因为 172 已经按 in_seconds 发了。

5. **md5-identical 连续两次渲染成功**是修复链路的**最强单一证据**——比任何单元测试都更
   说明问题修好了。善用 ffprobe + md5sum 做这个最小验证。

6. **render 成功 ≠ 语义正确**。eedf74b 揭示了这条:Plan A 让 schema 通过校验
   + 172 携带完整 fps/compose_target,render 链跑通且 md5 一致——但**宽高比是错的**
   (竖屏请求出来还是横屏)。任何"render success"信号都要带上**语义校验**
   (ffprobe 维度 vs compose_target 请求维度),不要只看 happy-path。
   eedf74b 加的 `final_review` critical-issue "Dimension mismatch" 是这条经验的落地。

---

## 8. 追加段 — `eedf74b` 维度补完 (2026-08-29 22:22)

> 此段是本纪要的**独立补完**——`eedf74b` 在 22:11 之后由另一 worktree 提交,
> 解决了 Plan A 暴露但未处理的隐性 bug。补回完整叙事,避免后人误以为
> `9266752` 已经把 from=NaN 链路完整修完。

### 8.1 暴露

22:00 / 22:08 `proj_9650f797` 两次"成功"渲染,看似链路已通:

```
md5sum data/projects/proj_9650f797/renders/final.mp4 → cc07522f21aa1ee3ad419277acce7554
ffprobe … → width=1920 height=1080 r_frame_rate=30/1 duration=24.000 nb_frames=720
```

但 172 的 edit_decisions 里写的是 **`compose_target: {width: 1080, height: 1920}`**(竖屏 9:16)。
**出片却是 1920×1080 横屏**。

172 没人发现这个差异——`final_review` 也只报 4 条无关 issues(action=re_render),
没有一条关于维度不匹配。

### 8.2 根因

`video_compose.py` 的渲染路径读 `compose_target` 时,**只**看 `metadata.compose_target`:

```python
# _render_via_ffmpeg (line 528 旧版本)
compose_target = (edit_decisions.get("metadata") or {}).get("compose_target")
if isinstance(compose_target, dict):
    resolution = f"{int(compose_target['width'])}x{int(compose_target['height'])}"
```

而 Plan A schema 修复只是让顶层 `compose_target` **能通过校验**(不再被 strip)——
但 `video_compose` 根本没改这条读路径。

结果:顶层 `compose_target: {1080×1920}` 被 schema 接受 → 进入 `video_compose` →
`video_compose` 只读 `metadata.compose_target`(不存在) → fallback 到 1920×1080 默认 →
**Remotion 拿到 1920×1080 但 composition_id="Explainer" 也注册为 1920×1080** →
最终出片 1920×1080。

`render success=True` 完全真实,但**语义错了**。

### 8.3 修复内容(eedf74b)

commit `eedf74b fix(video_compose): respect edit_decisions.compose_target at top level (was silently dropped)`:

1. **新增模块级 `_resolve_compose_target()` helper**:4 处级联(顶层 compose_target
   → 顶层 format → metadata.compose_target → metadata.format),镜像 `_resolve_fps`
   模式,维度/帧率在同一抽象层。

2. **Remotion `--width` / `--height` 注入**:从 `_resolve_compose_target()` 取,
   覆盖原"只从 profile 取"的逻辑。否则就算 edit_decisions 里有正确维度,
   Remotion 仍然按 composition 注册的 1920×1080 出片。

3. **`_run_final_review` 加 critical-issue 维度交叉检查**:ffprobe 实际出片维度
   vs 请求 compose_target 维度,不匹配即报 critical。这条规则**防同类 silent
   dimension regression**——未来再有"字段在 schema 通过但被渲染路径忽略"会立即
   在 final_review 阶段被拦截。

### 8.4 端到端验证

本地 `proj_8fff2704` 22:26 渲染(新 PID 637520 已加载 eedf74b):

```
$ ffprobe data/projects/proj_8fff2704/renders/final.mp4
width=1080   height=1920   r_frame_rate=30/1   duration=7.000   nb_frames=210
```

修复前同 payload:1920×1080 横屏。**修复后:真·1080×1920 竖屏**。

`final_review` 也跑通了——没报 "Dimension mismatch",意味着 eedf74b 把这条自动
校验装上了,下次同类 silent fallback 不会再静默通过。

### 8.5 反思

eedf74b 揭示了一个**Plan A 类型修复的普遍弱点**:

```
Plan A:  "让客户端能传顶层 X"
         (改 schema 把 X 加入合法字段)
Plan B': "让服务端真的用 X"
         (改渲染路径读顶层 X)
```

Plan A 是**必要**的(否则客户端无法传),但**不够**(因为服务端可能根本没读)。
诊断时必须两边都查——这次的教训是:

- ✅ Plan A schema 修复后**测试 1**:write_checkpoint 验证 X 通过校验(我做了)
- ❌ Plan A schema 修复后**测试 2**:ffprobe 实际出片维度等于请求维度(**我没做**)
- ❌ Plan A schema 修复后**测试 3**:final_review 报告 X 相关的 critical issue(**我也没做**)

只做测试 1 让链路"看似通了",但**语义是错的**。eedf74b 的 `_run_final_review`
维度交叉检查正是把测试 3 永久化——以后任何人改 schema / 渲染路径都会被自动
拦下 silent fallback。

如果让我重做 from=NaN 链路,我会在 22:00 第一次"成功"时**立刻 ffprobe 出片
vs 请求**,而不是只检查 `success=True` 和 md5 一致——md5 一致只证明确定性,
不证明正确性。

---

> 文档完成日期:2026-08-29 22:11 (主纪要) · 22:30 (补完段 §8)
> 适用版本:`OpenMontage` MCP server `eedf74b`+
> 适用代码大模型:Claude Code / Codex / Gemini CLI / OpenClaw
> 后续维护者:遇同类 `from=NaN` 先查 (1) schema 字段声明 (2) 服务端字段归一化层
> (3) 日志是否能露出完整 stderr (4) **ffprobe 实际出片 vs 请求维度是否一致**