# OM 概念科普：管道 / 流水线 / 模板 到底是什么

> **面向**：第一次接手 OpenMontage、或从 workbuddy 内置 Remotion 迁过来的同学
> **日期**：2026-09-10
> **相关**：[`AGENT_GUIDE.md`](../AGENT_GUIDE.md)、[`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md)、[`skills/INDEX.md`](../skills/INDEX.md)

---

## TL;DR

- **"管道"和"流水线"在 OM 里是同一个东西**（pipeline），没有区别，都指 `pipeline_defs/*.yaml`。
- **"模板"是个歧义词**，在 OM 里对应三个完全不同的东西，这是新人最大的困惑源。
- 真正需要区分的是三层：**管道**（做什么流程）/ **风格剧本**（长什么样）/ **阶段导演技能**（这步怎么做）。三者正交，可自由组合。

---

## §1 — 三层的分工

| | 管道 / 流水线 | 风格剧本（playbook） | 阶段导演技能 |
|---|---|---|---|
| 在哪 | `pipeline_defs/*.yaml` | `styles/*.yaml` | `skills/pipelines/<管道>/*-director.md` |
| 回答什么问题 | **做什么、按什么顺序、哪一步要人批** | **长什么样、什么调性、什么红线** | **这一步具体怎么做** |
| 数量 | 14 条生产管道 + 1 条字幕管道 | 6 个 | 每管道约 10 个 |
| 谁读 | agent 开工时选一条 | 各阶段 + `tools/video/video_compose.py` | agent 每进一个阶段读一次 |
| 换掉的代价 | 高，等于换整个工作流 | 低，改一行 `style:` | 不单独换 |

关系是**正交的**：管道是骨架，剧本是皮肤。

- 同一条 `cinematic` 管道，套 `anime-ghibli` 出吉卜力风，套 `shipinhao-commerce` 出视频号带货风；
- 反过来同一个 `shipinhao-commerce`，也能挂在 `cinematic` 或 `video-template-remix` 上。

两者的接口写在管道 yaml 里：

```yaml
# pipeline_defs/cinematic.yaml
compatible_playbooks:
  recommended: [flat-motion-graphics]
  also_works:  [clean-professional, minimalist-diagram]
  custom_allowed: true          # ← 自定义剧本走这条口子进来
```

---

## §2 — "模板"这个词的三个所指 ⚠️

中文一个"模板"，在 OM 里对应三样毫不相干的东西。说这个词之前先确认指的是哪个：

### 2.1 风格剧本 playbook（最常见的所指）

`styles/*.yaml`，由 `styles/playbook_loader.py` 加载，`schemas/styles/playbook.schema.json` 强校验。

定义配色、字体、节奏、音频、出图提示词前缀、质量红线。**跨项目复用，一次定标，后续所有同类片子自动继承。**

日常说"做一个模板"，九成指的是这个。

### 2.2 `video-template-remix` 管道（重名陷阱）

名字里有 template，但它是**一条管道**，含义是"拆解一条参考视频的结构再复刻"。输入是**别人的成片**。

跟 2.1 完全无关，只是撞名。它还是 OM 的**默认管道**，所以特别容易被误触发——如果你手上没有参考视频，多半不该走它。

### 2.3 Remotion 成片模板 / 自定义 TSX（代码层）

`remotion-composer/` 里的场景类型，以及 `create_remotion_video_share` 的 script 模式。管"这一帧的 DOM 怎么排"。属于渲染实现层，不是创意层。

### 2.4 附带一个近邻概念：`composition_mode`

跟"模板"贴得很近但独立：

- `templated` — 拼装现成 `cut.type` 场景，快，但长得跟别人一样
- `atelier` — 从零手作构图，贵，但是 hero 级别的活儿该走的路

它跟 playbook 也是正交的：**playbook 定调性，composition_mode 定"是拼装还是手作"。**

---

## §3 — 实际使用顺序

```
选管道（做什么流程）
  → 选 playbook（长什么样）              ← 模板在这里生效
  → 选 render_runtime + composition_mode（提案阶段锁定，之后不许换）
  → 逐阶段读 director 技能执行
  → 卡点处等人批（human_approved=True）
```

三个"锁定"要记住：

1. `render_runtime` 在提案阶段锁定，**不许中途静默切换**（Remotion ↔ HyperFrames ↔ FFmpeg）。两个都可用时必须同时呈现给人选。
2. `composition_mode` 同样是提案阶段的独立决策，要写进 `decision_log`。
3. 带 gate 的阶段必须 `human_approved=True`，`lib/checkpoint.py` 会拦。

---

## §4 — 参考组合：视频号带货短视频

```yaml
pipeline:         cinematic
style:            shipinhao-commerce      # 走 custom_allowed
composition_mode: atelier
render_runtime:   ffmpeg
```

`styles/shipinhao-commerce.yaml` 里额外扩展了一个 `delivery` 段（root 层 schema 允许扩展），把投放规格固化下来。关键是**安全区**——视频号带货比普通竖屏又紧一档：

| 方向 | 预留 px @1080×1920 | 原因 |
|---|---|---|
| 底部 | **470** | 账号名+文案+展开 ~330 **+ 商品卡 ~140** |
| 右侧 | 180 | 点赞/评论/转发/关注竖排 |
| 顶部 | 180 | 状态栏 + 顶部 tab |
| 左侧 | 48 | |

由此推导 `text_zone: x 48–900, y 180–1450`，**字幕基线锁死 y=1380**。模板同时保留 `safe_area_no_card`（底部 350），不挂链时切一个字段即可。

其余固化项：1080×1920 / H.264 high / yuv420p / 7M / AAC 128k / **-16 LUFS**；封面单出（九宫格按 1:1 中心裁切，主体必须落在中心 1080×1080 内）；CTA 摆 y 1150–1400 且必须指向商品卡；`i2v_rule` 要求 **I2V 首帧图必须先按 safe_area 构好图再送生成**（生成完就改不了构图了）。

---

## §5 — OM 和 workbuddy 内置 Remotion 的关系

先说清楚一件事：**Remotion 本身没有问题，它是个很强的渲染器。**"功能偏弱"的体感来自 workbuddy 的**生态面**——那里只挂了 Remotion 这一条腿，能做的事就被这一条腿的形状限定死了。

### 单一渲染器意味着什么

Remotion 的能力边界是"给定一份 React 描述，把它渲成帧"。它做这件事做得很好。但当它是**唯一**的出口时，所有需求都得先被翻译成 React 组件才能落地，于是：

- **不适合 React 表达的活儿被迫绕路**。长视频拼接、批量转码、色彩分级、响度归一，这些 FFmpeg 一行命令的事，在纯 Remotion 生态里要么写进组件里绕，要么就没有。
- **外部生成素材没有落脚点**。T2V / I2V 出来的 mp4、TTS 出来的 wav、检索来的 B-roll，Remotion 只能"播放"它们，不负责"决定要不要它们、从哪来、参数怎么给"。
- **一次渲染成本高**。改一个字幕位置要重跑整条 React 渲染链。

OM 里 Remotion 是 `render_runtime` 的**三个选项之一**，另两个是 HyperFrames 和 FFmpeg。这不是"替代 Remotion"，是**按镜头性质选工具**：图文动效走 Remotion，素材拼接与后期走 FFmpeg，HTML 合成走 HyperFrames。你昨天那个视频号项目走的就是 FFmpeg，因为主体是静帧 Ken Burns + 外部 I2V 片段拼接——用 Remotion 反而是绕路。

### 生态面的另一半：渲染之外的层

除了多渲染器，OM 还补了 Remotion 天然不覆盖的问题：场景怎么拆、素材从哪来、T2V 还是 I2V、配音 BGM 字幕对齐、投放安全区、哪步等人批。这些分别由管道、导演技能、工具层、风格剧本回答。

### 所以怎么选

| 需求 | 用什么 |
|---|---|
| 已有确定的 TSX，只要渲帧 | 直接 Remotion / `create_remotion_video_share` script 模式 |
| 单一镜头的图文动效 | Remotion 就够，套 OM 是过度工程 |
| 需要多种渲染手段混用（动效 + 拼接 + 调色 + 外部生成素材） | OM 管道，按镜头选 runtime |
| 有 brief 或参考视频，要走研究→脚本→素材→剪辑→渲染 | OM 管道 |
| 同一类片子反复批量出，需要统一调性与红线 | OM 管道 + 自定义 playbook |

---

## §6 — 常见误区速查

| 误区 | 纠正 |
|---|---|
| "管道和流水线不一样吧" | 一样，同一个 pipeline |
| "用 video-template-remix 做模板" | 那是"复刻参考视频"的管道，不是风格模板 |
| "换个风格得换管道" | 不用，改 `style:` 一行 |
| "playbook 里能定场景怎么排" | 不能，那是 composition_mode + 渲染层的事 |
| "渲染引擎中途换一个更快的" | 违反核心不变量，必须提案阶段锁定并记 `decision_log` |
| "直接写个 Python 脚本调工具更快" | 违反核心不变量 1，所有生产必须走管道 |

---

## 附：自定义 playbook 的最小流程

```bash
# 1. 照着现有的抄一份
cp styles/clean-professional.yaml styles/my-style.yaml

# 2. 改完校验（schema + 无障碍一起过）
python3 -c "
from styles.playbook_loader import load_playbook, validate_accessibility
p = load_playbook('my-style'); a = validate_accessibility(p)
print('OK', p['identity']['name'], '| errors:', a['error_count'], '| warnings:', a['warning_count'])
for i in a['issues']: print(' -', i['message'])
"
```

注意 `identity.category` 是枚举，只能取 `motion-graphics / whiteboard / cinematic / minimalist / retro / custom`；各子段大多 `additionalProperties: false`，**加自定义字段只能加在 root 层**（如 `delivery`）。
