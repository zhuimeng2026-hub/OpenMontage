# 执行台账模板

文档编写日期：2026-09-05。**目前只编写计划；没有实施任何任务。** 不把前一轮评估的测试通过结果复制成新功能验收。

## 状态约定

pending：未开始；in_progress：执行中；local_ready：本线实现与模块测试完成；integration_ready：I已合并指定版本待真实联调；passed：I确认对应汇合点和本卡验收完成；blocked：明确缺少依赖/授权/环境；failed：已执行但验收失败。

计划完成不计入T00–T23。A/B/C只写handoffs/<线>/<任务>.md；I独占本总表，追加证据、不回写旧失败。任务归属和G0–G5见[并行交接](05-parallel-delivery.md)。父任务必须全部分片合并、真实汇合点通过才passed。

| 任务 | 状态 | 实现版本/提交 | 测试证据 | 备注 |
|---|---|---|---|---|
| T00 | pending | — | — | 开发副本与基线 |
| T01 | pending | — | — | 契约和fixtures |
| T02 | pending | — | — | 素材登记 |
| T03 | pending | — | — | 分解工作区 |
| T04 | pending | — | — | GUI分解 |
| T05 | pending | — | — | 草稿绑定 |
| T06 | pending | — | — | Go快照 |
| T07 | pending | — | — | 时间线编译 |
| T08 | pending | — | — | RemixTimeline |
| T09 | pending | — | — | 音轨字幕 |
| T10 | pending | — | — | 已有音色TTS；无克隆 |
| T11 | pending | — | — | 合成适配 |
| T12 | pending | — | — | OM持久作业 |
| T13 | pending | — | — | Go用户身份 |
| T14 | pending | — | — | Go原子受理 |
| T15 | pending | — | — | worker恢复 |
| T16 | pending | — | — | GUI任务闭环 |
| T17 | pending | — | — | 辅助模式 |
| T18 | pending | — | — | MCP可靠性 |
| T19 | pending | — | — | 上传恢复 |
| T20 | pending | — | — | 不完整输入 |
| T21 | pending | — | — | 身份和日志 |
| T22 | pending | — | — | 端到端验收 |
| T23 | pending | — | — | 发布准备/受控发布 |

## 单次执行记录（复制追加）

```text
角色/任务ID/分片：
开始/结束时间：
开发前置及已导入交付：
contract版本/fixture SHA256：
汇合点及状态：
本次实际开发目录：
开始HEAD/结束HEAD或diff文件：
读取的关键函数/接口：
实际修改文件（绝对路径）：
实现结果：
契约是否变更：否 / 是（版本及协调记录）
测试1：命令 / 工作目录 / 退出码 / 核心断言 / 证据路径
测试2：
未运行测试及原因：
现有功能回归结果：
是否涉及声音克隆：必须为否
是否触碰真实配置/部署/收费生成：否，或写明确授权来源
mock覆盖范围/真实接口未验证项：
本线结论：local_ready / failed / blocked
I集成结论：integration_ready / passed / failed / blocked
未解决事项：
下一任务：
```

## 阻塞交接（不要只写“报错了”）

```text
任务/步骤：
最小复现：
预期/实际：
完整错误摘要（脱敏）：
尝试1及结果：
尝试2及结果：
已确认不受影响的部分：
缺少的唯一输入/依赖/决策：
建议下一步：
禁止执行的绕过办法：
```

## 交付总表（未来填写）

```text
代码修复：pending
direct完整验收：pending
assisted完整验收：pending
真实JWT双用户验收：pending
声音克隆：out_of_scope
生产发布：not_authorized_by_this_plan
```
