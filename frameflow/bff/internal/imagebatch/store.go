package imagebatch

import (
	"database/sql"
	"errors"
	"time"
)

type Batch struct {
	ID          string    `json:"id"`
	ProjectID   string    `json:"project_id"`
	ScriptID    string    `json:"script_id"`
	Status      string    `json:"status"`
	AssetCount  int       `json:"asset_count"`
	RenderJobID string    `json:"render_job_id,omitempty"`
	VideoURL    string    `json:"video_url,omitempty"`
	Error       string    `json:"error,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
	SessionID   string    `json:"-"`
}

type Store struct{ db *sql.DB }

func NewStore(db *sql.DB) *Store { return &Store{db: db} }

func (s *Store) Create(sessionID, id, projectID, scriptID string) (*Batch, error) {
	now := time.Now().UTC()
	stamp := now.Format(time.RFC3339Nano)
	_, err := s.db.Exec(`INSERT INTO image_batches (id,session_id,project_id,script_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)`, id, sessionID, projectID, scriptID, "collecting", stamp, stamp)
	if err != nil {
		return nil, err
	}
	return &Batch{ID: id, SessionID: sessionID, ProjectID: projectID, ScriptID: scriptID, Status: "collecting", CreatedAt: now, UpdatedAt: now}, nil
}

const columns = `id,session_id,project_id,script_id,status,asset_count,render_job_id,video_url,error,created_at,updated_at`

func scanRow(row *sql.Row) (*Batch, error) {
	var b Batch
	var created, updated string
	if err := row.Scan(&b.ID, &b.SessionID, &b.ProjectID, &b.ScriptID, &b.Status, &b.AssetCount, &b.RenderJobID, &b.VideoURL, &b.Error, &created, &updated); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	b.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	b.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return &b, nil
}

func (s *Store) Get(sessionID, id string) (*Batch, error) {
	return scanRow(s.db.QueryRow(`SELECT `+columns+` FROM image_batches WHERE session_id=? AND id=?`, sessionID, id))
}
func (s *Store) ByProject(sessionID, projectID string) (*Batch, error) {
	return scanRow(s.db.QueryRow(`SELECT `+columns+` FROM image_batches WHERE session_id=? AND project_id=?`, sessionID, projectID))
}

func (s *Store) IncAsset(sessionID, projectID string) (*Batch, error) {
	_, err := s.db.Exec(`UPDATE image_batches SET asset_count=asset_count+1,updated_at=? WHERE session_id=? AND project_id=?`, time.Now().UTC().Format(time.RFC3339Nano), sessionID, projectID)
	if err != nil {
		return nil, err
	}
	return s.ByProject(sessionID, projectID)
}

func (s *Store) Update(sessionID, id string, fn func(*Batch)) (*Batch, error) {
	b, err := s.Get(sessionID, id)
	if err != nil {
		return nil, err
	}
	fn(b)
	b.UpdatedAt = time.Now().UTC()
	_, err = s.db.Exec(`UPDATE image_batches SET status=?,asset_count=?,render_job_id=?,video_url=?,error=?,updated_at=? WHERE session_id=? AND id=?`, b.Status, b.AssetCount, b.RenderJobID, b.VideoURL, b.Error, b.UpdatedAt.Format(time.RFC3339Nano), sessionID, id)
	if err != nil {
		return nil, err
	}
	return b, nil
}

func (s *Store) List(sessionID string) ([]*Batch, error) {
	rows, err := s.db.Query(`SELECT `+columns+` FROM image_batches WHERE session_id=? ORDER BY created_at DESC`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []*Batch{}
	for rows.Next() {
		var b Batch
		var created, updated string
		if err := rows.Scan(&b.ID, &b.SessionID, &b.ProjectID, &b.ScriptID, &b.Status, &b.AssetCount, &b.RenderJobID, &b.VideoURL, &b.Error, &created, &updated); err != nil {
			return nil, err
		}
		b.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
		b.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
		out = append(out, &b)
	}
	return out, rows.Err()
}
