# VClaw → OpenMontage 上传、分解与用户图片重构评估

日期：2026-09-05。范围：`C:\vclaw` GUI / Go 控制面、`B:\` OM 源码、`B:\logs`、公网 `https://ds.aixifs.com/api/mcp/proxy`。VClaw HEAD：`8d04cfb`；结论以当前工作区内容为准，未要求工作区无未提交变更。

## 1. 结论

**当前不能认定“模拟普通用户上传图片和视频，经 VClaw 转发到 OM，分解后按用户图片重构，最终拿到正确成片”可以完整、可靠完成。**

- **基础能力存在**：GUI 文件选择、分块上传、MCP 代理、OM 镜头检测 / 抽帧 / 转写、图片视频渲染、发布与查询均有实现。
- **通信入口可达**：本次实际通过指定公网地址完成 initialize 与 tools/list，返回 OpenMontage / 34 个工具；同时在 B 盘 OM 日志找到对应请求。
- **默认重构实现不完整**：实际采用固定每图 3 秒的 Ken Burns 图片视频；“保留镜头”取首帧，原动态、原镜头时长及原音轨未进入最终合成。
- **存在确定性断点**：用户命名空间抽帧路径错误、重复图片导致镜头折叠、超过图片上限只生成首段、配音结果未接入合成、源视频未完整绑定到正式 Remix 包。
- **上线条件不足**：公网仍呈现临时开发身份模式；本次连通性不能证明真实微信 JWT、多用户隔离和完整任务恢复已经验收。

可完成性分层：

| 目标 | 当前判定 |
|---|---|
| 调通公网 MCP，读取工具清单 | 本次已验证 |
| 小图片 start / append / complete 上传 | 有近期日志证据；本次未重新上传 |
| GUI 上传真实视频并展示分解结果 | 实现存在，但新命名空间路径使预览读取形成明确断点 |
| 少量、互不重复的图片生成简单照片视频 | OM 有历史成功证据；当前 GUI 完整回路仍待验收 |
| 保持参考视频结构、动态与音频，仅替换指定图片 | 默认 GUI 路径不满足 |
| 正式 OpenClaw 辅助重构并自动回传成片 | 提交入口存在；源素材契约及 GUI 完成状态未闭环 |

## 2. 本次做了什么、没有证明什么

本次以代码和日志分析为主，未修改业务代码，未部署、重启服务、调用收费生成工具或发布新视频。

实际执行：

1. 读取项目指南、GUI / Go / OM 相关实现、部署交接文档与运行日志。B 盘在普通受限进程不可见，使用已获准的提升权限读取；B 盘对应 `\\192.168.20.173\voicebox`。
2. 无 Authorization 的公网 initialize、tools/list：均 HTTP 200；initialize 返回 Session 头；tools/list 返回 34 个工具。只创建协议会话，不上传媒体。
3. 对 `/api/studio/video-projects` 提交必定解析失败的 JSON `{`：HTTP 400、JSON `invalid body`。确认路由可到业务校验层，未创建项目；不等同于数据库创建成功。
4. `go test ./internal/handler ./internal/store ./internal/openclaw`：handler 通过（缓存），store 通过，openclaw 无测试文件。
5. GUI `vue-tsc --noEmit`：通过。未做 Tauri GUI 操作、完整打包或真实媒体端到端测试。
6. 提取当前 `reconstruct.ts` 的实际函数，经已安装 TypeScript 转译后独立执行：复现命名空间目录截断和数值字段丢失。

未证明：当前已安装 GUI 二进制等于所审源码；运行服务载入了磁盘上全部最新代码；真实微信登录正常；任意尺寸的视频上传可靠；当前用户图重构产物视听质量合格。线上 schema 与源码重点工具一致，是局部证据，不是完整版本一致证明。

## 3. 实际链路

```text
GUI 选产品图 / 本地视频
  → ensureVclawProject
  → POST ds.aixifs.com/api/studio/video-projects
  → montage.ts：MCP initialize + upload_asset_chunk(start/append/complete)
  → POST ds.aixifs.com/api/mcp/proxy
  → Go PrincipalAuth / scope / session owner bind / 用户签名断言
  → OM /mcp → upload_asset_chunk → 用户命名空间落盘

GUI 开始拆解
  → reconstruct.ts：execute_tool(scene_detect)
  → execute_tool(frame_sampler)
  → execute_tool(transcriber)
  → read_session_asset → GUI 分镜预览和字幕文案

GUI 生成：默认 direct
  → PUT remix-package 保存草稿快照
  → 重置两套 MCP client
  → keep 首帧 / replace 产品图 / generate 新图，逐张重新上传
  → 可选 voicebox_tts（返回值未用于合成）
  → create_remotion_video_share(photo-ken-burns, duration_per_image=3)
  → OM 后台 render → 字幕 → 微云发布
  → get_render_status
  → GUI 再次调用微云上传 / 分享

GUI 生成：openclaw_assisted
  → PUT remix-package
  → POST /api/studio/video-projects/:id/render
  → Go StartRun → OpenClaw
  → GUI 显示受理后 return，未在此流程追踪完成
```

指定 `/api/mcp/proxy` 接收的是 JSON-RPC，不是 multipart 表单。文件字节通过 `chunk_base64` 分块传送；GUI 当前每块 1 MiB，base64 后约 1.4 MiB；Go 单请求上限 4 MiB，二者匹配。外层 Nginx / CDN 的限制仍需真实大文件验收。

配置证据：GUI `.env` 和 `.env.production` 的 API 基址均指向 `ds.aixifs.com`，MCP 均指向指定 proxy；开发配置登录模式为 `skip`，production 配置为 `normal`。这证明构建输入，不能替代已安装应用的运行配置核查。

## 4. 缺失环节与优先级

P0：阻断主流程或生产上线。P1：可能产出错误 / 不完整成片。P2：恢复性与诊断不足。

### F01 — P0：新用户命名空间与抽帧目录不兼容【源码函数已复现】

位置：`C:\vclaw\openclaw\clawx-studio\src\services\reconstruct.ts:82`；`B:\tools\asset\read_session_asset.py:61`；`B:\tools\analysis\frame_sampler.py:51`。

`resolveAssetOutputDir` 只截取 `projects/` 后第一个目录，仍假定它就是 project_id。实际上传路径已有用户 / 服务命名空间。

本次执行当前函数得到：

```text
输入 projects/users/namespace123/project456/assets/_sessions/session789/video.mp4
输出 projects/users/artifacts/keyframes

输入 projects/services/namespace123/project456/assets/_sessions/session789/video.mp4
输出 projects/services/artifacts/keyframes
```

输出丢失 namespace 和 project_id。OM frame_sampler 的目录守卫只检查位于 projects 下，允许该错误路径；read_session_asset 则独立检查当前 principal 命名空间，拒绝读取。GUI `resolveAssetUrl` 捕获异常返回空字符串，表现为分解成功但卡片无图。相同输出目录和 `frame_0000.jpg` 命名还带来不同任务互相覆盖风险。

修复：由 OM 按 principal + project_id 返回规范工作区 / 关键帧资源引用；分析输出统一写入真实用户项目目录。不要由 GUI 猜测命名空间。URL 下载路径的 `_scratch` 输出也要使用同样的 principal 规则。

### F02 — P1：默认生成的是照片视频，不是参考视频时间线重构【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\App.vue:549`、`:601`；`B:\mcp_server.py:1742`；`B:\pipeline_defs\video-template-remix.yaml`。

生成循环对 keep 使用 `card.preview`；对 replace 使用产品图片；渲染参数固定 `photo-ken-burns`、每图 3 秒。未提交源视频片段、source_range 或每镜头时长。OM 据图片列表重新创建 cuts。

例：原片 5 秒、12 秒、8 秒三个镜头，当前输出变成约 3+3+3 秒图片段；keep 镜头的运动消失。与 OM Remix manifest 中保留 shot_durations、pacing、transitions、source_audio 的目标不一致。该 pipeline 存在，不代表 GUI direct 路径已经执行它。

修复：生成明确时间线。keep 指向源视频及入出点，replace 在对应槽位引用用户图片并保留时长；转场、字幕、音轨作为合成契约输入。若产品只需要照片视频，应明确名称和能力边界。

### F03 — P1：同一产品图用于多个镜头会被去重折叠【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\App.vue:584`；`B:\lib\workbuddy_session.py:328`；`B:\tools\asset_upload_chunk.py:364`。

GUI 每镜头上传一张图，假定上传次数等于镜头数。OM `register_image` 按 SHA / 路径去重；同一产品图用于多个镜头只保留一条素材。换成 `shot-1.png`、`shot-2.png` 文件名也不能避开 SHA 去重。最终渲染遍历素材集合，导致镜头数和时长缩水。GUI 已定义 `listSessionAssets`，本生成流程未用它核对。

修复：素材去重保留；另建不去重的 timeline instances，每个镜头独立引用同一个 asset_id。不要把“文件集合”当作“镜头序列”。

### F04 — P1：超过单次图片上限只渲染首段【代码 + 历史运行证据】

位置：`C:\vclaw\openclaw\clawx-studio\src\services\montage.ts:306`；`B:\mcp_server.py:1754`、`:1858`；`B:\logs\session_video.log:2234`。

OM 支持 `assets_offset` / `assets_limit`，默认上限来自 `OPENMONTAGE_MAX_SESSION_IMAGES`，未设置时为 20。GUI 未暴露或传递分页参数，也未遍历后续块及拼接。当前运行进程环境上限未直接读取，不能保证固定为 20。

历史确定案例：2026-09-01T07:57:19Z，job `03d792e1ca0a499eaff058d302c3de61`，会话有 24 图，日志 `count=20,total_in_session=24,is_last=false`，随后该 job rendered / published。证明“发布成功”可仅表示第一页成功。

修复：完整时间线后端统一生成，或分页渲染全部镜头后拼接；输出校验镜头数量、时长和 `is_last`，禁止首段直接作为完整成片。

### F05 — P1：配音生成与最终合成脱节；字幕沿用另一套时间轴【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\App.vue:512`、`:592`；`C:\vclaw\openclaw\clawx-studio\src\services\reconstruct.ts:689`；`B:\mcp_server.py:1033`、`:2012`。

`synthesizeVoice` 返回值被丢弃；`createRemotionVideo` 不接收音频引用，OM 此图片模板 asset_manifest 只有图片，没有把此次 TTS 结果放进音轨。调用 TTS 成功不等于成片有配音。原视频音轨也未传入。

字幕 `scriptToCues` 按原片 paragraph 起止时间分配文本行，而成片按每图 3 秒生成；文本行数超过 paragraph 数时，后面的行共用末段时间。存在字幕越界、重叠和不同步。

线上 `voicebox_tts` schema 必填为 `text/profile_id`，不含前端额外发送的 `operation`；是否拒绝额外字段取决于框架校验，应按线上 schema 对齐。此项尚未调用 TTS 验证，不作为已发生的错误。

修复：保存并传递 audio asset；指定混音 / 保留原声策略；用最终 timeline 和实际语音时长生成字幕，再检查音轨存在、音画时长和字幕边界。

### F06 — P1：正式 Remix 包未携带完整源视频绑定【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\App.vue:157`、`:213`；`C:\vclaw\openclaw\clawx-studio\src\services\remixPackage.ts:90`；`C:\vclaw\internal\handler\remix_package.go:188`。

创建项目只传 name；视频上传后服务端路径主要留在 GUI `sourceVideo.localVideoPath`。`buildRemixPackage` 的 keep 只有 `source_range` 和 `pending_source=true`，未设置 `source_asset`。生成包也未保存 sourceVideo 的路径。Go StartRun 读取项目的 `ReferenceFileKey`，在这条 GUI 路径没有看到相应绑定写入。

产品图资源可以进入 `replacement_asset`；但源视频不是一个完整可由后端恢复的输入。控制面仅校验 envelope 和 scenes 数组，允许这些 pending 项被保存、提交。不能把“PUT 成功”视为“可渲染契约完整”。

修复：上传完成后保存 source asset_id/file_key、时长、媒体类型、项目归属；提交前强校验所有 keep/replace/generate 输入是否齐全、资源可读、时间范围有效。

### F07 — P1：direct 再上传使用默认 OM 项目，绕开已上传的正式项目素材【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\App.vue:101`、`:546`、`:584`；`C:\vclaw\openclaw\clawx-studio\src\services\montage.ts:128`；`C:\vclaw\openclaw\clawx-studio\src\config.ts:53`。

首次上传显式传 `ensureVclawProject()` 返回的 ID；生成时重置 client 后再次上传，却没有传 projectId，回退到 `STUDIO_CONFIG.openMontageProjectId`（缺省 mclaw-demo）。渲染依赖新会话的默认项目，而 Remix 包属于控制面正式项目。会话隔离能减少历史图片污染，但不能保证项目关联正确。

此外，用户图片本地文件丢失时，代码退回 `card.preview`，会把用户选择的替换图改回参考首帧；缺提示词或空图直接跳过，可能仍显示生成成功。

修复：始终沿用正式 project_id 与资产引用；允许后端以资源 ID 构建新任务。缺图、缺提示词必须明确失败 / 待补充，不能静默换图或丢镜头。

### F08 — P1：完成状态、分享与实验任务回传不闭环【代码确定】

位置：`C:\vclaw\openclaw\clawx-studio\src\services\montage.ts:338`；`C:\vclaw\openclaw\clawx-studio\src\App.vue:532`、`:614`；`B:\mcp_server.py:2481`。

- `pollRenderStatus` 超过 10 分钟返回当前状态，不抛超时；GUI 只排除 failed，可能把 queued/rendering 当成“成片已生成”。
- OM 已在 create_remotion_video_share 后台执行发布，status 包含 share_url；GUI 忽略该链接，取 video_path 再上传微云。造成重复发布；第二次失败可掩盖第一次成功。
- openclaw_assisted 提交后直接 return，无当前流程的状态轮询 / 最终链接处理。Go 返回 run_id，而 GUI 提示读取 job_id/render_job_id，往往只显示“已受理”。处理器没有在这条分支创建可供原 production_jobs 状态接口查询的 job。

修复：统一 run/job 状态与可恢复任务 ID；仅终态成功展示完成；published 直接使用 share_url；发布失败和渲染失败分开；长任务继续查询或明确显示仍处理中。

### F09 — P0（生产上线）：公网当前为临时开发身份联调窗口【本次实测 + 部署文档】

位置：`C:\vclaw\internal\middleware\principal.go:134`；`C:\vclaw\docs\vclaw-production-domain-routing-handoff.md:132`。

无 Authorization 的 initialize/tools/list 均返回 200；对应 OM 日志有 `auth=YES` 和 VClaw user attached。源码 `VCLAW_DEV_USER_ID` 分支优先于真实 JWT，把请求归为配置的 dev principal；交接文档明确说明当前临时全局绕过。实测与该模式吻合，但未直接读取线上进程环境，其他网关身份注入也不能完全排除。

此模式适合指定联调窗口，不能证明真实用户鉴权完成；若直接用于多用户生产，各访问可能归入同一开发身份。上线前关闭绕过，并用两个真实用户验证 401/403、session 归属、资源命名空间和成片访问。

旧交接文档记录项目创建 502。本次无效 JSON POST 已返回 Go 风格 400，说明该路径当前不再表现为那个 502；尚未写库验证 201。不要将旧故障直接判作当前阻断。

### F10 — P2：分解兜底与预览对齐不可靠【部分函数已复现】

位置：`C:\vclaw\openclaw\clawx-studio\src\services\reconstruct.ts:90`、`:478`、`:530`；`C:\vclaw\openclaw\clawx-studio\src\App.vue:354`。

`pickField` 只接受字符串；OM 场景的 duration_seconds/end_seconds 是数值。数值 90 秒的示例本次返回 null，因此不足两个镜头时的按时长分段兜底可能拿到 0，无法启动。

timestamps 抽帧失败会退回 count 均匀抽帧；GUI 仍按数组索引配给镜头，未按 timestamp/scene_id 关联。抽取数量少于镜头数或分布不一致时，不能保证每张图属于显示的镜头。

修复：显式数值类型解析；保留 scene_id/timestamp 映射；部分失败透明呈现，预览失败不得被“拆解完成”掩盖。

### F11 — P2：长上传 / 长任务恢复和日志诊断不足【代码观察 / 待故障注入】

位置：`C:\vclaw\openclaw\clawx-studio\src\services\mcpClient.ts:63`、`:100`；`C:\vclaw\openclaw\clawx-studio\src\services\montage.ts:142`；`B:\logs\mcp_health.log`。

GUI 上传循环未持久化 upload_id/offset 或恢复断点；MCP client 无请求级 AbortSignal 超时 / 401 自动恢复，并发 ensureInit 没有共享初始化 Promise。montage 与 backendMcp 各自维护 session，需要明确任务会话策略；同 principal 跨 session 读取本身被 OM 允许，不能仅凭“两套 client”判定所有读取失败。

健康日志尾部反复 `status=ok`，同时 `tool_pending=3`、`upload_asset_chunk:3`。这是待核查信号，可能是挂起任务，也可能是统计清理问题；不足以宣布当前服务已经死锁。旧日志存在 PrincipalNotFound，随后有成功绑定上传，不能据旧异常断言现仍全量失败。

## 5. 日志证据如何解读

| 证据 | 能证明 | 不能证明 |
|---|---|---|
| mcp_server.log 近期 start / append / complete，68 字节图片，project 前缀 e2e | 小图片在有用户绑定时完整上传成功 | 大视频上传、GUI 完整操作成功 |
| mcp_server.log 约 44401–44561 行，scene_detect / frame_sampler / transcriber success=True | OM 曾成功执行各分解能力 | 新命名空间 GUI 预览可读、该运行与当前 UI 版本相同 |
| session_video.log 2231–2238 行，24 图取前 20 图并发布 | OM 图片渲染 + 字幕 + 微云曾跑通，也证明分页必须处理 | 所有镜头均被生成、原片结构被保留 |
| 当前公网请求对应 OM 日志 auth=YES / user attached | ds 入口请求确实到达该 OM 日志所对应服务 | 真实用户登录、正式身份隔离已验收 |
| decompose.log 8 月 31 日 workspace_violation / scene_count=0 | 历史监控曾发现问题 | 当前版本同样错误、所有成功日志无效 |

本次扫描 session_video.log：asset_uploaded 319、render_completed 233、weiyun_publish_completed 232、workflow_failed 101。日志含不同项目、重试与历史测试，**不是同一批任务的成功率分母，不能据此计算端到端成功率**。

## 6. 推荐补齐顺序

1. **先修资源契约**：principal/project 路径统一；源视频和产品图持久绑定；渲染输入使用 asset refs；不再将 GUI 本地路径作为唯一恢复依据。
2. **建立真正时间线**：镜头实例与素材分离；keep 使用源片；replace 保留槽位时长；消除去重导致镜头丢失、默认 3 秒和分页截断。
3. **接好音画**：TTS 音轨、原声策略、字幕时间轴、转场参数与最终合成连通；完整产物校验。
4. **任务闭环**：返回统一任务 ID；完成前严格状态判定；结果直接使用 OM share_url；可恢复轮询 / 发布重试 / 断点续传。
5. **正式身份验收**：结束开发绕过窗口后，验证真实 JWT 和两个用户的隔离，再做真实 GUI 全流程。

不建议先堆生成模型或增加效果按钮。主要缺口在跨层数据契约和时间线，而非 OM 缺少视频生成工具。

## 7. 下一轮模拟用户端到端验收方案

本轮产出评估报告。以下是后续执行清单，未标为已完成。

| 用例 | 操作 | 必须通过的断言 |
|---|---|---|
| 正常基础链路 | 真实登录，新建项目，上传 PNG/JPEG + 3 镜头短 MP4 | 项目 201；各文件 SHA 匹配；proxy / OM 请求关联；分解预览全部可见 |
| 重构语义 | 原镜头时长 2/5/8 秒；keep/replace/replace | 总时长约 15 秒；keep 保留动态；替换图正确；无意外删段 |
| 同图复用 | 两个镜头使用同一产品图 | 两个镜头实例均存在，不因 SHA 去重减少时长 |
| 分页 | 至少 24 个镜头，超过当前服务上限 | 所有分段生成并拼接；末段可见；不是只有前 20 图 |
| 音画同步 | 配音、原声策略、中文多行字幕 | ffprobe 确认音轨；人工试听；字幕无越界/重叠；正确片长 |
| 不完整输入 | 删除本地产品图、缺 prompt、关键帧读取失败 | 明确指出待修镜头；不替换成参考图，不静默跳过 |
| 长任务 | 排队/渲染超过 10 分钟 | GUI 不提前宣告完成；可恢复查询；无需重复渲染 |
| 中断与重启 | 上传中断、token 过期、关闭重开应用 | 不重复计费/发布；可续传或明确重试；任务及素材归属保留 |
| 发布失败 | 成片已渲染但发布失败 | 可访问/重试现有产物；不要求全部重做 |
| 用户隔离 | 两个正式用户，尝试复用另一方 session/asset/job | 越权拒绝；资源不覆盖；未携带身份请求 401 |
| 辅助模式 | 提交 openclaw_assisted | 返回真实 run_id；源视频可恢复；进度更新；最终链接回到 GUI |

验收应保存：客户端版本 / 服务版本、project_id、run/job_id、请求关联 ID、媒体 SHA、镜头时间线、最终 ffprobe JSON、逐镜头对照截图和脱敏日志。完成标准是“正确成片可播放、全部镜头和音轨符合选择”，而非只有接口 200 或任务 published。

## 8. 报告交付

主报告：`B:\docs\vclaw-om-upload-remix-assessment-2026-09-05.md`。

本地副本：`C:\OpenMontage_voicebox\docs\vclaw-om-upload-remix-assessment-2026-09-05.md`。

结论有效于本次读取与探测时点。未执行真实素材端到端生产；以上确定性缺陷已足以判定当前不能按完整重构闭环验收。
