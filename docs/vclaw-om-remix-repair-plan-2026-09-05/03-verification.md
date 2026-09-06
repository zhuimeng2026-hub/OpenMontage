# 检查命令、验收样例与发布清单

并行验收按[05第4节](05-parallel-delivery.md)的G0–G5汇合点执行。A/B/C的模块测试只支持local_ready；I核对跨仓库版本组合、契约和fixture后运行真实接口/GUI验收，才能标passed。mock不得充当真实接口证据；共享OM/Chromium/GPU测试由I排队。下列所有原验收项继续有效。

本文命令用于未来实施，不表示本轮已经运行。新增测试文件由对应任务创建，文件尚不存在时不能写“测试通过”。

## 1. 先固定真实执行环境

T00 创建 `baseline.md`，至少记录：

```text
V_SRC=C:\vclaw
GUI_SRC=C:\vclaw\openclaw\clawx-studio
OM_SRC=B:\
V_DEV=实际已确认的Go开发副本绝对路径
GUI_DEV=该副本内clawx-studio绝对路径
OM_DEV=实际已确认的OM测试副本绝对路径
OM_TEST_HOST=本机Windows或具名Linux测试主机
OM_PYTHON=该测试环境真实Python绝对路径
OM_RENDERER=该OM副本下remotion-composer
FFMPEG/FFPROBE/CHROMIUM=实际可执行路径
V_HEAD/OM_HEAD/GUI_LOCK_HASH/RENDERER_LOCK_HASH=读取结果
GUI_TEST_RUNNER_VERSION=实际固定版本
```

这些占位必须换成已验证路径，之后各任务复制 baseline 的**完整命令**。不得拿 Windows 的 B:\ 当 Linux 工作目录；不得把生产 `.venv` 当开发环境；不通过改系统 HOME/CODEX_HOME 等变量迁就测试。

所有会写测试输出的命令只能在 T00 开发副本/测试目录运行。基线与功能验收分开记录；原有失败可以标 baseline_failure，但新功能要求仍需满足。

## 2. 命令代号

### V-BASE / V-TEST

PowerShell，在实际 V_DEV 根执行：

```powershell
go test ./internal/handler ./internal/store ./internal/openclaw ./internal/openmontage
```

任务指定包后跑其测试；新增包由对应任务创建。例：

```powershell
go test ./internal/handler ./internal/store -run Remix -count=1
go test ./internal/mcpauth ./internal/openmontage -count=1
go test ./cmd/worker ./internal/handler ./internal/store -count=1
```

并发/幂等变更在支持 race detector 的环境补 `go test -race`；Windows缺C工具链时记录限制，在Linux测试副本执行，不把不支持race算功能通过。只有新增包准备就绪后，最终运行 `go test ./...`。

### GUI-TYPE / GUI-TEST

PowerShell，在实际 GUI_DEV 执行：

```powershell
& '.\node_modules\.bin\vue-tsc.cmd' --noEmit
```

T01 创建 `test:remix` 脚本，语义固定为 `vitest run`，限定到 Remix/MCP相关测试目录；不扫描所有第三方包。单卡示例：

```powershell
npm run test:remix -- src/services/__tests__/remixDraft.test.ts
```

全量新流程：

```powershell
npm run test:remix
```

使用明确 mock/injected transport，单元测试禁止碰公网、调用TTS/渲染或读取真实账户keyring。T01 的fixtures一致性检查可作为 test:remix 一项，并在Go/Python测试加载同份版本号。

### OM-BASE / OM-PY

在实际 OM_DEV，使用 baseline 中确定的 Python，不裸用可能指向系统Python的命令。Linux说明模板：

```bash
"$OM_TEST_PYTHON" -m pytest -q tests/test_asset_upload_chunk.py tests/test_read_session_asset.py tests/integration/test_project_workspace.py tests/test_render_queue.py
```

这里 OM_TEST_PYTHON 须由 T00 固定为真实绝对路径，且 OM_TEST_HOST 已确定。Windows等价使用 `& $OmTestPython -m pytest ...`。命令记录必须展示实际替换后的路径，不把占位变量当证据。

新增测试逐卡：

```text
T01 tests/remix/test_contract.py
T02 tests/remix/test_assets.py
T03 tests/remix/test_analysis_workspace.py
T07 tests/remix/test_compile.py
T09 tests/remix/test_audio_subtitles.py
T11 tests/remix/test_render_adapter.py
T12 tests/remix/test_jobs.py
T19 tests/remix/test_upload_recovery.py
```

对应命令为固定Python `-m pytest -q <文件>`。现有回归按影响选取：

- 上传：tests/test_asset_upload_chunk.py、tests/test_session_asset_concurrency.py。
- 资源权限：tests/test_read_session_asset.py、tests/integration/test_namespace_version.py、tests/integration/test_project_workspace.py。
- 抽帧：tests/regression/test_frame_sampler_workspace_guard.py。
- 旧图视频：tests/test_workbuddy_session_remotion_share.py、tests/test_resolve_session_asset_path.py。
- 队列：tests/test_render_queue.py、tests/test_session_context_thread_propagation.py。
- 合成：tests/test_video_compose_remotion_progress.py、tests/regression/test_resolve_compose_target_cascade.py。

某测试文件因源码版本移动，T00记录替代路径；不能删除测试要求。pytest退出码5“没收集到测试”不是通过。

### R-TYPE / R-LIST

在 OM_DEV 下 remotion-composer，使用已有node_modules：

```text
npx --no-install tsc --noEmit
npx --no-install remotion compositions src/index.tsx
```

R-LIST预期包含新 `RemixTimeline` 和所有原 compositions。禁止为了新增composition把旧注册项删掉。缺依赖不得由 npx 自动下载latest。

### RUST-TEST

在实际 GUI_DEV/src-tauri：

```powershell
cargo test commands_mcp
cargo test http_client
cargo test commands_upload
```

仅跑本卡受影响项；验证构建使用已有开发工具链。若测试过滤名与源码不匹配，先列出真实测试名，禁止零测试当通过。

### MEDIA-PROBE

对实际产物路径运行：

```text
ffprobe -v error -show_entries format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,duration -of json FINAL_PATH
```

FINAL_PATH须使用工具返回且验证存在的路径。不要把 CP file_key 直接当作Windows本地文件。保存 JSON 为验收证据。

阈值：video frame count 应与 duration_frames一致；容器duration受AAC尾部padding影响，允许与预期相差 `max(2/fps,0.10秒)`，超出需解释，不能扩大阈值消除失败。视频缺尾镜头即使总duration合格仍失败。

画面检查优先稳定时间点，远离转场边界；自动截图比较允许编码误差，但不得用过宽像素差阈值代替人工核对。mute 可接受无音轨；若有音轨，需音量分析确认静音，不能仅看codec存在。

## 3. 固定媒体样例

这些是未来测试要创建的fixture，**本轮未生成**。

| 名称 | 内容 | 用途 |
|---|---|---|
| source-15s.mp4 | 0–2s红色动态计时；2–7s绿色动态计时；7–15s蓝色动态计时；有连续参考音频 | 明确检测keep运动、源入出点、总时长 |
| product-A.png | 自制醒目“A”产品图；四角有标记 | 替换正确性、contain不裁图 |
| product-B.jpg | 与A外观明显不同的“B”图 | 检测错素材、缓存串图 |
| source-24shots.mp4 | 24个各1秒的不同编号镜头；末镜头写END-24 | 检测原20图上限截断 |
| narration-short.wav | 自制12秒音频，无第三方声音 | 15秒片尾补静音 |
| narration-long.wav | 自制16秒音频 | NARRATION_TOO_LONG |
| source-24fps.mp4 / source-60fps.mp4 | 带时间标记的不同fps源片 | 精确seek与输出30fps转换 |

图片/视频生成只用本地、可重复、无敏感内容的工具；为fixture生成与处理读取项目相应技能，记录参数和SHA256。不要下载陌生用户视频，也不需要购买素材。

## 4. 端到端验收矩阵

| ID | 执行 | 必须断言 | 对应卡 |
|---|---|---|---|
| A01 | 用户A登录，GUI新建项目，选图+本地MP4 | project_id不变；两文件SHA与OM一致；MCP start/append/complete完成；源视频有AssetRef | T02–T05 |
| A02 | GUI开始分解，拉预览和字幕 | 3镜头时间正确；预览可见；scene_id对应；输出位于A项目命名空间；未向users/artifacts写入 | T03–T04 |
| A03 | s1 keep，s2/s3均replace同一A图 | 3实例、450帧；s1运动保留；2–7和7–15秒都是A图；无镜头折叠 | T07–T11 |
| A04 | 24镜头全量输出 | 24实例；24秒；最后END-24可见；不能只返回20秒；201镜头明确拒绝而非截断 | T07,T11 |
| A05 | source/mute/tts分别渲染 | 原声正确裁剪；mute静音；tts无双音轨叠声；短配音补静音；长配音被拒绝；不调用克隆接口 | T09–T10 |
| A06 | 多行字幕、编辑文本、添加新cue | 时间戳保留；中文可读；无重叠越界；0.1–1.8秒字幕在2秒以后不可见 | T09,T20 |
| A07 | 保存v3，提交后继续改为v4 | 此job仍渲染v3；hash一致；v4只影响下次提交 | T06,T14 |
| A08 | 重复点击/重复相同key/丢submit响应 | 一个CP job、一个OM job、一次render；异快照同key冲突 | T12–T16 |
| A09 | 人工延长排队超过10分钟，重开GUI | 不显示已完成；重开恢复同job；没有第二次submit | T15–T16 |
| A10 | 发布失败后点重试 | render_ready保留；重试仅发布；render调用次数保持1；最终share_url来自OM | T12,T16 |
| A11 | 用户B尝试A的asset/session/job | 全拒绝；B新任务只读B文件；无凭据生产请求401；日志无token/原始session | T13,T21 |
| A12 | 丢append响应、断网恢复、应用重启 | 同session按offset续传；新session重新start；原文件SHA变化拒绝；无重复字节 | T18–T19 |
| A13 | assisted模式提交 | CP job/run_id/OM job关联；准确快照；未重新规划；最终成片回GUI；仅agent文字回复不算成功 | T17 |
| A14 | 未上传的替换图缺失、缺source、未知transition、旧v1草稿 | 明确待修项；没有参考首帧兜底；没有静默删镜头；没有legacy照片视频兜底 | T20 |

对真实Tauri GUI至少执行A01/A02/A03/A05/A09；Web mock不能代替桌面文件选择和Rust传输验证。A11先在隔离测试账号上执行，生产验证只用明确允许的两个测试账号。

网络证据分三层：

1. 单元mock：验证协议与错误逻辑。
2. 测试环境真实Go+OM：验证素材、身份签名与输出。
3. 指定公网ds地址+真实GUI：验证Nginx/CDN/打包配置。

任何一层未执行，记录 not_run/blocked。测试环境dev-principal成功不能替代第三层的真实JWT验收。

## 5. 每项验收记录格式

```text
验收ID：Axx
环境/版本：
执行时间：
操作者/测试用户代号：
project_id / CP job_id / OM job_id：
package_version / snapshot_sha256：
输入素材SHA：
操作：
预期：
实际：
证据：日志片段、请求摘要、ffprobe JSON、关键帧文件、试听结果
结论：passed / failed / blocked / not_run
关联修复任务：
```

只保留用户代号，不写真实openid、token、签名、Cookie或完整session。状态published只证明发布步骤；媒体内容仍由A03–A06断言。

## 6. 发布前必须齐备

- T00–T22完成记录；A01–A14结果，无以“待观察”包装的失败。
- 当前Go/OM/GUI/renderer版本和依赖锁，正式构建真实API地址，关闭开发跳过登录的计划。
- 增量DB迁移在副本成功，旧v1读取兼容，新增job_type不被旧worker误领。
- OM新工具/旧工具schema对照；新GUI不会在缺能力时偷偷使用legacy重构。
- pipeline/governance审核记录的正确接入；不得为了验收篡改human_approved常量。
- 发布和回滚范围已形成具体可审结果；未有部署授权时，状态保持ready_for_deployment，不改线上。

最后判定区分：代码修复完成、集成测试完成、生产发布完成。三者不是同一个状态。
