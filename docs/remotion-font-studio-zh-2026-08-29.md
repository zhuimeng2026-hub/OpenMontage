# 字体修复 & Remotion Studio 汉化 — 工作报告

> 日期：2026-08-29
> 涉及：remotion-composer/

---

## 一、字体自托管改造

### 问题背景

`src/` 里约 42 处使用 `Inter`、`Space Grotesk`、`JetBrains Mono`、`Playfair Display`，但这些字体：
- 既没有 `@font-face` 加载
- 也没有本地安装（`fc-list` 命中 0）
- `@remotion/google-fonts` 包虽然在 `package.json` 依赖里，但从没 import

结果：所有渲染的 Latin 文字实际回退成了 Chrome 默认的**衬线字体**（非设计意图），而中文因为系统装了 85 个 CJK 字体可以回退到 Noto CJK。

### 解决方案

#### 1. `src/fonts.ts` — 字体注册中心

```ts
import { continueRender, delayRender, staticFile } from "remotion";
```

- 用 `FontFace` API 逐字重加载静态 woff2 文件
- `delayRender()` 持柄，`document.fonts.ready` 后才 `continueRender()` —— 保证 Remotion 在字体真正 paint 后才截帧
- 失败不卡死（catch unblock + console error）
- 完全离线：CJK 走系统 Noto CJK，从不过网

导出 6 个字体栈常量：

| 常量 | 用途 | 示例 |
|---|---|---|
| `SANS` | 正文、标签、图表轴 | `Inter, CJK_SANS, system-ui, sans-serif` |
| `DISPLAY` | 标题、统计数字、副标题 | `Space Grotesk, Inter, CJK_SANS, ...` |
| `MONO` | 终端场景、代码 | `JetBrains Mono, CJK_MONO, monospace` |
| `SERIF` | 编辑体/引语 | `Playfair Display, Georgia, CJK_SERIF, serif` |
| `APPLE_DISPLAY` | ProductReveal 专用 | `SF Pro Display, Inter, CJK_SANS, ...` |
| `CJK` | 双语字幕中文行 | `CJK_SANS, system-ui, sans-serif` |

#### 2. 字体文件

位置：`public/fonts/`，共 28 个静态 woff2 文件，约 400KB：

```
Inter-wght{300,400,500,600,700,800,900}.woff2
SpaceGrotesk-wght{300,400,500,600,700}.woff2
JetBrainsMono-wght{400,500,600,700}.woff2
PlayfairDisplay-wght{400,500,600,700,800,900}.woff2
PlayfairDisplay-Italic-wght{400,500,600,700}.woff2
```

> 使用静态字重文件而非 variable font，避免 font-variation-settings 轴兼容问题

#### 3. 字体下载脚本

```bash
node scripts/download-fonts.js
```

从 Google Fonts API 下载（需要 Chrome UA，否则返回 TTF 而非 woff2）。新机器或升级字体时运行。

#### 4. 组件替换

约 42 处硬编码字体字面量 → `fonts.ts` 导出常量，覆盖 30 个文件：
`CinematicRenderer`, `CollageBurst`, `Explainer`, `LyricOverlay`, `TitledVideo`,
`BilingualCaptionOverlay`, `CalloutBox`, `CaptionOverlay`, `ComparisonCard`, `EndTag`,
`HeroTitle`, `ProductReveal`, `ProgressBar`, `ProviderChip`, `ScreenshotScene`,
`SectionTitle`, `StatCard`, `StatReveal`, `TerminalScene`, `TextCard`,
`charts/BarChart`, `charts/KPIGrid`, `charts/LineChart`, `charts/PieChart` 等。

> 注意：`CollageBurst` 和 `LyricOverlay` 原来的 `playfairFamily`/`playfairItalic` 别名已清理，直接用 `SERIF`

### 渲染验证

```
npx remotion still FontVerify
```

输出：`remotion-composer/out/FontVerify.png`

验证结果：
- **SANS (Inter)**：Latin 无衬线 ✅，中文 Noto CJK ✅
- **DISPLAY (Space Grotesk)**：r/g 字形正确 ✅，中文 Noto CJK ✅
- **MONO (JetBrains Mono)**：`const fn = () => 42;` 等宽 ✅，中文 Noto CJK ✅
- **SERIF (Playfair Display)**：Q 长尾、f 横杠正确 ✅，中文 Noto CJK ✅

---

## 二、Remotion Studio 汉化（方案 A）

### 问题背景

Remotion Studio（`npx remotion studio`）上游没有任何 i18n 支持，界面文案全部硬编码英文。

### 解决方案

#### 1. 词条字典

`scripts/studio-zh-dict.json` — 413 条翻译，来源为 `node_modules/@remotion/studio/dist/esm/previewEntry.mjs` 等文件的字符串字面量。

覆盖范围：
- 时间轴、播放控制
- 合成列表、资产面板
- 属性编辑器、Props
- 渲染按钮与队列
- 菜单、右键操作
- 快捷键说明

**不翻译**：字体名（约 400 条 Google Fonts 名）、Lambda/云渲染、AI Coding Agent、许可证、技术术语（JSON/HTML/CSS/Codec）

#### 2. 补丁脚本

`scripts/patch-studio-zh.js`

```bash
node scripts/patch-studio-zh.js          # 打补丁（幂等）
node scripts/patch-studio-zh.js --restore  # 从 .orig 备份还原
```

特性：
- 同时处理 ESM bundle（`dist/esm/*.mjs`）和 CJS 子模块（含单引号字面量）
- 幂等：第二次运行匹配数为 0，不重复替换
- 补丁后清理 webpack 缓存，确保重启生效
- `--restore` 从 `.orig` 备份还原

#### 3. 固化机制

`package.json` postinstall 钩子：

```json
{
  "scripts": {
    "studio:zh": "node scripts/patch-studio-zh.js",
    "postinstall": "node scripts/patch-studio-zh.js 2>/dev/null || true"
  }
}
```

每次 `npm install` 或 `remotion upgrade` 后自动静默重打补丁，无需手动操作。

#### 4. 升级注意事项

> 修改 `node_modules` 本质脆弱。`remotion upgrade` 后词条可能对不上，需要重跑：
> ```bash
> node scripts/patch-studio-zh.js
> ```

### 验证结果

截图位置：`remotion-composer/out/FontVerify.png`（Studio 界面）

| 元素 | 汉化 |
|---|---|
| 顶部菜单 | 文件 · 视图 · 合成 · 工具 · 帮助 |
| 左侧 Tab | 合成列表 · 资产 |
| 渲染按钮 | 渲染 ✅ |

零控制台错误，无白屏，无崩溃。

---

## 三、文件清单

### 新增文件

| 文件 | 说明 |
|---|---|
| `remotion-composer/src/fonts.ts` | 字体注册中心 |
| `remotion-composer/public/fonts/*.woff2` | 28 个静态 woff2 文件（约 400KB） |
| `remotion-composer/scripts/download-fonts.js` | 字体下载脚本 |
| `remotion-composer/scripts/studio-zh-dict.json` | 413 条 UI 翻译词条 |
| `remotion-composer/scripts/patch-studio-zh.js` | 幂等补丁脚本 |
| `remotion-composer/scripts/README-studio-zh.md` | Studio 汉化维护说明 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `remotion-composer/package.json` | postinstall 钩子 + studio:zh 脚本 |
| 30 个组件 | 42 处字面量 → `fonts.ts` 常量 |

---

## 四、后续维护

### 字体升级

```bash
# 新字体或新字重时运行
node scripts/download-fonts.js
```

### Studio 升级 Remotion

```bash
# 升级后重打补丁
node scripts/patch-studio-zh.js

# 或还原英文（如需要）
node scripts/patch-studio-zh.js --restore
```

### 新增词条翻译

编辑 `scripts/studio-zh-dict.json`，在 JSON 对象末尾追加 `"原文": "译文"`，然后重打补丁：

```bash
node scripts/patch-studio-zh.js
```
