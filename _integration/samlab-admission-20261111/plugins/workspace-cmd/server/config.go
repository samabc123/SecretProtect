package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

func loadConfig() (config, error) {
	tz, err := time.LoadLocation(env("TZ", "Asia/Shanghai"))
	if err != nil {
		tz = time.FixedZone("Asia/Shanghai", 8*60*60)
	}

	dbURL := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	if dbURL == "" {
		databaseURL := &url.URL{
			Scheme: "postgres",
			User:   url.UserPassword(env("DATABASE_USER", os.Getenv("POSTGRES_USER")), env("DATABASE_PASSWORD", os.Getenv("POSTGRES_PASSWORD"))),
			Host:   net.JoinHostPort(env("DATABASE_HOST", "postgres"), env("DATABASE_PORT", "5432")),
			Path:   "/" + env("DATABASE_DBNAME", env("POSTGRES_DB", "sub2api")),
		}
		dbURL = databaseURL.String()
	}
	redisDB, _ := strconv.Atoi(env("REDIS_DB", "1"))
	embeddingDimensions, err := strconv.Atoi(env("PROMPT_EMBEDDING_DIMENSIONS", "1024"))
	if err != nil || embeddingDimensions != promptEmbeddingDimensions {
		return config{}, fmt.Errorf("PROMPT_EMBEDDING_DIMENSIONS must be %d", promptEmbeddingDimensions)
	}
	memoryWorkerInterval, err := time.ParseDuration(env("MEMORY_WORKER_INTERVAL", "5m"))
	if err != nil || memoryWorkerInterval < time.Minute {
		return config{}, errors.New("MEMORY_WORKER_INTERVAL must be at least 1m")
	}
	knowledgeWorkerInterval, err := time.ParseDuration(env("KNOWLEDGE_WORKER_INTERVAL", "5m"))
	if err != nil || knowledgeWorkerInterval < time.Minute {
		return config{}, errors.New("KNOWLEDGE_WORKER_INTERVAL must be at least 1m")
	}
	secret := strings.TrimSpace(os.Getenv("SESSION_SECRET"))
	if len(secret) < 32 || secret == "change-me" {
		return config{}, errors.New("SESSION_SECRET must be a random value of at least 32 characters")
	}

	cookieSecure := strings.EqualFold(env("COOKIE_SECURE", "true"), "true")
	deepSeekKey := strings.TrimSpace(os.Getenv("DEEPSEEK_API_KEY"))
	deepSeekBaseURL := strings.TrimRight(env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), "/")
	deepSeekModel := env("DEEPSEEK_MODEL", "deepseek-v4-flash")
	promptModelAPIKey := strings.TrimSpace(os.Getenv("PROMPT_SERVICE_API_KEY"))
	if promptModelAPIKey == "" {
		promptModelAPIKey = deepSeekKey
	}
	return config{
		ListenAddr:                env("LISTEN_ADDR", ":8090"),
		UpstreamURL:               strings.TrimRight(env("UPSTREAM_URL", "http://sub2api:8080"), "/"),
		DatabaseURL:               dbURL,
		RedisAddr:                 env("REDIS_ADDR", "redis:6379"),
		RedisPassword:             os.Getenv("REDIS_PASSWORD"),
		RedisDB:                   redisDB,
		DeepSeekKey:               deepSeekKey,
		DeepSeekBaseURL:           deepSeekBaseURL,
		DeepSeekModel:             deepSeekModel,
		PromptModelBaseURL:        strings.TrimRight(env("PROMPT_MODEL_BASE_URL", deepSeekBaseURL), "/"),
		PromptModelAPIKey:         promptModelAPIKey,
		PromptPolishModel:         env("PROMPT_POLISH_MODEL", deepSeekModel),
		PromptEmbeddingBaseURL:    strings.TrimRight(env("PROMPT_EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"), "/"),
		PromptEmbeddingAPIKey:     strings.TrimSpace(os.Getenv("PROMPT_EMBEDDING_API_KEY")),
		PromptEmbeddingModel:      env("PROMPT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
		PromptEmbeddingDimensions: embeddingDimensions,
		PrivacyFilterURL:          strings.TrimRight(env("PRIVACY_FILTER_URL", "http://privacy-filter:8088"), "/"),
		MemoryWorkerInterval:      memoryWorkerInterval,
		KnowledgeWorkerInterval:   knowledgeWorkerInterval,
		StorageDir:                env("STORAGE_DIR", "/data/files"),
		SessionSecret:             secret,
		CookieSecure:              cookieSecure,
		Timezone:                  tz,
		MigrationsDir:             env("MIGRATIONS_DIR", "/app/migrations"),
		RunMigrations:             strings.EqualFold(env("RUN_MIGRATIONS", "true"), "true"),
		ScoringEngineVersion:      scoringEngineVersionValue(env("SCORING_ENGINE_VERSION", "v5")),
		ScoringSelfConsistency:    selfConsistencySamples(env("SCORING_SELF_CONSISTENCY_SAMPLES", "0")),
		ScoringConfidenceFloor:    confidenceFloor(env("SCORING_CONFIDENCE_THRESHOLD", "0.5")),
	}, nil
}

// scoringEngineVersionValue 规整评分引擎版本，非法值回退到 v5。
func scoringEngineVersionValue(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "v4":
		return "v4"
	default:
		return "v5"
	}
}

// selfConsistencySamples 读取自洽采样次数，默认 0 表示关闭（不额外调用模型）。
func selfConsistencySamples(value string) int {
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return 0
	}
	if parsed > 5 {
		return 5
	}
	return parsed
}

// confidenceFloor 读取置信度复核阈值，需在 0.1 到 0.9 之间。
func confidenceFloor(value string) float64 {
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil || parsed < 0.1 || parsed > 0.9 {
		return 0.5
	}
	return parsed
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func (a *app) migrate(ctx context.Context) error {
	entries, err := os.ReadDir(a.cfg.MigrationsDir)
	if err != nil {
		return err
	}
	if _, err := a.db.Exec(ctx, `CREATE TABLE IF NOT EXISTS workspace_plugin_migrations (
		version integer PRIMARY KEY,
		applied_at timestamptz NOT NULL DEFAULT now()
	)`); err != nil {
		return err
	}

	type migration struct {
		version int
		path    string
	}
	migrations := make([]migration, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		prefix, _, ok := strings.Cut(entry.Name(), "_")
		if !ok {
			continue
		}
		version, err := strconv.Atoi(prefix)
		if err != nil || version <= 0 {
			continue
		}
		migrations = append(migrations, migration{version: version, path: filepath.Join(a.cfg.MigrationsDir, entry.Name())})
	}
	sort.Slice(migrations, func(i, j int) bool { return migrations[i].version < migrations[j].version })
	for _, item := range migrations {
		var applied bool
		if err := a.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM workspace_plugin_migrations WHERE version=$1)`, item.version).Scan(&applied); err != nil {
			return err
		}
		if applied {
			continue
		}
		data, err := os.ReadFile(item.path)
		if err != nil {
			return err
		}
		tx, err := a.db.Begin(ctx)
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, string(data)); err == nil {
			_, err = tx.Exec(ctx, `INSERT INTO workspace_plugin_migrations(version) VALUES($1) ON CONFLICT DO NOTHING`, item.version)
		}
		if err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("migration %d: %w", item.version, err)
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("migration %d commit: %w", item.version, err)
		}
	}
	return nil
}
