# 图片上传配额与幂等性 Bug：修复及二次复检报告

> 用途：记录图片批次上传 422 配额死锁从首次修复（`e514e71`）到二次复检加固后的最终实现，供工程师或其他模型交叉验证。
> 文中路径均相对于仓库根目录 `OpenMontage/`。

---

## 0. 元信息

| 项 | 值 |
|---|---|
| 模块 | `frameflow/bff`（Go BFF + Web 前端）及上游 `tools/asset_upload_chunk.py` |
| 业务约束 | 单个图片批次至少 5 张、最多 10 张才能提交渲染 |
| 原始现象 | 部分上传失败后重试，批次尚未达到 5 张却收到 422 `reason=quota`，形成无法继续上传也无法渲染的死锁 |
| 首次修复 | `e514e71`，解决串行重试下的 422 死锁主路径 |
| 二次复检结论 | 首次修复方向正确但不完整；已进一步修复幂等语义、权威计数同步、脚本模式隔离和前端向下纠偏 |
| 当前验证状态 | Go build/vet/test、无缓存全量测试、Python 单测、JavaScript 语法检查及 diff 检查均通过 |

---

## 1. 原始故障与首次修复效果

### 1.1 原始故障链路

修复前，`upload_asset_chunk` 使用会话级 `assetCount[scope]` 判断上传配额，并且 complete 失败也可能增加计数。典型链路如下：

```text
选择 5 张图片
  → 4 张提交成功，1 张失败
  → 重试、重选或重开批次时会话计数继续累加
  → 会话计数达到 10，但当前批次实际仍不足 5 张
  → start 被 422 quota 拒绝
  → 无法补齐 5 张，也无法渲染清零
```

根因是将“本批次真实已提交资源数”和“跨重试累积的会话计数”混为一谈，同时 complete 的失败语义不够严格。

### 1.2 `e514e71` 已解决的部分

首次修复引入批次感知的配额检查：当 `project_id` 对应活动图片批次时，start 使用该批次的 `asset_count` 判断是否达到 10，而不是使用可能泄漏的 session counter。它确实解决了串行场景中“批次只有 4 张但历史会话计数已满”的 422 死锁。

因此，`e514e71` 的主修复结论成立：正常串行上传、单张失败后重试，可以继续补齐到最低 5 张。

---

## 2. 二次复检发现的问题

首次修复仍有以下缺口：

| 问题 | 风险 |
|---|---|
| 前端根据错误字符串 `/asset already exists/` 伪造 `success:true` | 未验证资源是否属于当前 session/project；同名不同内容也可能被误判成功 |
| 前端上传后仅在 `serverCount > uploadedCount` 时回写 | 服务端计数较小时无法向下纠偏，本地“至少 5 张”判断可能虚高 |
| complete 只检查 `error`，未要求明确 `success:true` | 上游返回 `success:false` 且无 `error` 时仍可能计数 |
| complete 无条件对批次 `IncAsset` | 重传、重复 complete 或上游已去重时会虚增；并发下可能超过 10 |
| 新建批次调用全局 `ResetAsset(scope)` | 图片批次创建会清空同一 scope 的脚本模式 session counter，两个模式互相干扰 |
| 仅因 `project_id` 非空就当作图片批次 | 脚本模式默认 `project_id=frameflow-default`，若直接走批次同步，会找不到批次并绕过 session quota |
| 上游同名文件一律报 `asset already exists` | 无法区分同内容重传与同名不同内容冲突 |
| 上游新文件名但 SHA 相同，先写 target 再由 `register_image` 按 SHA 去重 | 新 target 未登记，形成孤儿文件；返回 asset 还可能指向非 canonical 路径 |
| `asset_count` JSON 数字转换缺少转换前范围检查 | 极大 `float64` 转换为 `int` 存在不安全边界 |

这些问题不会否定首次修复对串行死锁的效果，但会破坏“同内容重传幂等、不同内容不误判、批次计数以服务端为准”的完整目标。

---

## 3. 最终修复方案

最终方案以三个原则为核心：

1. **上游资源幂等必须基于内容 SHA，而不是错误字符串。**
2. **批次计数优先采用上游返回的权威 `asset_count`，本地只保留有界兼容 fallback。**
3. **批次模式、脚本模式和浏览器展示计数彼此隔离，并都以明确成功语义为前提。**

| 层级 | 最终策略 |
|---|---|
| 上游上传工具 | 同名同 SHA 返回真正幂等成功；同 SHA 改名返回 batch 中 canonical asset 并删除孤儿 target；同名不同 SHA 失败且不覆盖 |
| BFF complete 判定 | 只有顶层 `success == true` 且 `error` 为空才允许更新任何计数 |
| BFF 批次识别 | 先 `ByProject(scope, projectID)`，仅真实存在且状态为 `collecting` 的记录走图片批次路径 |
| BFF 权威同步 | 合法顶层 `asset_count` 直接 `SetAssetCount`，允许向上或向下同步 |
| BFF 兼容 fallback | 旧上游缺少合法 `asset_count` 时，仅非 deduplicated 的批次成功可原子 `+1`，且不能超过 10 |
| BFF 脚本模式 | 无真实 collecting 批次时回到 session counter；deduplicated 或失败均不增加 |
| 批次创建 | 不再调用 `ResetAsset(scope)`，避免清空脚本模式计数 |
| 前端 | 不解析错误字符串伪造成功；上传后精确采用服务端计数，允许向下纠偏；渲染前再次刷新服务端计数 |

---

## 4. 逐文件改动

### 4.1 `tools/asset_upload_chunk.py`

complete 阶段计算本次上传内容的 SHA-256，并按以下规则处理：

- 目标文件不存在：写入 target，然后调用 `register_image`。
- 目标文件已存在且 SHA 相同：视为真正幂等成功，不覆盖文件；重新调用 `register_image` 恢复/确认 batch 状态，清理 `.part` 和 state。
- 目标文件已存在但 SHA 不同：返回 `asset already exists; use a different filename`，不覆盖已有文件。
- 新文件名但 SHA 已在 batch 中存在：`register_image` 按 SHA 去重后，从 `batch.assets` 查找同 SHA 的 canonical asset；删除刚写入但未登记的新 target，并让返回的 `data.asset`、`asset_manifest.assets` 和 artifacts 都指向 canonical 实际文件。

返回语义：

- 正常新增：`success:true`、`deduplicated:false`。
- 同名同内容或改名同内容：`success:true`、`deduplicated:true`，且 batch 的 assets 数量不增加。
- 同名不同内容：`success:false`，已有资源保持不变。

### 4.2 `tests/test_asset_upload_chunk.py`

上传工具测试现覆盖：

- 分块上传完整往返及 session 隔离；
- 非法跨 session 继续上传；
- 文件名清洗；
- 同内容同名重传成功且 `deduplicated:true`；
- 同名不同内容仍失败；
- 同内容改名后 `deduplicated:true`；
- 改名产生的临时 target 已删除；
- 返回 asset 路径存在，并等于第一张图片的 canonical 路径；
- batch assets 仍只有一项。

### 4.3 `frameflow/bff/internal/imagebatch/store.go`

业务边界统一为：

```go
const (
    MinBatchImages = 5
    MaxBatchImages = 10
)
```

新增/收紧计数接口：

- `SetAssetCount(sessionID, projectID, count)`：只允许 `0..MaxBatchImages`，只更新 `status='collecting'` 的批次，并检查 `RowsAffected()==1`。
- `IncAsset(sessionID, projectID)`：SQL 条件包含 `status='collecting' AND asset_count < MaxBatchImages`，并检查受影响行数；达到上限、批次不存在或状态不符时返回错误。

因此，旧上游兼容 fallback 也无法把本地批次计数增加到 10 以上。

### 4.4 `frameflow/bff/handlers/mcp.go`

complete 的最终判定和计数流程如下：

```text
明确 success:true 且 error 为空？
  否 → 不计数
  是 → 查询 project_id 是否对应 collecting 图片批次
       ├─ DB 查询错误 → 记录日志并安全返回，不误判为脚本或批次
       ├─ 是真实批次
       │    ├─ 顶层 asset_count 是 0..10 的整数 JSON 数字 → SetAssetCount 权威同步
       │    ├─ deduplicated:true → 不增加
       │    └─ 缺少合法 asset_count → 有界 IncAsset fallback，并记录日志
       └─ 不是 collecting 批次 → 脚本模式
            ├─ deduplicated:true → session counter 不增加
            └─ 普通成功 → session counter +1
```

关键细节：

- `success:false` 即使没有 `error` 也不会计数。
- 批次路径不再无条件 `IncAsset`。
- 权威 `asset_count` 可以向下同步，修正本地历史虚高。
- `authoritativeAssetCount` 在将 `float64` 转为 `int` 前检查 NaN、Infinity、范围和整数性；极大值不会进入转换。
- `project_id=frameflow-default` 不再被误判为图片批次，脚本模式 quota 继续生效。

### 4.5 `frameflow/bff/handlers/upload_quota_test.go`

Go 回归测试覆盖：

- 批次 4 张时允许继续上传，达到 10 张才拒绝；
- 泄漏的 session counter 不阻塞新的活动图片批次；
- 脚本模式 session quota 仍生效；
- 只有明确 `success:true` 且无错误才算成功；
- `frameflow-default` 普通成功使 session count 从 0 变为 1；
- 脚本模式 `deduplicated:true` 和 `success:false` 均不增加 session count；
- 权威 `asset_count` 可以把本地计数从 4 向下同步到 2；
- `SetAssetCount` 拒绝越界值，`IncAsset` 拒绝超过 10。

### 4.6 `frameflow/bff/handlers/image_batch.go`

- `validateImageCount` 使用统一的 `MinBatchImages` / `MaxBatchImages`。
- Create 不再调用 `h.Sessions.ResetAsset(scope)`。

图片批次配额由持久化批次记录隔离；创建图片批次不应清空同一用户正在使用的脚本模式 session counter。

### 4.7 `frameflow/bff/web/mcp-client.js`

- 删除根据 `/asset already exists/i` 错误字符串构造 `{success:true}` 的逻辑。
- complete 只接受服务端真实顶层 `success === true`；其他响应抛出上传失败。

幂等成功现在由上游基于 SHA 明确返回，不再由浏览器猜测。

### 4.8 `frameflow/bff/web/index.html`

- 单张上传成功后优先使用响应中的 `asset_count` 更新 `uploadedCount`。
- `deduplicated:true` 时不重复增加本地计数，也不重复 push 本地预览。
- 一轮上传结束后，fetch 得到 `sc` 时执行精确赋值 `uploadedCount = sc`，允许向下纠偏。
- 非 demo 模式点击渲染时先 fetch 当前批次服务端 `asset_count`，同步后再检查是否至少 5 张。
- 上传前仍以服务端当前计数计算剩余可上传数量。

---

## 5. 验证记录

### 5.1 已执行并通过

在 `frameflow/bff` 下使用工作区 `GOCACHE`：

```powershell
go build ./...
go vet ./...
go test ./...
go test -count=1 ./...
```

结果：全部通过。`go test -count=1 ./...` 禁用测试缓存，确认全量 Go 测试真实重新执行。

仓库根目录：

```powershell
pytest -q tests/test_asset_upload_chunk.py
```

结果：`8 passed`。

JavaScript 与差异检查：

```powershell
node --check frameflow/bff/web/mcp-client.js
git diff --check
```

结果：均通过。

### 5.2 未执行项及原因

- 未执行 Go race detector：当前 Go 环境未启用 CGO，无法运行 `go test -race`。这不等同于已证明无竞态。
- 未执行真实远端 MCP / 部署环境 E2E。
- 未做部署环境网络中断、反向代理超时、服务重启或多实例并发等故障注入 E2E。

本报告不把本地密闭测试描述为远端或部署环境验证。

---

## 6. 二次复检清单

- [x] 原 `e514e71` 修复能解决串行失败重试导致的 422 死锁。
- [x] 同名同 SHA 重传由上游返回真实幂等成功。
- [x] 同 SHA 改名返回 canonical asset，删除未登记的新 target。
- [x] 同名不同 SHA 失败且不覆盖已有文件。
- [x] complete 必须明确 `success:true` 且 `error` 为空才计数。
- [x] `success:false` 且无 error 不计数。
- [x] 图片批次优先采用合法顶层 `asset_count`，允许向下同步。
- [x] 缺少权威计数时，仅普通成功使用有界 fallback，最多 10。
- [x] `deduplicated:true` 在图片批次和脚本模式都不增加计数。
- [x] 只有数据库中真实存在的 collecting batch 才走批次路径。
- [x] `frameflow-default` 继续走脚本模式 session quota。
- [x] 批次 Create 不再 ResetAsset，不干扰脚本模式。
- [x] 前端不再根据错误字符串伪造成功。
- [x] 前端服务端计数可向上、向下精确回写。
- [x] 非 demo 渲染前重新读取服务端计数并校验至少 5 张。
- [x] Go build/vet/test、`go test -count=1`、Python 8 项测试、Node 语法检查、diff 检查通过。
- [ ] race detector：CGO 未启用，未执行。
- [ ] 部署环境故障注入和真实远端 E2E：未执行。

---

## 7. 残留风险与后续设计

### 7.1 仍未解决：跨实例/多客户端的 start → complete 并发预占

当前 start 阶段读取批次 `asset_count` 判断是否达到 10，但没有在数据库中预占上传槽位。单实例串行和一般重试已正确；然而在跨实例或同一用户多客户端的极端并发下，多个 start 可能同时看到 `asset_count < 10` 并全部通过，然后各自在上游 complete。

本地 BFF 的有界 fallback 和权威同步不会把本地 `asset_count` 持久化到 10 以上，但它们无法撤销已经在上游提交的额外资源。因此，上游实际资源数在极端竞态下仍可能超过 10。

这不是本轮应通过扩大锁范围临时掩盖的问题。后续建议设计持久化 `upload_reservations` 表：

1. start 在数据库事务中原子检查 `committed + active_reservations < MaxBatchImages` 并预占一个槽位；
2. reservation 绑定 session、batch/project、upload_id 和过期时间；
3. complete 成功时把 reservation 原子转换为 committed；
4. complete 失败或显式取消时释放 reservation；
5. 后台或请求路径回收超时 reservation；
6. 同一 upload_id 的 start/complete 必须幂等；
7. 多实例共享同一数据库约束，不能依赖进程内 mutex。

在该设计落地并通过多客户端并发测试前，不能声称“任何并发条件下上游都绝不会超过 10”。

### 7.2 验证边界

当前结论建立在本地自动化测试和静态/构建检查上。上线前仍建议在部署环境执行：

- 5 张中 1 张故障后重试并成功渲染；
- 同名同内容、同名不同内容、同内容改名三组真实上传；
- 浏览器刷新、双标签页及两台设备同时上传；
- BFF 重启、上游超时和 complete 响应丢失后的恢复；
- 多实例并发逼近 10 张上限。

这些属于后续部署验证计划，不是本报告已完成的测试。
