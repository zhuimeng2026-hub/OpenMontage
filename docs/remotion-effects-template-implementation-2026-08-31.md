# Remotion 模板侧消费 `metadata.effects` 改动清单（修订版 v2.1）

> 关联文档：
> - `remotion-effects-field-review-2026-08-31.md`（B 盘原评审）
> - `remotion-effects-remote-verification-2026-08-31.md`（本地 D:\vclaw\docs，含真实抽样）
>
> ⚠️ **v2 修订说明**：v1 版前提"模板未读取 metadata.effects / B 盘是基线未处理 effects"
> 经复核 **错误**。本版基于 B:\mcp_server.py 与 B:\lib\effects_parser.py 实测代码重写。
> 真实阻塞点是 **effects 解析词表不支持旋转/缩放幅度/淡入**，而非"未读取"。
>
> ⚠️ **v2.1 修订说明**：v2 落实施工后补漏 4 处实施期发现的契约细节——
> 见 §3.1(d) 与 §4 表格修订。**v2.1 已落地实施**（参见 `lib/effects_parser.py` 当前实现 + pytest 52 passed）。

---

## 0. 复核结论（先读，纠正 v1）

- B:\mcp_server.py **已消费** effects：
  - `:1590` `if effects: edit_decisions["metadata"]["effects"] = effects`
  - `:1603` `effects_animation_for_cut(effects, index, n)` 解析成每 cut 的 animation token
  - 硬编码 `motion = ["zoom-in","pan-left","ken-burns","pan-right"]`（:1565）**只是 effects 为空时的回退**
- 解析器 `B:\lib\effects_parser.py` 把自由文本 → **封闭 token 集合**
  `KNOWN_ANIMATION_TOKENS = {zoom-in, zoom-out, pan-left, pan-right, ken-burns, parallax}`（:42）
- 测试文本 `"Apply a CONTINUOUS full 360-degree rotation (0→360) + zoom 0.4x→1.6x + 0.5s fade-in"`
  经 `segment_animation`（:77）扫描：仅命中 "zoom" → 降级为 `zoom-in`；`rotation`/`360`/`fade`
  **无关键字** → 被丢弃。成片只剩极轻推近 → 肉眼像静态。这与抽样截图一致。
- **结论**：effects 链路已端到端接通；阻塞点是**词表 + TSX 渲染分支不支持 rotate / scale 幅度 / fade**。
  改动落在「扩展词表（含结构化参数）+ Explainer 增加渲染 case」，不是"去读 metadata.effects"。

---

## 1. 当前 effects 链路（实测）

```
create_remotion_video_share(effects)
  └─ mcp_server.py:1590   edit_decisions["metadata"]["effects"] = effects
  └─ apply_effects_to_edit_decisions()        # lib/effects_parser.py:115
       改写  edit_decisions.cuts[i].transform.animation = token
             edit_decisions.cuts[i].transform.effects    = segment 原文（仅文本，未渲染）
             scene_plan[i].shot_language.camera_movement = token
  └─ remotion-composer/src/Explainer.tsx::ImageScene             # 按 token+params 渲染（同仓库子目录，非单独仓库）
```

`effects_parser.py` 模块注释（:18-21）明确契约：新增 token 须**三处同步改**
（本文件 / Explainer switch / `KNOWN_ANIMATION_TOKENS` + 测试）。

---

## 2. 根因：词表与参数模型不足以表达目标效果

| 目标效果 | 当前是否支持 | 说明 |
|---|---|---|
| zoom-in / zoom-out | ✅ | token 已有 |
| pan-left / pan-right | ✅ | token 已有 |
| ken-burns / parallax | ✅ | token 已有 |
| **rotation（任意角度，如 360°）** | ❌ | 无 token、无关键字 |
| **zoom 幅度（0.4x→1.6x）** | ❌ | token 只表方向，表不了数值区间 |
| **fade-in / fade-out（0.5s）** | ❌ | 无 token、无关键字 |

`segment_animation`（:77）无匹配时回退 `zoom-in` → 静默丢信息，是本次"看似静态"的直接原因。

---

## 3. 改造方案

### 方案 A（推荐先上）：扩展 token + 引入结构化 `animation_params`

**3.1 `B:\lib\effects_parser.py`**

(a) `KNOWN_ANIMATION_TOKENS`（:42）扩为：
```python
{"zoom-in","zoom-out","pan-left","pan-right","ken-burns","parallax",
 "rotate","fade-in","fade-out"}
```
(b) `EFFECTS_KEYWORD_TO_ANIMATION`（:51）增加：
```python
(("rotate","rotation","旋转","转场旋转","360"), "rotate"),
(("fade in","fade-in","淡入"), "fade-in"),
(("fade out","fade-out","淡出"), "fade-out"),
```
(c) **关键**：token 只表"类型"，数值（角度/幅度/时长）需结构化。改 `apply_effects_to_edit_decisions`
    同时 emit 参数 dict，**对称写入** `cut.transform["animation_params"]` **与**
    `scene_plan[i].shot_language["animation_params"]`（与现有 `animation` 双写模式一致），
    例如：
```python
# 解析 "rotation (0->360)" → {"rotate": [0.0, 360.0]}
# 解析 "zoom 0.4x->1.6x"   → {"scale": [0.4, 1.6]}
# 解析 "0.5s fade-in"        → {"fade_in": 0.5}
transform["animation"] = token
transform["animation_params"] = extract_animation_params(segment)   # 新增
shot_language["camera_movement"] = token
shot_language["animation_params"] = extract_animation_params(segment)  # 镜像写入
```
> `segment_animation` 仍保留（无匹配回退 zoom-in），但 `extract_animation_params` 用正则抽数值，
> 失败则该参数为空 dict `{}`（**必须静默，不抛异常**），TSX 用 token 默认幅度兜底。
> 分隔符正则必须**同时支持** Unicode `→` (U+2192) 与 ASCII `->`（详见 §3.1(d) gap 1 与 §4 表格）。

(d) **v2.1 实施期补漏**（4 项，已在当前代码中修复，doc 同步回来）：
1. **分隔符正则同时支持 `→` 与 `->`**：v2 写法 `[-→=]` 在 ASCII 输入 `"0.4x->1.6x"` 上 `\s*[-→=]\s*` 形态因 `\s*` 后字符非数字回溯失败。改为 `[-→=]+>?`（允许 `→` / `->` / `=>` / `-` / `=` 等单/双字符形态），rotation 区间正则同款修复。
2. **`fade-in` / `fade-out` 秒数位置双向支持**：v2 仅"关键词之后"形态，缺 `"0.5s fade-in"` / `"1.2s fade in"`。改为 alternation：
   ```python
   r"(?:(\d+(?:\.\d+)?)\s*s\s+fade[- ]?in|fade[- ]?in(?:\s+(\d+(?:\.\d+)?)\s*s)?)"
   # 取 group(1) or group(2)；均未匹配则默认 0.5 秒
   ```
   `fade-out` 同款。
3. **`rotate` bare word 默认 `[0, 360]`**：仅有 `rotate`/`rotation`/`360` 而无区间、无 `deg` 时，回退 `{"rotate": [0, 360]}`（v2 §4 表格已描述但实现易漏，需在 `extract_animation_params` 显式写）。
4. **旧测试断言同步**：`tests/test_remotion_effects_and_subtitles.py::test_segment_animation_zh_keywords` 中
   `"开篇旋转切入" == "zoom-in"` 断言**必须翻成 `"rotate"`**——v2 加 rotate keyword tuple 含 `旋转` 后，
   `旋转` 不再 fallthrough，必然回 `rotate`。**不要改回去**——否则 rotate token 失效会反咬一口。

**3.1bis 实施后实测**（2026-08-31）：
- `py_compile lib/effects_parser.py` ✓
- `KNOWN_ANIMATION_TOKENS` 9 个（`fade-in, fade-out, ken-burns, pan-left, pan-right, parallax, rotate, zoom-in, zoom-out`）
- `extract_animation_params("Apply a CONTINUOUS full 360-degree rotation (0→360) + zoom 0.4x->1.6x + 0.5s fade-in")`
  → `{"rotate": [0.0, 360.0], "scale": [0.4, 1.6], "fade_in": 0.5}` ✓
- `extract_animation_params("garbage with no keywords whatsoever")` → `{}` 不抛异常 ✓
- `pytest tests/test_remotion_effects_and_subtitles.py` → **52 passed**

**3.2 `remotion-composer/src/Explainer.tsx::ImageScene`（同仓库子目录 `/opt/OpenMontage_Voicebox/remotion-composer/`）**

(α) **签名扩展**：ImageScene 增 `animationParams?: Record<string, any>` prop：
```tsx
const ImageScene: React.FC<{
  src: string;
  animation?: string;
  animationParams?: Record<string, any>;   // 新增
}> = ({ src, animation, animationParams }) => { ... }
```
调用点 `Explainer.tsx:713` / `:722`（`cut.source && isImage` 分支）须同步透传：
```tsx
<ImageScene
  src={cut.source}
  animation={animation}
  animationParams={cut.transform?.animation_params}   // 新增
/>
```

(β) **CSS transform 模板统一**：所有 case（含既有 zoom/pan/ken-burns/parallax 与新增 rotate）
必须用同一字符串模板，rotate 放最前以避免影响 scale/translate 方向：
```tsx
let rotateDeg = 0;     // 默认 0（非 rotate case 也走同一模板）
// ...switch 计算 rotateDeg / scale / translateX / translateY ...
transform: `rotate(${rotateDeg}deg) scale(${scale}) translate(${translateX}px, ${translateY}px)`
```
不允许任何 case 单独覆写整字符串（防止 scale/translate 被吞）。

(γ) **fade 分层语义**：`ImageScene:357` 的 `spring fadeIn` 是**每 cut 都有的"基础淡入"**（无 params 时的默认值）。
当 `animationParams.fade_in` 存在时，**替换** spring 计算为 params 驱动的 `interpolate`：
```tsx
const fadeIn = animationParams?.fade_in != null
  ? interpolate(frame, [0, animationParams.fade_in * fps], [0, 1],
      { extrapolateRight: "clamp" })
  : spring({ frame, fps, config: { damping: 18, stiffness: 80 } });

const fadeOut = animationParams?.fade_out != null
  ? interpolate(frame, [durationInFrames - animationParams.fade_out * fps, durationInFrames], [1, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
  : interpolate(frame, [durationInFrames - 8, durationInFrames], [1, 0.3],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
```
最终 `opacity = fadeIn * fadeOut`。注意：params 缺失时**回退到 spring / 旧 fadeOut**——零回归。

(δ) **switch 新增 case**（接续现有 if/else 链，:377-396 之后）：
```tsx
} else if (anim === "rotate") {
  const [from, to] = Array.isArray(animationParams?.rotate)
    ? animationParams.rotate
    : [0, 360];
  rotateDeg = interpolate(frame, [0, durationInFrames], [from, to],
    { extrapolateRight: "clamp" });
}
// "fade-in" / "fade-out" 不进入此 switch — 它们仅靠 (γ) 替换 fadeIn/fadeOut 计算，
// scale/translate 仍走默认（即不动），保持图片位置不变只改透明度。
```

(ε) **zoom 幅度读 params.scale**：既有 `zoom-in` 分支改为：
```tsx
if (anim === "zoom-in") {
  if (Array.isArray(animationParams?.scale)) {
    scale = interpolate(progress, [0, 1], animationParams.scale);
  } else {
    scale = 1 + progress * 0.18;   // 默认值不变
  }
}
```
`zoom-out` 同样处理（区间反向则颠倒前后即可）。其它既有分支**保持原样**，仅统一 transform 字符串模板。

**3.3 测试 `B:\tests\test_remotion_effects_and_subtitles.py`**

每个新 token + 参数解析加 case（模块注释:41 要求）。**注意**：rotate 关键词（`旋转`）落地后，
旧测试 `test_segment_animation_zh_keywords` 中 `"开篇旋转切入" == "zoom-in"` 必须翻成 `"rotate"`
——见 §3.1(d) gap 4。落地后实际 pytest 跑出 **52 passed**（含全套新单测）。

### 方案 B（更准，代价高）：LLM 解析 → `EffectOp[]` JSON
`create_remotion_video_share` 收到 effects 先调 LLM 转结构化 JSON 写入
`metadata.effects_parsed`，模板读之。每次生成多一次 LLM 调用 + 需 schema 校验 + 失败回退。
建议作为 v2 之后的增强，不在本清单强制。

---

## 4. 解析规则示例（结构化，供方案 A 实现参考）

| 文本特征（正则，大小写不敏感） | 提取为 `animation_params` |
|---|---|
| `rotation` / `rotate` + 可选 `(\d+)\s*[-→=]+>?\s*(\d+)?\s*(?:deg|度)?` | `{"rotate":[from,to]}`；仅 `rotation`/`rotate`/`360` 无区间 → `[0,360]`（bare default，见 §3.1(d) gap 3） |
| `zoom\s*([\d.]+)x?\s*[-→=]+>?\s*([\d.]+)x?` | `{"scale":[from,to]}`（分隔符同款 `[-→=]+>?` 同时支持 `→` 与 `->`，见 §3.1(d) gap 1） |
| `(?:(\d+(?:\.\d+)?)\s*s\s+)?fade[- ]?in(?:\s+(\d+(?:\.\d+)?)\s*s)?` | `{"fade_in": sec or 0.5}`（秒数可前可后，见 §3.1(d) gap 2） |
| `(?:(\d+(?:\.\d+)?)\s*s\s+)?fade[- ]?out(?:\s+(\d+(?:\.\d+)?)\s*s)?` | `{"fade_out": sec or 0.5}`（同上） |
| `ken\s?burns` | token `ken-burns`（幅度仍走默认） |
| `pan\s*(left\|right\|up\|down)` | token `pan-*` |

`applyEffects(params, frame, totalFrames, fps)` 用 `interpolate` + 可选 `spring` 折算 transform。

---

## 5. 验收标准（联调用）

1. 带 `effects="Apply a CONTINUOUS full 360-degree rotation (0→360) + zoom 0.4x→1.6x + 0.5s fade-in"`
   调 `create_remotion_video_share`，成片**肉眼可见**整圈旋转 + 由小变大 + 开头淡入
   （对照静态抽样 `https://share.weiyun.com/LGIHrznu`，应明显不同）。
2. **不传** effects → 行为同原 `motion` round-robin（回归不破坏）。
3. 文本无法解析（无关键字）→ 回退 `zoom-in`，不黑屏、不空镜。
4. 字段名 `effects` 不得变更（前端已发布，textarea 直传）。
5. `transform.animation_params` 缺失/非法时，TSX 用 token 默认幅度兜底。

---

## 6. 提交前自查（给开发）

- [ ] `KNOWN_ANIMATION_TOKENS` / `Explainer.tsx` switch / `tests` 三处同步新增 token
- [ ] `animation_params` 提取正则覆盖 rotation/zoom 幅度/fade，且失败不抛异常
- [ ] `remotion-composer/src/Explainer.tsx::ImageScene` 已改（**同仓库子目录 `/opt/OpenMontage_Voicebox/remotion-composer/`，非单独仓库**）
- [ ] 真实生成抽样复测通过（用上面 360 旋转文本，对照静态成片）
- [ ] `release/mvp` 同步本改动

---

## 7. 关联文件（精确路径）

| 文件 | 位置 | 作用 |
|---|---|---|
| `mcp_server.py` | B:\（:1590-1613） | 写入 `metadata.effects` + 调解析 |
| `lib/effects_parser.py` | B:\（:42/:51/:77/:115） | 文本→token + params（**本次主要改这里**） |
| `Explainer.tsx::ImageScene` | `/opt/OpenMontage_Voicebox/remotion-composer/src/Explainer.tsx`（:349，同仓库子目录） | 按 token+params 渲染（**本次也要改**） |
| `tests/test_remotion_effects_and_subtitles.py` | B:\ | 解析单测 |

---

## 8. 复测脚本位置

`D:\vclaw\docs\remotion-effects-remote-verification-2026-08-31.md` 含走标准 MCP `tools/call` 的
抽样脚本（upload_asset_chunk → create_remotion_video_share(effects=...) → get_render_status）。
