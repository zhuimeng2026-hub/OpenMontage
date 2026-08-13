// Package template models the "fixed script (template) + variable images
// (scenarios) -> batch render" workflow that drives FrameFlow.
//
// A Template holds the render parameters and a project namespace prefix. Each
// Scenario carries a business key; its images are resolved by the
// BusinessImageFetcher and uploaded into a per-scenario project bucket
// (project_base + "-" + scenarioID) so every scenario renders independently.
// A BatchJob tracks the aggregate of rendering many scenarios at once.
package template

import (
	"sync"
	"time"

	"frameflow-bff/internal/business"
)

// Template is the reusable "fixed script": identical render behaviour, only
// the per-scenario images vary.
type Template struct {
	ID               string    `json:"id"`
	Name             string    `json:"name"`
	ProjectBase      string    `json:"project_base"`       // prefix for per-scenario project buckets
	AspectRatio      string    `json:"aspect_ratio"`       // e.g. "9:16"
	DurationPerImage int       `json:"duration_per_image"` // seconds per image
	TitleTemplate    string    `json:"title_template"`
	SessionID        string    `json:"-"`
	CreatedAt        time.Time `json:"created_at"`
	UpdatedAt        time.Time `json:"updated_at"`
}

// Scenario is one variable-data unit: a business key + the images resolved for
// it + the render outcome.
type Scenario struct {
	ID          string              `json:"id"`
	TemplateID  string              `json:"template_id"`
	BusinessKey string              `json:"business_key"`
	ImageRefs   []business.ImageRef `json:"image_refs"`
	Status      string              `json:"status"` // pending|fetching|uploading|rendering|done|failed
	Error       string              `json:"error,omitempty"`
	RenderJobID string              `json:"render_job_id,omitempty"`
	VideoURL    string              `json:"video_url,omitempty"`
	SessionID   string              `json:"-"`
	CreatedAt   time.Time           `json:"created_at"`
}

// BatchJob aggregates rendering N scenarios under one template.
type BatchJob struct {
	ID          string            `json:"id"`
	TemplateID  string            `json:"template_id"`
	ScenarioIDs []string          `json:"scenario_ids"`
	Status      string            `json:"status"`  // running|done
	Outputs     map[string]string `json:"outputs"` // scenarioID -> videoURL
	SessionID   string            `json:"-"`
	CreatedAt   time.Time         `json:"created_at"`
}

// Store keeps templates / scenarios / batch jobs in process memory, keyed by
// BFF session. Swap for a DB + shared cache for production / multi-instance.
type Store struct {
	mu     sync.RWMutex
	bySess map[string]*sessionData
	seq    uint64
}

type sessionData struct {
	templates map[string]*Template
	scenarios map[string]*Scenario
	jobs      map[string]*BatchJob
}

func NewStore() *Store {
	return &Store{bySess: make(map[string]*sessionData)}
}

func (s *Store) data(sid string) *sessionData {
	d, ok := s.bySess[sid]
	if !ok {
		d = &sessionData{
			templates: make(map[string]*Template),
			scenarios: make(map[string]*Scenario),
			jobs:      make(map[string]*BatchJob),
		}
		s.bySess[sid] = d
	}
	return d
}

func (s *Store) nextID(prefix string) string {
	s.seq++
	return prefix + time.Now().Format("20060102150405") + "-" + itoa(s.seq)
}

func itoa(n uint64) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

// ---------- Template ----------

func (s *Store) SaveTemplate(sid, name, projectBase, aspect, titleTpl string, duration int) *Template {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	t := &Template{
		ID:               s.nextID("tpl-"),
		Name:             name,
		ProjectBase:      projectBase,
		AspectRatio:      aspect,
		DurationPerImage: duration,
		TitleTemplate:    titleTpl,
		SessionID:        sid,
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	s.data(sid).templates[t.ID] = t
	return t
}

func (s *Store) GetTemplate(sid, id string) *Template {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.data(sid).templates[id]
}

func (s *Store) ListTemplates(sid string) []*Template {
	s.mu.RLock()
	defer s.mu.RUnlock()
	d := s.data(sid)
	out := make([]*Template, 0, len(d.templates))
	for _, t := range d.templates {
		out = append(out, t)
	}
	return out
}

// ---------- Scenario ----------

func (s *Store) AddScenario(sid, templateID, businessKey string) *Scenario {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	sc := &Scenario{
		ID:          s.nextID("scn-"),
		TemplateID:  templateID,
		BusinessKey: businessKey,
		Status:      "pending",
		SessionID:   sid,
		CreatedAt:   now,
	}
	s.data(sid).scenarios[sc.ID] = sc
	return sc
}

func (s *Store) GetScenario(sid, id string) *Scenario {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.data(sid).scenarios[id]
}

func (s *Store) ListScenarios(sid, templateID string) []*Scenario {
	s.mu.RLock()
	defer s.mu.RUnlock()
	d := s.data(sid)
	out := make([]*Scenario, 0)
	for _, sc := range d.scenarios {
		if sc.TemplateID == templateID {
			out = append(out, sc)
		}
	}
	return out
}

func (s *Store) setScenario(sid, id string, fn func(*Scenario)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if sc := s.data(sid).scenarios[id]; sc != nil {
		fn(sc)
	}
}

func (s *Store) SetScenarioStatus(sid, id, status, errMsg string) {
	s.setScenario(sid, id, func(sc *Scenario) { sc.Status = status; sc.Error = errMsg })
}
func (s *Store) SetScenarioImages(sid, id string, imgs []business.ImageRef) {
	s.setScenario(sid, id, func(sc *Scenario) { sc.ImageRefs = imgs })
}
func (s *Store) SetScenarioRenderJob(sid, id, renderJobID string) {
	s.setScenario(sid, id, func(sc *Scenario) { sc.RenderJobID = renderJobID })
}
func (s *Store) SetScenarioVideo(sid, id, url string) {
	s.setScenario(sid, id, func(sc *Scenario) { sc.VideoURL = url })
}

// ---------- BatchJob ----------

func (s *Store) CreateBatchJob(sid, templateID string, scenarioIDs []string) *BatchJob {
	s.mu.Lock()
	defer s.mu.Unlock()
	job := &BatchJob{
		ID:          s.nextID("job-"),
		TemplateID:  templateID,
		ScenarioIDs: scenarioIDs,
		Status:      "running",
		Outputs:     make(map[string]string),
		SessionID:   sid,
		CreatedAt:   time.Now(),
	}
	s.data(sid).jobs[job.ID] = job
	return job
}

func (s *Store) GetBatchJob(sid, id string) *BatchJob {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.data(sid).jobs[id]
}

func (s *Store) SetJobOutput(sid, jobID, scenarioID, url string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job := s.data(sid).jobs[jobID]; job != nil {
		job.Outputs[scenarioID] = url
	}
}

func (s *Store) SetJobDone(sid, jobID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if job := s.data(sid).jobs[jobID]; job != nil {
		job.Status = "done"
	}
}
