// Package queries provides sqltmpl templates for dynamic SQL generation.
// Templates are embedded at compile time.
package queries

import (
	"embed"
	"fmt"
	"text/template"

	"github.com/alash3al/sqltmpl"
	"github.com/pgvector/pgvector-go"
)

//go:embed *.sql.tmpl
var queryFS embed.FS

// Queries wraps sqltmpl templates with typed execution helpers.
type Queries struct {
	tpl *sqltmpl.Template
}

// New parses embedded templates and returns a ready Queries instance.
func New() (*Queries, error) {
	textTpl, err := template.ParseFS(queryFS, "*.sql.tmpl")
	if err != nil {
		return nil, fmt.Errorf("parse query templates: %w", err)
	}

	tpl := sqltmpl.New(textTpl, func(i int) string {
		return fmt.Sprintf("$%d", i)
	})

	return &Queries{tpl: tpl}, nil
}

// RecallEpisodes returns SQL + args for episode vector search.
// If namespaceIDs is nil, searches all namespaces.
func (q *Queries) RecallEpisodes(namespaceIDs []int64, vector pgvector.Vector, limit int) (string, []any, error) {
	args := map[string]any{
		"vector": vector,
		"limit":  limit,
	}
	if len(namespaceIDs) > 0 {
		args["namespace_ids"] = namespaceIDs
	}
	return q.tpl.Execute("recall_episodes", args)
}

// RecallFacts returns SQL + args for fact vector search.
// If namespaceIDs is nil, searches all namespaces.
func (q *Queries) RecallFacts(namespaceIDs []int64, vector pgvector.Vector, limit int) (string, []any, error) {
	args := map[string]any{
		"vector": vector,
		"limit":  limit,
	}
	if len(namespaceIDs) > 0 {
		args["namespace_ids"] = namespaceIDs
	}
	return q.tpl.Execute("recall_facts", args)
}

// FetchEpisodes returns SQL + args for batch episode read after a checkpoint.
func (q *Queries) FetchEpisodes(namespaceID int64, afterID int64, limit int) (string, []any, error) {
	return q.tpl.Execute("fetch_episodes", map[string]any{
		"namespace_id": namespaceID,
		"after_id":     afterID,
		"limit":        limit,
	})
}

// FetchFacts returns SQL + args for batch fact read after a checkpoint.
func (q *Queries) FetchFacts(namespaceID int64, afterID int64, limit int) (string, []any, error) {
	return q.tpl.Execute("fetch_facts", map[string]any{
		"namespace_id": namespaceID,
		"after_id":     afterID,
		"limit":        limit,
	})
}

// FetchRelationships returns SQL + args for batch relationship read after a checkpoint.
func (q *Queries) FetchRelationships(namespaceID int64, afterID int64, limit int) (string, []any, error) {
	return q.tpl.Execute("fetch_relationships", map[string]any{
		"namespace_id": namespaceID,
		"after_id":     afterID,
		"limit":        limit,
	})
}
