// Multi-upstream MCP reverse proxy.
//
// Routes incoming HTTP requests to one of N upstream MCP servers based on
// URL path prefix. All routes require `Authorization: Bearer PROXY_CLIENT_TOKEN`.
//
// Built-in upstream slots:
//
//   /mcp, /mcp/                -> UPSTREAM_MCP_URL   (OpenMontage: Bearer auth, path-rewriting)
//   /render-progress, ...      -> UPSTREAM_MCP_URL   (OpenMontage SSE: path-preserving)
//   /voicebox, /voicebox/      -> VOICEBOX_UPSTREAM_URL (Voicebox: pass-through X-Voicebox-Client-Id)
//
// Voicebox slot is opt-in via VOICEBOX_UPSTREAM_URL. To add a third upstream,
// extend the upstreamRegistry below.
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/joho/godotenv"
)

// upstream describes one MCP server this proxy can route to.
type upstream struct {
	name         string
	listenPrefix string // inbound path prefix clients hit, e.g. "/voicebox"
	upstreamURL  *url.URL
	// authStrategy controls what gets injected into outbound requests:
	//   "bearer-static" -> always set Authorization: Bearer <staticToken>
	//   "voicebox-passthrough" -> keep caller's X-Voicebox-Client-Id, fall back to default
	authStrategy string
	staticToken  string // for "bearer-static"
	defaultCID   string // for "voicebox-passthrough" fallback
	// rewriteMode controls how the inbound path maps to the upstream path:
	//   "always-upstream-path" -> always send to upstreamURL.Path (typical for /mcp)
	//   "preserve-suffix" -> replace listenPrefix with upstreamURL.Path, keep the rest
	rewriteMode string
	// pathSlash controls whether the rewritten path ends with a "/":
	//   "trailing-slash" -> always append "/" (e.g. Voicebox via FastMCP mount)
	//   "no-slash" -> strip trailing "/" (e.g. OpenMontage Starlette mount)
	pathSlash string
}

type config struct {
	upstreams   []*upstream
	clientToken string
	listenAddr  string
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func sessionHash(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "-"
	}
	digest := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", digest[:])[:16]
}

func newRequestID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func loadConfig() config {
	if err := godotenv.Load(); err != nil {
		log.Printf("No .env file found, using environment variables")
	}

	// OpenMontage upstream (legacy single-upstream variables preserved).
	rawURL := firstNonEmpty(os.Getenv("UPSTREAM_MCP_URL"), os.Getenv("MCP_URL"))
	if rawURL == "" {
		log.Fatalf("UPSTREAM_MCP_URL is required; configure the upstream MCP endpoint in .env")
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme == "" || u.Host == "" || u.Path == "" {
		log.Fatalf("UPSTREAM_MCP_URL must be a full MCP endpoint URL: %q", rawURL)
	}
	upstreamToken := firstNonEmpty(os.Getenv("UPSTREAM_MCP_TOKEN"), os.Getenv("mcp_key"))
	if upstreamToken == "" {
		log.Fatalf("UPSTREAM_MCP_TOKEN (or legacy mcp_key) is not set")
	}
	clientToken := strings.TrimSpace(os.Getenv("PROXY_CLIENT_TOKEN"))
	if clientToken == "" {
		log.Fatalf("PROXY_CLIENT_TOKEN is required; refusing to expose the upstream MCP token")
	}

	upstreams := []*upstream{
		{
			name:         "openmontage",
			listenPrefix: "/mcp",
			upstreamURL:  u,
			authStrategy: "bearer-static",
			staticToken:  upstreamToken,
			rewriteMode:  "always-upstream-path",
			pathSlash:    "no-slash", // Starlette mount("/mcp", ...) routes /mcp (no slash)
		},
	}

	// Voicebox upstream (opt-in).
	voiceboxURL := firstNonEmpty(os.Getenv("VOICEBOX_UPSTREAM_URL"), os.Getenv("VOICEBOX_URL"))
	if voiceboxURL != "" {
		vu, err := url.Parse(voiceboxURL)
		if err != nil || vu.Scheme == "" || vu.Host == "" {
			log.Fatalf("VOICEBOX_UPSTREAM_URL must be a full HTTP URL: %q", voiceboxURL)
		}
		vbPrefix := firstNonEmpty(os.Getenv("VOICEBOX_LISTEN_PREFIX"), "/voicebox")
		if !strings.HasPrefix(vbPrefix, "/") {
			log.Fatalf("VOICEBOX_LISTEN_PREFIX must start with '/': %q", vbPrefix)
		}
		upstreams = append(upstreams, &upstream{
			name:         "voicebox",
			listenPrefix: vbPrefix,
			upstreamURL:  vu,
			authStrategy: "voicebox-passthrough",
			defaultCID:   firstNonEmpty(os.Getenv("VOICEBOX_DEFAULT_CLIENT_ID"), "voicebox-relay"),
			rewriteMode:  "always-upstream-path",
			pathSlash:    "trailing-slash", // FastMCP mount("/mcp", ...) needs /
		})
		log.Printf("Voicebox upstream enabled: %s -> %s", vbPrefix, voiceboxURL)
	} else {
		log.Printf("Voicebox upstream disabled (set VOICEBOX_UPSTREAM_URL to enable)")
	}

	return config{
		upstreams:   upstreams,
		clientToken: clientToken,
		listenAddr:  ":" + firstNonEmpty(os.Getenv("PORT"), "8080"),
	}
}

// normalizePath applies the upstream's slash convention to a base path.
//   "trailing-slash" -> ensure path ends with "/"
//   "no-slash" -> strip trailing "/"
func normalizePath(path, slashMode string) string {
	switch slashMode {
	case "no-slash":
		return strings.TrimRight(path, "/")
	case "trailing-slash":
		return ensureTrailingSlash(path)
	default:
		return path
	}
}

// ensureTrailingSlash returns path with a "/" suffix unless it already has one.
// Empty paths become "/".
func ensureTrailingSlash(path string) string {
	if path == "" {
		return "/"
	}
	if strings.HasSuffix(path, "/") {
		return path
	}
	return path + "/"
}

func acceptHeader(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "application/json, text/event-stream"
	}
	if !strings.Contains(value, "text/event-stream") {
		return value + ", text/event-stream"
	}
	return value
}

func newTransport() http.RoundTripper {
	return &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   20,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 120 * time.Second, // upstream render / re-enum can be slow
		ExpectContinueTimeout: time.Second,
	}
}

// makeDirector returns a Director function tailored to one upstream.
func makeDirector(u *upstream) func(*http.Request) {
	return func(r *http.Request) {
		start := time.Now()
		*r = *r.WithContext(context.WithValue(r.Context(), "mcp_start", start))
		requestID := newRequestID()
		r.URL.Scheme = u.upstreamURL.Scheme
		r.URL.Host = u.upstreamURL.Host
		switch u.rewriteMode {
		case "always-upstream-path":
			r.URL.Path = normalizePath(u.upstreamURL.Path, u.pathSlash)
			r.URL.RawPath = ""
		case "preserve-suffix":
			suffix := strings.TrimPrefix(r.URL.Path, u.listenPrefix)
			if !strings.HasPrefix(suffix, "/") {
				suffix = "/" + suffix
			}
			r.URL.Path = normalizePath(u.upstreamURL.Path, u.pathSlash) + strings.TrimPrefix(suffix, "/")
			r.URL.RawPath = ""
		default:
			r.URL.Path = normalizePath(u.upstreamURL.Path, u.pathSlash)
			r.URL.RawPath = ""
		}
		r.Host = u.upstreamURL.Host

		switch u.authStrategy {
		case "bearer-static":
			r.Header.Set("Authorization", "Bearer "+u.staticToken)
		case "voicebox-passthrough":
			if strings.TrimSpace(r.Header.Get("X-Voicebox-Client-Id")) == "" {
				r.Header.Set("X-Voicebox-Client-Id", u.defaultCID)
			}
		}

		r.Header.Set("Accept", acceptHeader(r.Header.Get("Accept")))
		r.Header.Set("Cache-Control", "no-cache")
		r.Header.Set("X-Request-Id", requestID)
		log.Printf("[%s] >> %s %s -> %s (client=%s session_hash=%s request_id=%s)",
			u.name, r.Method, r.URL.Path, u.upstreamURL.String(),
			r.RemoteAddr, sessionHash(r.Header.Get("Mcp-Session-Id")), requestID)
	}
}

func makeProxy(u *upstream) *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Transport:     newTransport(),
		FlushInterval: -1,
		Director:      makeDirector(u),
		ModifyResponse: func(r *http.Response) error {
			startVal := r.Request.Context().Value("mcp_start")
			elapsed := "?"
			if t, ok := startVal.(time.Time); ok {
				elapsed = time.Since(t).Round(time.Millisecond).String()
			}
			requestID := r.Request.Header.Get("X-Request-Id")
			r.Header.Set("X-Request-Id", requestID)
			log.Printf("[%s] << %s %s upstream=%d (%s) request_id=%s len=%d",
				u.name, r.Request.Method, r.Request.URL.Path, r.StatusCode,
				elapsed, requestID, r.ContentLength)
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			requestID := r.Header.Get("X-Request-Id")
			log.Printf("[%s] XX %s %s request_id=%s transport error: %v",
				u.name, r.Method, r.URL.Path, requestID, err)
			http.Error(w, "MCP upstream unavailable", http.StatusBadGateway)
		},
	}
}

func auth(next http.Handler, expected string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+expected {
			log.Printf("[auth] 401 unauthorized %s %s from %s",
				r.Method, r.URL.Path, r.RemoteAddr)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func setupLogging() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	path := firstNonEmpty(os.Getenv("LOG_FILE"), "proxy.log")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		log.Printf("WARN: cannot open log file %q (%v); falling back to stderr/journald", path, err)
		return
	}
	log.SetOutput(f)
	log.Printf("logging to file %q", path)
}

func main() {
	setupLogging()
	cfg := loadConfig()

	mux := http.NewServeMux()

	// /mcp + /mcp/      : OpenMontage MCP (path rewritten to upstream /mcp)
	// /render-progress*  : OpenMontage SSE (path preserved)
	// /voicebox, /voicebox/* : Voicebox MCP (path rewritten, passthrough client id)
	for _, u := range cfg.upstreams {
		rewrite := u.rewriteMode
		if u.name == "openmontage" {
			rewrite = "preserve-suffix-for-render-progress-only"
			_ = rewrite
		}
		switch u.name {
		case "openmontage":
			mux.Handle("/mcp", auth(makeProxy(u), cfg.clientToken))
			mux.Handle("/mcp/", auth(makeProxy(u), cfg.clientToken))
			// Render progress uses a separate proxy variant that preserves the
			// inbound path suffix (so /render-progress/{job_id} reaches the SSE
			// endpoint at the upstream's same path).
			rp := u
			rp.rewriteMode = "preserve-suffix"
			mux.Handle("/render-progress", auth(makeProxy(rp), cfg.clientToken))
			mux.Handle("/render-progress/", auth(makeProxy(rp), cfg.clientToken))
		case "voicebox":
			mux.Handle(u.listenPrefix, auth(makeProxy(u), cfg.clientToken))
			mux.Handle(u.listenPrefix+"/", auth(makeProxy(u), cfg.clientToken))
		}
	}

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		log.Printf("[health] %s %s from %s", r.Method, r.URL.Path, r.RemoteAddr)
		w.Header().Set("Content-Type", "application/json")
		upstreamList := make([]map[string]any, 0, len(cfg.upstreams))
		for _, u := range cfg.upstreams {
			upstreamList = append(upstreamList, map[string]any{
				"name":          u.name,
				"listen_prefix": u.listenPrefix,
				"upstream":      u.upstreamURL.String(),
			})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status":      "ok",
			"upstream":    cfg.upstreams[0].upstreamURL.String(),
			"upstreams":   upstreamList,
			"client_auth": true,
		})
	})

	server := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("Starting MCP proxy on %s", cfg.listenAddr)
	log.Printf("Upstreams: %d (auth required: yes)", len(cfg.upstreams))
	for _, u := range cfg.upstreams {
		log.Printf("  - %s: %s -> %s", u.name, u.listenPrefix, u.upstreamURL.String())
	}
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}