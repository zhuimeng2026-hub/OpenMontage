# 远程 `create_remotion_video_share` 效果字段复核（2026-08-31）

> 本文件是 `B:\docs\remotion-effects-field-review-2026-08-31.md` 的复核补充。
> 复核时 B 盘（SMB 只读挂载，noacl）不可写，故暂存于本地；B 盘解锁后请把本节并入原文档 §10。

## 结论：远程已按要求处理，三项待确认全部闭合

实测地址：`http://192.168.20.173:8900/mcp`（MCP，`tools/list`，Bearer 用
`D:\vclaw\config.yaml:37` 的 `gateway.mcp_token`）。

### 实测事实

- `serverInfo`：`OpenMontage`，`version: 1.29.1`。
- 工具 `create_remotion_video_share` 的 `inputSchema.properties` 实测包含：

  | 字段 | 类型 | 工具 description 说明 |
  |---|---|---|
  | `effects` | `string \| null` | free-text 自然语言视觉/运镜/转场描述，透传为 `metadata.effects`，供下游 Remotion 模板/未来渲染器消费。"Mirrors VClaw Studio's 视频效果 panel" |
  | `subtitles` | `array[object] \| null` | cue 字典（`index`/`start`/`end`/`text`），透传为 `metadata.subtitles`。"Closed the schema fork with VClaw Studio (see docs/remotion-effects-field-review-2026-08-31.md §6)" |

- 字段名正是评审文档建议的 `effects`，内容为自由文本，与现有 textarea 零改动即可下发。
- 工具 description 明确该工具**已改为非阻塞**：校验入参 → 认领 `render_job_id` → 后台线程
  跑 render→upload→share，立即返回 job id，用 `get_render_status` 轮询。与 VClaw 侧
  `pollRenderStatus` 调用方式吻合。

### 对应 §6 / §8 三件事

1. 效果字段名 → 定为 `effects`（实测确认）。
2. 内容格式 → 自由文本（实测确认）。
3. 版本分叉 → **已澄清：B 盘本仓库 `mcp_server.py` 与 /opt 部署版是同一份**（均已含 `effects` +
   `subtitles`，且 `lib/effects_parser.py` 已消费 effects）。先前"B 盘是基线、未处理 effects"的判断
   错误（详见下方「真实生成抽样复核」与 `remotion-effects-template-implementation-2026-08-31.md` v2）。

### 客户端（VClaw Studio）已接

- `clawx-studio/src/services/montage.ts`：`CreateVideoOptions` 增 `effects?: string`；
  `createRemotionVideo()` 的 `callTool('create_remotion_video_share', {...})` 增 `effects: opts.effects`。
- `clawx-studio/src/App.vue`：合成视频页「生成视频」与分解对标视频页步骤 4「生成」两处调用
  均传 `effects: videoEffects.value || undefined`。`videoEffects` 为顶层 ref，跨页面共享。
- 验证：`vue-tsc --noEmit` 零错误；`vite build` 成功（188.90 KB）。

## 遗留（非阻塞）

1. ~~`effects` 仅验证到 schema 透传~~ → **已抽样确认**：链路端到端接通，但解析词表
   `{zoom-in,zoom-out,pan-left,pan-right,ken-burns,parallax}`（lib/effects_parser.py:42）不含
   rotate/zoom 幅度/fade，导致"360 rotation + zoom 0.4→1.6 + fade-in"降级为 `zoom-in`，成片看似静态。
   **真实改造点在解析词表 + Explainer.tsx 渲染分支**（见 implementation 文档 v2），不在"读取 metadata.effects"。
2. ~~B 盘与 /opt 版本分叉~~ → 已澄清：B 盘与 /opt 是同一份，无需 cherry-pick。
3. B 盘当前为只读挂载，无法回写原评审文档；本复核报告暂存于
   `D:\vclaw\docs\remotion-effects-remote-verification-2026-08-31.md`。

## 真实生成抽样（2026-08-31 复核）

为确认 `effects` 是否真被下游消费，按标准 MCP `tools/call` 走完整链路实测（Bearer 同前）：

- `upload_asset_chunk`（start→append→complete）上传一张 480×480 测试 PNG（带十字/对角参考线，便于肉眼判断旋转/缩放）。
- `create_remotion_video_share` 带 `effects="Apply a CONTINUOUS full 360-degree rotation (0->360) + zoom 0.4x->1.6x + 0.5s fade-in"`（自由文本）。
- 结果：调用成功返回 `render_job_id`；约 35s 后 `status=published`（rendering→rendered→published）。
- 成片：`share_url = https://share.weiyun.com/LGIHrznu`，`video_path = /opt/OpenMontage_Voicebox/projects/vclaweffectstest/renders/8630c3605e2e4d17bcafd685705ba833-f3d6b93bf81944be858922c92c0b4a01.mp4`。

通道层结论：
1. ✅ `effects` 字段已落地且被服务端接受（带 effects 调用成功，无未知字段报错）。
2. ✅ 端到端生成成功、成片已发布。
3. ⚠️ **无法自动证明模板真消费 `metadata.effects`**：`get_render_status` 返回不回显 effects 文本（之前 `CONTAINS effects=True` 是 `project_id="vclaweffectstest"` 命名误报）；本地无 ffmpeg 不能抽帧，微云 share 是网页需登录不能直接拉 mp4；MCP 无取回视频帧的工具。

=> 需人工点开 share_url 肉眼确认是否出现 360° 旋转/缩放/淡入。若未生效，说明下游 Remotion 模板未读 `metadata.effects`，需 /opt 侧改模板（对应 §10.4 遗留点）。
