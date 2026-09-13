# 小步执行任务卡

状态：全部待执行。路径别名 V、GUI、OM 见 README；实施前 T00 必须把它们映射到实际开发副本。本文件中的新增文件名为计划，不是当前代码存在证明。

每张卡共同规则：只读必要文件；先补能暴露问题的测试；实现后跑本卡测试；再跑受影响模块的现有回归。新增业务/安全/时间线逻辑必须有有意义的断言，不写仅验证自己常量的测试。测试命令定义在 `03-verification.md`，依赖缺失先报告，不能把“未运行”标为通过。

并行调度见[05-parallel-delivery](05-parallel-delivery.md)。下列为开发前置，模块测试通过仅local_ready；I按汇合点标passed。允许修改范围还须服从唯一文件归属。T10/T19拆A/C，T20/T21拆A/B/C；各线只执行自己的分片，不抢改其他线文件。

## T00 — 开发副本与基线

**所属线**：I。**真实验收**：G0，由I负责；跨线分片见05第5节。

**前置**：获得后续代码实施授权；本计划编写阶段不执行本卡。**产物**：仅基线记录，不改功能。

读取：两个仓库指南、现有评估报告、当前 Git 状态、GUI/Remotion package.json、Go 模块、OM requirements 与测试配置。

步骤：

1. 核实 B 盘是否对应正在运行服务的共享源目录。禁止直接在运行服务目录上连续修改生产 Python。
2. 记录 V/GUI/OM 源目录、独立开发副本、各自 HEAD、未提交改动、运行平台。保留用户改动；禁止 reset --hard、clean 或覆盖共享目录。
3. 执行宿主选择：GUI/Go 在 Windows 开发副本；OM Python/Remotion 在与生产依赖一致的测试环境。B 上的 `.venv` 如属 Linux，不在 Windows 直接启动。
4. 只读确认 Go、Node、Python、FFmpeg、Chromium 路径；记录 GUI 和 renderer 独立 node_modules，不能互相混用。
5. 跑 V-BASE、GUI-TYPE、OM-BASE，记录原有失败。缺依赖先写 blocked，不擅自升级全仓依赖。

验收：`baseline.md` 含实际路径映射、版本、测试命令、退出码；执行记录中只有 T00 passed，其余 pending。

停止：无法区分测试副本与线上目录；路径/权限未获准；无一致的 OM 运行环境。

## T01 — v2 契约与共享测试样例

**所属线**：I。**真实验收**：G0，由I负责；跨线分片见05第5节。

**开发前置**：T00。**解决**：后续跨语言字段漂移。

允许：新增 OM `schemas/remix/remix-package-v2.schema.json`、V `internal/model/remix_v2.go`、GUI `src/types/remixV2.ts`；三仓各自测试 fixture 目录。GUI package/lock 仅为新增最小测试入口改动。

步骤：

1. 把 C1–C9 转成类型、JSON Schema 和纯校验接口；此卡不接数据库、不调用 MCP。
2. 固定合法样例 three-scenes（2/5/8 秒、同图重复）；负例各一个：无源视频、负时长、重叠、重复 scene_id、丢 asset、未知 mode、未知 transition、201 镜头。
3. 正例 v1 草稿和 v2 草稿分别覆盖；draft 与 ready 校验分开。
4. GUI 若无测试入口，新增 Vitest 的最小配置和 `test:remix` script；只用与现有 Vite/Node 兼容且经官方说明核对的固定版本，写入 lock 和 baseline，不使用未固定 latest，不升级 Vue/Vite/Tauri。此版本核对是环境兼容检查，不改产品设计。
5. 新增固定 `fixtures/remix-v2/contract-cases.json`，三个语言的测试读取相同内容；副本 SHA 必须相同。实现简短一致性检查，失败即止。

新增测试：OM `tests/remix/test_contract.py`；V `internal/model/remix_v2_test.go`；GUI `src/services/__tests__/remixContract.test.ts`。

验收：正负例在 Go/Python/TS 得到相同接受/拒绝结果；数值单位均为 ms。不能仅检查 JSON 能解析。

停止：需要改变 C3 字段；依赖版本不兼容且必须升级现有框架。

## T02 — OM 素材登记与 owner/project 解析

**所属线**：A。**真实验收**：G1，由I负责；跨线分片见05第5节。

**开发前置**：T01。**解决**：F01/F06/F07。

允许：新增 `lib/remix_assets.py`；修改 `tools/asset_upload_chunk.py` 的 complete 出参/登记；必要时修改 `tools/asset_upload.py` 的对应登记；新增 `tests/remix/test_assets.py`。不改 namespace 算法。

步骤：

1. 用 ProjectWorkspace 建立每项目素材登记，不依赖当前 session 图片集合。record 原子写入；复用现有项目持久化/锁机制，避免全局共享可变字典作为唯一存储。
2. complete 校验实际 SHA/媒体属性后登记，补齐规范 AssetRef；保留原 id/relative_path/asset 返回以兼容旧客户端。
3. resolver 输入 principal/project/asset_id；检查 record、实际文件、SHA、media 类型及 read_roots。record 缺失不得按客户端任意 file_key 信任打开。
4. 支持同一用户重启后新 session 解析其本项目资源；不同用户/项目不自动复用。视频和音频也登记，不只图片。
5. 旧未登记素材仅通过显式验证后的登记步骤迁入；禁止扫描导入其他用户整个 projects 树。

验收：图片/视频都返回 AssetRef；同素材重复 complete 不新增冲突资源；用户 B 无法读取用户 A；同 id 不同 SHA 报错；symlink 逃逸拒绝；新 session 同用户可读。

命令：OM-PY 对 `test_assets.py`；现有 `test_asset_upload_chunk.py`、`test_read_session_asset.py`、`integration/test_project_workspace.py`。

## T03 — OM 分解工作区、登记和预览工具

**所属线**：A。**真实验收**：G1，由I负责；跨线分片见05第5节。

**开发前置**：T02。**解决**：F01/F10。

允许：新增 `tools/asset/remix_workspace.py`、`tools/asset/remix_asset.py`；mcp_server.py 只增加薄工具入口；registry 按现有机制发现；新增 `tests/remix/test_analysis_workspace.py`。

步骤：

1. 实现 C5 的 prepare/register/get 三个工具。prepare 要求已登记视频，生成唯一 analysis_id 和 frames/transcript 子目录。
2. analysis record 保存 source_asset_id、project_id、创建者、输出目录；路径计算全交 ProjectWorkspace。
3. register 限于该项目已允许的上传/analysis/TTS 输出；校验媒体格式与真实文件。analysis_id 和资源目录不对应即拒绝。
4. get 默认仅元数据；预览由服务端缩略图处理，保持原素材，限制尺寸/响应；向 GUI 返回可解码 preview，不返回任意外部 URL。
5. 检查现有 scene_detect/frame_sampler/transcriber 接入的项目上下文：新路径调用必须对 input/output 都做 owner/project 校验。若通用 execute_tool 不具备入口校验，增加面向 Remix 的薄包装/统一 guard，不能削弱 read_session_asset 来“修好预览”。新增包装名登记到 C5 后再由 T04 使用。

验收：两个项目同时分析，输出目录不同；新命名空间预览可读；错误 namespace 永远拒绝；两次分析同视频不互盖帧；640 像素预览有有效 mime。

停止：试图靠接受 `projects/users/artifacts` 或允许跨用户读取修复。

## T04 — GUI 分解调用与 scene_id 预览

**所属线**：C。**真实验收**：G1，由I负责；跨线分片见05第5节。

**开发前置**：T01。**解决**：F01/F10。

允许：GUI `reconstruct.ts` 的纯解析帮助函数；新增 `src/services/remixAnalysis.ts`、`__tests__/remixAnalysis.test.ts`。本卡先完成服务适配器与测试；App.vue 的真实上传结果接线由T05完成，避免依赖尚未保存的source_asset_id。不动最终生成分支。

步骤：

1. 新analyze适配器参数明确 project_id + source_asset_id；先 prepare，使用其 source_path/frames_dir/transcript_dir，不调用 resolveAssetOutputDir。本卡使用T02真实返回结构的fixture，旧UI待T05切换，不能为编译通过伪造asset_id。
2. 现有 URL 输入若保留：先在本项目 source 目录下载并登记，再走相同分析步骤；下载未成功即止。不要继续向全局 sources/_scratch 落盘。
3. 显式解包 success/data；数值用专用有限数解析，允许数字或规范数字字符串；不用 pickField 找数值。
4. 每镜头构造 scene_id 和首帧时间；注册关键帧时保留 scene_id/timestamp。抽帧缺项，显示对应镜头“预览失败”，不能让后一张图前移补位。
5. 转写保留逐 segment 时间；转写失败作为可见 warning，可继续手写字幕。均匀分段仅在用户选择或明确确认降级时使用，并保存来源说明。

验收：numeric duration=90000ms 不丢失；缺第二帧时第三帧仍属于第三镜头；多项目路径无串写；没有任何新生成路径含默认 mclaw-demo。

命令：GUI-TEST 指定 remixAnalysis；GUI-TYPE。

## T05 — GUI 素材绑定、草稿与重载

**所属线**：C。**真实验收**：G1，由I负责；跨线分片见05第5节。

**开发前置**：T04。**解决**：F06/F07。

允许：App.vue 的 pickImages/pickLocalVideo/buildRemixPackage；`services/remixPackage.ts`；相关媒体类型；新增 `__tests__/remixDraft.test.ts`。

步骤：

1. 新建项目后建立一个明确 projectId。所有上传显式传它，完成后保存 source AssetRef 和 product AssetRef。
   接线startAnalyze到T04服务，传入已保存source AssetRef；使用scene_id映射结果。T05完成后新主流程不得再用resolveAssetOutputDir或数组位置推断预览归属。
2. v2 草稿包含 source_asset_id；keep 引用视频并保存 source 区间；replace 引用图片；同图多个场景保留独立 scene_id。
3. 保存使用 CP base_version；排队串行草稿 PUT，避免 debounce 请求与最终提交竞态。请求在途时最终保存等待其完成，再使用返回 version。
4. 409 显示冲突并重新取最新版本供用户处理，不静默覆盖另一份修改。
5. 重开项目从 CP 草稿恢复素材/镜头/字幕/音频策略；预览重新按 asset_id 拉取，不能依赖失效 blob URL 和本地图片路径。
6. 旧草稿缺源视频标记 draft/待绑定，不制造 pending_source=false 假数据。

验收：同项目上传与保存 ID 一致；删除客户端源图片后已上传素材仍可选；重启恢复数据一致；并发编辑不会覆盖不可变历史。

命令：GUI-TEST remixDraft；GUI-TYPE。源文件丢失测试应区分“已上传可恢复”和“尚未上传需补图”。

## T06 — Go v2 校验与不可变版本

**所属线**：B。**真实验收**：G1，由I负责；跨线分片见05第5节。

**开发前置**：T01。**解决**：F06。

允许：`internal/handler/remix_package.go`、已有 store RemixPackage 文件、T01 model；新增/扩展 remix_package_test。不在此卡接队列。

步骤：

1. v1 维持兼容保存；v2 校验 C3 draft。明确 package schema_version 与 CP version 两个不同字段。
2. persist 保留指定版本原始规范 JSON 字符串与 hash，GET 返回 content_hash；错误类型保持稳定。
3. 新增 submit-ready 的纯校验函数：所有场景资源、时间、音频、review、容量合法。这里做结构检查；真正文件权限由 OM 二次校验。
4. 读取快照只按 requested version，不总取 latest。禁止后续草稿 PUT 修改过去版本。

验收：v1 草稿可读；v1 不能直接按 v2 渲染；v2 draft 可存但不可提交；版本 3 提交后版本 4 编辑不改变 v3 字符串/hash；跨 tenant 404/403。

命令：V-TEST handler/store 的 Remix 测试；T01 跨语言 fixture 检查。

## T07 — OM 时间线编译器

**所属线**：A。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T02。**解决**：F02/F03/F04。

允许：新增 `lib/remix_contract.py`、`lib/remix_compile.py`、`tests/remix/test_compile.py`。不启动渲染。

步骤：

1. 按 C6 验证 snapshot 原始 UTF-8 hash，parse 后执行 ready 校验；所有 AssetRef 通过 T02 resolver。
2. 编译为 renderer props：每个 scene 独立 instance_id/fromFrame/durationInFrames/src/trim 信息/type/fit/transition；source_path 由服务器填。
3. keep 的源媒体区间必须存在且不超源长；replace 验证对应参考视频区间和图片类型；不要误用图片时长作验证。
4. 时间转换严格用 C2；200 镜头内完整编译，不读取 session assets，不按 asset 去重，不取前 20 项。
5. 输出 asset_manifest、scene_plan、edit_decisions、编译统计：expected_scene_count、duration_frames、source hash、snapshot hash。按 OM 现有 schema 生成字段；参考现有 metadata.delivery_promise/renderer_family 检查，并按C10设计审核适配，不伪造批准记录。

验收：2/5/8 秒转成 60/150/240 帧，总 450；两个 replace 同 src 仍有两个 instance；24/200 项不缺；201 项明确拒绝；坏 hash、越界、未登记资源拒绝。

命令：OM-PY test_compile。必须包含非整秒边界、24/60fps 源媒体数据样例。

## T08 — RemixTimeline 画面组件

**所属线**：A。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T07。**解决**：F02/F03。

允许：新增 OM `remotion-composer/src/remix/RemixTimeline.tsx`、`types.ts`、必要的小组件；修改 Root.tsx 注册 `RemixTimeline`；新增静态 props fixtures。不改旧 Explainer/CinematicRenderer 行为。

步骤：

1. 阅读当前安装的 Remotion API、本项目 CinematicRenderer 的 trim 实现及适用 Remotion skill。复用已有资源解析和字体机制。
2. 视频场景使用正确 source trim 的视频组件；图片场景持续指定帧数。所有画面里的原媒体音轨默认 muted，音频统一交 T09。
3. 采用 Sequence 的全局 fromFrame/duration；不强制 3 秒，不从图片数量推片长。
4. 实现 C2 cut/fade 规则、contain/cover、固定背景。无参数时不加 Ken Burns 或额外动画。
5. calculateMetadata 从编译产物 duration_frames/width/height/fps 取值；传入 0/不合法即失败，不能默认生成 30 秒。

验收：3 镜头画面顺序与 boundary 截图正确；keep 两个时刻内容不同（有动态）；同图重复出现；frame 449 有画面，450 为结束；fade 不减少长度。

命令：R-TYPE、R-LIST；此卡小样预览使用 T01 fixture，不发布、不收费。

## T09 — 音频和字幕轨道

**所属线**：A。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T08。**解决**：F05。

允许：RemixTimeline 的音轨/字幕组件；remix_compile 中 audio/subtitle 映射；新增 `tests/remix/test_audio_subtitles.py` 与 renderer fixture。

步骤：

1. source 模式按每段 source 区间生成音轨，包括 replace 场景；不重复播放被 muted 视频的音频。
2. mute 模式不播放 source/TTS。tts 模式播放登记 narration，原声全静音。
3. 先 probe 音轨真实时长：长于时间线超过一帧返回 NARRATION_TOO_LONG；短音频尾部静音；不自动加速/截断。
4. 字幕逐条按 ms→frame 转换，使用 CJK 字体。time validation 早于 render；不再用 paragraph 行数制造字幕时段。
5. 字幕位置、音频模式等写入编译报告，便于人工对照。

验收：三个音频模式使用同一个 15 秒测试片；source 在 replace 时间仍有原声；tts 只有配音；mute 静音；字幕只在指定区间；长 TTS 被拒绝。

命令：OM-PY test_audio_subtitles、R-TYPE；实际 ffprobe/试听由 T11/T22 完成。

## T10 — 已有音色 TTS 准备与绑定

**所属线**：A/C。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T05,T03。**解决**：F05；明确无声音克隆。

允许：GUI `reconstruct.ts` 的 synthesizeVoice、App.vue 音频设置；可新增 `services/remixAudio.ts`；OM register_remix_asset 的音频登记补充；新增 remixAudio 测试。

步骤：

1. UI 三种策略：原声、静音、已有音色配音。只有 TTS 选择时显示现有音色列表和文案。
2. 按真实工具 schema 构造 TTS 参数，去掉额外 operation；不调用 clone_voice/list cloning samples 等接口创建资源。
3. TTS 输出明确放在当前项目音频目录；使用由 OM 提供的允许路径/登记机制，不让 GUI 拼 principal namespace。
4. 将 TTS 返回值解包，检查文件已可在 OM 访问，再登记，保存 narration_asset_id 与音色引用、文本 hash。
5. 文案、音色、engine 改变即将旧 narration 标记过期，生成前需要重新准备。重试同一已完成请求复用音频，不重复消费。
6. TTS 服务失败允许用户明确选择 source/mute；不自动换音色、不生成一段无声的“成功配音”。

验收：mock TTS 的音频 id 最终出现在 v2 快照；更新文案后禁止沿用旧音频；没有任何 clone_voice 调用；旧音色列表空时清楚报错。

命令：GUI-TEST remixAudio；授权真实 TTS 冒烟留待 T22。该卡不能要求用户先上传参考声音。

## T11 — video_compose 接入与产物验证

**所属线**：A。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T09。**解决**：F02/F04/F05。

允许：OM `tools/video/video_compose.py` 的新 family 分支与 staging；新增 `tools/video/remix_render.py`（BaseTool）；`tests/remix/test_render_adapter.py`。

步骤：

1. family 新增 reference-remix→RemixTimeline。仅这条分支消费 T07 props，保持旧 family 行为。
2. 所有视频、图片、音频及 fade 所需上一帧资源通过现有唯一 job staging；扩展 stageable 字段表，不绕过绝对路径泄漏检查。
3. 使用现有 render gate/concurrency；内部 queue owner 来自已验证 principal，不用调用者随意传入值。
4. 渲染输出在当前项目 renders/job_id 下；写 render_report，包括预期/实际片长、镜头数、音轨、完整 snapshot hash。
5. 完成后 ffprobe 校验可解码、尺寸、fps、duration，音频规则和最后镜头。失败产物不得标 ready/published。
6. 24 镜头直接传完整 timeline，一次渲染。首版不实现 session 分页或自动多片拼接，避免两套时序算法。

验收：本地 fixtures 生成 3 镜头和 24 镜头完整成片；最后镜头实际可见。旧 Remotion 回归不受影响。

命令：OM-PY test_render_adapter + 已有 video_compose 相关回归；R-TYPE；媒体命令 MEDIA-PROBE。不以 JSON 列表长度代替实际视频检查。

## T12 — OM 持久幂等作业与发布重试

**所属线**：A。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T11。**解决**：F08/F11。

允许：新增 `lib/remix_jobs.py`、`tools/video/remix_job.py`（BaseTool）；mcp_server.py 薄工具注册；`tests/remix/test_jobs.py`。复用 lib/render_queue.py 接口，避免大改旧 session workflow。

步骤：

1. 实现 submit/status/retry-publish，持久保存 owner/project/request/hash/status/artifact。状态按 C7。
2. 原子写幂等索引与作业；同请求重试返回原 job。多进程竞争测试不能只测一个内存锁。
3. 队列任务仅执行已验证时间线合成和发布。实际 worker thread/context 恢复 principal 后再访问资源；禁止靠无签名 uid 字符串伪造权限。
4. 复用现有发布工具；发布失败保留 render artifact；retry 不再触发 render。published 取真实分享链接。
5. 重启恢复：扫描本作业记录，已完成报告可恢复 rendered；不确定运行中状态标 INTERRUPTED 或重新领取有租约的任务，不能无限 running。
6. status/retry 都独立校验 owner，当前新 session 同用户可查；队列 metadata 不将真实 token 写盘。

验收：重复 submit 一次渲染；响应丢失后相同 request_id 得同 job；异 hash 冲突；进程重启可查；另一用户 job 不可见；发布重试 render 调用次数仍为 1。

命令：OM-PY test_jobs + 既有 test_render_queue.py。真实发布不在单元测试进行。

## T13 — Go→OM 用户身份和 MCP 客户端

**所属线**：B。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T01。**解决**：F09/F08。

允许：`internal/handler/mcp_proxy.go` 仅提取签名帮助函数；新增 `internal/mcpauth/assertion.go`；扩展现有 `internal/openmontage` client；对应协议测试。不改签名格式或放宽 PrincipalAuth。

步骤：

1. 提取 upstreamAssertionPath/signVClawUserAssertion 为可复用内部包，proxy 仍按原测试行为工作。
2. 新方法为指定 actor_user_id 初始化独立 MCP session，并在后续请求发送同一用户断言/session；body 序列化完成后签名同一字节串。
3. 用户连接缓存 key 必须含用户/目标端点；不能把一个用户的 session 给另一用户复用。身份值只来自 CP 持久作业，不接受 GUI 自报 owner。
4. 实现 C5 submit/status/retry 的 typed 调用，解析 JSON/SSE、RPC error、success=false；长渲染通过异步 job，不让请求等待整个渲染。
5. 不向浏览器暴露上游 token/断言 secret；日志只写脱敏 session 摘要与 correlation ID。

验收：两用户各自 session；签名错误/漏字段拒绝；合法身份可读取自己此前 GUI 上传素材；超时后不改为 service principal；旧 mcp_proxy 测试全部通过。

命令：V-TEST mcpauth/openmontage/handler；上游用 httptest fake，不用线上用户目录做越权试验。

## T14 — Go 原子受理与任务查询 API

**所属线**：B。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T06,T13。**解决**：F06/F08。

允许：`internal/handler/remix_package.go`、`cmd/server/main.go`、model/store 任务扩展；新增 `internal/store/store_remix_jobs.go` 与增量 migration；新增 `internal/handler/remix_jobs.go`。

步骤：

1. 检查实际 production_jobs schema，增加 actor/version/hash/upstream_job 等持久字段或专用 remix metadata 表。使用显式增量迁移，不删除旧表。
2. 新 POST render 从指定版本取快照，ready 校验；事务创建 job + outbox，幂等 unique 键按 C6。JSON 网络响应为 202。
   同时按C10绑定本次已认证用户确认的版本/hash/范围；不接受客户端自报另一个actor或全局human_approved。
3. 查询 API 使用 C7 统一状态；以 job 所属 project/tenant/actor 权限校验。不会因为 URL projectId 相同就放行任意 jobId。
4. retry-publish 仅对 render_ready 且当前用户有权限的作业入队。请求幂等。
5. 首版 direct/assisted 都创建真实 CP job；实际下发留 T15/T17。无配置不得返回假 run_id。

验收：两个并发同键请求只产生一 job/一 outbox；同键异版本409；事务失败两条记录都不残留；跨用户不可查；数据库副本迁移前后旧项目可读。

命令：V-TEST store/handler。只对临时测试 DB 迁移；真实 controlplane.db 不在本卡触碰。

## T15 — worker 下发、状态同步与恢复

**所属线**：B。**真实验收**：G2，由I负责；跨线分片见05第5节。

**开发前置**：T14。**解决**：F08/F11。

允许：`cmd/worker/main.go` 新 remix job_type 分支；新增 `internal/handler/remix_worker.go` 或已有 worker 包等价文件；store 的领取租约/重试操作；测试。

步骤：

1. 从 outbox 原子领取，加载 actor + 精确版本原始快照；通过 T13 发给 OM，用 CP job_id 作为 request_id。
2. 先持久保存 OM job_id，再将后续工作变成轮询任务；不要占住一次长 HTTP 请求直到视频生成完。
3. 查询状态落 CP；状态迁移单向（发布重试例外）；网络错误保留上次有效状态，记录可重试错误，不伪造失败成片。
4. 领取要有 lease_until 与重领策略；指数退避、最大连续尝试、下一次时间可见。旧任务类型保持原逻辑。
5. 在“OM 已受理，CP 未保存响应”崩溃点，用同 request_id 对账；不能创建第二次渲染。
6. 接入已有计费接口时复用 production_job 作为结算幂等实体；轮询/发布重试不二次预留费用。无既有规则可对应时停止交协调者，禁止编造费用逻辑。

验收：超时、丢响应、worker kill 后恢复都不重复渲染；terminal 状态停止轮询；publish failed 保留 artifact；没有 sim 前缀 run_id。

命令：V-TEST worker/handler/store 对应单元与集成测试。

## T16 — GUI 正式生成、恢复与最终链接

**所属线**：C。**真实验收**：G3，由I负责；跨线分片见05第5节。

**开发前置**：T05,T10。**解决**：F02/F07/F08。

允许：App.vue generateFinal/项目载入；remixPackage.ts；新增 `services/remixJobs.ts`、状态展示小组件、测试。不移除独立照片视频页面或旧工具。

步骤：

1. 用户确认后等待草稿 PUT 完成，保存最终 v2，POST render(package_version,idempotency_key)。此分支不再 reset client→重新逐张 upload→create_remotion_video_share。
   确认界面包含C10要求的范围和版本；用户未确认不得只改review.status后自动提交。
2. 一次用户提交动作生成一次 key，网络重试复用；编辑新快照后的新动作生成新 key。
3. 保存当前 project/job 选择，按认证用户隔离；启动时 GET job 恢复进度，不能跨用户恢复前一个人的 UI 结果。
4. 只按 C7 状态展示；超时显示“仍处理中，可稍后查看”，保留任务 ID。published 直接展示 share_url，不调用 shareViaWeiyun 二次上传。
5. failed + render_ready 提供“重试发布”；failed 渲染显示错误镜头/原因；渲染成功但无公开链接必须有受控下载或明确发布状态。

验收：mock 12 分钟未完成不会显示成功；published 只一次发布；关闭重开能继续；已上传用户图不会因本地文件移走而回退成参考首帧。

命令：GUI-TEST remixJobs/generateFinal；GUI-TYPE。模拟 API call count 必须断言旧生成工具调用次数为 0。

## T17 — OpenClaw 辅助模式闭环

**所属线**：B。**真实验收**：G4，由I负责；跨线分片见05第5节。

**开发前置**：T15。**解决**：F08/F06。

允许：V `internal/openclaw/client.go`、remix worker；solution `product-video-production/mcp/stdio-mcp.mjs` 及相关只读快照工具；对应 gateway handler。先读这些目录适用指南。

步骤：

1. assisted 与 direct 共用 CP job。prompt 提供 project/job/version/hash 和受权限控制的快照读取方式；不能只说“渲染 version 3”却不给读取接口。
2. OpenClaw 可以协助检查已确认契约；不能重新规划、换素材/音色或改变 timeline。生成缺项返回 UNRESOLVED_SCENE，由用户补齐。
3. 将最终已验证 snapshot 交给相同 OM submit 能力；避免 OpenClaw 以全局 service identity 直接读取用户素材。推荐通过 CP 已授权 job 调用，让 worker 按 actor 下发。
4. run_id、CP job_id、OM job_id 分开保存；OpenAI-compatible response id 只表示 agent 调用身份，不作为成片完成证据。
5. agent 回调/查询必须验证 gateway 身份和 job 归属，完成要带可验证 OM job/artifact；返回文字“完成”不得变成 published。

验收：fake agent 只拿快照指定版本；返回聊天成功但无产物仍未完成；错误版本/hash拒绝；无 runtime 配置明确不可用；GUI 最终同样收到真实 share_url。

停止：现有 agent runtime 无法安全读取快照或关联任务，记录缺口，禁止在 README 宣称 assisted 完成。direct 不受其阻塞可单独验收，但整计划未完成。

## T18 — MCP 初始化、刷新与有界超时

**所属线**：C。**真实验收**：G3，由I负责；跨线分片见05第5节。

**开发前置**：T01。**解决**：F11。

允许：GUI mcpClient.ts/backendMcp.ts/montage.ts 的 client 管理；auth.ts 刷新适配；Tauri commands_mcp.rs/http_client.rs；对应测试。

步骤：

1. 生产 Tauri MCP 统一到现有 mcp_rpc command；Web 测试使用注入的 fetch transport。不要边用 plugin-http 边假定 Rust allowlist 在起作用。
2. ensureInit 使用共享 Promise；失败清掉 Promise，成功通知只发一次；并发 tools/call 共用初始化结果。
3. 401 刷新使用 single-flight；后续请求获取最新 access token，不缓存旧 token。401 重试最多一次且只对明确未执行的鉴权失败；普通网络超时不盲目重试生产工具。
4. 请求超时按固定 operation 分类：元数据30s、chunk60s、分解/TTS至多900s；render submit30s后查幂等状态。Rust 不能仍被全局30s截断长请求，也不接受客户端随意无限超时。
5. JSON/SSE 测试覆盖分段 data、多行、心跳、notification、按 JSON-RPC id 匹配响应，禁止取最后一个 data 就当结果。
6. 同项目可用明确 workflow session；退出登录清空 session/cache。新登录用户不得复用旧 session。

验收：10 个并发调用仅一次 initialize；两次401终止并要求登录；TTS不被30秒截断；超时返回可恢复错误；域名 allowlist 没被放宽。

命令：GUI-TEST mcpClient；RUST-TEST 对 MCP/http_client；GUI-TYPE。

## T19 — 上传幂等状态、续传与安全重试

**所属线**：A/C。**真实验收**：G4，由I负责；跨线分片见05第5节。

**开发前置**：T02,T18。**解决**：F11。

允许：OM asset_upload_chunk.py / mcp_server.py 的上传 operation enum；GUI montage.ts 上传状态；必要时 commands_upload.rs；相应测试。

步骤：

1. 按 C9 实现 status、append 重发对账、重复 complete；保留已完成上传元数据至少当前配置 TTL，不能 complete 后立刻删除所有幂等证据。
2. GUI 从服务端 offset 前进；append 超时先 status，不猜测服务器是否写入。文件 size/SHA变化立即停止本次 upload。
3. 同会话短时断网可继续；应用重启后 session 不持久化，重新 start，已完成素材按 hash 复用。界面明确“重新上传”与“继续上传”。
4. 上传失败保持镜头选择但标资源未就绪，不能进入 render-ready。
5. 清理过期上传只清用户项目下已确认无活跃 lease 的临时目录；不清真实原始素材，不递归清整个 projects。

验收：丢 append 响应不产生字节重复；错 offset 拒绝；重复 complete 同 asset；另一 session/用户 status 拒绝；重启后不向旧 upload_id 强行 append。

命令：OM-PY test_asset_upload_chunk 与新增 `tests/remix/test_upload_recovery.py`；GUI-TEST uploadRecovery；必要时 RUST-TEST upload。

## T20 — 不完整输入与兼容防护

**所属线**：A/B/C。**真实验收**：G4，由I负责；跨线分片见05第5节。

**开发前置**：T16,T19。**解决**：F07/F10，防止错误成功。

允许：GUI ready validator/UI 文案；Go/OM 同步验证漏项；v1 显式迁移适配；对应测试。不能顺带开发新生成模型。

步骤：

1. 缺图、空 prompt 的旧 generate、关键帧失败、source asset未绑定分别给出具体 scene_id 的问题。
2. 禁止 card.preview 作为用户替换图的静默 fallback；已上传图优先 server asset，真正丢失则需要用户补齐。
3. v1 迁移仅提取可靠字段；缺区间/原片/音轨策略的项仍 draft；显示需确认项，再保存为 v2 新版本。
4. 未知原片转场/烧录字幕/超600秒等边界显式显示，不把删镜头、截断、改静音作为自动修复。
5. direct/assisted 任一路径失败均不得回退照片电影工具；独立照片视频功能如保留，入口明确区分。

验收：所有失败都有确定提示与未解决数量；24镜头含1个缺图不能生成23镜头后宣告成功；旧草稿不会在页面打开时自动创建新版本。

命令：GUI-TEST ready/legacy；V-TEST 和 OM-PY 对应 ready 负例。

## T21 — 身份、查询权限和可诊断日志

**所属线**：A/B/C。**真实验收**：G4，由I负责；跨线分片见05第5节。

**开发前置**：T12,T15,T18。**解决**：F09/F11。

允许：V config/principal 的部署环境防护与测试；新 job 路由权限；OM remix status/retry日志；health 计数清理；部署示例文档。真实 .env 不改。

步骤：

1. 按C11落实环境判别和功能开关：生产配置遇到非空dev-principal绕过时拒绝启动；测试模式保持显式。不猜测域名就切生产模式，也不在本卡改真实.env。
2. 检查所有新入口：asset get/register、submit/status/retry、CP快照读取、agent callback都有 owner/project控制。完成产物下载入口同样受控。
3. 每个任务日志携带 request_id、CP job、OM job、project、phase、duration；不打印token、断言、完整session、base64和用户文案全量。
4. 工具 pending 在 finally 清理，区分等待、执行、超时；heartbeat=ok 不代表业务无挂起，提供 oldest_pending_age/phase。
5. 两用户隔离自动测试；仅测试自己创建的隔离 fixture，禁止枚举线上他人目录/任务。

验收：生产fixture无凭证401；已登录缺scope403；同tenant不同actor按已定义授权策略验证；猜jobId无产物泄露；pending异常后归零；日志无秘密字段。

命令：V-TEST middleware/handler；OM-PY jobs/assets/health；日志脱敏fixture断言。

## T22 — 完整集成与真实 GUI 验收

**所属线**：I。**真实验收**：G5，由I负责；跨线分片见05第5节。

**开发前置**：G4通过。**解决**：将“单元通过”升级为业务证据。

允许：新增测试/fixture/验收记录，不改业务逻辑。发现缺陷返回对应任务，不在此卡补大型实现。

步骤：

1. 按 03-verification 的 A01–A14 全部执行；先 mock/本地媒体，再测试环境真实 Tauri GUI，最后授权情况下公网真实用户路径。
2. 素材采用自制2/5/8秒动态短片、产品图和24镜头短片；记录SHA。生成测试视频是后续验收行为，遵守项目相关视频工具规则；本计划编写时不生成。
3. 必须通过 GUI 文件对话框/实际已支持交互完成一次图片+视频上传，不只用脚本直接调用后端。
4. 比对 final 与 source 关键帧、时长、keep动态、用户替换、音轨、字幕、末镜头；仅 ffprobe 正常不代表用户图片正确。
5. 保留每个阶段版本号和日志，说明公网测试是否关闭开发身份。无法完成真实登录则生产身份验收 blocked，不能借dev模式标通过。

验收：`acceptance-results.md` 每项有证据、实际/预期/状态；无声音克隆调用；无缺镜头；direct及assisted分别记录。

## T23 — 发布与回滚计划执行准备

**所属线**：I。**真实验收**：G5后发布准备/授权发布，由I负责；跨线分片见05第5节。

**开发前置**：T22 passed。**产物**：可审核发布包、迁移/回滚步骤；部署只在另有明确授权时执行。

允许：版本清单、迁移脚本、部署说明、测试记录。不要直接覆盖 B 上运行代码。

步骤：

1. 记录三项版本：Go server/worker、OM code+schema、GUI包+renderer。记录依赖锁与实际运行配置摘要，不含秘密。
2. 兼容发布顺序：OM新增能力（旧API保留）→Go迁移及server/worker→GUI v2启用。任一步失败不让GUI退回错误的legacy重构。
3. 迁移先备份并在DB副本测试；发布期间防止旧worker领取未知job_type；新功能flag默认关闭，全部就绪后启用。
4. 回滚先停新任务受理，允许已有任务完成或明确中断，保留job/asset/snapshot；回滚二进制前确认旧代码能读增量schema，不能DROP列或删除v2数据。
5. 线上结束开发绕过必须协调真实登录验证窗口，不在未准备登录时临时改env把全部用户锁出。
6. 发布后执行简化A01/A02/A05/A11验收；不启动全量昂贵重渲染。

验收：发布步骤每条有宿主、目录、命令、预期、回滚对应项；代码实现与生产部署完成状态分开记录。
