package brain

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/pgvector/pgvector-go"
)

// RecallResult is a unified result from semantic search across episodes and facts.
type RecallResult struct {
	ID          int64     `json:"id"`
	NamespaceID int64     `json:"namespace_id"`
	Content     string    `json:"content"`
	Confidence  float32   `json:"confidence,omitempty"`
	Score       float32   `json:"score"`
	Type        string    `json:"type"`
	OccurredAt  string    `json:"occurred_at,omitempty"`
	ValidFrom   string    `json:"valid_from,omitempty"`
	CreatedAt   string    `json:"created_at"`
}

// Recall searches episodes and facts by semantic similarity across the given namespaces.
// Each namespace path matches itself and all descendants. Namespaces is required.
func (b *Brain) Recall(ctx context.Context, namespaces []string, query string, limit int) ([]RecallResult, error) {
	if err := validateContent(query); err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = 10
	}
	if limit > 100 {
		limit = 100
	}

	vec, err := b.embedder.Embed(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("embed: %w", err)
	}

	pgVec := pgvector.NewVector(vec)

	nsIDs, err := b.resolveNamespaceIDs(ctx, namespaces)
	if err != nil {
		return nil, err
	}

	// Search facts first (higher quality, consolidated)
	factLimit := limit
	factSQL, factArgs, err := b.queries.RecallFacts(nsIDs, pgVec, factLimit)
	if err != nil {
		return nil, fmt.Errorf("build fact query: %w", err)
	}

	factRows, err := b.pool.Query(ctx, factSQL, factArgs...)
	if err != nil {
		return nil, fmt.Errorf("query facts: %w", err)
	}
	defer factRows.Close()

	var results []RecallResult
	for factRows.Next() {
		var id int64
		var namespaceID int64
		var content string
		var confidence float32
		var score float32
		var createdAt time.Time

		if err := factRows.Scan(&id, &namespaceID, &content, &confidence, &createdAt, &score); err != nil {
			return nil, fmt.Errorf("scan fact: %w", err)
		}
		results = append(results, RecallResult{
			ID:          id,
			NamespaceID: namespaceID,
			Content:     content,
			Confidence:  confidence,
			Score:       score,
			Type:        "fact",
			CreatedAt:   createdAt.Format(time.RFC3339),
		})
	}
	if err := factRows.Err(); err != nil {
		return nil, fmt.Errorf("fact rows: %w", err)
	}

	// Search episodes for remaining slots
	episodeLimit := limit - len(results)
	if episodeLimit > 0 {
		epSQL, epArgs, err := b.queries.RecallEpisodes(nsIDs, pgVec, episodeLimit)
		if err != nil {
			return nil, fmt.Errorf("build episode query: %w", err)
		}

		epRows, err := b.pool.Query(ctx, epSQL, epArgs...)
		if err != nil {
			return nil, fmt.Errorf("query episodes: %w", err)
		}
		defer epRows.Close()

		for epRows.Next() {
			var id int64
			var namespaceID int64
			var content string
			var score float32
			var occurredAt time.Time
			var createdAt time.Time

			if err := epRows.Scan(&id, &namespaceID, &content, &occurredAt, &createdAt, &score); err != nil {
				return nil, fmt.Errorf("scan episode: %w", err)
			}
			results = append(results, RecallResult{
				ID:          id,
				NamespaceID: namespaceID,
				Content:     content,
				Score:       score,
				Type:        "episode",
				OccurredAt:  occurredAt.Format(time.RFC3339),
				CreatedAt:   createdAt.Format(time.RFC3339),
			})
		}
		if err := epRows.Err(); err != nil {
			return nil, fmt.Errorf("episode rows: %w", err)
		}
	}

	// Sort all results by score descending
	sort.Slice(results, func(i, j int) bool {
		return results[i].Score > results[j].Score
	})

	if len(results) > limit {
		results = results[:limit]
	}

	return results, nil
}
