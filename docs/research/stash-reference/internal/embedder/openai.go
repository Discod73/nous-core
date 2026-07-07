package embedder

import (
	"context"
	"errors"

	"github.com/openai/openai-go"
	"github.com/openai/openai-go/option"
)

// OpenAI uses the OpenAI-compatible SDK to generate embeddings.
// Works with any OpenAI-compatible endpoint: api.openai.com,
// openrouter.ai, local Ollama, Together, vLLM, etc.
// The model string is passed as-is to the API — no stripping or
// transformation. Use the format your endpoint expects:
//
//	OpenRouter:    "openai/text-embedding-3-small"
//	OpenAI direct: "text-embedding-3-small"
//	Ollama:        "nomic-embed-text"
type OpenAI struct {
	client openai.Client
	model  string
	dims   int
}

// NewOpenAI creates an OpenAI embedder.
// baseURL: the API endpoint (e.g. "https://openrouter.ai/api/v1")
// apiKey:  the API key for the endpoint
// model:   required — the model string for this endpoint (no default)
// dims:    required — the vector dimension for this model (no default)
// Returns error if model or apiKey is empty, or dims <= 0.
func NewOpenAI(baseURL, apiKey, model string, dims int) (*OpenAI, error) {
	if apiKey == "" {
		return nil, errors.New("embedder: apiKey is required")
	}
	if model == "" {
		return nil, errors.New("embedder: model is required")
	}
	if dims <= 0 {
		return nil, errors.New("embedder: dims must be greater than zero")
	}

	client := openai.NewClient(
		option.WithBaseURL(baseURL),
		option.WithAPIKey(apiKey),
	)

	return &OpenAI{
		client: client,
		model:  model,
		dims:   dims,
	}, nil
}

// Model returns the model string as passed at construction.
func (o *OpenAI) Model() string {
	return o.model
}

// Dims returns the vector dimensions as passed at construction.
func (o *OpenAI) Dims() int {
	return o.dims
}

// Embed generates a vector embedding for the given text using the OpenAI API.
func (o *OpenAI) Embed(ctx context.Context, text string) ([]float32, error) {
	resp, err := o.client.Embeddings.New(ctx, openai.EmbeddingNewParams{
		Input: openai.EmbeddingNewParamsInputUnion{
			OfArrayOfStrings: []string{text},
		},
		Model:      o.model,
		Dimensions: openai.Int(int64(o.dims)),
	})
	if err != nil {
		return nil, err
	}

	embedding := resp.Data[0].Embedding
	vec := make([]float32, len(embedding))
	for i := range embedding {
		vec[i] = float32(embedding[i])
	}
	return vec, nil
}
