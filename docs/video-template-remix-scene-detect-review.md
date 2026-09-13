# 视频模板复刻与 SceneDetect 修复记录

## 目标

本次会话围绕两个业务目标展开：

1. 对比 `C:\Users\Admin\claude-video` 与 OpenMontage 的视频分解能力，并为“复用原视频结构、替换指定素材”的业务场景增加专用流水线。
2. 调查 B 站视频 `BV12oGu6uEX8` 通过 MCP 上传后只显示一个镜头的问题，并修复长视频分镜的静默退化。

## 对比验证

测试视频：`https://www.bilibili.com/video/BV12oGu6uEX8/`，约 719 秒。由于直接下载受 B 站访问限制，使用公开播放接口取得可测试的 360p 视频版本。

| 工具 | 结果 | 结论 |
|---|---:|---|
| `claude-video` | 181 个候选切点，最终 50 帧 | 有镜头候选、去重和抽帧，但没有可用转录 |
| OpenMontage `VideoAnalyzer` | 137 个镜头、50 帧 | 能生成结构化镜头时间线、节奏统计和分析 brief |

因此，OpenMontage 更适合作为后续“按原结构替换素材”的流水线输入；`claude-video` 的候选切点和抽帧策略可作为补充参考。

## “只识别一个镜头”的根因

### MCP 上传不等于分镜分析

`tools/asset_upload.py::UploadAsset.execute` 的职责是保存文件并登记资产。它返回的 `asset_manifest` 只包含一个源视频资产，不会自动执行 `scene_detect` 或 `video_analyzer`。

所以，上传完成后看到一个资产是正常的；要得到镜头列表，必须显式执行分析阶段。

### 长视频 FFmpeg 超时被伪装成成功

旧实现的 FFmpeg 检测固定使用 120 秒超时。超过超时后，后备路径把异常吞掉、清空检测输出，随后无条件生成 `[0, 总时长]` 单镜头并返回成功。

这对约 12 分钟、4K 或高压缩率视频尤其容易触发。相同视频的有效分析结果有 137 个镜头，因此问题在分析链路而不是视频内容。

## `video-template-remix` 流水线

新增 manifest：`pipeline_defs/video-template-remix.yaml`。

阶段顺序：

`idea → script → scene_plan → assets → edit → compose → publish`

核心约束：

- 默认保留镜头边界、镜头时长、节奏、转场、字幕位置、源音频和响度。
- 只替换用户明确批准的素材槽位。
- 每个替换素材必须有来源和授权记录。
- `scene_plan.metadata.template_slots` 保存每个镜头的槽位策略，避免向严格 schema 的 scene 对象写入未声明字段。
- 合成阶段按已批准的 `render_runtime` 路由；不允许静默切换 Remotion、HyperFrames 或 FFmpeg。

默认入口已接入 `config.yaml`、pipeline loader、checkpoint、MCP status/checkpoint 和 VideoAnalyzer。

## SceneDetect 修复

文件：`tools/analysis/scene_detect.py`

- 使用视频时长和分辨率计算 FFmpeg 超时，范围 60–900 秒。
- 超过 300 秒的视频按 180 秒分段检测。
- 分段结果转换为全局时间戳，并按 `min_scene_length_seconds` 去重边界。
- 完全失败返回 `success=False`、`status=failed`，不再伪造单镜头成功。
- 部分分段失败但仍有有效镜头时返回 `status=degraded`，保留镜头和诊断信息。
- 将状态和诊断写入 scene JSON artifact，避免下游只读文件时丢失失败信息。
- VideoAnalyzer 保留 degraded 镜头，同时把诊断记录到分析元数据。

## 本机验证

复核脚本：`utils/verify_video_template_remix.py`

运行：

```bash
python utils/verify_video_template_remix.py
```

脚本会检查默认流水线、阶段顺序，并自动生成三段硬切样片，强制走 FFmpeg SceneDetect 路径。当前结果：

- `ok: true`
- 默认流水线：`video-template-remix`
- 三段样片识别为 3 个镜头
- SceneDetect 定向测试与 Phase 2 相关测试通过

最后一次针对性回归：`66 passed`；主线合入后的流水线/SceneDetect 复核：`10 passed`。

## Git 记录

- 首次实现提交：`cc596842441f9b4c319f6037cbcb3fe95d52f4e5`
- `OpenMontage_Voicebox` 合入修复：`81ee3cb6da217b99f0cfe4ee9b0b0c46648d3ca7`
- 复核脚本提交：`b53df7c`
- 当前主线合入修复：`8453a52`

## 文件整理说明

本次会话生成的分析记录统一保存在本目录。代码、流水线、测试和 `utils` 复核脚本属于 OpenMontage 运行/验证所需文件，保留在其规范目录；Claude Code/OpenMontage 必需的配置与指导文件也不移动。

本次 B 站测试下载缓存目录 `.tmp-video-compare` 已清理，避免外部视频、虚拟环境和中间文件进入仓库。用户已有的 `OpenMontage-mcp-proxy/mcp-proxy.exe` 未移动或修改。
