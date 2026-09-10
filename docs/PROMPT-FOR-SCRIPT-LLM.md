# Prompt Template — Script Generation for OpenMontage Rendering

> **Purpose**: A script-LLM handoff template for OM's `cinematic` pipeline. The renderer is the agent executing that pipeline; it is not tied to a particular LLM vendor. This document describes one production profile, not OpenMontage's entire capability boundary.
>
> **Review status**: **Pending renderer-side verification.** Part 0, added 2026-09-10, records local code findings, proposed clarifications, and information the renderer maintainer must supply. Parts 1–5 and the Appendix retain the original draft for comparison; their conflicting rules and automatic-behavior claims are not a verified execution contract.
>
> **Original snapshot**: 2026-09-10. The original draft cites `docs/PIPELINES-AND-OFFLINE-CAPABILITIES.md` and `docs/RESOURCES.md`. During this review, `docs/RESOURCES.md` was absent from `C:/OpenMontage_voicebox`; availability on another render host remains to be checked. No paid generation, endpoint access, quota, or remote asset reachability was tested in this review.
>
> **Companion docs** (the script LLM should skim these for context, not re-read in full):
> - [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — what OM can and can't do
> - [`RESOURCES.md`](RESOURCES.md) — what assets already exist on disk
> - [`MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) — audio gotchas, loop recipes, license traps
> - [`INDEX.md`](INDEX.md) — docs/ folder map

---

## Part 0 — 能力边界补齐与渲染端复核请求

### 0.1 这份文档需要回答什么

目标：让一个**没有本机文件、工具和账号访问权限的脚本 LLM**，只凭交接材料就能判断：哪些镜头能写、需要用户提供什么、哪些参数可选、哪些效果不能承诺、输出怎样进入实际流水线。

请将所有描述区分为以下四类，避免把一次演示的配置当成系统上限：

| 类别 | 应如何描述 | 本文中的例子 |
|---|---|---|
| 实现约束 | 指明工具、模型、字段、条件和代码/schema 来源 | 某模型允许的生成时长、分辨率、参考素材组合 |
| 当前运行条件 | 指明渲染主机、检查时间、依赖、配置状态和最近成功证据 | 权限、额度、模型缓存、字体、BGM 文件、图片托管是否可用 |
| 本次创作选择 | 标明默认值、是否可修改，以及如何传入修改 | 明亮风格、竖屏、女声、5–7 镜头、指定 BGM |
| 未覆盖或未验证 | 明确写“未覆盖 / 未验证”，说明需要补充的输入或验证 | 商品跨镜头一致性、精确口型、参考视频、多音轨、其他流水线 |

`cinematic` 模板没有描述的能力，不等于整个 OM 不支持。其他流水线、Remotion、HyperFrames、角色动画、素材剪辑等，应分别查对应 manifest 和工具契约。本文件不承担全平台功能清单的职责。

### 0.2 本次已从本地文件确认的事实

以下仅表示**当前工作区的实现声明或文件状态**，不表示服务端已实测成功：

| 项目 | 已确认内容 | 依据 |
|---|---|---|
| 视频工具名称 | tool 为 `minimax_h3_video`；provider 为 `minimax_h3_kapon`，两者不可混用 | [视频工具](../tools/video/minimax_h3_video.py) 的 `name` / `provider` |
| 模型参数 | H3：整数 4–15 s，768P / 2K；H3-Max：整数 5–15 s，480P / 768P | 同文件的 `_MODEL_DURATION_RANGE`、`_MODEL_RESOLUTIONS`、`input_schema` |
| 素材模式 | 两模型均声明支持 T2V、I2V、首尾帧；`last_frame_image` 已存在于工具输入，但原模板未提供 | 同文件的输入定义与 `content[]` 构建 |
| 参考素材 | H3 输入 schema 声明最多 9 张参考图、3 段参考视频、3 段参考音频；H3-Max 拒绝这些输入；参考模式不能和首尾帧混用，参考音频不能单独使用 | 同文件的 `reference_*` 输入与 `_validate_inputs`；服务端限制仍待复核 |
| 提示词 | 工具 schema 声明最多 7000 个 Unicode 字符，并写明原生支持中文；原稿“避免非 ASCII”不能直接作为硬限制 | 同文件的 `prompt` 定义 |
| 状态检测 | 此视频工具的 `get_status()` 只检查 token 是否存在，未验证模型权限、余额或连通性 | 同文件的 `get_status()` |
| 标准脚本结构 | canonical script 必需字段为 `version`、`title`、`total_duration_seconds`、`sections`，且禁止未声明的顶层字段；原模板 `project_meta/audio/scenes/cta` 不能直接作为该 artifact | [script schema](../schemas/artifacts/script.schema.json) |
| 其他字段映射 | canonical scene 类型使用 `generated` 等枚举，不直接接受 `T2V/I2V`；Kokoro 工具参数叫 `voice`，模板叫 `voice_id` | [scene_plan schema](../schemas/artifacts/scene_plan.schema.json)、[Kokoro 工具](../tools/audio/kokoro_tts.py) |
| 调色 | `bright_clean` 是多个可选 profile 之一；`color_grade` 工具的输入默认值为 `cinematic_warm`，不能据此宣称整个 cinematic 流水线固定为 `moody_dark` | [调色工具](../tools/enhancement/color_grade.py) |
| 资源索引 | 当前工作区缺少被引用的 `docs/RESOURCES.md`；不能据原稿确认其中音乐和商品图已就绪 | 当前工作区文件检查；其他主机待复核 |

### 0.3 P0 — 使用模板前必须统一的交接规则

以下“建议”均为**待复核的协议修改**。本次仅补充文档，未新增解析器、schema、工具能力或自动重试逻辑。

#### B01 — 明确 JSON 接收入口及转换责任

当前问题：Part 1 称其为 strict schema，但提供的是含 `<int>`、注释和省略场景的结构示意。它也与实际 canonical script 不同。本次在 `lib/`、`tools/`、`scripts/`、`schemas/`、`tests/` 中未检索到处理 `project_meta`、`scene-final-cta`、`bgm_override` 的交接实现；这不排除另一主机或人工 agent 存在相应处理。

请渲染端补齐：

- 接收入口：文件、命令或工具名；由程序解析，还是由 renderer agent 阅读并转换。
- 外部交接格式的版本号与完整 schema：必需/可选字段、类型、枚举、默认值、`null` 和未知字段策略。
- 明确 `<5 MB` 是项目约定还是实际传输限制，采用十进制 MB 还是 MiB，计算范围是 JSON 文档还是完整请求体；按序列化后的 UTF-8 字节计数并计入 Base64 膨胀，不能用原图片文件大小代替。
- `project_meta/audio/scenes` 如何分别转成 `proposal_packet`、`script`、`scene_plan`、`asset_manifest`、`edit_decisions`；尤其说明旁白如何拆到 `sections[].text`，视频参数如何保留到素材生成阶段。
- 至少明确 `voice_id → voice`、`scene_id → id`、生成模式与 canonical scene 类型的映射。不要直接把外部 JSON 写成标准 `script` artifact。
- 参数校验发生在入口、BaseTool 包装层还是服务端。工具有 `input_schema` 不代表所有调用方式都执行完整 schema 校验。
- 若由 agent 转换，明确写成“agent 按映射生成并校验 artifacts”；只有提供实际入口与验证证据后，才写“renderer 自动解析”。

验收：给出无占位符、无注释、可解析的完整 JSON，以及它转换后通过仓库 schema 校验的结果。示例素材若尚未核验，标明仅用于结构校验。

#### B02 — 分开“成片剪辑时长”和“请求生成时长”

当前冲突：Part 1 同时规定每镜 4–8 s、生成 5–15 s；总时长既允许 ±1 s，又要求严格相等；5–7 镜头每镜最多 8 s，无法达到 60 s；schema 未包含要求中的 `duration` 字段。

建议统一为：

- `start_seconds/end_seconds` 表示成片时间轴，区间为 `[start, end)`。从 0 开始，按序连续，无重叠和空洞，最后一镜结束时间等于目标时长；场景 ID 唯一。
- 分别定义生成秒数和素材截取范围。可增加 `generation_duration_seconds` 与明确的 trim 字段，或由 renderer 按文档规则推导；字段名称及映射由渲染端定稿。生成秒数须满足具体模型的整数范围。
- 3 s 的剪辑镜头可以来自更长的合规生成片段；不能直接把 3 s 传给要求更长时长的模型。超出单次生成上限的镜头应拆分，或标记需要已验证的延长能力。
- 5–7 场景标为 30 s 示例的节奏建议。可用的时间轴示例：30 s = 6 × 5 s；60 s = 10 × 6 s。若坚持 5–7 场景，60 s 可采用 6 × 10 s，同时修改原 8 s 上限。最终选择必须同步到 prompt、校验与示例。
- 对硬切，场景时长求和应等于目标时长；有交叉淡化时，明确重叠如何计入时间轴。输出编码误差另按 FPS 规定容差，不与脚本规划的 ±1 s 混用。

验收：30 s、60 s 和“生成 5 s、剪用 3 s”三个案例均可解释；非法生成时长、时间轴重叠/空洞能在提交前报告。

#### B03 — 统一生成模式，补齐首尾帧和参考素材输入

建议对外逐项写清：

| 模式 | 脚本所需输入 | 需要渲染端确认的行为 |
|---|---|---|
| T2V | 文字 `prompt` + 具体 `ratio`；图像字段按 schema 统一省略或设为 `null` | 下发时不附图；取消 Part 4 为所有 T2V 自动生成首帧的描述，除非显式改为获准的 I2V 流程 |
| I2V | 文字 `prompt` + 一张有效 `first_frame_image` | 原句应修正为“只有图像字段须为 URL/data URI”，不能让读者理解为 prompt 也须为 URL |
| first+last frame | 文字 `prompt` + `first_frame_image` + `last_frame_image` | 补齐末帧字段、格式/尺寸约束、两图与输出比例的处理规则，以及服务端验证样例 |
| reference media | 当前模板未覆盖；工具端存在模型有条件支持的 `reference_images/videos/audios` | 明确本版是否开放；若开放，新增模式、字段、数量/组合限制和示例，不能仅因提到 H3 支持就让 LLM自行添加字段 |

还需说明：输入图像尺寸、文件体积、MIME、比例限制；T2V 可用比例列表；I2V 的 `adaptive` 与项目目标比例冲突时，是裁切、留边还是拒绝。工具输入还声明 `mm_file://`，应明确其是否刻意排除在外部交接格式之外。

验收：T2V 不发生意外图片生成/上传；首尾帧样例确实提交两张图；H3-Max 携带参考素材在付费提交前被拒绝。

#### B04 — CTA 只计入一次

当前冲突：严格形状和完整示例要求顶层 `cta`，文末却要求二选一；附录已有 23–30 s 的 CTA 镜头，又增加 27–30 s 的顶层 CTA，并承诺自动去重。

建议：下一版只在 `scenes` 中保留最终 CTA 镜头，删除顶层 `cta`。如需兼容旧格式，应指定转换规则、冲突时报错规则和去重实现位置；确认之前不要承诺自动去重。CTA 的品牌图、二维码及目标链接必须来自已登记素材，不能使用未定义的 `img-cta-qr-code`。

验收：CTA 不重复生成、不重复拼接，总时长不增加；二维码/Logo 的 overlay ID 可以解析到真实文件。

#### B05 — 提供可携带的素材清单与缺失素材流程

当前问题：没有 `RESOURCES.md` 就无法核对 BGM 和产品图；域名前缀与 `<name>.png` 不能证明文件存在；`/tmp/` 路径没有说明属于哪台主机。

请渲染端随 prompt 提供已核验的素材清单，至少包含：`asset_id`、类型、用途、所属主机、实际路径或可用 URL、文件哈希、尺寸/时长、来源、检查时间和使用条件。临时签名 URL 需标明有效期；不要把 token 或凭据放进 prompt。

同时明确：

- 脚本 LLM 只能引用清单中给定的素材，不得猜测 URL、编造 Base64 或假装验证过文件。
- 缺少商品图、末帧、Logo、二维码或音乐时，采用哪一种入口：在写脚本前补齐，或返回另行定义的缺失素材结果。当前成功 schema 没有此分支，应由渲染端补齐后再使用。
- 资源在脚本 LLM、本地 renderer、远程生成服务之间如何传递；本地文件限制只针对哪些 API 字段，而不是禁止 renderer 使用本地素材。
- URL 的格式检查、renderer 实际下载与生成服务端可达性是不同检查；明确分别由谁验证，不能把 renderer 下载成功当作服务端必然可达。
- 首帧生成/上传是有依赖和成本的制作阶段。缺少图片生成能力时，需要用户素材或获准的其他方案，不能从“视频模型可用”推断“首帧也能生成”。

验收：补回资源索引或更正引用；在目标渲染主机逐项解析素材；生成文件按 [项目目录约定](../AGENT_GUIDE.md) 放入 `projects/<project-id>/assets/`，成片放入 `renders/`。

#### B06 — 补一份带环境信息的能力快照

请由实际渲染主机提供以下记录；未知值明确写 `unknown`：

| 必填信息 | 复核内容 |
|---|---|
| 快照身份 | `checked_at`（含时区）、主机/服务标识、操作系统、仓库 revision、工具版本；本地与远程执行边界 |
| 视频路径 | tool、provider、模型白名单、分辨率/时长/模式；配置检查、权限检查、额度检查、最近成功调用分别记录 |
| 首帧与托管 | 图片工具、素材输入来源、托管方式、服务端取图验证；缺一项时的处理方式 |
| 音频 | 实际可用 TTS、voice、语言、模型缓存/依赖、BGM 文件；离线是否仍需首次下载 |
| 合成 | 实际可用的 FFmpeg / Remotion / HyperFrames、字体、输出规格；本项目选择的 runtime 与 composition mode |
| 成本与耗时 | 币种、计费单位、生成时长/剪用时长如何计费、图片/上传/重试成本、预算；串行或并行、并发数与观测时间 |
| 证据 | 脱敏日志或项目 artifact 路径、成功/失败时间；“代码支持”“已配置”“实测通过”分别记录 |

运行端可从 `registry.provider_menu_summary()` 开始检查，但还需核验最终选择的工具。原稿“只有这两个模型可用”“5–10 分钟完成”“H3-Max 更便宜/更可靠”应降为注明环境和条件的观测或待确认项。特别是本地成本表在 768P 下为两模型配置了相同单价，不能笼统写 H3-Max 总是更便宜；真实价格仍以实际账单/服务配置为准。

### 0.4 P1 — 写脚本所需的效果边界与验收规则

#### B07 — 商品、人物与画面控制能承诺到什么程度

请补一张“支持方式 / 所需输入 / 可保证项 / 不保证项 / 失败处理”表，覆盖：商品外观及颜色一致性、人物身份跨镜头保持、指定动作、手部操作、首尾帧过渡、精确运镜、品牌文字与 Logo、口型同步。

建议写法：商品镜头引用已确认商品素材；提示词用于表达期望，不能仅凭字段被接受就承诺结果严格一致。品牌文字、二维码等如由后期覆盖，说明具体输入和位置控制。原稿“产品在至少 60% 帧中出现”应作为质量目标，并说明如何抽检；它不是已证明的模型硬保证。复杂镜头无法满足时，由 renderer 报告具体偏差，不擅自替换产品或省略要求。

#### B08 — 旁白、字幕、BGM 的时间关系

- 明确每场 `subtitle_text` 是营销短句还是完整旁白字幕；若是前者，不能承诺与旁白逐字同步。
- 完整旁白字符串加场景起止时间，不会自动给出每句语音的准确边界。请说明使用分段合成、对齐工具或仅作整条旁白铺底；并补充台词与场景的对应字段/规则。
- 150 字与每镜 12 字属于本 profile 的文案约束。统一字符计数方法，说明标点、空格、英文、数字怎样计算；合成后仍以实际音频时长检查能否放入时间轴。
- 说明旁白过长/过短、品牌读音错误、无旁白、换声音及中英混读的处理；语速变化范围和是否允许截断需明确。
- 统一“无需指定音乐”与必填 `bgm_track` 的矛盾；确认 `bgm_override` 是否受支持及其含义。缺少音乐时的处理必须写明。
- 明确模型生成音轨是否保留，避免与配音/BGM 重复；补充响度、真峰值、ducking、淡入淡出和循环接缝的验收标准。
- 音乐使用条件记录应包含来源页面、许可名称和核验日期；原稿 CC0/CC0-equivalent 的说法待来源复核，不应仅据“已下载”确认许可。

#### B09 — 画布、叠层与交付规格

生成模型的 `768P/2K` 与最终成片画布分别定义。请补充最终宽高、FPS、容器/编码、像素格式、音频采样率、目标平台安全区和文件命名。

字幕坐标 `(114, 1180, 540×120)` 必须注明基准画布；不能在 9:16、16:9、1:1 下原样复用。Logo/CTA overlay 应说明资源 ID、位置单位、尺寸、出现时段和动画是否受支持。调色默认值应服从本项目风格选择，不能固定覆盖所有创作方向；如承诺亮度区间，补测量方法与样例依据。

#### B10 — 错误返回、重试和流水线交接

请区分两类失败并说明返回格式：

- **脚本问题**：JSON/schema 错误、时间轴冲突、缺失素材、非法参数；返回字段路径、scene ID、原因与修改方向。
- **运行问题**：权限/额度、网络/取图、排队/超时、下载失败、画面质量问题；由 renderer 报告和处理，不应要求脚本 LLM 重写台词来解决权限或网络问题。

原稿将 `2013` 写成 HTTP 状态，应核对它是响应体业务码还是 HTTP 状态；空 `content.url` 与下载视频后的黑帧也应分开。请提供实际错误样例、轮询时限、可重试条件/次数、任务 ID 保存与超时后的查询策略，避免重复提交产生重复费用。补充补帧允许的最大长度；补帧、自动重试和去重均应标明实现位置，未实现就写“由 agent 按规则处理”或“待实现”。

Part 4 还需补回外部脚本进入 [cinematic manifest](../pipeline_defs/cinematic.yaml) 的阶段映射：proposal、script、scene_plan、assets 的审阅及 checkpoint，edit/compose 的标准 artifacts、runtime 决策和 final review。已有授权按项目规则记录，不能仅凭粘贴 JSON 就推断所有阶段均获批。不要将 preflight 检查与付费素材生成混为同一步。

### 0.5 请渲染端如何反馈

请逐项填写 B01–B10，使用同一格式：**结论（接受 / 修正 / 不支持 / 待验证）→ 最终规则 → 代码、schema 或脱敏实测证据 → 对 Part 1/示例的修改**。本地实现与远程主机不同时，注明环境和版本差异。

建议复核完成后交付：

1. 一段无需依赖缺失文件的可复制 prompt，包含实际能力快照和核验过的素材清单。
2. 一份确定版本的交接 schema/agent 转换规则，以及完整 30 s、60 s 样例；CTA、时长、素材字段保持一致。
3. 无付费调用的结构/时间轴检查结果；若引用渲染成功作为证据，提供已有运行的主机、时间与 artifacts，不将本次文档复核视为付费试跑授权。
4. 明确列出的剩余限制；将通过复核的规则同步回 Parts 1–5 和 Appendix，删除矛盾旧描述，再将本文件状态改为 verified。

在上述复核完成前，可依据原稿讨论创意，但不要向脚本 LLM 宣称“这份文件已完整描述 OM 全部能力”或“照填 JSON 必定自动渲染成功”。

---

## Part 1 — The Prompt to Send to the Script LLM

**Original draft — reconcile Part 0 before production use.** After renderer-side review, copy the revised block into the script LLM and replace the bracketed fields `[LIKE_THIS]` with your project specifics. The block below is retained as the review baseline, not a validated schema.

---

```
You are writing a video script for OpenMontage, an AI video production
platform. The script you produce will be handed to a renderer that will
execute it via OM's `cinematic` pipeline.

Your output must be a single JSON document — no prose explanation, no
markdown formatting around it, no code fences. Just the JSON. The
renderer will not parse anything except valid JSON.

## Product / brief

- Product: [PRODUCT_NAME, e.g. "小鹏哥 潮牌旅行箱"]
- Brand: [BRAND_NAME, e.g. "小鹏哥 (Xiaopeng Ge)"]
- Target audience: [e.g. "Gen-Z urban professionals, 22-32, TikTok-first"]
- Tone / style: [e.g. "TikTok vertical, fast-paced, EU/US aesthetic but
  with Chinese subtitles. Bright, optimistic, not dark or moody."]
- Target video duration: [TOTAL_SECONDS, e.g. 30 or 60]
- Aspect ratio: [9:16 / 16:9 / 1:1, default 9:16 for TikTok]
- Language: [e.g. "Chinese narration (Simplified) + English on-screen text OK"]
- Must-include features: [LIST 3-5 product features to highlight]

## Hard constraints (the renderer will reject the script if violated)

- 5–7 scenes total. Each scene: 4–8 seconds. Sum must equal target
  duration ±1 s.
- Every scene MUST declare `type` ∈ {"T2V","I2V","first+last frame"}.
- T2V scenes (no image): `prompt` only, no `first_frame_image`.
- I2V scenes: `prompt` AND `first_frame_image` MUST be a public HTTPS URL
  OR a data:image/...;base64,... URI. Local paths will be rejected.
- `model` MUST be "MiniMax-H3" or "MiniMax-H3-Max" (H3-Max = faster +
  cheaper; H3 = better quality + supports multi-image reference).
- `duration` MUST be 5–15 s per scene (H3-Max hard floor is 5 s; H3 is 4 s).
- `resolution` MUST match model: H3-Max = "480P" or "768P"; H3 = "768P"
  or "2K". Other values will be rejected.
- `ratio` MUST be set per scene. For T2V: a concrete ratio (NOT "adaptive")
  like "9:16". For I2V: "adaptive" or a concrete ratio.
- Subtitles: short (≤12 chars per scene). Match the visual beat.
- Voiceover text: full narration string for the entire video, ≤150
  Chinese characters total.
- Total output JSON size: < 5 MB. (Large data URIs count.)

## Visual style guide (apply to every scene's `prompt`)

- Cinematic but TikTok-fast — camera movement in every shot (dolly-in,
  handheld follow, push-in, parallax). No static frames.
- Lighting: bright, optimistic, daylight or warm interior. NO neon
  night / moody-dark / silhouette / "black with neon spillover".
  The renderer will adjust color grade to "bright_clean" anyway, but
  prompts should not fight it.
- Composition: rule of thirds, shallow depth of field, the product
  featured prominently in at least 60% of frames.
- People: if any, 25-32 year-old, casual style, diverse — but
  characters are optional and most scenes work fine product-only.
- Style keywords to include in prompts: "cinematic", "high energy",
  "commercial", "modern", "sharp detail". Avoid: "dark", "moody",
  "cinematic dark", "low key".

## Background music (do NOT specify in the script — pre-assigned)

- The renderer will use `/tmp/pixabay_happy.mp3` (124.6 s, CC0,
  "Optimistic Pleasant Light Music" by alex-morgan, RMS 7362) as the
  primary BGM. Bright, optimistic, no lyrics. The script does NOT
  need to specify music — it's auto-selected from RESOURCES.md.
- If you need a specific mood that's clearly incompatible with happy
  optimistic (e.g. a horror reveal), add a `bgm_override: true` field
  in the top-level `audio` block and the renderer will re-evaluate.

## Voiceover

- Voice: female Mandarin, `zf_xiaobei` (Kokoro-82M, 24 kHz).
- One continuous narration string for the whole video, ≤150 chars.
- Per-scene timestamps are derived from `start_seconds`/`end_seconds`,
  do NOT include timing markers in the text.

## Subtitles

- Burned into the video via ffmpeg drawtext (Noto Sans CJK SC Bold).
- One subtitle line per scene, ≤12 Chinese characters, in scene's
  `subtitle_text` field.
- The renderer positions subtitles in lower-third (114, 1180, 540×120)
  by default; trust that.

## Existing assets you can reference

- First-frame product stills at: `https://ocbot.aixifs.com/videopic/<name>.png`
  — confirm any URL you reference is actually uploaded there. If you
  reference a URL that doesn't exist, the renderer will surface the 2013
  error and the run will fail.
- Background music: `/tmp/pixabay_happy.mp3` (described above).
- Voice: Kokoro-82M / `zf_xiaobei`.

## Output schema (strict)

Output EXACTLY this shape:

{
  "project_meta": {
    "project_id": "kebab-case-id",
    "title": "<short title>",
    "target_duration_seconds": <int>,
    "ratio": "9:16",
    "category": "product-acquisition",
    "color_grade": "bright_clean",
    "language": "zh",
    "audience": "<one line>",
    "tone": "<one line>"
  },
  "audio": {
    "bgm_track": "/tmp/pixabay_happy.mp3",
    "voice": {
      "enabled": true,
      "voice_id": "zf_xiaobei",
      "provider": "kokoro_tts",
      "text": "<full narration ≤150 chars>"
    }
  },
  "scenes": [
    {
      "scene_id": "scene-1-hook",
      "type": "T2V",
      "start_seconds": 0,
      "end_seconds": 5,
      "prompt": "<English cinematic prompt, 1-2 sentences, no text/logo in frame>",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "≤12 Chinese chars",
      "overlay": null
    },
    {
      "scene_id": "scene-2-product",
      "type": "I2V",
      "start_seconds": 5,
      "end_seconds": 10,
      "prompt": "<...>",
      "first_frame_image": "https://...png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "...",
      "overlay": null
    }
    // ... 3-5 more scenes, sum durations = target_duration_seconds
  ],
  "cta": {
    "scene_id": "scene-final-cta",
    "type": "T2V",
    "start_seconds": <total - 3>,
    "end_seconds": <total>,
    "prompt": "<CTA-focused prompt, brand-relevant>",
    "first_frame_image": null,
    "model": "MiniMax-H3-Max",
    "resolution": "768P",
    "ratio": "9:16",
    "subtitle_text": "立即购买 / 戳链接",
    "overlay": "img-cta-qr-code"
  }
}

Validation rules the renderer will check:
- scene.end_seconds - scene.start_seconds ∈ [4, 8] (per scene)
- sum(scenes[].end_seconds - scenes[].start_seconds) == target_duration_seconds
- every first_frame_image is either null, https://..., or data:image/...;base64,...
- every model ∈ {"MiniMax-H3", "MiniMax-H3-Max"}
- every resolution matches model constraint (H3-Max: 480P|768P; H3: 768P|2K)
- voice.text ≤ 150 Chinese chars
- subtitle_text ≤ 12 Chinese chars per scene
- output JSON < 5 MB

Begin. Output the JSON only.
```

---

## Part 2 — Walkthrough of Why Each Constraint Exists

For the human user (you), not the script LLM. Useful when reviewing the script LLM's output to catch issues before handing to the renderer.

### Why `5–8` seconds per scene?

- H3 / H3-Max video generation has a **hard floor of 5 s** (H3-Max) / 4 s (H3). A 3-s scene would 4xx.
- 5 s is the minimum billable unit for kapon's paygo pricing — 4-s requests get rounded up.
- 8 s is the maximum where you can fit 5–7 scenes in 30 s without rushing the visual story.
- If the target is 60 s, scenes can stretch to 10–12 s (but be careful: long shots are harder to QA for visual drift).

### Why MiniMax-H3 / H3-Max only?

- All other online video providers on this host are **either unavailable (no API key) or exhausted** (Hailuo Token Plan 4-shot 2067 cap).
- These two models are routed through the **kapon OneHub proxy** with a Bearer token (`KAPON_API_TOKEN` in `.env`).
- See [`MUSIC-ISSUES-AND-FIXES.md`](../docs/MUSIC-ISSUES-AND-FIXES.md) for the I2V URL requirement (must be public HTTPS — kapon datacenter IPs get blocked by imgbb etc.).

### Why `bright_clean` color grade?

- Today's `cinematic` pipeline defaults to `moody_dark` (avg luminance 18–52 / 255) — unsuitable for office demo.
- `bright_clean` lifts shadow toe and midtones (avg luminance target 80–110 / 255). The ffmpeg `curves` filter does this after rendering.
- **Prompt-level reinforcement**: do NOT use "dark"/"moody"/"low key" in prompts. The prompt and the color grade fight each other.

### Why `Kokoro / zf_xiaobei` voiceover?

- Kokoro-82M is the only **offline-capable high-quality Mandarin TTS** on this host.
- `zf_xiaobei` is the default Mandarin female voice (warm, clear, professional).
- See [`MUSIC-ISSUES-AND-FIXES.md`](../docs/MUSIC-ISSUES-AND-FIXES.md) §1 for the 40-second warm-up caveat — the renderer batches all voice into one Kokoro call to amortize.

### Why `pixabay_happy.mp3` as default BGM?

- CC0-equivalent (commercial use OK, no attribution required).
- Bright, optimistic, low-key, RMS energy 7362 — fits an OM capability demo without being saccharine.
- Already downloaded; renderer doesn't need to re-fetch.
- See [`RESOURCES.md`](../docs/RESOURCES.md) §3 for the comparison table and license verification.

### Why `<5 MB` JSON output cap?

- The MCP Streamable-HTTP transport has practical body-size limits around 5–10 MB on this host. Larger payloads risk timeout or 413.
- Data URIs count toward this. If you need a 4-MB first-frame, you have ~1 MB of headroom for the rest of the script. If you need bigger, **host the image on `ocbot.aixifs.com/videopic/` first** and reference by URL.

---

## Part 3 — Failure Modes the Script LLM Should Pre-Avoid

The script LLM, reading only the prompt block, may not anticipate these. If the renderer fails, it's most likely one of these:

| Failure | Cause | Fix |
|---|---|---|
| `kapon HTTP 403 model_not_allowed` | The KAPON_API_TOKEN doesn't have H3 / H3-Max permission. | Re-check the kapon console. If only H3 is allowed, switch all scenes to H3. |
| `kapon HTTP 400 illegal base64 at input byte 4` | The script passed a data URI but Base64 was malformed (Bash `base64 -w0` is fine; Python `base64.urlsafe_b64encode` is NOT). | Use Python `base64.b64encode` (standard alphabet). |
| `kapon HTTP 2013 media url unreachable` | The `first_frame_image` URL is on imgbb / another anti-bot-blocked CDN. kapon's datacenter IP gets 403 from imgbb. | Host the image on `https://ocbot.aixifs.com/videopic/` and reference that URL. |
| Scene rendering stalls / times out at 600 s | The kapon generation queue is congested (peaks during EU business hours). | Renderer retries once; if still failing, escalate or queue for off-peak. |
| Generated video is shorter than requested | Some providers under-run by 0.1–0.5 s on the trailing frame. | Renderer pads with a hold-last-frame freeze to match the planned duration. |
| Generated video has a black final frame | Some models return Success with an empty `content.url`. | Renderer retries; if 2nd attempt also empty, the script LLM is asked to regenerate the scene. |

The script LLM should design around these: use H3-Max over H3 (more reliable), use HTTPS URLs over data URIs when possible, keep prompts under 7000 chars (H3 hard limit), avoid non-ASCII in `prompt` (Kokoro handles CJK fine, but kapon's tokenizer sometimes drops diacritics).

---

## Part 4 — End-to-End Render Pipeline (After Script LLM Produces JSON)

Original proposed flow; automatic behavior and its mapping to OM's actual pipeline remain subject to B01–B10 verification:

1. **Validate JSON** against the schema in Part 1. Reject and surface errors.
2. **Pre-flight**:
   - Generate first-frame PNGs (if any scene is T2V without first_frame_image, generate via `minimax_image`).
   - Upload all first-frame PNGs to `https://ocbot.aixifs.com/videopic/`.
   - Render BGM at target duration via ffmpeg loop + fade (recipe in [`RESOURCES.md`](../docs/RESOURCES.md) §5).
   - Synthesize Kokoro voiceover from `audio.voice.text` (single call).
3. **Per-scene generation**:
   - Submit to kapon v2 endpoint with `model`, `duration`, `resolution`, `ratio`, `content[]`.
   - Poll until Success/Fail (typical 60–120 s for 5–8 s clip).
   - Download signed URL → save as `renders/scene_N.mp4`.
4. **Per-scene post**:
   - Scale to target resolution if needed.
   - Burn `subtitle_text` via ffmpeg `drawtext` (Noto Sans CJK SC).
   - Apply overlay (CTA / logo) via ffmpeg `overlay`.
5. **Compose**:
   - Concat all scenes via ffmpeg concat demuxer.
   - Mux voiceover + BGM via `amix` (BGM duck under voice, `sidechaincompress`).
   - Apply color grade `bright_clean` via ffmpeg `curves + eq`.
   - Output `final_30s_v1.mp4`.
6. **QA**: spot-check 5 frames, audio loudness, duration. Run `final_review.json`-equivalent checks.
7. **Deliver**: report `final_<duration>_v1.mp4` path; if QA fails, surface specific issue and request re-render of the offending scene.

The whole pipeline typically runs in **5–10 minutes** for a 30 s video, mostly dominated by kapon's per-scene generation latency.

---

## Part 5 — Quick-Start Workflow

1. Resolve Part 0 with the renderer maintainer and update Part 1; then copy the reviewed prompt and replace the `[LIKE_THIS]` brackets.
2. Send to your preferred script LLM.
3. Paste the JSON output back to the renderer.
4. Renderer validates, generates, composes — produces the final mp4.
5. Spot-check the result; iterate on `prompt` per scene if visuals drift.

If the script LLM has questions about OM capabilities, point it at [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md). If it has questions about available assets, point it at [`RESOURCES.md`](RESOURCES.md).

---

## Appendix — Real Worked Example (Xiaopeng Ge Travel Luggage TikTok)

For reference, here is the original **skeleton** the script LLM might produce. **Review fixture only:** it contains the duplicate-CTA issue in B04, and its image URLs/overlay IDs are unverified. It must be updated after review; JSON syntax alone does not establish schema or render validity.

```json
{
  "project_meta": {
    "project_id": "xiaopeng-ge-tiktok-luggage-30s",
    "title": "小鹏哥 · 城市旅行箱 30s",
    "target_duration_seconds": 30,
    "ratio": "9:16",
    "category": "product-acquisition",
    "color_grade": "bright_clean",
    "language": "zh",
    "audience": "Gen-Z urban professionals, 22-32, TikTok-first",
    "tone": "Fast-paced, bright, optimistic, EU/US aesthetic with Chinese subtitles"
  },
  "audio": {
    "bgm_track": "/tmp/pixabay_happy.mp3",
    "voice": {
      "enabled": true,
      "voice_id": "zf_xiaobei",
      "provider": "kokoro_tts",
      "text": "小鹏哥潮牌旅行箱，德国拜耳PC材质，TSA海关锁，一拉就走。今天的旅程，明天就到了。"
    }
  },
  "scenes": [
    {
      "scene_id": "scene-1-hook",
      "type": "T2V",
      "start_seconds": 0,
      "end_seconds": 5,
      "prompt": "Cinematic tracking shot following a young traveler wheeling a sleek hardshell suitcase through a sunlit European cobblestone street, slow dolly-in, shallow depth of field, sharp modern commercial, no text no logo no person face",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "一拉就走",
      "overlay": null
    },
    {
      "scene_id": "scene-2-detail",
      "type": "I2V",
      "start_seconds": 5,
      "end_seconds": 11,
      "prompt": "Close-up of hardshell suitcase surface with reflective texture, daylight, slow push-in, shallow DOF, sharp detail",
      "first_frame_image": "https://ocbot.aixifs.com/videopic/xiaopeng-ge-detail.png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "拜耳PC材质",
      "overlay": null
    },
    {
      "scene_id": "scene-3-feature",
      "type": "I2V",
      "start_seconds": 11,
      "end_seconds": 17,
      "prompt": "Hand operating the TSA lock on a hardshell suitcase, daylight interior, fast cuts feel, sharp detail",
      "first_frame_image": "https://ocbot.aixifs.com/videopic/xiaopeng-ge-tsa-lock.png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "TSA海关锁",
      "overlay": null
    },
    {
      "scene_id": "scene-4-lifestyle",
      "type": "T2V",
      "start_seconds": 17,
      "end_seconds": 23,
      "prompt": "Young traveler with suitcase in a sunny airport terminal, cinematic dolly-in, bright optimistic commercial mood, no text no logo",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "今天的旅程",
      "overlay": null
    },
    {
      "scene_id": "scene-5-cta",
      "type": "T2V",
      "start_seconds": 23,
      "end_seconds": 30,
      "prompt": "Hardshell suitcase centered on bright minimal background with subtle motion blur of wheels rolling forward, commercial product shot, bright daylight, no text no logo",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "戳链接立即购买",
      "overlay": "img-cta-qr-code"
    }
  ],
  "cta": {
    "scene_id": "scene-final-cta",
    "type": "T2V",
    "start_seconds": 27,
    "end_seconds": 30,
    "prompt": "Same as scene-5",
    "first_frame_image": null,
    "model": "MiniMax-H3-Max",
    "resolution": "768P",
    "ratio": "9:16",
    "subtitle_text": "戳链接",
    "overlay": "img-cta-qr-code"
  }
}
```

Note: in this example the `cta` block is redundant with the last scene — the renderer will dedup. The script LLM should include either the last scene or the `cta` block, not both. (This is a known quirk in the schema; v2 will remove `cta` as a top-level field.)
