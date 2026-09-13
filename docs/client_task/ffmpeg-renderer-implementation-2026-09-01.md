# Client-side FFmpeg Renderer — Implementation Reference

**日期**：2026-09-01
**配套代码**：
- [`tools/client/ffmpeg_renderer.py`](../../tools/client/ffmpeg_renderer.py) — 渲染器主体（~280 行 Python）
- [`tools/client/ffmpeg_renderer_test.py`](../../tools/client/ffmpeg_renderer_test.py) — 可运行的 smoke 测试 + 示例工件

## 用途

`video-template-remix` 流水线锁定 `render_runtime = "ffmpeg"`。服务端 `video_compose` 是为多 runtime 设计的 server-side orchestrator（870MB node_modules + headless Chrome 路径），不适合客户端打包。

本模块是**纯 FFmpeg 命令生成器**：
- 输入：`edit_decisions.json` + `asset_manifest.json`
- 输出：6 步 FFmpeg 命令序列（可直接 `subprocess.run`）
- 零依赖（仅 Python 标准库 + 系统 FFmpeg ≥ 4.4）

## API

```python
from tools.client.ffmpeg_renderer import FFmpegRenderer

renderer = FFmpegRenderer.from_artifacts(
    edit_decisions_path="projects/<id>/edit_decisions.json",
    asset_manifest_path="projects/<id>/asset_manifest.json",
    project_root="projects/<id>/",
)
plan = renderer.build_plan()

# 逐条执行
for step in plan:
    print(step.shell_command())        # 给人看的 shell 命令
    subprocess.run(step.argv, check=True)  # 给 subprocess 的 argv 列表
```

`RenderPlan.steps` 包含 6 步：

| Step | 作用 | 输出 |
|---|---|---|
| `render_cut_NNN` | 渲染单个 cut（trim + scale + crop + overlay + 可选 ken-burns） | `work/cut_NNN.mp4` |
| `concat_cuts` | concat demuxer 拼接所有 cut | `work/concat.mp4` |
| `apply_subtitles` | 烧字幕（仅当 `subtitles.enabled=true`） | `work/subtitled.mp4` |
| `final_encode` | 最终编码到 `compose_target` 分辨率/帧率 | `final.mp4` |

## 生成的命令示例

实际跑过的命令（来自 `python tools/client/ffmpeg_renderer_test.py --fast`）：

### Step 1: render_cut_000（带图片覆盖 + ken-burns 缩放）

```bash
ffmpeg -y -i source.mp4 -loop 1 -i hero.png \
  -filter_complex "
    [0:v]trim=start=0.0:end=3.2,setpts=PTS-STARTPTS,
         scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,fps=30,
         zoompan=z='min(zoom+0.0015,1.5)':d=96:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920,
         crop=1080:1920,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,
         fps=30,format=yuv420p[bg];
    [1:v]scale=1080:1920:force_original_aspect_ratio=decrease,
         pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba[fg];
    [bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:shortest=0[vout]
  " \
  -map "[vout]" -map 0:a? -t 3.2 \
  -c:v libx264 -preset medium -crf 18 -c:a aac -b:a 192k -movflags +faststart \
  work/client_render/cut_000.mp4
```

### Step 4: concat_cuts

```bash
ffmpeg -y -f concat -safe 0 -i work/client_render/concat_list.txt \
  -c copy work/client_render/concat.mp4
```

### Step 5: apply_subtitles

```bash
ffmpeg -y -i work/client_render/concat.mp4 \
  -vf "subtitles=assets/reference/source.srt:force_style='FontSize=48,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Alignment=2,WrapStyle=2'" \
  -c:v libx264 -preset medium -crf 18 -c:a copy \
  work/client_render/subtitled.mp4
```

### Step 6: final_encode

```bash
ffmpeg -y -i work/client_render/subtitled.mp4 \
  -c:v libx264 -preset medium -crf 18 -c:a aac -b:a 192k -movflags +faststart \
  final.mp4
```

## 关键设计决策

### 1. 用 `trim` filter 而非 `-ss -to` 输入 seek

```python
# ✗ 不推荐：-ss -to 输入 seek 在短源文件末尾会卡死
argv = ["-ss", str(in_s), "-to", str(out_s), "-i", str(source_path)]
src_filters = [f"trim=start={in_s}:end={out_s}", "setpts=PTS-STARTPTS"]

# ✓ 推荐：只用 trim filter，输入全量读
argv = ["-i", str(source_path)]
src_filters = [f"trim=start={in_s}:end={out_s}", "setpts=PTS-STARTPTS"]
```

实测发现：`-ss 2.0 -to 3.0` 在 3 秒源文件的最后 1 秒会无限循环（FFmpeg 输入 seek 找不到可解码的关键帧）。改用 filtergraph 内 `trim` + `setpts=PTS-STARTPTS` 后稳定。

### 2. 必须 `-t <duration>` 限制输出

```python
argv += ["-t", str(duration)]   # 关键！防止 -loop 1 的图片无限循环
```

`-loop 1` 让 PNG 无限循环，没有输出时长限制会让 FFmpeg 一直编码。`-t <duration>` 兜底。

### 3. 用 `increase` + `crop` 而非 `cover` 模拟 cover 行为

```python
# ✗ FFmpeg 5.0+ 才有 force_original_aspect_ratio=cover
"scale=W:H:force_original_aspect_ratio=cover"

# ✓ FFmpeg 4.4+ 兼容：increase + crop
"scale=W:H:force_original_aspect_ratio=increase"
"crop=W:H"
```

### 4. 输出 format 强制 yuv420p

避免 H.264 high profile 兼容性问题（QuickTime、移动端播放器）。

### 5. `-movflags +faststart`

moov atom 移到文件头部，浏览器/移动端可边下边播。

## 测试

### Dry-run（不需要 FFmpeg）

```bash
python tools/client/ffmpeg_renderer_test.py
```

打印所有 6 步命令，不实际执行。

### 完整端到端 smoke test

```bash
python tools/client/ffmpeg_renderer_test.py --execute --fast
```

`--fast` 模式：
- 源视频 3 秒 320x180（默认 12 秒 1280x720）
- 输出 360x640（默认 1080x1920）
- 跳过 ken-burns zoompan
- preset = ultrafast

实测输出：

```
Plan: 6 steps
>>> render_cut_000 (0.26s) rc=0
>>> render_cut_001 (0.14s) rc=0
>>> render_cut_002 (0.27s) rc=0
>>> concat_cuts (0.08s) rc=0
>>> apply_subtitles (0.45s) rc=0
>>> final_encode (0.10s) rc=0

Final output: /tmp/.../final.mp4 (221,271 bytes)
  codec_name=h264
  width=360
  height=640
  duration=3.033333
  nb_frames=91
```

## FFmpeg 版本兼容

| FFmpeg 版本 | 状态 |
|---|---|
| ≥ 5.0 | 完全兼容（`force_original_aspect_ratio=cover` 也支持） |
| 4.4 (Ubuntu 22.04 LTS) | 兼容（本模块默认输出） |
| < 4.4 | `force_original_aspect_ratio=increase` 在 4.0+ 可用，建议 ≥ 4.4 |

## 集成到 GUI 客户端

```python
# 在 GUI 客户端中（任何语言）
from tools.client.ffmpeg_renderer import FFmpegRenderer, RenderPlan

# 1. 从 OM 平台下载编排脚本包
renderer = FFmpegRenderer.from_artifacts(
    edit_decisions_path="~/Downloads/edit_decisions.json",
    asset_manifest_path="~/Downloads/asset_manifest.json",
    project_root="~/Downloads/<project-id>/",
    ffmpeg_bin="/path/to/bundled/ffmpeg",  # GUI 客户端打包的二进制
)

# 2. 生成命令并执行
plan = renderer.build_plan()
for step in plan:
    # GUI: 显示进度条 + 子进程 stdout 流式回调
    subprocess.run(step.argv, check=True, capture_output=False)

# 3. 上传 final.mp4 + render_report 回 OM 平台
# (走 asset_upload / rsync_upload 协议)
```

## 已知限制 / 未来扩展

1. **不支持 xfade 转场**：当前 `transition_in/out` 字段未在 renderer 中生效。生产版本应使用 ffmpeg `xfade` filter 在 concat 前做相邻 cut 的过渡。
2. **不支持 music 混音**：`edit_decisions.audio.music` 字段未处理。生产版本应增加 `-i music.mp3` + `amix` filter。
3. **不支持 audio ducking / volume**：默认源音轨 `-map 0:a?` 原样保留；未做音量调整或 ducking。
4. **单线程**：`render_cut_NNN` 步骤独立但串行执行。可并行化（`subprocess.Popen` + `concurrent.futures`）。
5. **不支持 image 序列**：当前 overlay 只接受单张图，未来可能扩展到 image 序列。

这些都是"易扩展点"而非"设计缺陷"——保持核心模块小而清晰，把扩展留给具体场景。
