package handlers

import (
	"crypto/sha256"
	"fmt"
	"log"
	"math"
	"net/http"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
)

const maxMCPBodyBytes = 2 << 20

var uploadFilenamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`)
var uploadExtensionPattern = regexp.MustCompile(`^\.[A-Za-z0-9]{1,10}$`)

var allowedMCPTools = map[string]bool{
	"upload_asset_chunk":          true,
	"create_remotion_video_share": true,
	"get_render_status":           true,
}

// MCPProxy receives { "tool": "<name>", "args": { ... } } and forwards it to the
// OpenMontage MCP server as a tools/call, returning the extract()-ed structured
// result (mirrors om_mcp_probe.py). The caller's BFF session cookie selects the
// long-lived MCP client so uploads and the create call share one session.
//
// For the manual upload -> create flow it also enforces the user's per-tier
// MaxFilesPerSubmission cap, mirroring the pre-check the batch-render surface
// performs in TemplateHandler.BatchRender. The cap is tracked per BFF session
// in SessionStore (completed-upload count), reset when a video is created.
func (h *Handlers) MCPProxy(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxMCPBodyBytes)
	var req struct {
		Tool string                 `json:"tool"`
		Args map[string]interface{} `json:"args"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if req.Tool == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "tool is required"})
		return
	}
	if !allowedMCPTools[req.Tool] {
		c.JSON(http.StatusBadRequest, gin.H{"error": "tool is not allowed"})
		return
	}
	sid := h.ensureSession(c)
	// Key the upstream MCP session mapping by the stable WeChat identity (or the
	// device session when anonymous) so the same account maps to the SAME upstream
	// Mcp-Session-Id across machines — that is what makes uploaded assets and the
	// generated video consistent cross-device (like email). See plan:
	// cosmic-pulse-babbage.
	scope := renderQueueOwnerID(sid)
	operation, _ := req.Args["operation"].(string)
	projectID, _ := req.Args["project_id"].(string)
	uploadDiag := ""
	originalFilename := ""
	storedFilename := ""
	if req.Tool == "upload_asset_chunk" {
		// The upstream MCP requires a strict ASCII basename. Normalize at the
		// BFF boundary so old browsers and old MCP workers cannot reject a user
		// upload merely because its local filename contains Chinese or spaces.
		originalFilename, _ = req.Args["filename"].(string)
		sanitizeUploadFilename(req.Args)
		storedFilename, _ = req.Args["filename"].(string)
		uploadDiag = " " + uploadArgsSummary(req.Args)
	}
	log.Printf("[bff-mcp] start tool=%s operation=%s sid_hash=%s scope_hash=%s project_id=%s%s", req.Tool, operation, mcp.ShortHashForLog(sid), mcp.ShortHashForLog(scope), projectID, uploadDiag)
	start := time.Now()
	var resultErr error
	defer func() {
		log.Printf("[bff-mcp] done tool=%s operation=%s scope_hash=%s project_id=%s elapsed_ms=%d err=%v%s",
			req.Tool, operation, mcp.ShortHashForLog(scope), projectID, time.Since(start).Milliseconds(), resultErr, uploadDiag)
	}()

	// Pre-check the per-submission file cap on a new upload. Rejecting at the
	// "start" step prevents any bytes from being sent upstream once the cap is
	// reached. (upload_asset_chunk's operation lives in req.Args.)
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
	if req.Tool == "create_remotion_video_share" {
		// Never trust a browser-supplied fairness key. Bind scheduling to the
		// authenticated WeChat identity (or this BFF session when anonymous).
		if req.Args == nil {
			req.Args = make(map[string]interface{})
		}
		req.Args["queue_owner_id"] = renderQueueOwnerID(sid)
	}

	res, err := h.Store.Call(scope, req.Tool, req.Args)
	if err != nil {
		resultErr = err
		log.Printf("[bff-mcp] upstream_failed tool=%s operation=%s sid_hash=%s project_id=%s err=%v%s", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, err, uploadDiag)
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	if failure, ok := res["error"].(string); ok && failure != "" {
		resultErr = fmt.Errorf("%s", failure)
		log.Printf("[bff-mcp] tool_error tool=%s operation=%s sid_hash=%s project_id=%s error=%q%s", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, failure, uploadDiag)
	}
	if req.Tool == "upload_asset_chunk" && originalFilename != "" && storedFilename != "" && res != nil {
		// Keep the user-facing name available while the upstream receives only
		// the safe basename. The frontend can display the original name and use
		// stored_filename for any later asset lookup.
		res["original_filename"] = originalFilename
		res["stored_filename"] = storedFilename
	}

	// A successful render submission enters the caller's own render queue. The
	// queue is keyed by the stable owner identity (scope), so each user only ever
	// sees their own jobs — never another caller's (owner isolation is structural,
	// not a filter) and the same account sees the same queue across machines.
	if req.Tool == "create_remotion_video_share" {
		if jobID := digString(res, "render_job_id"); jobID != "" {
			name, _ := req.Args["title"].(string)
			if name == "" {
				name = "帧流作品"
			}
			resLabel, _ := req.Args["aspect_ratio"].(string)
			if resLabel == "" {
				resLabel = "9:16"
			}
			jobStatus := "渲染中"
			if mapUpstreamStatus(digString(res, "status")) == "排队" {
				jobStatus = "排队"
			}
			h.Store.RecordJob(scope, mcp.RenderJob{
				JobID:     jobID,
				Name:      name,
				Res:       resLabel,
				Status:    jobStatus,
				CreatedAt: time.Now(),
			})
		}
	}

	// Update counters only after an explicitly successful complete. Batch counts
	// are durable and authoritative; the session counter is only a legacy
	// script-mode fallback.
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "complete" {
			h.recordUploadComplete(scope, projectID, res, uploadDiag)
		}
	} else if req.Tool == "create_remotion_video_share" {
		h.Store.ResetAsset(scope)
	}

	c.JSON(http.StatusOK, res)
}

func uploadCompleteSucceeded(res map[string]interface{}) bool {
	success, ok := res["success"].(bool)
	if !ok || !success {
		return false
	}
	errorText, _ := res["error"].(string)
	return strings.TrimSpace(errorText) == ""
}

func uploadWasDeduplicated(res map[string]interface{}) bool {
	deduplicated, _ := res["deduplicated"].(bool)
	return deduplicated
}

func authoritativeAssetCount(res map[string]interface{}) (int, bool) {
	if res == nil {
		return 0, false
	}
	var count int
	switch value := res["asset_count"].(type) {
	case int:
		count = value
	case int64:
		if value < 0 || value > int64(imagebatch.MaxBatchImages) {
			return 0, false
		}
		count = int(value)
	case float64:
		if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 || value > float64(imagebatch.MaxBatchImages) || math.Trunc(value) != value {
			return 0, false
		}
		count = int(value)
	default:
		return 0, false
	}
	return count, count >= 0 && count <= imagebatch.MaxBatchImages
}

func (h *Handlers) recordUploadComplete(scope, projectID string, res map[string]interface{}, uploadDiag string) {
	if !uploadCompleteSucceeded(res) {
		log.Printf("[bff-mcp] upload_complete_not_counted scope_hash=%s project_id=%s deduplicated=%t%s", mcp.ShortHashForLog(scope), projectID, uploadWasDeduplicated(res), uploadDiag)
		return
	}
	if projectID != "" && h.ImageBatches != nil {
		batch, err := h.ImageBatches.ByProject(scope, projectID)
		if err != nil {
			log.Printf("[bff-mcp] upload_batch_lookup_failed scope_hash=%s project_id=%s err=%v%s", mcp.ShortHashForLog(scope), projectID, err, uploadDiag)
			return
		}
		if batch == nil || batch.Status != "collecting" {
			// Names such as frameflow-default are used by legacy script-mode
			// uploads and do not identify a durable image batch.
			if uploadWasDeduplicated(res) {
				return
			}
			h.Store.IncAsset(scope)
			return
		}
		if count, ok := authoritativeAssetCount(res); ok {
			if _, err := h.ImageBatches.SetAssetCount(scope, projectID, count); err != nil {
				log.Printf("[bff-mcp] upload_asset_count_sync_failed scope_hash=%s project_id=%s asset_count=%d err=%v%s", mcp.ShortHashForLog(scope), projectID, count, err, uploadDiag)
			}
			return
		}
		if uploadWasDeduplicated(res) {
			log.Printf("[bff-mcp] upload_deduplicated_without_count scope_hash=%s project_id=%s%s", mcp.ShortHashForLog(scope), projectID, uploadDiag)
			return
		}
		if _, err := h.ImageBatches.IncAsset(scope, projectID); err != nil {
			log.Printf("[bff-mcp] upload_asset_count_fallback_failed scope_hash=%s project_id=%s err=%v%s", mcp.ShortHashForLog(scope), projectID, err, uploadDiag)
			return
		}
		log.Printf("[bff-mcp] upload_asset_count_fallback_increment scope_hash=%s project_id=%s%s", mcp.ShortHashForLog(scope), projectID, uploadDiag)
		return
	}
	if uploadWasDeduplicated(res) {
		return
	}
	h.Store.IncAsset(scope)
}

func sanitizeUploadFilename(args map[string]interface{}) {
	filename, ok := args["filename"].(string)
	if !ok || filename == "" {
		return
	}
	if safe, renamed := safeUploadFilename(filename); renamed {
		args["filename"] = safe
	}
}

func safeUploadFilename(filename string) (string, bool) {
	if uploadFilenamePattern.MatchString(filename) {
		return filename, false
	}
	// Treat both slash styles as path separators, then retain only a safe
	// extension. A short hash prevents two different user filenames from
	// colliding in the same MCP session after normalization.
	base := path.Base(strings.ReplaceAll(filename, "\\", "/"))
	ext := ""
	if dot := strings.LastIndex(base, "."); dot >= 0 && dot+1 < len(base) {
		candidate := base[dot:]
		if uploadExtensionPattern.MatchString(candidate) {
			ext = strings.ToLower(candidate)
		}
	}
	hash := sha256.Sum256([]byte(filename))
	stem := readableUploadStem(base, ext)
	safe := fmt.Sprintf("upload-%x-%s%s", hash[:4], stem, ext)
	return safe, true
}

func readableUploadStem(base, ext string) string {
	stem := strings.TrimSuffix(base, ext)
	var b strings.Builder
	lastUnderscore := false
	for _, r := range stem {
		switch {
		case r >= 'A' && r <= 'Z', r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
			lastUnderscore = false
		case r == ' ' || r == '.' || r == '(' || r == ')' || r == '[' || r == ']':
			if !lastUnderscore {
				b.WriteByte('_')
				lastUnderscore = true
			}
		}
	}
	readable := strings.Trim(b.String(), "_-.")
	if readable == "" {
		return "image"
	}
	if len(readable) > 80 {
		readable = readable[:80]
	}
	return readable
}

// uploadArgsSummary emits only non-content diagnostics. The original filename
// is intentionally represented by a short hash so logs can correlate retries
// without leaking a user's local path or filename.
func uploadArgsSummary(args map[string]interface{}) string {
	filename, _ := args["filename"].(string)
	hash := sha256.Sum256([]byte(filename))
	ext := ""
	if dot := strings.LastIndex(filename, "."); dot >= 0 && dot+1 < len(filename) {
		ext = strings.ToLower(filename[dot:])
	}
	totalBytes := numberString(args["total_bytes"])
	offset := numberString(args["offset"])
	_, hasUploadID := args["upload_id"].(string)
	return fmt.Sprintf("upload_diag={filename_hash=%x filename_len=%d filename_safe=%t extension=%q total_bytes=%s offset=%s upload_id_present=%t}", hash[:4], len([]byte(filename)), uploadFilenamePattern.MatchString(filename), ext, totalBytes, offset, hasUploadID)
}

func numberString(value interface{}) string {
	switch number := value.(type) {
	case float64:
		return strconv.FormatInt(int64(number), 10)
	case int:
		return strconv.Itoa(number)
	case int64:
		return strconv.FormatInt(number, 10)
	default:
		return "-"
	}
}

// quotaRejectForUpload decides whether an upload_asset_chunk "start" must be
// rejected for quota reasons. It returns (reject, httpStatus, body).
//
// The check is batch-aware. When the upload targets an active ("collecting")
// image batch we enforce the cap against that batch's authoritative committed
// image count (b.AssetCount), NOT the leaky session-wide counter. The
// session counter only resets on a successful render, so across abandoned
// batches and repeated retries it over-counts and would block a legitimate
// retry before the user reaches the required minimum of 5 images — a 422
// deadlock. Using the batch count keeps the quota scoped to the current
// submission and always leaves room to reach the minimum. The session-wide
// cap remains the fallback for script mode / uploads without a batch.
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
