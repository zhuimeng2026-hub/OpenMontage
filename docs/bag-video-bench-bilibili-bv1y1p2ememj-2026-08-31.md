# 箱包对标视频 — B 站通勤包翻包记

> 用于「参数生成」流水线作为风格 / 节奏 / 镜头语言对标的对标视频。
> 下载日期：2026-08-31

## 元数据

| 字段 | 值 |
|---|---|
| 平台 | 哔哩哔哩 (bilibili.com) |
| BV 号 | `BV1Y1p2eMEmJ` |
| 原始链接 | https://www.bilibili.com/video/BV1Y1p2eMEmJ/ |
| 标题 | 翻包记｜体制内小姐姐通勤包里有什么？｜单肩包｜腋下包｜斜挎包 |
| 时长 | 180.3s ≈ **3:00** |
| 播放量 | 9.4k |
| 本地文件 | `docs/bag-video-bench-bilibili-bv1y1p2ememj-2026-08-31.mp4` (17 MB) |
| 视频编码 | HEVC (h.265), 1920×1080, 30 fps |
| 音频编码 | AAC, 立体声 |

## 选片理由

- **时长匹配**：正好 3 分钟，匹配「3 分钟左右」需求。
- **品类匹配**：通勤包（单肩 / 腋下 / 斜挎）属于「箱包」细分，单人多包展示，便于做参数化拆解（容量、隔层、肩带、材质）。
- **结构清晰**：单人讲解 + 实物镜头循环，节奏快，镜头类型集中在：开箱特写 / 360° 转表 / 内部隔层 / 肩带演示 / 出镜口播。对参数化模板友好（每段大致可对应一个 scene spec）。
- **画质够用**：1080p30 HEVC，17 MB，下游 Remotion / ffmpeg 兼容。

## 已知边界

- B 站 1080P 高码率版本要求大会员登录，本次下载的是**非大会员可用**的最高画质 (HEVC 1080P 645 kbps VBR)。如对码率有更高要求，需要补充 cookies 重下。
- 原视频可能带 B 站水印，必要时通过 `--postprocessor-args "-vf delogo=..."` 去除。
- 仅供内部对标参考，**不进入产品交付链路**。

## 复现下载

```bash
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
/root/.pyenv/versions/3.11.8/bin/yt-dlp --user-agent "$UA" \
  --add-header "Referer:https://www.bilibili.com" \
  -f "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/best[ext=mp4][height<=1080]/best" \
  --merge-output-format mp4 \
  -o "bag-video-bench-bilibili-bv1y1p2ememj-2026-08-31.%(ext)s" \
  "https://www.bilibili.com/video/BV1Y1p2eMEmJ/"
```