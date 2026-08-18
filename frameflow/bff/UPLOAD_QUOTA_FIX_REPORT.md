# 上传模块 422 `reason=quota` 死锁 Bug：检查 / 修复 / 复检报告

> 用途：本文档完整记录该 Bug 的定位、修复与复检过程，供其他大模型/工程师交叉验证。
> 交叉验证者只需在仓库中对照「改动清单」与「验证清单」逐项核对即可。
> 所有路径相对于仓库根目录 `OpenMontage/`，改动集中在 `frameflow/bff/`。

---

## 0. 元信息

| 项 | 值 |
|---|---|
| 模块 | `frameflow/bff`（Go 后端 + `web/` 前端） |
| 业务约束 | 单一渲染会话（image batch）**至少 5 张、至多 10 张**图片才能渲染 |
| 现象 | 选 5 张图、1 张上传失败后继续上传，前端报 422，后台日志 `reason=quota` |
| 严重程度 | 高（阻断用户凑齐 5 张 → 渲染永远失败 → 死锁） |
| 是否阻塞上线 | 是（核心上传链路被废掉） |
| 修复日期 | 2026-08-18 |
| 验证状态 | `go build` / `go vet` / `go test ./...` 全绿（见 §6） |

---

## 1. 现象（原始证据）

后台日志（用户原话，已脱敏）：

```
2026/08/18 16:30:21 [bff-mcp] start tool=upload_asset_chunk operation=start sid_hash=5931ef7d scope_hash=a1e9325d project_id=frameflow-batch-batch-e86fe4199dbbe2a7e1e1af8f upload_diag={filename_hash=591bc8d9 filename_len=14 filename_safe=true extension=".png" total_bytes=1103521 offset=- upload_id_present=false}
2026/08/18 16:30:21 [bff-mcp] upload_rejected operation=start scope_hash=a1e9325d project_id=frameflow-batch-batch-e86fe4199dbbe2a7e1e1af8f reason=quota upload_diag={filename_hash=591bc8d9 ...}
```

前端表现：上传接口返回 HTTP 422（`Unprocessable Entity`），错误体含 `reason=quota`，界面提示上传失败。

---

## 2. 复现路径（交叉验证者可据此复现）

1. 在图片上传模块选 **5 张图片** 开始上传；
2. 其中 **1 张上传失败**（例如网络抖动 / 上游 `asset already exists` / 超时），其余 4 张成功提交（批次 `asset_count=4`）；
3. 用户重新选择那张失败的文件，或继续补传图片；
4. 多次失败 / 重试 / 放弃重开批次后，**会话级计数器被累加至 `MaxFilesPerSubmission=10`**；
5. 此后**任何合法重试**（`asset_count` 仍为 4，远未到 5）都在 `upload_asset_chunk` 的 `"start"` 步骤被后端 **422 `reason=quota`** 拒绝；
6. 用户**永远无法凑齐 5 张** → 渲染永远失败 → 计数器永不因渲染成功而重置 → **死锁**。

> 关键洞察：422 的真正原因**不是配额太小**，而是「泄漏的会话级计数器 + 失败也计数」导致的**死锁**。

---

## 3. 根因分析

### 3.1 原配额判定逻辑（修复前，`handlers/mcp.go` 的 `MCPProxy`）

上传配额原先用 `SessionStore.assetCount[scope]` 这个**会话级、只增不减**的计数器判断：

- 计数器**只在成功渲染（`create_remotion_video_share`）时 `ResetAsset` 清零**；
- `upload_asset_chunk` 的 `"complete"` 操作**无论上游是否真成功都 `IncAsset`**。上游在重复文件时会返回 `res["error"]="asset already exists"`，但旧代码照常 `+1`。

### 3.2 死锁链路

```
选 5 张 → 4 成功 / 1 失败 → 重试 / 重选继续上传，计数器继续累加
   ↓
多次失败、重试、放弃重开批次 → 计数器累加到 10 (MaxFilesPerSubmission)
   ↓
本批次仅 4 张的合法重试也被 422 拒绝（会话计数器已 = 10）
   ↓
永远凑不齐 5 张 → 渲染失败 → 计数器永不重置 → 死锁
```

### 3.3 设计缺陷小结

| 缺陷 | 后果 |
|---|---|
| 配额计数器是**会话级、只增不减**（仅渲染成功才清零） | 跨批次 / 重试会累积，最终封死新批次 |
| `"complete"` **失败也计数**（`res["error"]` 存在时仍 `IncAsset`） | 失败、重复文件都虚增配额 |
| 新建批次**不清零**会话计数器 | 用户每重试一轮都离死锁更近一步 |

---

## 4. 修复方案

核心思路：**让配额判定「批次感知」**，用批次权威的 `asset_count` 而非泄漏的会话计数器；仅真正成功才计数；新建批次即清零。

| # | 修复点 | 文件 | 作用 |
|---|---|---|---|
| 1 | 配额判定改为批次感知（抽出 `quotaRejectForUpload`） | `handlers/mcp.go` | 用 `b.AssetCount` 判断封顶 10，不再用泄漏计数器 |
| 2 | `"complete"` 仅真正成功才 `IncAsset` | `handlers/mcp.go` | 失败 / 重复文件不再虚增配额 |
| 3 | 新建批次即 `ResetAsset` | `handlers/image_batch.go` | 每轮新提交都从满配额开始，打破死锁 |
| 4 | 统一业务常量 `MinBatchImages=5/MaxBatchImages=10` | `internal/imagebatch/store.go` | 渲染校验与上传配额共用单一真源 |
| 5 | 前端对 `asset already exists` 幂等（视为成功） | `web/mcp-client.js` | 重新上传已存在文件不再误报失败 |
| 6 | 前端用服务端 `asset_count` 回写可上传余量与 `uploadedCount` | `web/index.html` | 「至少 5 张」门槛以服务器真实数量为准 |

---

## 5. 逐文件改动（供交叉验证逐项核对）

### 5.1 `frameflow/bff/internal/imagebatch/store.go`

新增业务边界常量（单一真源）：

```go
// MinBatchImages / MaxBatchImages are the business bounds for a single render
// session's image set. A submission needs AT LEAST MinBatchImages to render
// (the upstream can't produce a meaningful montage from fewer) and is capped at
// MaxBatchImages. Both the render-time validator (validateImageCount) and the
// upload-time quota check must agree on these, so they live here as the single
// source of truth.
const (
	MinBatchImages = 5
	MaxBatchImages = 10
)
```

### 5.2 `frameflow/bff/handlers/image_batch.go`

**(a) `validateImageCount` 改用统一常量**（原硬编码 5/10）：

```go
func validateImageCount(count int) error {
	if count < imagebatch.MinBatchImages || count > imagebatch.MaxBatchImages {
		return fmt.Errorf("image batch requires %d to %d images", imagebatch.MinBatchImages, imagebatch.MaxBatchImages)
	}
	return nil
}
```

**(b) `Create` 在开新批次时立即清零会话计数器**（打断死锁关键一步）：

```go
	if _, ok := imageScripts[req.ScriptID]; !ok {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": "unknown script_id", "scripts": imageScripts})
		return
	}

	// A brand-new submission must start with the full upload quota. The
	// session-wide counter only resets on a successful render, so without this a
	// user who abandoned a broken batch (or retried several times) would carry
	// stale "used" counts into the next batch and hit the 422 quota wall before
	// reaching the required minimum of 5 images.
	h.Sessions.ResetAsset(scope)

	id := "batch-" + randHex(12)
	...
```

> 注：`ResetAsset` 已提前到 `CreateBatch`（上游会话创建）**之前**，确保即使上游创建失败，计数器也已重置，且逻辑可被测试覆盖。

### 5.3 `frameflow/bff/handlers/mcp.go`

**(a) 在 `MCPProxy` 的 `upload_asset_chunk` `"start"` 预校验中，改为调用 `quotaRejectForUpload`：**

```go
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "start" {
			if reject, status, body := h.quotaRejectForUpload(scope, projectID); reject {
				resultErr = fmt.Errorf("upload quota reached")
				log.Printf("[bff-mcp] upload_rejected operation=start scope_hash=%s project_id=%s reason=quota%s", mcp.ShortHashForLog(scope), projectID, uploadDiag)
				c.JSON(status, body)
				return
			}
		}
	}
```

**(b) `"complete"` 仅真正成功才计数**（新增对 `res["error"]` 的判断）：

```go
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "complete" {
			if errStr, ok := res["error"].(string); ok && errStr != "" {
				log.Printf("[bff-mcp] upload_complete_error scope_hash=%s project_id=%s error=%q%s", mcp.ShortHashForLog(scope), projectID, errStr, uploadDiag)
			} else {
				h.Store.IncAsset(scope)
				if h.ImageBatches != nil {
					if projectID, _ := req.Args["project_id"].(string); projectID != "" {
						h.ImageBatches.IncAsset(scope, projectID)
					}
				}
			}
		}
	} else if req.Tool == "create_remotion_video_share" {
		h.Store.ResetAsset(scope)
	}
```

**(c) 新增可单测的辅助函数 `quotaRejectForUpload`**（核心修复逻辑，批次感知）：

```go
// quotaRejectForUpload decides whether an upload_asset_chunk "start" must be
// rejected for quota reasons. It returns (reject, httpStatus, body).
//
// The check is batch-aware. When the upload targets an active ("collecting")
// image batch we enforce the cap against that batch's authoritative committed
// image count (b.AssetCount), NOT the leaky session-wide counter. ...
func (h *Handlers) quotaRejectForUpload(scope, projectID string) (bool, int, gin.H) {
	tier := h.Limits.Resolve(scope)
	lim := limits.ForTier(tier)
	if projectID != "" && h.ImageBatches != nil {
		if b, berr := h.ImageBatches.ByProject(scope, projectID); berr == nil && b != nil && b.Status == "collecting" {
			if b.AssetCount >= imagebatch.MaxBatchImages {
				return true, http.StatusUnprocessableEntity, gin.H{
					"error": fmt.Sprintf(
						"本批次最多 %d 张图片，当前已上传 %d 张",
						imagebatch.MaxBatchImages, b.AssetCount),
					"files": b.AssetCount,
					"max":   imagebatch.MaxBatchImages,
				}
			}
			// Batch count is authoritative here; do not also apply the
			// session-wide cap (which may be stale from prior attempts).
			return false, 0, nil
		}
	}
	if h.Store.AssetCount(scope) >= lim.MaxFilesPerSubmission {
		return true, http.StatusUnprocessableEntity, gin.H{
			"error": fmt.Sprintf(
				"your %q tier allows at most %d images per submission; this submission has already reached the limit",
				tier, lim.MaxFilesPerSubmission),
			"files": h.Store.AssetCount(scope),
			"max":   lim.MaxFilesPerSubmission,
		}
	}
	return false, 0, nil
}
```

> 关键语义：
> - 带 `project_id` 且批次处于 `collecting`：`b.AssetCount`（封顶 `MaxBatchImages=10`）为权威。即便会话计数器因历史重试被累加到 10，只要本批次 `asset_count < 10`，重试仍被放行；因此**总能凑到 5 张**。
> - 无 `project_id`（脚本模式）：沿用会话级 `MaxFilesPerSubmission` 兜底。

### 5.4 `frameflow/bff/web/mcp-client.js`

分块上传 `chunkUpload` 的 `complete` 步骤对「`asset already exists`」做**幂等兼容**：

```js
    // 3) complete
    var complete = await mcpCall('upload_asset_chunk', {
      operation: 'complete', project_id: projectId, filename: filename, upload_id: uploadId
    });
    // The upstream rejects a re-upload of an already-committed file with
    // "asset already exists". That is not a real failure for the user — the
    // image is already part of this batch — so treat it as a successful upload
    // instead of surfacing a false "上传失败" and blocking the required 5.
    var completeErr = (complete && (complete.error || (complete.data && complete.data.error))) || '';
    if (completeErr && /asset already exists/i.test(completeErr)) {
      return Object.assign({}, complete, { success: true, already_exists: true });
    }
    if (!complete || complete.success === false || (complete.data && complete.data.success === false)) {
      throw new Error('chunk complete 失败：' + JSON.stringify(complete));
    }
    return complete;
```

### 5.5 `frameflow/bff/web/index.html`

**(a) 新增服务端真实数量回写辅助 `fetchBatchAssetCount`（约 1758 行）**：

```js
  // Reconcile the local upload count with the server's authoritative batch
  // asset_count. ... Returns null when unavailable (e.g. demo mode).
  async function fetchBatchAssetCount(){
    if (!currentImageBatch || window.FFMCP.demo) return null;
    try {
      var r = await fetch(bffBase() + '/api/image-batches/' + encodeURIComponent(currentImageBatch.id), { credentials: 'include' });
      if (!r.ok) return null;
      var b = await r.json();
      return (b && typeof b.asset_count === 'number') ? b.asset_count : null;
    } catch (e){ return null; }
  }
```

**(b) `handleFiles` 用服务端 `asset_count` 计算可上传余量（约 1981–1990 行）**：

```js
    var list = Array.prototype.slice.call(files);
    // 用服务端真实的批次 asset_count 计算可上传余量 ...
    var serverCount = await fetchBatchAssetCount();
    var baseCount = (serverCount != null) ? serverCount : uploadedCount;
    var available = maxFilesPerSubmission - baseCount;
    if (available <= 0){
      showToast('本批次已达到 ' + maxFilesPerSubmission + ' 张上限，请先开始渲染');
      return;
    }
```

**(c) `handleFiles` 一轮结束后用服务端真实数量回写 `uploadedCount`（约 2019–2024 行）**：

```js
    setUploading(false);
    renderUploadPreview();
    refreshSessionAssets(); // 同步刷新「本会话已上传图片」面板
    if (uploadInput) uploadInput.value = '';
    // 一轮结束后用服务端真实数量回写计数，保证「至少 5 张」渲染门槛以真实数量为准
    var sc = await fetchBatchAssetCount();
    if (sc != null && sc > uploadedCount) uploadedCount = sc;
```

---

## 6. 复检 / 验证（本次执行结果）

### 6.1 自动化测试（核心回归保障）

新增 `frameflow/bff/handlers/upload_quota_test.go`，3 个测试**在旧逻辑下会失败**，确认其守护了修复：

| 测试 | 验证点 |
|---|---|
| `TestUploadQuotaBatchAwareDoesNotBlockReachingMinimum` | 批次 `asset_count=4` 时重试第 5 张**不被拒绝**；`=10` 时才 422 |
| `TestUploadQuotaIgnoresLeakySessionCounter` | 会话计数器被累加到 10（生产死锁状态），但新批次 `asset_count=0` **仍允许上传**（旧代码会 422） |
| `TestUploadQuotaScriptModeFallback` | 脚本模式兜底：`scope` 空时允许；计数器达 `MaxFilesPerSubmission` 时 422 |

### 6.2 执行命令与结果

```bash
cd frameflow/bff
go build ./...        # → 通过，无错误
go vet ./...          # → 通过，无告警
go test ./...         # → 全部通过（含上述 3 个新测试 + 原有测试）
```

> 运行单个回归测试：
> ```bash
> go test ./handlers/... -run 'UploadQuota|ValidateImageCount' -v
> ```

### 6.3 测试覆盖说明

`quotaRejectForUpload` 不依赖上游 MCP（不发起网络请求），仅依赖本地 SQLite + `imagebatch`/`state`/`limits`/`mcp` 包，测试为**密闭（hermetic）**。

---

## 7. 交叉验证清单（请其他模型逐项核对）

- [ ] **根因是否成立**：确认 `handlers/mcp.go` 旧逻辑用 `h.Store.AssetCount(scope)` 做上传配额判定，且 `IncAsset` 在 `complete` 时不论成功失败都执行（见 5.3b）。
- [ ] **死锁逻辑是否成立**：确认 `ResetAsset` 仅出现在 `create_remotion_video_share` 与（修复后）`image_batch.go Create`，会话计数器会在多次失败后累加至 `MaxFilesPerSubmission`。
- [ ] **修复 1（批次感知）**：`quotaRejectForUpload` 中带 `project_id` 且 `Status=="collecting"` 时，用 `b.AssetCount >= MaxBatchImages` 判断，且**不再叠加**会话级 `MaxFilesPerSubmission`（见 5.3c 注释 "do not also apply the session-wide cap"）。
- [ ] **修复 2（仅成功计数）**：`complete` 分支在 `res["error"]` 非空时**不** `IncAsset`（见 5.3b）。
- [ ] **修复 3（新建清零）**：`image_batch.go Create` 在 `CreateBatch` 之前调用 `h.Sessions.ResetAsset(scope)`（见 5.2b）。
- [ ] **修复 4（常量统一）**：`imagebatch.MinBatchImages=5 / MaxBatchImages=10`，且 `validateImageCount` 与 `quotaRejectForUpload` 均引用之（见 5.1 / 5.2a / 5.3c）。
- [ ] **修复 5（前端幂等）**：`mcp-client.js` 对 `asset already exists` 返回 `{success:true, already_exists:true}`（见 5.4）。
- [ ] **修复 6（前端回写）**：`index.html` 上传前用 `fetchBatchAssetCount()` 算 `available`、上传后用其回写 `uploadedCount`（见 5.5a/b/c）。
- [ ] **业务约束守住了**：任何路径下，单批次渲染前 `asset_count` 必须 ≥ 5（≤ 10）；新批次重试不受历史失败拖累。
- [ ] **回归测试可失败性**：把 `quotaRejectForUpload` 改回旧逻辑（用 `h.Store.AssetCount(scope) >= lim.MaxFilesPerSubmission`），应能观察到 `TestUploadQuotaBatchAwareDoesNotBlockReachingMinimum` 与 `TestUploadQuotaIgnoresLeakySessionCounter` 失败。
- [ ] **构建/测试全绿**：在本仓库 `go build ./... && go vet ./... && go test ./...` 应通过。

---

## 8. 残留 / 后续事项（非阻塞）

1. **上游 `tools/asset_upload_chunk.py` 未改动**：其「重复文件返回错误」语义保留，前端已做幂等兼容（5.4）。若希望上游直接返回已存在资源（而非报错），可另开一轮优化。
2. **会话级 `MaxFilesPerSubmission` 仍存在于脚本模式兜底**：与批次模式解耦后，仅服务于非批次上传，不影响批次死锁。
3. **前端 `uploadedCount` 仍为本地缓存**：已用 `fetchBatchAssetCount()` 回写兜底，但未改为纯服务端驱动；如追求强一致可进一步改为每轮从服务端拉取。
4. **建议测试环境复现**：用「选 5 张 → 故意让 1 张失败 → 重试」验证修复后该批次能正常达到 5 张并渲染。
