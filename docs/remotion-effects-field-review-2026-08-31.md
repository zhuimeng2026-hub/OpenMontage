# create_remotion_video_share：缺「视频效果」入参 —— 复核结论与建议落点

- 日期：2026-08-31
- 提出方：VClaw Studio（客户端，`D:\vclaw`）
- 复核对象：OpenMontage MCP 工具 `create_remotion_video_share`
- 代码依据：`mcp_server.py:1433`（本仓库，即 B 盘这份源码）
- 状态：**字段已加，待双方部署对齐**（见第 9 节）

---

## 1. 结论（TL;DR）

1. ~~`create_remotion_video_share` **没有任何「效果」类入参**~~ —— 已新增 `effects: Optional[str]`（自由文本），详见 §9。
   客户端在「视频效果」页收集的自然语言效果描述**现在可以下发**了。
2. 效果在模板分支**仍是硬编码的**（`mcp_server.py:1517` 的 `motion` 列表，调用方无法影响）；
   `effects` 字段只是把描述透传到 `edit_decisions.metadata.effects`，**真正消费方（Remotion 模板）还没接**——下游消费是单独的待办，不在本次范围。
3. 唯一能实现任意 Remotion 效果的现成通道仍是 `code` 入参（传自定义 TSX），本仓库 `.env:75` 已开 `CUSTOM_COMPOSITION_ENABLED=true`；它吃的是 TSX 源码，不是自然语言。
4. ⚠️ 第 6 节提到的 `subtitles` 分叉——`subtitles: Optional[list[dict]]` 已补回本仓库，详见 §9.1。
5. ⚠️ **本仓库的修改尚未推到 /opt 线上部署**（见第 9.4 节），仍需部署对齐。

---

## 2. 当前入参 schema

`mcp_server.py:1433`：

```python
@mcp.tool()
async def create_remotion_video_share(
    project_id: Optional[str] = None,
    script_id: str = "photo-ken-burns",
    duration_per_image: float = 3.0,
    aspect_ratio: str = "9:16",
    title: Optional[str] = None,
    code: Optional[str] = None,
    queue_owner_id: Optional[str] = None,
    delivery_promise_override: Optional[dict] = None,
) -> dict[str, Any]:
```

取值约束：

| 入参 | 取值 | 校验位置 |
|---|---|---|
| `script_id` | `photo-ken-burns` / `cinematic-montage` / `ecommerce-product-demo` | `1456-1474` |
| `duration_per_image` | 1–30 秒；`duration × 图片数 ≤ 600` | `1490-1497` |
| `aspect_ratio` | `9:16`(1080×1920) / `16:9`(1920×1080) / `1:1`(1080×1080) | `1498-1501` |
| `code` | 自定义 TSX，需 `CUSTOM_COMPOSITION_ENABLED=true` | `1463-1470` |
| 图片数量 | 默认上限 20（`OPENMONTAGE_MAX_SESSION_IMAGES`） | `1493-1495` |
| `ecommerce-product-demo` | 至少 4 张图（四槽位模板） | `1575-1577` |

---

## 3. 效果现在是怎么决定的（硬编码）

```python
# mcp_server.py:1517
motion = ["zoom-in", "pan-left", "ken-burns", "pan-right"]

# mcp_server.py:1543-1559（模板分支，逐个图片轮转）
animation = motion[index % len(motion)]
cuts.append({..., "transform": {"animation": animation}})
scene_plan.append({..., "shot_language": {"camera_movement": animation, "shot_size": "full-frame"}})
```

也就是说：第 1 张 `zoom-in`、第 2 张 `pan-left`、第 3 张 `ken-burns`、第 4 张 `pan-right`，第 5 张回到 `zoom-in`。
调用方没有任何途径表达「开篇旋转切入」「结尾粒子汇聚」这类诉求。

---

## 4. 建议：字段定名与落点

### 4.1 建议在函数签名上增加一个自由文本入参

```python
async def create_remotion_video_share(
    ...,
    effects: Optional[str] = None,      # 或 effect_prompt / motion_prompt，字段名请你们定
) -> dict[str, Any]:
```

字段名三个候选（`effects` / `effect_prompt` / `motion_prompt`）客户端都能接受，**只要定一个并写进 schema**，
客户端改一处即可生效（见第 7 节），不用动 UI。

### 4.2 落点：模板分支的 `edit_decisions`

`mcp_server.py:1560-1564` 是模板分支组装 `edit_decisions` 的地方，建议把效果描述挂在 metadata 上：

```python
edit_decisions = {
    "version": "1.0", "cuts": cuts, "render_runtime": "remotion",
    "renderer_family": renderer_family, "composition_mode": "templated",
    "metadata": {
        "title": ..., "script_id": script_id, "targetDurationSeconds": ...,
        "compose_target": {"width": width, "height": height, "fit": "cover"},
        "effects": effects,          # ← 新增：透传效果描述
    },
}
```

若希望逐镜头生效，也可以同时进 `scene_plan[i].shot_language`（`1558` 行）与
`cuts[i].transform.animation`（`1550` 行）——这两处目前都只写死 `animation`。

**注意**：`1569-1574` 的 `_ensure_governance_fields()` 是渲染前的治理校验
（`renderer_family` 与 `metadata.delivery_promise` 缺失会被 `video_compose._pre_compose_validation` 拦下），
新增字段请放在它之后不会被覆盖的位置，或直接并入 metadata（metadata 会被整体保留）。

### 4.3 内容格式建议

客户端收集的是**自然语言、按时间顺序分段**的描述，例如：

```
开篇：旋转切入 0.5s（rotate 0→15deg，opacity 0→1，spring 缓动）
中段：Ken Burns 慢推（scale 1→1.08，3s ease-out）
镜头间：Smoothstep 转场 0.6s
结尾：粒子汇聚淡出（200 粒子，4s）
```

如果希望结构化，也可以约定传 JSON 数组（每个元素一个镜头/段落的效果），
请明确一种即可，客户端会按约定调整输入提示。

---

## 5. 备选通道：`code`（自定义 TSX）

`code` 入参允许调用方直接传 Remotion TSX 源码（`composition_mode: "custom"`，`1521-1541`），
绕过模板的 `script_id` 查找，上传的图片通过 `images` 传入供 `staticFile()` 引用。
本仓库 `.env:75` 已 `CUSTOM_COMPOSITION_ENABLED=true`，即**这条通道现在是开着的**。

但它要求 TSX 而非自然语言。若短期内不想改 schema，可以走这条路：
让 DeepSeek 直接产出 Remotion TSX，客户端把 TSX 当 `code` 传。
代价是客户端要新增一个「TSX 代码」输入框并承担编译失败的风险，不如加文本字段稳妥。

---

## 6. ⚠️ 版本分叉：本仓库与 /opt 部署版入参不一致

| 入参 | 本仓库（B 盘，`mcp_server.py:1433`） | /opt 部署版（据 VClaw 侧转述） |
|---|---|---|
| `project_id` | ✅ | ✅ |
| `script_id` | ✅ | ✅ |
| `duration_per_image` | ✅ | ✅ |
| `aspect_ratio` | ✅ | ✅ |
| `title` | ✅ | ✅ |
| `subtitles` | ❌ **没有** | ✅ **有** |
| `code` | ✅ | — |
| `queue_owner_id` | ✅ | — |
| `delivery_promise_override` | ✅ | — |

**影响**：客户端 `montage.ts` 现在会带 `subtitles` 调用（字幕开关功能依赖它）。
这个字段**只对本仓库以外的那份（/opt）有效**。请确认：

- 线上跑的到底是哪一份？（客户端连的是 `192.168.20.173:8900`）
- 若以 /opt 为准，本仓库的 `mcp_server.py` 是否已经落后/超前？要不要同步？
- 加效果字段时，两边都要加，避免再出现这种分叉。

补充线索：`docs/mcp-remote-tool-list.json` 里的 108 个远程工具**不含** `create_remotion_video_share`
（只有 `remotion_caption_burn`）。据此推测该工具由 BFF 合成/白名单暴露，而非内部 registry 直出
（与 `clawx-studio/verify-subtitle.md` 里「BFF 白名单仅放行 upload_asset_chunk /
create_remotion_video_share / get_render_status」的记录一致）。改 schema 时请连同 BFF 的白名单转发一起确认。

---

## 7. 客户端现状（字段定名后的改动量）

- 效果输入：`clawx-studio/src/App.vue` —— 侧栏「视频效果」页，`v-model="videoEffects"` 的多行输入框，含 DeepSeek 取词链接。
- 下发位置：`clawx-studio/src/services/montage.ts` 的 `createRemotionVideo()`：

  ```ts
  return c.callTool('create_remotion_video_share', {
    script_id: ..., duration_per_image: ..., aspect_ratio: ..., title: ..., subtitles: ...,
    // effects: videoEffects,   ← 字段定名后加这一行即可
  });
  ```

**改动量：一行。** UI 不用动。

---

## 8. 请复核确认的三件事

1. 效果字段名定为哪个？（`effects` / `effect_prompt` / `motion_prompt` / 其他）
2. 内容格式：自由文本，还是结构化 JSON（逐镜头/逐段落）？
3. 第 6 节的分叉怎么收敛 —— 线上到底跑哪份 `mcp_server.py`？`subtitles` 是否要补回本仓库？

---

## 9. 已落地的修复（2026-08-31）

8.1 三件事的回应：
- **字段名**：定为 `effects`（与 clawx-studio UI 中 `videoEffects` ref / 「视频效果」页签同根，最通用）。
- **格式**：自由文本（与现有 textarea 零改动即可下发）。后端原样透传到 `edit_decisions.metadata.effects`，未来若需结构化再升 JSON。
- **分叉**：`subtitles` 字段已补回本仓库（详见 9.2）。

### 9.1 修改清单（本仓库 `mcp_server.py:1433` 签名）

新增两个透传参数：

```python
async def create_remotion_video_share(
    ...,
    effects: Optional[str] = None,
    subtitles: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
```

- `effects` —— 自由文本，自然语言的效果/运镜/转场描述。透传到 `edit_decisions.metadata.effects`，由后续 Remotion 模板或自定义合成消费。
- `subtitles` —— cue 列表（`{index, start, end, text}`）。透传到 `edit_decisions.metadata.subtitles`。

两个分支（自定义 `code` 合成 + 模板分支）都接住，详见 `mcp_server.py:1535-1541` 与 `mcp_server.py:1560-1564`。`_ensure_governance_fields` 不覆盖 `metadata` 中已存在的字段（`mcp_server.py:1392`），所以透传安全。

未传时不写入 metadata（back-compat：`frameflow_e2e.py`、`monitor_render/*` 等老调用方不受影响）。

### 9.2 修改清单（vclaw 客户端 `/opt/vclaw/openclaw/clawx-studio/`）

- `src/services/montage.ts` —— `CreateVideoOptions` 增加 `effects?: string`，`createRemotionVideo()` 把它传给 MCP。
- `src/App.vue:556-568` `buildAndSubmit()` —— 把 `videoEffects.value.trim()` 一并下传（空串走 `undefined`）。

UI 不用动：textarea 早已存在（`App.vue:1520`），原本就在 `videoEffects` ref 里，只是没接到 MCP。

### 9.3 测试覆盖

新增 4 个测试到 `tests/test_create_remotion_share_governance_fields.py`：

- `test_create_share_passes_effects_to_metadata` —— 模板分支 `effects` → `metadata.effects`。
- `test_create_share_passes_subtitles_to_metadata` —— 模板分支 `subtitles` → `metadata.subtitles`。
- `test_create_share_omits_effects_subtitles_when_not_supplied` —— 未传时不写入（back-compat）。
- `test_create_share_custom_code_passes_effects_subtitles` —— `code` 分支同样透传。

全部 18 条用例（含原有 14 条）通过：`make test` / `pytest tests/test_create_remotion_share_governance_fields.py -v`。

### 9.4 部署对齐（待）

- 本仓库的修改在 `release/mvp-v0.1-phase-0-5` 分支，未推到线上 MCP（`192.168.20.173:8900`）。
- VClaw Studio 端的修改在 `clawx-studio/` 工作树，未做构建发布。
- 双方各自部署到位后：
  - Studio 端在「视频效果」页填文本 → 出现在成片里（前提：Remotion 模板/合成主动读 `metadata.effects`，目前模板分支还在硬编码 `motion = [zoom-in, pan-left, ken-burns, pan-right]`，下游消费需要单独实现）。
  - Studio 端字幕开关开启时 → `subtitles` 入 `metadata.subtitles`（同样依赖下游消费方实现烧录，目前仓库已有独立工具 `burn_subtitles` 但与本流程未串联，是另一个待办）。
