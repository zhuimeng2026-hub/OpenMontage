package handlers

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
)

const maxMCPBodyBytes = 2 << 20

var uploadFilenamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`)
var uploadExtensionPattern = regexp.MustCompile(`^\.[A-Za-z0-9]{1,10}$`)

// The browser-facing tool allowlist now lives in config (MCP_ALLOWED_TOOLS,
// falling back to config.DefaultMCPAllowedTools) so deployments can tighten it
// without a rebuild. See mcpToolAllowed below.

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
	// tools/list and initialize are intentionally not accepted here: they are
	// internal MCP client operations, never browser-facing tool calls.
	if !h.mcpToolAllowed(req.Tool) {
		c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("tool %q is not allowed", req.Tool)})
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
	// Any render submission — the photo/video share plus the two FrameFlow media
	// workflows (captions, cloned voice) — enters the caller's own render queue,
	// so those jobs are visible in "我的渲染任务" like any other.
	if isRenderSubmissionTool(req.Tool) {
		if jobID := renderJobID(res); jobID != "" {
			name := firstString(req.Args, "title", "name")
			if name == "" {
				name = "帧流作品"
			}
			resLabel := firstString(req.Args, "aspect_ratio", "resolution")
			if resLabel == "" {
				resLabel = firstString(res, "aspect_ratio", "resolution")
			}
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
	} else if isRenderSubmissionTool(req.Tool) {
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

// MCPRawProxy is a transparent JSON-RPC passthrough for external (non-browser)
// callers. It accepts standard MCP JSON-RPC envelopes (initialize / tools/list
// / tools/call) and forwards them verbatim to the upstream MCP through the
// SessionStore's per-owner Client, returning the upstream response unchanged
// (including SSE framing). The owner key is derived from the bearer token via
// RequireBearer (see auth.go) and never collides with any WeChat user.
//
// Unlike MCPProxy which reshapes requests to {tool, args}, this endpoint
// preserves the upstream MCP protocol — it is intended for CLI/agent callers
// that already speak MCP and don't need the browser-friendly adapter.
//
// Body size cap: 256 MB. Larger payloads should not go through the BFF —
// upload them via upload_asset_chunk or the dedicated asset endpoint instead.
func (h *Handlers) MCPRawProxy(c *gin.Context) {
	const maxRawBody = 256 * 1024 * 1024
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxRawBody)
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request body exceeds 256 MB"})
		return
	}
	if len(body) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "empty request body"})
		return
	}
	// Parse just enough to log the method name. Malformed JSON still gets
	// forwarded so the upstream produces a proper JSON-RPC -32600 error.
	var peek struct {
		Method string `json:"method"`
	}
	_ = json.Unmarshal(body, &peek)
	method := peek.Method
	if method == "" {
		method = "unknown"
	}
	// Pull the validated bearer token out of the gin Context (set by RequireBearer).
	tokenVal, _ := c.Get("agent_token")
	token, _ := tokenVal.(string)
	if token == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing agent token"})
		return
	}
	scope := renderQueueOwnerIDForAgent(token)
	start := time.Now()
	status, contentType, raw, callErr := h.Store.RawCall(scope, method, body)
	elapsed := time.Since(start).Milliseconds()
	if callErr != nil {
		log.Printf("[bff-mcp-raw] err method=%s scope_hash=%s elapsed_ms=%d err=%v", method, mcp.ShortHashForLog(scope), elapsed, callErr)
		c.JSON(http.StatusBadGateway, gin.H{"error": "upstream mcp unavailable", "detail": callErr.Error()})
		return
	}
	if status >= 400 {
		log.Printf("[bff-mcp-raw] upstream_error method=%s scope_hash=%s elapsed_ms=%d upstream_status=%d body_len=%d",
			method, mcp.ShortHashForLog(scope), elapsed, status, len(raw))
	}
	if contentType == "" {
		contentType = "application/json"
	}
	// Forward the rotating upstream Mcp-Session-Id so the client can keep using
	// the same session across calls (essential for upload_asset_chunk ->
	// create_remotion_video_share continuity). Also forward Content-Length.
	c.Header("Mcp-Session-Id", h.Store.SessionIDForOwner(scope))
	c.Data(status, contentType, raw)
}

// VoiceboxMCPProxy is a thin, stateless JSON-RPC pass-through to the local
// voicebox FastMCP endpoint (default: http://lanes.ymxt.top:8900/voicebox/mcp/).
//
// Why this exists alongside /api/mcp-raw: voicebox's MCP transport does NOT
// rotate Mcp-Session-Id per response in a way that requires client-side
// pinning, and there is no per-owner upload->create continuity to preserve.
// Routing the call through the OpenMontage SessionStore would force a fresh
// SQLite write on every probe and bind all callers to one Mcp-Session-Id,
// which is wrong for an always-streaming, stateless proxy.
//
// Auth: RequireBearer() — same EXTERNAL_AGENT_TOKEN as /api/mcp-raw.
// Body cap: 256 MB (matches MCPRawProxy).
// Header policy (inbound -> outbound):
//   - Authorization: STRIPPED (voicebox has no shared secret here;
//     X-Voicebox-Client-Id is its identity). The BFF never injects a
//     bearer to the upstream.
//   - X-Voicebox-Client-Id: forwarded verbatim; falls back to a stable
//     per-token hash if the caller forgot to set it.
//   - Mcp-Session-Id: forwarded verbatim if present.
//
// Response: streamed (io.Copy) so SSE notifications from voicebox are not
// buffered behind the rest of the body. Status + Content-Type are copied
// from the upstream. No DB writes.
func (h *Handlers) VoiceboxMCPProxy(c *gin.Context) {
	const maxRawBody = 256 * 1024 * 1024
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxRawBody)
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request body exceeds 256 MB"})
		return
	}
	if len(body) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "empty request body"})
		return
	}

	// Peek the JSON-RPC method for logging. Malformed JSON still forwards.
	var peek struct {
		Method string `json:"method"`
	}
	_ = json.Unmarshal(body, &peek)
	method := peek.Method
	if method == "" {
		method = "unknown"
	}

	// Re-confirm the bearer: RequireBearer() would have aborted already if it
	// failed, but checking once in the handler guards against future route-table
	// regressions where the middleware chain is reshuffled.
	tokenVal, _ := c.Get("agent_token")
	token, _ := tokenVal.(string)
	if token == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing agent token"})
		return
	}

	upstreamReq, err := http.NewRequestWithContext(c.Request.Context(), http.MethodPost, h.Cfg.VoiceboxUpstreamURL, bytes.NewReader(body))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "cannot build upstream request", "detail": err.Error()})
		return
	}
	upstreamReq.Header.Set("Content-Type", "application/json")
	upstreamReq.Header.Set("Accept", "application/json, text/event-stream")

	// Auth translation: the inbound Bearer (EXTERNAL_AGENT_TOKEN) authenticated
	// this caller to the BFF; it is NOT a valid MCP_API_TOKEN for OpenMontage.
	// The upstream OpenMontage :8900 enforces BearerTokenAuthMiddleware on
	// every route (including /voicebox/mcp/*), so we must inject the upstream
	// token here. The voicebox fastmcp behind the proxy never sees the
	// Authorization header because the OpenMontage proxy strips it.
	if h.Cfg.MCPAPIToken != "" {
		upstreamReq.Header.Set("Authorization", "Bearer "+h.Cfg.MCPAPIToken)
	}

	// Forward X-Voicebox-Client-Id (with fallback) and Mcp-Session-Id.
	clientID := c.GetHeader("X-Voicebox-Client-Id")
	if clientID == "" {
		sum := sha256.Sum256([]byte("agent:" + strings.TrimSpace(token)))
		clientID = "bff-agent-" + hex.EncodeToString(sum[:6])
	}
	upstreamReq.Header.Set("X-Voicebox-Client-Id", clientID)
	if sid := c.GetHeader("Mcp-Session-Id"); sid != "" {
		upstreamReq.Header.Set("Mcp-Session-Id", sid)
	}

	start := time.Now()
	resp, err := http.DefaultClient.Do(upstreamReq)
	if err != nil {
		log.Printf("[bff-voicebox-mcp] upstream_err method=%s client_id=%s elapsed_ms=%d err=%v",
			method, clientID, time.Since(start).Milliseconds(), err)
		c.JSON(http.StatusBadGateway, gin.H{"error": "upstream voicebox mcp unavailable", "detail": err.Error()})
		return
	}
	defer resp.Body.Close()

	// Copy upstream headers (sans hop-by-hop). Mcp-Session-Id flows back so a
	// caller can pin to it across calls if they want.
	for k, vs := range resp.Header {
		switch strings.ToLower(k) {
		case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
			"te", "trailer", "transfer-encoding", "upgrade":
			continue
		}
		for _, v := range vs {
			c.Header(k, v)
		}
	}

	c.Status(resp.StatusCode)
	if _, err := io.Copy(c.Writer, resp.Body); err != nil {
		log.Printf("[bff-voicebox-mcp] copy_err method=%s client_id=%s elapsed_ms=%d err=%v",
			method, clientID, time.Since(start).Milliseconds(), err)
		return
	}
	log.Printf("[bff-voicebox-mcp] done method=%s client_id=%s upstream_status=%d elapsed_ms=%d",
		method, clientID, resp.StatusCode, time.Since(start).Milliseconds())
}

// isRenderSubmissionTool reports whether a successful tool call starts a render
// job that must land in the caller's render queue. Besides the photo/video
// share, the two FrameFlow media workflows (captions and cloned voice) submit a
// render as well, so they need the same queue bookkeeping and asset-counter
// reset — otherwise a media job would be invisible in "我的渲染任务" and would
// leave the upload quota stuck at its cap for the next submission.
func isRenderSubmissionTool(tool string) bool {
	switch tool {
	case "create_remotion_video_share", "create_captioned_video_share", "create_cloned_voice_video_share":
		return true
	default:
		return false
	}
}

// renderJobID accepts both spellings: the share tools return render_job_id,
// the media workflows return job_id.
func renderJobID(res map[string]interface{}) string {
	if id := digString(res, "render_job_id"); id != "" {
		return id
	}
	return digString(res, "job_id")
}

// firstString returns the first non-empty string found under any of keys.
func firstString(values map[string]interface{}, keys ...string) string {
	for _, key := range keys {
		if value, ok := values[key].(string); ok && value != "" {
			return value
		}
	}
	return ""
}

// mcpToolAllowed is the server-side allowlist for POST /api/mcp. It is
// configurable through MCP_ALLOWED_TOOLS (CSV) and falls back to
// config.DefaultMCPAllowedTools() when unset. The MCP handshake and tools/list
// are performed internally by the client and never pass through this handler.
func (h *Handlers) mcpToolAllowed(tool string) bool {
	allowed := []string(nil)
	if h != nil && h.Cfg != nil {
		allowed = h.Cfg.MCPAllowedTools
	}
	if len(allowed) == 0 {
		allowed = config.DefaultMCPAllowedTools()
	}
	for _, candidate := range allowed {
		if candidate == tool {
			return true
		}
	}
	return false
}
