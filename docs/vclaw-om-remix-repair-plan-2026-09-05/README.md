# VClaw / OM 可执行修复计划书

版本：1.1，2026-09-05。状态：**仅计划，尚未实施**。

依据：`B:\docs\vclaw-om-upload-remix-assessment-2026-09-05.md`。本计划为后续逐任务交给低成本编码模型执行而编写。本轮只新增文档，不代表授权立即修改代码、迁移数据或部署服务。

## 1. 交给执行模型的阅读顺序

1. 本文：边界、架构、任务顺序。
2. [并行执行与四角色交接](05-parallel-delivery.md)：三条线、文件归属、汇合点、四份角色提示词。
3. [接口与数据契约](01-contracts.md)：每个执行任务必须遵守的固定契约。
4. [任务卡](02-task-cards.md)：一次只执行指定的一个任务 ID。
5. [验收与发布清单](03-verification.md)：具体测试用例、命令和预期结果。
6. [执行记录模板](04-execution-ledger.md)：任务完成后记录事实，不填写推测。

阅读代码前仍需读取所在仓库 AGENTS.md / AGENT_GUIDE.md。本计划给出的是工程修复设计；未来真实视频生产继续遵守 OM pipeline、审核与 checkpoint 规则。

## 2. 本版本固定边界

### 必须实现

- 本地图片、视频经 `https://ds.aixifs.com/api/mcp/proxy` 上传到 OM；素材和任务属于同一真实用户、同一项目。
- 视频分镜、抽帧和转写能显示到 GUI；每张预览绑定明确 scene_id。
- keep 保留原视频片段动态；replace 用用户图片占据指定镜头时段；同图可复用多次；镜头不会被去重或分页上限丢失。
- 原声保留、静音、已有音色 TTS 三种音频策略；字幕按最终时间线同步。
- 正式任务提交、持续查询、重启恢复、结果回传；渲染与发布失败分开处理。
- 开发身份绕过与生产环境隔离；两用户访问边界可验证。

### 明确不处理

**声音克隆完全排除**：不新增克隆音色创建、不采集参考录音、不上传克隆样本、不训练/复刻用户声音、不修改任何 clone_voice API。TTS 仅消费系统已经可用且获准使用的音色，用户不需要先克隆声音。不要删除共享项目已有的克隆功能；只让本重构流程不依赖、不暴露它。

本轮修复也不新增图生视频模型、自动商品抠图换物、口型同步、音乐生成、自动复刻所有特效。replace 的明确语义是“用图片替换整个指定镜头画面”，不是在原视频物体位置做跟踪替换。

已有 generate 模式保留兼容，但本版本新 direct 主流程仅接受已经生成并绑定的素材；未完成生成的槽位返回待补素材，不能绕过审核自动调用模型。

参考视频任意复杂转场的自动识别不作为已解决能力。首个验收使用硬切参考片；本版本支持显式确认的 cut / fade。遇到不支持或无法确定的转场，提示用户确认处理方式，不能声称像素级复刻。此限制与声音克隆排除分别记录。

## 3. 目标架构：固定一种做法

执行模型不得在任务中重新选择架构。

```text
GUI
  ├─ 文件上传 / 分解工具调用 → VClaw MCP proxy → OM
  ├─ 编辑并保存 RemixPackage v2 → VClaw 版本快照
  ├─ POST /api/studio/video-projects/:id/render
  │    → VClaw 原子写 production_job + job_queue
  │    → worker 读取不可变快照
  │    → 携带原用户可信签名调用 OM submit_remix_render
  └─ GET /api/studio/video-projects/:id/jobs/:jobId
       ← VClaw 持久任务状态 ← OM get_remix_render_status

OM
  ├─ ProjectWorkspace：唯一文件路径权威
  ├─ 素材登记：asset_id → 已验证的文件 / 媒体属性
  ├─ Remix 校验 / 时间线编译：确定性输入转换
  ├─ 新 RemixTimeline 合成组件：精确镜头 + 音轨 + 字幕
  └─ 现有 video_compose / 公平队列 / 发布能力
```

分工不变：Go 管身份、任务、快照与账单；OM 管素材、合成和媒体状态；GUI 管编辑与展示。禁止把生成决策写成新的 Python 自动导演；新增 Python 逻辑仅做资源验证、确定性契约转换和已批准渲染任务执行。

### 实现选择

1. 上传仍复用现有 `upload_asset_chunk`，不另建整文件上传接口。
2. 正式 Remix 不再调用“渲染会话全部图片”的 `create_remotion_video_share`。旧工具继续兼容照片视频场景。
3. 素材集合去重；时间线镜头实例绝不按素材去重。
4. 新增窄用途 `RemixTimeline` Remotion composition，复用 `video_compose` 的 staging、公平队列和运行检测，不修改 Explainer 等旧模板的语义。
5. Remotion 是本工程设计拟用的现有合成路径；不安装或切换新引擎。真实生产时仍记录已确认的 runtime；如果该 runtime 不可用，明确失败，不自动换引擎。
6. 新 direct 和 openclaw_assisted 共用 VClaw 任务入口与最终快照。辅助模式只能消费指定快照，不能重新规划用户已确认的镜头。
7. 状态不靠 GUI localStorage 当真相源；localStorage 只存用户范围内的项目/任务选择和编辑草稿，不存 token、完整 MCP session。
8. 不新增第三个常驻服务、Redis、消息队列产品或数据库引擎；复用现有 SQLite job_queue / production_jobs。

## 4. 已有文件与拟新增文件的区别

现有代码入口（以函数名定位，行号可能变化）：

| 代号 | 实际路径 |
|---|---|
| V | `C:\vclaw` |
| GUI | `C:\vclaw\openclaw\clawx-studio` |
| OM | `B:\`，共享目录 `\\192.168.20.173\voicebox` |
| App | GUI 下 `src/App.vue` |
| 上传 | GUI 下 `src/services/montage.ts`、`src-tauri/src/commands_upload.rs` |
| 分解 | GUI 下 `src/services/reconstruct.ts` |
| MCP | GUI 下 `src/services/mcpClient.ts`、`backendMcp.ts`、`src-tauri/src/commands_mcp.rs`、`http_client.rs` |
| CP | V 下 `internal/handler/remix_package.go`、`project.go`、`mcp_proxy.go`、`cmd/server/main.go` |
| CP 任务 | V 下 `internal/store/store.go`、`internal/model/models.go`、`cmd/worker/main.go` |
| OM 接口 | OM 下 `mcp_server.py`、`tools/tool_registry.py` |
| OM 资源 | OM 下 `lib/project_workspace.py`、`lib/namespace_version.py`、`tools/asset_upload_chunk.py`、`tools/asset/read_session_asset.py` |
| OM 合成 | OM 下 `tools/video/video_compose.py`、`lib/render_queue.py`、`remotion-composer/src/Root.tsx` |

任务卡中的“新增”文件、API、测试名在本计划编写时**尚不存在**，不要当成已经可调用的接口。新名字由契约文件统一规定。现有路径若不存在，先 `rg --files` 找符号；找不到就报告漂移，禁止创建同名空实现来骗测试。

## 5. 顺序与交付门槛

采用 **3条开发线＋1个统一集成负责人**。先由I完成T00/T01并通过G0，再按下表并行开发；同一条线内部一次一张卡。完整文件归属、分片与角色提示词见[并行交接方案](05-parallel-delivery.md)。

开发前置达到local_ready或passed且交付已导入即可开发；不要求所有跨线真实接口先完成。测试替身仅用于模块测试。passed必须由I完成对应汇合点真实验收后登记。T10/T19按A/C分片，T20/T21按A/B/C分片；分片可按自身组件依赖先准备，父任务依赖如下。

| ID | 交付物 | 所属线 | 开发前置 | 真实验收汇合点 |
|---|---|---|---|---|
| T00 | 工作副本与基线记录 | I | 无 | G0 |
| T01 | v2 契约、纯逻辑测试入口及共享 fixtures | I | T00 | G0 |
| T02 | OM 素材登记及按用户项目解析 | A | T01 | G1 |
| T03 | OM 分解工作区与资源登记接口 | A | T02 | G1 |
| T04 | GUI 分解路径与预览关联修复 | C | T01 | G1 |
| T05 | GUI 源视频/产品图绑定及 v2 草稿 | C | T04 | G1 |
| T06 | Go v2 校验、版本快照与兼容 | B | T01 | G1 |
| T07 | OM 确定性时间线编译器 | A | T02 | G2 |
| T08 | RemixTimeline 画面与镜头实例 | A | T07 | G2 |
| T09 | 音轨/字幕合成及时间校验 | A | T08 | G2 |
| T10 | 已有音色 TTS 准备与 UI 绑定 | A/C | T05,T03 | G2 |
| T11 | 接入 video_compose 和媒体验收 | A | T09 | G2 |
| T12 | OM 持久幂等 Remix 作业 | A | T11 | G2 |
| T13 | Go 到 OM 的用户身份与协议客户端 | B | T01 | G2 |
| T14 | Go 原子受理、outbox 与查询 API | B | T06,T13 | G2 |
| T15 | worker 提交、轮询与故障恢复 | B | T14 | G2 |
| T16 | GUI 正式生成、状态恢复和分享 | C | T05,T10 | G3 |
| T17 | OpenClaw 辅助模式任务闭环 | B | T15 | G4 |
| T18 | MCP 初始化、刷新与有界超时 | C | T01 | G3 |
| T19 | 上传同会话续传 / 跨会话安全重试 | A/C | T02,T18 | G4 |
| T20 | 缺素材、旧草稿、未支持能力防误报 | A/B/C | T16,T19 | G4 |
| T21 | 生产身份、job 权限与日志 | A/B/C | T12,T15,T18 | G4 |
| T22 | 集成与真实 GUI 验收 | I | G4通过 | G5 |
| T23 | 发布/回滚材料与受控发布验收 | I | T22 passed | G5后发布准备/授权发布 |

汇合顺序：G0契约冻结 → G1上传/分解/草稿 → G2生成与任务后端 → G3 GUI direct与传输 → G4辅助模式/可靠性/权限 → G5全量业务验收。各汇合点完整准入和断言见05第4节。T23发布准备与真实部署分别记录；计划本身不授权部署。

### 原报告覆盖

| 发现 | 任务 |
|---|---|
| F01 路径 | T02–T04 |
| F02 时间线 | T07–T11；复杂未知转场仍按本计划边界标明 |
| F03 重复图 | T07–T08 |
| F04 截断 | T07,T11,T22 |
| F05 配音字幕 | T09–T10 |
| F06 源视频绑定 | T02,T05–T06 |
| F07 项目错位/换图 | T05,T16,T20 |
| F08 状态分享 | T12–T17 |
| F09 身份 | T13,T21,T23 |
| F10 兜底与预览 | T04,T20 |
| F11 恢复诊断 | T18–T19,T21–T22 |

## 6. 复制给低成本模型的单任务提示词

```text
你执行修复计划中的【Txx】，只做这一张卡，不做后续任务。
计划目录：B:\docs\vclaw-om-remix-repair-plan-2026-09-05
先读 README、01-contracts、05-parallel-delivery、02-task-cards 的 Txx、04-execution-ledger。
确认I分配的角色、工作树、分片与唯一文件写入范围。
先确认G0已放行、开发前置为local_ready或passed且已导入；再读取该卡代码与项目指南。
报告与源码不一致时以当前源码为准，记录差异，不靠猜测补 API。
按契约实现：允许改动文件、操作步骤、测试矩阵、停止条件全部遵守。
不新增声音克隆，不调用 clone_voice，不采集参考录音。
不修改真实 .env，不打印密钥，不部署，不跑收费生成，除非本次另有明确授权。
代码修改完成后运行该卡指定检查；记录命令、退出码、关键断言与差异。
在handoffs/<线>/<任务>.md提交交接，最高标local_ready；I独占总台账并负责passed。再输出：完成项、改动文件、测试、未解决问题、下一任务 ID。
禁止因为类型检查通过就宣称端到端成功。禁止跳过测试、删除断言或改成 stub。
```

卡片过大时：拆成 `Txx-a/Txx-b`，仅切分已有步骤，不改契约；父任务全部子项合并且对应真实汇合点通过后，由I标passed。执行模型不需要自行估算费用或承诺时长。

## 7. 停止条件

- G0未放行，或开发依赖交付未就绪/未导入；契约字段冲突；适用指令与拟实现存在实质冲突。
- 要求修改任务卡范围外的身份系统、计费规则、数据库架构或共享模板才能继续。
- 需要生产凭据/用户选择而当前没有；测试需要的 runtime 无法确认。
- 连续两次修改仍无法解释同一失败：记录最小复现、错误、尝试，交回协调者；不要扩大改动范围。

普通文件移动、命名、局部函数组织不需要额外设计会议；遵循任务卡即可。环境阻塞不等于功能已实现，也不要自动安装或升级一整套依赖。
