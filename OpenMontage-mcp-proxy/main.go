package main

import (
	"encoding/json"
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

type proxyConfig struct {
	upstreamURL                            *url.URL
	upstreamToken, clientToken, listenAddr string
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func loadConfig() proxyConfig {
	if err := godotenv.Load(); err != nil {
		log.Printf("No .env file found, using environment variables")
	}
	rawURL := strings.TrimSpace(os.Getenv("UPSTREAM_MCP_URL"))
	if rawURL == "" {
		log.Fatal("UPSTREAM_MCP_URL is required; configure the upstream MCP endpoint in .env")
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme == "" || u.Host == "" || u.Path == "" {
		log.Fatalf("UPSTREAM_MCP_URL must be a full MCP endpoint URL: %q", rawURL)
	}
	upstreamToken := firstNonEmpty(os.Getenv("UPSTREAM_MCP_TOKEN"), os.Getenv("mcp_key"))
	if upstreamToken == "" {
		log.Fatal("UPSTREAM_MCP_TOKEN (or legacy mcp_key) is not set")
	}
	clientToken := strings.TrimSpace(os.Getenv("PROXY_CLIENT_TOKEN"))
	if clientToken == "" {
		log.Fatal("PROXY_CLIENT_TOKEN is required; refusing to expose the upstream MCP token")
	}
	return proxyConfig{upstreamURL: u, upstreamToken: upstreamToken, clientToken: clientToken, listenAddr: ":" + firstNonEmpty(os.Getenv("PORT"), "8080")}
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
		Proxy:             http.ProxyFromEnvironment,
		DialContext:       (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2: true, MaxIdleConns: 100, MaxIdleConnsPerHost: 20,
		IdleConnTimeout: 90 * time.Second, TLSHandshakeTimeout: 15 * time.Second,
		ExpectContinueTimeout: time.Second,
	}
}

func buildProxy(cfg proxyConfig) *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Transport: newTransport(), FlushInterval: -1,
		Director: func(r *http.Request) {
			r.URL.Scheme, r.URL.Host = cfg.upstreamURL.Scheme, cfg.upstreamURL.Host
			r.URL.Path, r.URL.RawPath = cfg.upstreamURL.Path, cfg.upstreamURL.RawPath
			r.URL.RawQuery, r.Host = r.URL.RawQuery, cfg.upstreamURL.Host
			r.Header.Set("Authorization", "Bearer "+cfg.upstreamToken)
			r.Header.Set("Accept", acceptHeader(r.Header.Get("Accept")))
			r.Header.Set("Cache-Control", "no-cache")
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("upstream MCP request failed: %v", err)
			http.Error(w, "MCP upstream unavailable", http.StatusBadGateway)
		},
	}
}

func auth(next http.Handler, expected string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+expected {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func main() {
	cfg := loadConfig()
	proxy := buildProxy(cfg)
	mux := http.NewServeMux()
	mux.Handle("/mcp", auth(proxy, cfg.clientToken))
	mux.Handle("/mcp/", auth(proxy, cfg.clientToken))
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "upstream": cfg.upstreamURL.String(), "upstream_auth": true, "client_auth": true})
	})
	server := &http.Server{Addr: cfg.listenAddr, Handler: mux, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 120 * time.Second}
	log.Printf("Starting MCP proxy on %s", cfg.listenAddr)
	log.Printf("Upstream: %s; client authentication enabled", cfg.upstreamURL.String())
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
