# ffmpeg vs Remotion: 选择决策文档

> 写给后来者：在 video-template-remix 流水线（以及类似的「保真重制」流水线）中，什么场景该用 ffmpeg，什么场景必须切到 Remotion。

## TL;DR

**保真重制（preserve UGC source + 替换局部素材）→ ffmpeg**
**重写场景（talking head / chart / kinetic typography / avatar）→ Remotion**

具体到 bag-video-remix 这个项目：源是 B 站 UGC 翻包记，目标 = 保留 24 scene 的真实机位 + 替换物件图 + 加 filter-level polish。**全程 ffmpeg**，不需要 Remotion。

---

## 决策框架

| 任务类型 | ffmpeg | Remotion | 理由 |
|---|---|---|---|
| **保真重制（preserve source timeline）** | ✅ | ❌ | Remotion 会重写 scene，丢掉原机位 / 微晃 / 节奏 |
| **替换静态素材（图片 / 字幕 / logo）** | ✅ | ❌ | `overlay` filter 一行；React 组件是 over-engineered |
| **filter-level 操作**（Ken Burns / 调色 / delogo / ducking）| ✅ | ❌ | ffmpeg 一行 filter；Remotion 要写组件 + 内部还是走 ffmpeg |
| **TalkingHead / 数字人讲解** | � | ✅ | Remotion 有 `<TalkingHead>` 专用组件，ffmpeg 无法生成 |
| **word-by-word karaoke 字幕** | ❌ | ✅ | `remotion_caption_burn` 专用，Remotion-only parity |
| **数据图表（bar / line / pie + 入场动画）** | ❌ | ✅ | Remotion `remotion-composer/` 自带 8 个 chart 组件 |
| **Kinetic typography**（每行字幕带 motion）| ❌ | ✅ | React `interpolate()` 天然表达；ffmpeg drawtext 不便 |
| **HTML / UI-driven composition**（网页 → 视频）| ❌ | ✅ | Remotion 用 React DOM 渲染网页再 capture |
| **纯 trim / concat**（无任何重写）| ✅ | ❌ | ffmpeg 一行；Remotion 是 over-engineered |

## 决策树

```
任务起点
├── 是「保真重制」吗？ → source video 在 timeline 中保留
│   ├── 是 → ffmpeg
│   └── 否，继续 ↓
├── 涉及以下场景吗？
│   ├── talking head / avatar → Remotion
│   ├── word-by-word caption burn → Remotion
│   ├── 数据图表 + 入场动画 → Remotion
│   ├── kinetic typography → Remotion
│   ├── HTML/UI 截图 → Remotion
│   └── 都不是 → ffmpeg
└── 纯 concat/trim？ → ffmpeg
```

## 工程成本对比

以 bag-video-remix 这个 31 段 180s 视频为例：

| 操作 | ffmpeg 实现 | Remotion 实现 |
|---|---|---|
| 31 段 trim + overlay | 31 行 subprocess | 31 个 React 组件 + props 路由 |
| Ken Burns 5% zoom | 1 行 `zoompan=z='min(zoom+0.0008,1.05)'` | 每组件维护 `interpolate(frame, [0, duration*30], [1, 1.05])` |
| 调色（统一色温）| 1 行 `eq=brightness=-0.02:gamma=0.98` | `<ColorMatrix values={...}>` JSX + 矩阵数学转换 |
| BGM + ducking | 1 行 `sidechaincompress` + `amix` | 装 react-sidechain-compressor 包 / 自己实现 audio analyser |
| 字幕 mask | 1 行 `delogo=x=0:y=850:w=1920:h=140` | `<Sequence>` + `<AbsoluteFill>` + opacity state |
| **总代码量** | **~150 行 Python** | **~600+ 行 TSX** |
| **渲染时间** | **~3 min** | **~15-30 min**（含 webpack 启动 / React mount / 浏览器 headless）|

## Render runtime 切换的代价

如果中途决定从 ffmpeg 切到 Remotion，要改的：
1. `edit_decisions.json` 的 `render_runtime` 从 `"ffmpeg"` 改为 `"remotion"`
2. 写 `bespoke.entry` / `composition_id` / `art_direction` 三个字段
3. **重写所有 31 段**为 React 组件（不只是 polish — 整个 scene_plan 都要翻译）
4. 重新跑 compose + publish stages
5. **render_runtime_swap_detected = true** 会被 final_review 标记

所以选型要在 idea / scene_plan 阶段就决定，不能事后切换。

## 双 runtime 时代的协作模式

当源是 UGC 实拍 + 目标是「视觉重制」（如本项目），常见 pattern：
- **ffmpeg 做 heavy lifting**：源视频保留 + 静态素材替换 + filter polish
- **Remotion 做 overlay**（如果需要）：在 ffmpeg 输出之上再渲一层 kinetic typography / data viz / avatar

但这只在确实需要 Remotion 的 feature 时才有意义；本项目不需要，所以全部 ffmpeg。

## 何时必须放弃 ffmpeg

如果用户后续要求：
- "字幕要 karaoke 效果，每字带 highlight + 时间轴"
- "要加数字人 avatar 在右下角讲解"
- "每个 KPI 数字要有 count-up 动画"

这些是 Remotion-only features，ffmpeg 实现成本极高或不可行。这时切 runtime。

## 不在范围内的对比

- **HyperFrames**（OpenMontage 第三个 runtime）— HTML/GSAP motion graphics。本项目不适用。
  - HyperFrames 适合：kinetic type、product reveal、website-to-video
  - 不适合：UGC 保真重制（与 ffmpeg 同劣势）

## 相关决策日志

`projects/bag-video-remix-2026-08-31/artifacts/decision_log.json` 中：
- `d-002`: render_runtime_selection → ffmpeg (score 1.0 vs remotion 0.4 vs hyperframes 0.2)
- `d-010`: composition_mode → segment_based (chained overlay 在 31 层时 600s 超时失败)

## 一句话总结

> 保真重制 = ffmpeg；重写场景 = Remotion。**两者不是替代关系，是分工关系**——混用时各取所长。本项目（UGC 翻包记保真重制）只需要 ffmpeg 的工具栈。
