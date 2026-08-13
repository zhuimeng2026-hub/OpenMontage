# OpenMontage × HyperFrames 示例

这是一个 **HyperFrames** 的最小可运行示例，演示「一张 `index.html` → 一段 MP4」：

- 背景视频（可选，`assets/bg.mp4`）
- **GSAP** 入场动画（标题 / 副标题 / 品牌 chip），注册成 `paused` 时间线，渲染器逐帧 seek，保证确定性
- **烧录字幕**：纯 HTML 文本覆盖层，会被一起截进 MP4

> 与 `remotion-composer/` 下的 Remotion 管线**相互独立**，互不影响。

## 需求

- **Node.js 22+**（`node -v` 确认）
- **FFmpeg**（渲染必需，HyperFrames 用它编码 MP4）
  - 本仓库已自带一份（Windows x64），无需单独安装：
    `remotion-composer/node_modules/@remotion/compositor-win32-x64-msvc/ffmpeg.exe`
  - 临时加入 PATH 再渲染（Windows PowerShell）：
    `$env:PATH = "C:\Users\huawei\OpenMontage\remotion-composer\node_modules\@remotion\compositor-win32-x64-msvc;" + $env:PATH`
  - 或系统安装：macOS `brew install ffmpeg` / Windows `winget install Gyan.FFmpeg` / Ubuntu `sudo apt install ffmpeg`

## 运行

```bash
cd hyperframes-demo

# 1) 浏览器预览，可拖动时间轴逐帧检查
npx hyperframes preview

# 2) 渲染成 MP4（无头 Chrome 逐帧 seek + FFmpeg 编码）
npx hyperframes render
```

输出默认在 `out/demo.mp4`。

## 自定义

- 换背景视频：把任意 `.mp4` 放到 `assets/bg.mp4`（缺失时渐变背景自动兜底）。
  也可取消 `index.html` 中 `#bg` `<img>` 注释，改用背景图。
- 改文案 / 时间：编辑 `index.html` 里各元素的 `data-start` / `data-duration` / 字幕文本。
- 加音频：在 `#stage` 内加
  `<audio class="clip" data-start="0" data-duration="8" data-track-index="6" data-volume="0.5" src="assets/music.mp3">`。

## 关键约定（来自 HyperFrames）

- 根元素：`data-composition-id` + `data-start` + `data-width` + `data-height`
- 每个素材：`class="clip"` + `data-start` / `data-duration` / `data-track-index`
- GSAP：建 `gsap.timeline({ paused: true })` 并挂到 `window.__timelines[compositionId]`
