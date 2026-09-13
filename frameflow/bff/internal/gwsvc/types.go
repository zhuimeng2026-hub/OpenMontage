package gwsvc

// VerbRequest is the JSON body every gateway verb accepts (except production-status,
// which takes project_id as a query param and ignores Payload).
//
// ProjectID is required for tenant validation; Payload is an open bag so each
// verb can grow its own input schema without a breaking type bump. The handler
// is responsible for shape-checking Payload for the specific verb.
type VerbRequest struct {
	ProjectID string                 `json:"project_id" binding:"required"`
	Payload   map[string]interface{} `json:"payload"`
}

// VerbResponse is the JSON shape every gateway verb returns.
//
// Verb echoes the caller's request verb (PascalCase). ProjectID is the project
// the verb acted on. Status is one of the 13-state unified enum from jobsvc
// (StatusCreated / StatusAssetAnalyzing / ...). JobID is set when a new job was
// spawned (storyboard/animatic/sample/render) and omitted for read-only verbs.
// Detail is an open bag for verb-specific extras (e.g. cost_reserved on a render).
type VerbResponse struct {
	Verb      string                 `json:"verb"`
	ProjectID string                 `json:"project_id"`
	Status    string                 `json:"status"` // 13-state enum
	JobID     string                 `json:"job_id,omitempty"`
	Detail    map[string]interface{} `json:"detail,omitempty"`
}
