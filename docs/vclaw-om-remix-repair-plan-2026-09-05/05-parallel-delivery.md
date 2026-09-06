# 并行执行与四角色交接

计划版本：1.1，2026-09-05。组织：**A：OM开发线，B：VClaw Go开发线，C：GUI/Tauri开发线，I：统一集成负责人**。当前只更新计划，所有代码任务仍未执行，没有启动代理。

本文件与README的新调度表取代1.0的“默认按编号串行”。任务卡的功能、测试与禁止事项继续有效；调度只允许提前模块开发，不允许提前宣称完整验收。

## 1. 分析来源与开发副本

前期实际检查的代码与日志：

| 范围 | 实际读取位置 | 说明 |
|---|---|---|
| VClaw Go控制面、代理、worker | `C:\vclaw\internal`、`C:\vclaw\cmd` | 包括mcp_proxy、remix_package、store及OpenClaw接口 |
| 桌面GUI与Rust层 | `C:\vclaw\openclaw\clawx-studio\src`、`src-tauri` | App.vue、上传、分解、MCP、任务接口 |
| OM服务端与渲染组件 | `B:\mcp_server.py`、`B:\lib`、`B:\tools`、`B:\remotion-composer`等 | B盘对应`\\192.168.20.173\voicebox`；读取时需要有共享目录访问权限的进程 |
| OM运行日志 | `B:\logs` | mcp_server、session_video、mcp_health、decompose |
| 本地指南与报告副本 | `C:\OpenMontage_voicebox` | 指南与B盘指南曾校验内容相同；本轮文档在该目录编写后同步B盘 |

**没有把C盘OM副本当成B盘全部代码的等价替代。** 当前安装的GUI二进制、远程Go运行版本以及OM进程是否载入磁盘最新内容，仍要T00/T22核实。VClaw审计时HEAD为8d04cfb，结论也包含当时工作区内容，不代表固定发布版本。

这些是分析源目录，不是要求所有模型直接在上面同时写代码。T00由I分配A/B/C各自独立开发副本，并记录真实绝对路径。B与C属于同一个VClaw仓库，但必须使用不同工作树；A不能直接编辑B盘可能正在运行的OM代码。

## 2. 状态和放行规则

- `local_ready`：本线代码、类型、单元/契约测试通过，有提交或完整patch；可供依赖任务开发，但尚未完成跨线联调。
- `integration_ready`：I已合并指定版本、核对fixture/schema一致，具备真实联调条件；仍不是通过。
- `passed`：对应汇合点真实联调及任务断言通过，由I统一标记。
- `pending/in_progress/blocked/failed`沿用原定义。

G0之前不得放行并行编码。G0之后，README“开发前置”中的任务达到local_ready或passed，且其交付已被导入开发副本，才允许开工。仅知道另一个模型正在写，不等于依赖可用。

缺后端实现时，可以使用T01固定契约的mock完成模块测试；mock只在测试目录/注入测试transport中存在，生产路径不得返回伪造成功。接口未真实联调时最高local_ready。

## 3. 三条线的任务顺序

| 角色 | 主要任务与本线建议顺序 | 不负责 |
|---|---|---|
| A / OM | T02→T03→T07→T08→T09→T11→T12；之后T19-A、T20-A、T21-A；T10-A音频登记补充可在T03后准备 | Go/GUI代码、生产配置切换 |
| B / Go | T06→T13→T14→T15→T17；之后T20-B、T21-B | OM核心、App.vue、Tauri |
| C / GUI | T04→T05→T18→T10→T16；之后T19-C、T20-C、T21-C | OM Python、Go迁移和worker |
| I / 集成 | T00→T01→G0；维护契约、文件租约与合并；G1→G2→G3→G4→G5；主责T22/T23 | 不作为第四条并行功能开发线，不直接改各线正在修改的文件 |

上述是队列顺序，不要求等其他线同编号任务结束。开发前置以README表为准。例如：

- G0后：A做T02，B做T06，C做T04，可以同时开始。
- B的T13可以对假OM实现做协议测试，不等T12完成；真实签名与用户素材读取在G2验证。
- A的T07在T02就绪后即可基于冻结快照编译，不再等GUI草稿和Go真实接口全部完成。
- C的T10按已有音色TTS契约开发，不等渲染结束；声音克隆仍排除。
- T10与T11/T12分别在C/A线上推进；A负责T10里Python补充部分。
- T17与T18/T19/T20的前端工作可以并行，但T19-A须排入A的队列，不能由C抢改mcp_server.py。

## 4. 汇合点：真实验收必须集中

| 汇合点 | 所需交付达到local_ready | I执行的真实检查 | 可放行的父任务 |
|---|---|---|---|
| G0 契约冻结 | T00/T01 | 环境、类型、schema、正负fixture、源码路径与依赖版本一致 | T00/T01；之后三线可开发 |
| G1 上传与草稿 | T02/T03/T04/T05/T06 | 真实上传→分解→预览→保存/重载；Go保存v2，OM资源归属正确 | T02–T06 |
| G2 生成和任务后端 | T07/T08/T09/T10/T11/T12/T13/T14/T15；G1通过 | Go原用户签名读取已上传资源；2/5/8秒、同图复用、音轨、24镜头、幂等受理与发布重试 | T07–T15 |
| G3 GUI direct与传输 | T16/T18；G2通过 | 真实Tauri提交固定版本、过期刷新、长请求、轮询、重开恢复、分享；不二次发布 | T16/T18 |
| G4 辅助与可靠性 | T17/T19/T20/T21全部分片；G3通过 | assisted快照关联、断点/重启、旧草稿、双用户、生产模式与日志测试 | T17/T19/T20/T21 |
| G5 全量验收与发布准备 | T22；G4通过 | A01–A14、版本证据、完整GUI实际操作；再评审T23发布包 | T22；T23的准备/发布分别记录 |

G1所需的现有基础MCP transport可以先用当前版本，T18完整改造在G3集中检查。G2的TTS真实生成若未获准，音轨部分可用自制音频先验证，但T10实际TTS集成必须标未验收，G2不能全部passed。

汇合点失败：I记录具体失败属于哪张卡，把问题退回该线修复；其他线可继续无依赖的模块任务。不能通过扩大mock、跳过断言、把错误成片改名来放行。

## 5. 跨线任务拆分与唯一写入者

父任务只在全部分片合并及汇合点通过后passed。分片ID固定；一条线不能自行把父任务标完成。

| 父任务 | 分片 | 唯一写入者与范围 |
|---|---|---|
| T01 | I统筹 | I固定契约和共享fixture；Go/TS/Python类型变更按预定patch交给I统一合入，再放G0 |
| T10 | T10-A / T10-C | A补OM音频路径/登记与测试；C做已有音色TTS选择、出参处理、narration绑定与UI测试 |
| T19 | T19-A / T19-C | A做upload status/append/complete幂等与Python测试；C做上传重试、offset对账、Tauri文件检查 |
| T20 | T20-A / T20-B / T20-C | A做OM ready检查；B做Go快照/提交负例；C做缺素材/旧草稿/用户提示；测试样例由I同步 |
| T21 | T21-A / T21-B / T21-C | A做OM owner/status/pending日志；B做Go身份/配置/路由与迁移相关；C核对生产构建开关和UI缓存隔离；I运行双用户联调 |
| T22 | I统筹 | 各线提供测试与fixture；I操作真实集成环境、汇总媒体证据，禁止开发者自签全流程通过 |
| T23 | I统筹 | A/B/C提供各自发布与回滚材料；I评审兼容顺序，只在另获发布授权时部署 |

共享文件锁定：

| 文件/目录 | 唯一所属线 |
|---|---|
| OM mcp_server.py、tools/asset_upload_chunk.py、lib/remix_*、remotion-composer | A |
| V internal/handler、internal/store、internal/model、internal/openmontage、internal/mcpauth、cmd | B |
| V openclaw/solutions/product-video-production 中辅助agent/MCP接入 | B |
| GUI src/App.vue、src/services、src/types、src-tauri、GUI package/lock | C |
| 计划契约、共享fixture版本清单、总台账、集成分支 | I |

任务卡写了“允许修改”只表示功能范围，不覆盖本表的唯一写入权。需要另一线修改时，发接口变更请求/分片需求，不直接跨目录编辑。A内的T09/T11和C内的T05/T10/T16均会触碰同文件，**本线保持串行**，不要再拆出多个同时写入代理。

## 6. 交付与合并协议

1. I在T00分配工作树、基线commit和task owner；分支建议codex/remix-om、codex/remix-go、codex/remix-gui以及集成分支。只在后续实施授权下创建，本轮不创建。
2. 一个任务/分片一个可审提交或patch；交付注明所属repo、base/head、contract版本、fixture SHA、依赖交付ID、测试命令/退出码、未验收项。
3. 各线只写`handoffs/<线>/<任务>.md`（实施时创建），I独占04-execution-ledger总表。这样不会三个模型同时覆盖台账。
4. I按依赖合并。VClaw的Go与GUI同repo但不同工作树，不能复制整个目录覆盖；OM是另一个repo，需要版本组合清单，不能只记录一个HEAD。
5. 合并冲突由文件所属线提供修复patch，I核对后合入；不能让第四个模型凭文本冲突随意选择ours/theirs。
6. I运行受影响回归及该汇合点真实用例，更新状态；各线下一任务使用I指定的依赖版本，不能混用别人的未提交目录。
7. 契约变化必须提出`CR-编号：旧字段/新字段/原因/影响任务/兼容策略`；I更新契约版本与fixtures后，通知受影响线重跑。没有CR不得靠新增兼容别名临时蒙混。
8. 不在三条线同时跑大规模渲染压测；占用同一OM/Chromium/GPU环境的测试由I排队，先限制资源竞争再解释超时。

交付格式：

```text
角色/任务分片：
开发副本绝对路径/仓库：
base_commit/head_commit或patch路径：
contract_version/fixture_sha256：
导入的依赖交付：
实际修改文件：
测试命令/退出码/关键断言：
mock覆盖了什么：
真实接口尚未验证什么：
状态：local_ready / blocked / failed
合并注意事项：
声音克隆：未涉及
```

## 7. 四份可直接复制的角色提示词

### I：统一集成负责人

```text
你负责VClaw/OM修复计划的统一集成，不直接抢改三条线正在编辑的文件。
计划：B:\docs\vclaw-om-remix-repair-plan-2026-09-05
先读README、01-contracts、05-parallel-delivery、04-execution-ledger。
先完成T00/T01并放行G0，再按开发依赖给A/B/C各派一张卡或分片。
冻结契约、fixture、开发目录、文件归属。你独占总台账，检查交付patch后合并。
local_ready不是passed；按G1–G5运行真实联调后才更新passed。
最终负责T22验收、T23发布准备；未经本次另行授权不部署。
不要把聊天run_id当成成片完成；不允许mock/跳测绕过汇合点。
本版本只用已有音色TTS，不实现声音克隆。
本提示词是未来实施交接模板；只有用户明确授权实施后才开始代码工作。
```

### A：OM开发线

```text
你负责A线：OM Python、资源/时间线/作业、Remotion renderer。
计划：B:\docs\vclaw-om-remix-repair-plan-2026-09-05
只在I分配的OM独立开发副本写代码，B:\是分析源，不能默认直接改线上共享目录。
G0放行后依次处理T02/T03/T07/T08/T09/T11/T12及分配的T10-A/T19-A/T20-A/T21-A。
一次一张卡，遵守01-contracts、02-task-cards、05-parallel-delivery的依赖和文件归属。
缺Go/GUI实现时可按冻结fixture做模块测试，不声称真实集成通过。
交付patch和handoffs/A/<任务>.md，最高自行标local_ready；passed由I决定。
不修改VClaw Go/GUI，不改全局鉴权，不调用声音克隆，不部署。
```

### B：VClaw Go开发线

```text
你负责B线：Go快照、MCP用户断言、job/outbox/worker和OpenClaw辅助接入。
计划：B:\docs\vclaw-om-remix-repair-plan-2026-09-05
代码源是C:\vclaw；只写I分配的Go开发工作树，GUI目录属于C线。
G0后可先做T06/T13，再做T14/T15/T17及T20-B/T21-B。
T13可以模拟OM测试协议，真实用户资源访问必须等G2，不可造成功响应。
迁移、代理签名和worker由你统一修改；不能使用legacy sim-id兜底。
按冻结契约保存原始快照hash与actor，不自报用户、不打印凭据。
交付patch和handoffs/B/<任务>.md，最高自行标local_ready；不抢改OM/App.vue。
不执行生产迁移或部署，不新增声音克隆。
```

### C：GUI/Tauri开发线

```text
你负责C线：GUI与Tauri文件/MCP/认证传输。
计划：B:\docs\vclaw-om-remix-repair-plan-2026-09-05
源是C:\vclaw\openclaw\clawx-studio；只写I分配的GUI工作树。
G0后按T04/T05/T18/T10-C/T16/T19-C/T20-C/T21-C推进，一次一张卡。
先用固定契约mock开发；T04不猜namespace，T16不走旧照片视频兜底。
App.vue、reconstruct.ts、montage.ts等同线串行，不再派多人同时改。
T10只做已有音色TTS与音轨绑定，不做录音上传或声音克隆。
需要OM/Go补接口时交给A/B，不跨线直接修改。
交付patch和handoffs/C/<任务>.md，最高自行标local_ready，真实GUI验收由I组织。
不部署、不修改真实.env。
```

## 8. 边界

可以并行的是工程实现与模块测试；数据契约变更、合并冲突、跨用户权限验收、资源密集测试和生产发布由I统一协调。串行改为并行不会减少任何功能验收项，也不会把声音克隆纳入本版本。
