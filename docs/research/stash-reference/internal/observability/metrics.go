package observability

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	eventsProcessed = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_events_processed_total",
			Help: "Total number of events processed by consolidation",
		}, []string{"namespace"},
	)
	factsCreated = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_facts_created_total",
			Help: "Total number of facts created during consolidation",
		}, []string{"namespace"},
	)
	factsDeduplicated = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_facts_deduplicated_total",
			Help: "Total number of facts skipped due to semantic deduplication",
		}, []string{"namespace"},
	)
	relationshipsCreated = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_relationships_created_total",
			Help: "Total number of relationships extracted",
		}, []string{"namespace"},
	)
	llmCalls = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_llm_calls_total",
			Help: "Total number of LLM calls made during consolidation",
		}, []string{"namespace"},
	)
	clustersFound = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_clusters_found_total",
			Help: "Total number of clusters evaluated",
		}, []string{"namespace"},
	)
	eventsRead = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_events_read_total",
			Help: "Total number of events read during consolidation",
		}, []string{"namespace"},
	)
	duration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "consolidation_duration_seconds",
			Help:    "Duration of consolidation runs",
			Buckets: prometheus.DefBuckets,
		}, []string{"namespace"},
	)
	errorsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "consolidation_errors_total",
			Help: "Number of errors encountered during consolidation",
		}, []string{"namespace"},
	)
)

// Observation carries the metrics that should be exported for a run.
type Observation struct {
	Namespace          string
	EventsRead         int
	EventsProcessed    int
	ClustersFound      int
	FactsCreated       int
	FactsDeduplicated  int
	RelationshipsFound int
	LLMCalls           int
	Duration           time.Duration
	Errors             int
}

// RecordConsolidation exports the provided observation to Prometheus.
func RecordConsolidation(obs Observation) {
	if obs.Namespace == "" {
		obs.Namespace = "default"
	}
	eventsProcessed.WithLabelValues(obs.Namespace).Add(float64(obs.EventsProcessed))
	eventsRead.WithLabelValues(obs.Namespace).Add(float64(obs.EventsRead))
	clustersFound.WithLabelValues(obs.Namespace).Add(float64(obs.ClustersFound))
	factsCreated.WithLabelValues(obs.Namespace).Add(float64(obs.FactsCreated))
	factsDeduplicated.WithLabelValues(obs.Namespace).Add(float64(obs.FactsDeduplicated))
	relationshipsCreated.WithLabelValues(obs.Namespace).Add(float64(obs.RelationshipsFound))
	llmCalls.WithLabelValues(obs.Namespace).Add(float64(obs.LLMCalls))
	duration.WithLabelValues(obs.Namespace).Observe(obs.Duration.Seconds())
	errorsTotal.WithLabelValues(obs.Namespace).Add(float64(obs.Errors))
}
