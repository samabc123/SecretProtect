package main

import (
	"context"
	"embed"
	"errors"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

//go:embed web/*
var webFS embed.FS

const (
	maxUploadBytes = 100 * 1024 * 1024
	cookieName     = "workspace_session"
)

type config struct {
	ListenAddr                string
	UpstreamURL               string
	DatabaseURL               string
	RedisAddr                 string
	RedisPassword             string
	RedisDB                   int
	DeepSeekKey               string
	DeepSeekBaseURL           string
	DeepSeekModel             string
	PromptModelBaseURL        string
	PromptModelAPIKey         string
	PromptPolishModel         string
	PromptEmbeddingBaseURL    string
	PromptEmbeddingAPIKey     string
	PromptEmbeddingModel      string
	PromptEmbeddingDimensions int
	PrivacyFilterURL          string
	MemoryWorkerInterval      time.Duration
	KnowledgeWorkerInterval   time.Duration
	StorageDir                string
	SessionSecret             string
	CookieSecure              bool
	Timezone                  *time.Location
	MigrationsDir             string
	RunMigrations             bool
	// 评分引擎相关。v4 表示沿用旧行为，v5 表示启用档位化与异常检测的新引擎。
	ScoringEngineVersion   string
	ScoringSelfConsistency int
	ScoringConfidenceFloor float64
}

type identity struct {
	ID       int64  `json:"user_id"`
	Username string `json:"username"`
	Email    string `json:"email,omitempty"`
	Role     string `json:"role"`
}

type sessionRecord struct {
	Identity  identity  `json:"identity"`
	ExpiresAt time.Time `json:"expires_at"`
	UpstreamToken string `json:"upstream_token,omitempty"`
}

type app struct {
	cfg                     config
	db                      *pgxpool.Pool
	redis                   *redis.Client
	redisOK                 bool
	http                    *http.Client
	sessions                map[string]sessionRecord
	sessionKey              []byte
	mu                      sync.RWMutex
	lastJob                 string
	reportWake              chan struct{}
	memoryWake              chan struct{}
	knowledgeWake           chan struct{}
	memoryMu                sync.RWMutex
	memoryLastSuccess       time.Time
	memoryLastError         string
	memoryBacklog           int64
	memoryProcessed         int64
	memoryFailures          int64
	memoryLastConsolidation time.Time
	knowledgeMu             sync.RWMutex
	knowledgeLastSuccess    time.Time
	knowledgeLastError      string
	knowledgeBacklog        int64
	knowledgeProcessed      int64
	knowledgeFailures       int64
}

type envelope struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		client := &http.Client{Timeout: 3 * time.Second}
		resp, err := client.Get("http://127.0.0.1:8090/_workspace/health")
		if err != nil || resp.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		_ = resp.Body.Close()
		return
	}

	cfg, err := loadConfig()
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatal(err)
	}
	if err := db.Ping(ctx); err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		if err := (&app{cfg: cfg, db: db}).migrate(ctx); err != nil {
			log.Fatal(err)
		}
		log.Print("workspace migrations applied")
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "sanitize-evidence" {
		updated, sanitizeErr := (&app{cfg: cfg, db: db}).sanitizeStoredEvidence(ctx)
		if sanitizeErr != nil {
			log.Fatal(sanitizeErr)
		}
		log.Printf("sanitized %d weekly evidence cards", updated)
		return
	}

	a := &app{
		cfg:           cfg,
		db:            db,
		http:          &http.Client{Timeout: 180 * time.Second},
		sessions:      make(map[string]sessionRecord),
		sessionKey:    []byte(cfg.SessionSecret),
		reportWake:    make(chan struct{}, 1),
		memoryWake:    make(chan struct{}, 1),
		knowledgeWake: make(chan struct{}, 1),
	}
	if cfg.RedisAddr != "" {
		a.redis = redis.NewClient(&redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPassword, DB: cfg.RedisDB})
		if err := a.redis.Ping(ctx).Err(); err == nil {
			a.redisOK = true
		} else {
			log.Printf("redis unavailable, using in-memory sessions: %v", err)
		}
	}
	if cfg.RunMigrations {
		if err := a.migrate(ctx); err != nil {
			log.Fatal(err)
		}
	}
	if err := os.MkdirAll(cfg.StorageDir, 0700); err != nil {
		log.Fatal(err)
	}
	go a.scheduler(ctx)
	go a.reportWorker(ctx)
	if a.memoryWorkerConfigured() {
		go a.memoryWorker(ctx)
	} else {
		log.Print("prompt memory worker disabled: extraction, privacy-filter, or embedding configuration is incomplete")
	}
	if a.knowledgeWorkerConfigured() {
		go a.knowledgeWorker(ctx)
	} else {
		log.Print("knowledge worker disabled: extraction, privacy-filter, or embedding configuration is incomplete")
	}

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           a.security(a.routes()),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("workspace-plugin listening on %s", cfg.ListenAddr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
