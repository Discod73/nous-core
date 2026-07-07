package config

import (
	"os"
	"time"

	"github.com/caarlos0/env/v11"
	"github.com/joho/godotenv"
)

type Config struct {
	// Store (PostgreSQL only)
	StoreDSN      string `env:"STASH_POSTGRES_DSN,required"`
	VectorDim     int    `env:"STASH_VECTOR_DIM,required"`
	MaxResultSize int    `env:"STASH_MAX_RESULT_SIZE,required"`

	// OpenAI (embeddings + reasoning)
	OpenAIAPIKey   string `env:"STASH_OPENAI_API_KEY,required"`
	OpenAIBaseURL  string `env:"STASH_OPENAI_BASE_URL,required"`
	EmbeddingModel string `env:"STASH_EMBEDDING_MODEL,required"`
	ReasonerModel  string `env:"STASH_REASONER_MODEL,required"`

	// Memory
	ContextTTL time.Duration `env:"STASH_CONTEXT_TTL,required"`

	// Server
	HTTPAddr  string `env:"STASH_HTTP_ADDR,required"`
	LogLevel  string `env:"STASH_LOG_LEVEL,required"`
	LogFormat string `env:"STASH_LOG_FORMAT,required"`

	// Consolidation
	ConsolidationBatchSize           int     `env:"STASH_CONSOLIDATION_BATCH_SIZE" envDefault:"100"`
	ConsolidationSimilarityThreshold float64 `env:"STASH_CONSOLIDATION_SIMILARITY_THRESHOLD" envDefault:"0.85"`
	ConsolidationDedupThreshold      float64 `env:"STASH_CONSOLIDATION_DEDUP_THRESHOLD" envDefault:"0.95"`
	ConsolidationWindow              string  `env:"STASH_CONSOLIDATION_WINDOW" envDefault:"168h"`
	DecayFactor                          float64 `env:"STASH_DECAY_FACTOR" envDefault:"0.95"`
	ExpiryThreshold                      float32 `env:"STASH_EXPIRY_THRESHOLD" envDefault:"0.1"`
	HypothesisAutoConfirmThreshold       float32 `env:"STASH_HYPOTHESIS_AUTO_CONFIRM_THRESHOLD" envDefault:"0.9"`
	HypothesisAutoRejectThreshold        float32 `env:"STASH_HYPOTHESIS_AUTO_REJECT_THRESHOLD" envDefault:"0.9"`
}

func NewFromFile(filename string) (*Config, error) {
	if _, err := os.Stat(filename); err == nil {
		if err := godotenv.Load(filename); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	cfg := &Config{}
	opts := env.Options{
		RequiredIfNoDef: true,
	}
	if err := env.ParseWithOptions(cfg, opts); err != nil {
		return nil, err
	}
	return cfg, nil
}
