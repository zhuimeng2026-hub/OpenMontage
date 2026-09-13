# Remotion 模板 Token 扩展 —— effects 真正驱动渲染的设计方案

- 日期：2026-08-31
- 状态：**方案，待排期** —— 写入 docs 等下次有空闲时实现
- 上游依赖：commit `2a12448`（keyword 解析）+ `0dc12e2`（`lib.effects_parser` 抽出 + `video_compose._render` 调用）+ `168b2b9`（vclaw preview 透传）
- 模板目录：`/opt/OpenMontage_Voicebox/remotion-composer/src/`（live MCP 从这里跑）

---

## 1. 现状（commit 0dc12e2 之后）

`lib.effects_parser.EFFECTS_KEYWORD_TO_ANIMATION` 解析自然语言 → 写入
`cuts[i].transform.animation` / `scene_plan[i].shot_language.camera_movement`，
下游是 `remotion-composer/src/Explainer.tsx::EnhancedVideoScene::anim` 的 switch。

目前 `anim` switch 只认 6 个 token：

```ts
// Explainer.tsx:377-396（当前实现）
if (anim === "zoom-in")    { scale = 1 + progress * 0.18; }
else if (anim === "zoom-out")    { scale = 1.18 - progress * 0.18; }
else if (anim === "pan-left")    { translateX = -40..+40; scale = 1.15; }
else if (anim === "pan-right")   { translateX = +40..-40; scale = 1.15; }
else if (anim === "ken-burns" || anim === "ken-burns-slow-zoom") {
  scale = 1 + progress * 0.22;
  translateX = 0..-25; translateY = 0..-15;
}
else if (anim === "parallax")    { translateY = -15..+15; scale = 1.1; }
// "static" / "none" → just display
```

未识别的 animation 走兜底（`apply_effects_to_edit_decisions` 解析失败时落 `zoom-in`），
所以用户写"旋转切入 / 粒子汇聚 / Smoothstep 转场"这些今天都被静默地渲染成 zoom-in。
**这是隐藏 bug**：用户在 Studio「视频效果」面板写的精细描述被吃掉、预览和文字不一致。

---

## 2. 用户实际在写什么

`openclaw/clawx-studio/src/App.vue:1522` 的 textarea 占位符（用户大概率按这个格式写）：

> 开篇使用旋转切入 0.5s（rotate 0→15deg，opacity 0→1，spring）；
> 中段使用 Ken Burns 慢推（scale 1→1.08，3s ease-out）；
> 结尾使用粒子汇聚淡出（200 粒子，4s）；
> 转场用 Smoothstep 0.6s。可写多段。

可识别诉求（按出现频度）：

| 用户中文表述 | 期望 token | 当前是否被吃 |
|---|---|---|
| 旋转切入 / 旋转 / rotate | `rotate-in` | ❌ 兜底 zoom-in |
| Ken Burns 慢推 / 电影感 / 漂移 | `ken-burns` | ✅ |
| 粒子汇聚 / 粒子淡出 / particle | `particle-out` | ❌ |
| 推近 / 放大 | `zoom-in` | ✅ |
| 拉远 / 缩小 | `zoom-out` | ✅ |
| 左摇 / 右摇 | `pan-left` / `pan-right` | ✅ |
| Smoothstep 转场 | `smoothstep` | ❌（仅 fade / cut） |
| spring 缓动 | （easing修饰符，非独立token） | ❌ |
| ease-out | （easing修饰符） | ❌ |

---

## 3. 提议：扩到 10 个 token + easing 修饰符

### 3.1 新增的 4 个相机/视觉 token

| Token | 触发关键词（zh + en） | 渲染语义 |
|---|---|---|
| `rotate-in` | rotate, 旋转, 旋转切入, 转, rotate in | 开篇旋转 0°→N° + opacity 0→1（spring 默认） |
| `rotate-out` | rotate out, 旋转退场, 旋转淡出 | 收尾 N°→0° + opacity 1→0 |
| `particle-out` | particle, 粒子, 粒子汇聚, 粒子淡出 | 复用 `<ParticleOverlay />`，cut 末尾扩散淡出 |
| `static` | static, 静止, 定格, freeze | 已有等价（fall-through）；显式 token 让 keyword 表不再兜底到 zoom-in |

> 注：`zoom-in / zoom-out / pan-left / pan-right / ken-burns / parallax` 全部保留。

### 3.2 转场 token（独立于 cut 内 animation）

`cuts[i].transition_in` 目前只有 `"fade"` / `"cut"`（mcp_server.py:1576）。
新增：

| Token | 触发关键词 | 渲染语义 |
|---|---|---|
| `smoothstep` | smoothstep, 平滑, smooth step | 两 cut 间 crossfade，缓动用 smoothstep（CSS `cubic-bezier(0.4, 0, 0.2, 1)`），默认 0.6s |
| `wipe` | wipe, 擦除, 横扫 | 左→右 wipe（Remotion `<TransitionSeries>` 已有 primitive） |
| `zoom-cross` | zoom cross, 推拉切换 | zoom-in × zoom-out 同步 |

> 这些是 cut-level 切换而不是 cut 内 animation，存在 `cuts[i].transition_in` 字段。
> 但 keyword 解析目前只填 `transform.animation` —— 需要扩展
> `lib.effects_parser` 的切分逻辑，让含"转场"关键词的段落走 transition 路径。

### 3.3 Easing 修饰符

`rotate-in / particle-out` 默认 spring；用户写 `(spring)` / `(ease-out)` 等应该被捕获。
方案：**保持 token 不变，在 cut 元数据里加 `easing` 字段**，Explainer.tsx 按
easing 字段选择 interpolate 函数。

```ts
cut.transform = { animation: "rotate-in", easing: "spring" }
// 或  cut.transform = { animation: "ken-burns", easing: "ease-out" }
```

`lib.effects_parser` 在解析每个段落时，把 `(spring)` / `(ease-out)` / `(linear)`
等括号注释当成 easing 修饰符提取，存到 `cut.transform.easing`。

---

## 4. 落点：3 个改动 + 1 个开关

| # | 文件 | 改动 |
|---|---|---|
| 4.1 | `lib/effects_parser.py` | `EFFECTS_KEYWORD_TO_ANIMATION` 加 `rotate-in` / `rotate-out` / `particle-out` 关键词组；`apply_effects_to_edit_decisions` 额外填充 `cut.transform.easing` 与 `transition_in` 字段（基于段落里的 "(spring)" / "Smoothstep" 等标记） |
| 4.2 | `remotion-composer/src/Explainer.tsx` | `EnhancedVideoScene` 的 anim switch 加 4 个分支：rotate-in（rotate 0→15° + opacity 0→1，spring）+ rotate-out（对称）+ particle-out（挂 `<ParticleOverlay />`）+ transitionIn 选择器（fade / smoothstep / wipe / zoom-cross） |
| 4.3 | `remotion-composer/src/components/ParticleOverlay.tsx` | 已有，确认支持 `triggerAt="end"` 这种切尾触发的 prop；缺则加（设计下个段落） |
| 4.4 | `lib/effects_parser.py::KNOWN_ANIMATION_TOKENS` | 同步加入新 token（Explainer.tsx switch 是 single source of truth，KNOWN_ANIMATION_TOKENS 是测试断言面） |

> 实施时先扩 4.1（解析），再 4.2（渲染），最后 4.3（粒子触发逻辑）。
> 4.4 跟随 4.2 一起改。

---

## 5. 渲染细节（Explainer.tsx 草图）

```tsx
// 4.2 草图 —— 真实施时按 commit 走
if (anim === "rotate-in") {
  const t = spring ease({ frame, fps, config: { damping: 14 } });
  rotate = interpolate(t, [0, 1], [15, 0]);    // 15°→0°
  opacity = interpolate(t, [0, 1], [0, 1]);
} else if (anim === "rotate-out") {
  const t = spring ease({ frame: frame - durationInFrames * 0.6, fps, ... });
  rotate = interpolate(t, [0, 1], [0, -15]);
  opacity = interpolate(1 - t, [0, 1], [1, 0]);
} else if (anim === "particle-out") {
  // 显示静态图，最后 30% 帧挂 ParticleOverlay 触发汇聚
  return (
    <AbsoluteFill>
      <Img src={...} style={{ transform: `scale(${scale}) ...` }} />
      {frame > durationInFrames * 0.7 && (
        <ParticleOverlay triggerAt="end" count={200} ... />
      )}
    </AbsoluteFill>
  );
}

// transition_in 选择器（cut 间切换）
const TRANSITION_RENDERERS = {
  fade:       (a, b, t) => <FadeTransition a={a} b={b} progress={t} />,
  smoothstep: (a, b, t) => <FadeTransition a={a} b={b}
                                progress={smoothstep(t)} />,  // smoothstep easing
  wipe:       (a, b, t) => <WipeTransition direction="ltr" progress={t} />,
  zoom_cross: (a, b, t) => <ZoomCrossTransition a={a} b={b} progress={t} />,
};
```

Easing 修饰符的接入（保持 token 不变、参数化）：

```tsx
const EASINGS = {
  linear: (t: number) => t,
  "ease-out": (t: number) => 1 - Math.pow(1 - t, 3),
  spring: (t: number) => /* 调用 Remotion spring() */,
};
function pickEasing(cut: Cut): EasingFn {
  return EASINGS[cut.transform?.easing ?? "linear"] ?? EASINGS.linear;
}
```

---

## 6. 向后兼容 / 兜底

- 新 token 不会被老版本 Explainer 识别 —— 但本次仓库内 Explainer.tsx 也会同步改，**没有跨版本兼容问题**（live MCP 单仓交付）。
- `apply_effects_to_edit_decisions` 在 effects 为空时仍是 no-op；老调用方不受影响。
- `EFFECTS_KEYWORD_TO_ANIMATION` 表是顺序敏感（first match wins）—— 扩新条目时**保持旧条目顺序**，新条目放在表尾，避免破坏既有 keyword 解析。
- `cuts[i].transform.easing` 是新字段，老 Explainer 不读不影响渲染。

---

## 7. 测试

### 7.1 Python（lib.effects_parser）

`tests/test_remotion_effects_and_subtitles.py` 加：

- `test_segment_animation_recognises_new_tokens` —— "旋转切入" / "rotate in" / "粒子" / "particle" 各映射到对应 token
- `test_segment_animation_unknown_still_falls_back_to_zoom_in` —— 未列出的关键词仍兜底 zoom-in
- `test_apply_effects_to_edit_decisions_writes_easing_modifier` —— 解析 "(spring)" / "(ease-out)" 等括号注释 → `cut.transform.easing`
- `test_apply_effects_to_edit_decisions_writes_transition` —— "转场 Smoothstep" 等段落 → `cut.transition_in`

### 7.2 TSX（Explainer.tsx）

在 `remotion-composer/` 加：

- `src/__tests__/EnhancedVideoScene.test.tsx`（如项目有 jest/vitest 配置；否则手测）—— 对每个 token 渲染一帧快照，断言 transform / opacity 不为默认 zoom-in
- `src/__tests__/TransitionRenderers.test.tsx` —— 4 种 transition 在 t=0/0.5/1 三点的 progress 值

### 7.3 Live MCP smoke

跑 `make demo` 或 scripts/regression 里的 photo-ken-burns 流程，确认没破坏既有 token。

---

## 8. 排期建议（待定）

不阻塞当前 4 级预览上线。建议分两期：

- **Phase A**（小，半日）：只扩 `lib.effects_parser` 的 keyword 表 + `apply_effects_to_edit_decisions` 的 easing/transition 写入。**不动 Explainer.tsx**。结果是 metadata 富了，但渲染仍是 zoom-in 兜底；用户能看到 metadata 正确但视觉无变化。这是为 Phase B 铺路。
- **Phase B**（大，1-2 天）：Explainer.tsx 加 4 个 anim 分支 + transition 选择器 + ParticleOverlay 触发逻辑。回归 + 截图对比。

如果非要一次到位，可以合到一起，但 Phase A 先 commit 是更稳的回滚点。

---

## 9. 相关链接

- [`remotion-effects-field-review-2026-08-31.md`](remotion-effects-field-review-2026-08-31.md) —— 触发本次扩展的原始 review
- [`lib/effects_parser.py`](../lib/effects_parser.py) —— 待扩的 keyword 表
- [`remotion-composer/src/Explainer.tsx`](../remotion-composer/src/Explainer.tsx) —— 待扩的渲染 switch
- [`remotion-composer/src/components/ParticleOverlay.tsx`](../remotion-composer/src/components/ParticleOverlay.tsx) —— 粒子组件，确认是否支持切尾触发

---

## 10. 决策记录（待定）

- [ ] 是否把"转场"token 做成 cut 级（`transition_in`）还是 animation 级（混淆在 cut 内）？倾向前者 —— transition 是 cut 间关系，animation 是 cut 内动作，混了会破坏 Explainer 的渲染契约。
- [ ] Easing 修饰符是否只对 rotate-in / particle-out 生效（其它走默认 linear）？还是统一应用到所有 token？倾向后者 —— 一致性优先，但要确认 `spring()` 在 zoom-in/pan-* 上的视觉效果是否可接受。
- [ ] ParticleOverlay 的 count/颜色是否也要从 effects 解析？例：用户写"200 粒子，深红色"——是否要扩展 `apply_effects_to_edit_decisions` 支持数字 + 颜色提取？还是只接 count 默认 100，颜色走 template 默认？倾向**留给 Phase B+**，本次只接 `triggerAt` + `count`。