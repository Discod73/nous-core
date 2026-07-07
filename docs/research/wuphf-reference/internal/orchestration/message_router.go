package orchestration

import (
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// atMentionPattern matches @slug patterns in messages.
var atMentionPattern = regexp.MustCompile(`@(\S+)`)

// AgentInfo describes an available agent for message routing.
type AgentInfo struct {
	Slug      string
	Expertise []string
	RoleTerms []string
}

// MessageRoutingResult is the output of a Route call.
type MessageRoutingResult struct {
	Primary       string // agent slug
	Collaborators []string
	IsFollowUp    bool
	TeamLeadAware bool
}

type threadContext struct {
	agentSlug    string
	lastActivity time.Time
}

// MessageRouter routes free-text messages to the most appropriate agent.
type MessageRouter struct {
	router         *TaskRouter
	recentThreads  map[string]*threadContext
	followUpWindow time.Duration
	teamLeadSlug   string
	mu             sync.Mutex
}

// NewMessageRouter returns a MessageRouter with a 30s follow-up window.
func NewMessageRouter() *MessageRouter {
	return &MessageRouter{
		router:         NewTaskRouter(),
		recentThreads:  make(map[string]*threadContext),
		followUpWindow: 30 * time.Second,
	}
}

// SetTeamLeadSlug configures which agent slug acts as the team lead.
func (m *MessageRouter) SetTeamLeadSlug(slug string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.teamLeadSlug = slug
}

// getTeamLeadSlug returns the configured team-lead slug, defaulting to "team-lead".
// Caller must hold m.mu.
func (m *MessageRouter) getTeamLeadSlug() string {
	if m.teamLeadSlug != "" {
		return m.teamLeadSlug
	}
	return "team-lead"
}

// RegisterAgent registers an agent's expertise with the underlying TaskRouter.
func (m *MessageRouter) RegisterAgent(slug string, expertise []string) {
	skills := make([]SkillDeclaration, len(expertise))
	for i, e := range expertise {
		skills[i] = SkillDeclaration{Name: e, Description: e, Proficiency: 1.0}
	}
	m.router.RegisterAgent(slug, skills)
}

// UnregisterAgent removes an agent from the message router.
func (m *MessageRouter) UnregisterAgent(slug string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.router.UnregisterAgent(slug)
	delete(m.recentThreads, slug)
}

// RecordAgentActivity marks an agent as recently active.
func (m *MessageRouter) RecordAgentActivity(agentSlug string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if tc, ok := m.recentThreads[agentSlug]; ok {
		tc.lastActivity = time.Now()
	} else {
		m.recentThreads[agentSlug] = &threadContext{
			agentSlug:    agentSlug,
			lastActivity: time.Now(),
		}
	}
}

// Route decides which agent(s) should handle a message.
func (m *MessageRouter) Route(message string, availableAgents []AgentInfo) MessageRoutingResult {
	m.mu.Lock()
	defer m.mu.Unlock()

	result := MessageRoutingResult{}

	teamLead := m.getTeamLeadSlug()

	// 1. Check for explicit @slug mention — highest priority, outranks follow-up.
	if slug := m.detectAtMention(message, availableAgents); slug != "" {
		result.Primary = slug
		result.TeamLeadAware = slug == teamLead
		return result
	}

	// 2. Check follow-up — route to the recently active agent.
	if followUpSlug := m.detectFollowUp(message); followUpSlug != "" {
		result.Primary = followUpSlug
		result.IsFollowUp = true
		result.TeamLeadAware = true
		return result
	}

	// 3. New directive: always route to team-lead first per spec.
	// Still populate collaborators for informational purposes.
	result.Primary = teamLead
	result.TeamLeadAware = true

	result.Collaborators = m.inferCollaborators(message, availableAgents, teamLead)
	return result
}

var followUpPattern = regexp.MustCompile(
	`(?i)^(also|and |too |that |it |the results|those |these |this |what about|how about|can you also)`,
)

// detectFollowUp returns the most recently active agent slug if the message
// looks like a follow-up and was within the follow-up window.
func (m *MessageRouter) detectFollowUp(message string) string {
	if !followUpPattern.MatchString(strings.TrimSpace(message)) {
		return ""
	}
	var best *threadContext
	for _, tc := range m.recentThreads {
		if time.Since(tc.lastActivity) <= m.followUpWindow {
			if best == nil || tc.lastActivity.After(best.lastActivity) {
				best = tc
			}
		}
	}
	if best != nil {
		return best.agentSlug
	}
	return ""
}

// detectAtMention returns the slug of an explicitly @mentioned agent, if any.
// Caller must hold m.mu.
func (m *MessageRouter) detectAtMention(message string, agents []AgentInfo) string {
	matches := atMentionPattern.FindAllStringSubmatch(message, -1)
	if len(matches) == 0 {
		return ""
	}
	known := make(map[string]bool, len(agents))
	for _, a := range agents {
		known[a.Slug] = true
	}
	for _, match := range matches {
		slug := match[1]
		if known[slug] {
			return slug
		}
	}
	return ""
}

// ExtractSkills returns generic routing terms inferred from the message text.
func (m *MessageRouter) ExtractSkills(message string) []string {
	return extractRoutingTerms(message)
}

// ExtractRoutingTerms returns normalized routing terms for arbitrary message text.
func ExtractRoutingTerms(message string) []string {
	return extractRoutingTerms(message)
}

var routingWordPattern = regexp.MustCompile(`[a-z0-9]+`)

var routingStopWords = map[string]struct{}{
	"a": {}, "an": {}, "and": {}, "are": {}, "as": {}, "at": {}, "be": {}, "but": {}, "by": {},
	"can": {}, "could": {}, "do": {}, "for": {}, "from": {}, "have": {}, "help": {}, "i": {},
	"in": {}, "is": {}, "it": {}, "just": {}, "make": {}, "me": {}, "need": {}, "new": {},
	"of": {}, "on": {}, "or": {}, "our": {}, "please": {}, "set": {}, "should": {}, "that": {},
	"the": {}, "their": {}, "then": {}, "there": {}, "this": {}, "to": {}, "up": {}, "us": {}, "want": {},
	"hello": {}, "hi": {}, "hey": {}, "thanks": {}, "thank": {},
	"we": {}, "with": {}, "you": {}, "your": {},
}

func (m *MessageRouter) inferCollaborators(message string, availableAgents []AgentInfo, teamLead string) []string {
	messageTerms := extractRoutingTerms(message)
	if len(messageTerms) == 0 {
		return nil
	}

	type scoredAgent struct {
		slug  string
		score float64
	}

	var scored []scoredAgent
	for _, agent := range availableAgents {
		if agent.Slug == teamLead {
			continue
		}
		score := scoreAgentAgainstMessage(messageTerms, agentRoutingTerms(agent))
		if score >= 0.28 {
			scored = append(scored, scoredAgent{slug: agent.Slug, score: score})
		}
	}

	sort.Slice(scored, func(i, j int) bool {
		if scored[i].score == scored[j].score {
			return scored[i].slug < scored[j].slug
		}
		return scored[i].score > scored[j].score
	})

	result := make([]string, 0, len(scored))
	for _, item := range scored {
		result = append(result, item.slug)
	}
	return result
}

func agentRoutingTerms(agent AgentInfo) []string {
	return RoutingTerms(agent.Slug, agent.Expertise, agent.RoleTerms, nil)
}

// AgentRoutingTerms returns normalized routing terms for a slug plus its metadata.
func AgentRoutingTerms(slug string, expertise []string, roleTerms []string) []string {
	return RoutingTerms(slug, expertise, roleTerms, nil)
}

// RoutingTerms returns normalized routing terms for a routing candidate.
func RoutingTerms(slug string, expertise []string, roleTerms []string, extraTerms []string) []string {
	terms := make([]string, 0, 1+len(expertise)+len(roleTerms)+len(extraTerms))
	terms = append(terms, slug)
	terms = append(terms, expertise...)
	terms = append(terms, roleTerms...)
	terms = append(terms, extraTerms...)
	return dedupeTerms(normalizeRoutingTerms(terms))
}

func scoreAgentAgainstMessage(messageTerms, agentTerms []string) float64 {
	if len(messageTerms) == 0 || len(agentTerms) == 0 {
		return 0
	}

	bestScores := make([]float64, 0, len(messageTerms))
	for _, messageTerm := range messageTerms {
		best := 0.0
		for _, agentTerm := range agentTerms {
			if score := similarity(messageTerm, agentTerm); score > best {
				best = score
			}
		}
		if best >= 0.3 {
			bestScores = append(bestScores, best)
		}
	}

	if len(bestScores) == 0 {
		return 0
	}

	sort.Float64s(bestScores)
	top := 2
	if len(bestScores) < top {
		top = len(bestScores)
	}
	sum := 0.0
	for i := len(bestScores) - top; i < len(bestScores); i++ {
		sum += bestScores[i]
	}
	return sum / float64(top)
}

// ScoreMessageAgainstAgent returns the metadata routing score for a message.
func ScoreMessageAgainstAgent(message string, slug string, expertise []string, roleTerms []string) float64 {
	return ScoreMessageAgainstTerms(message, AgentRoutingTerms(slug, expertise, roleTerms))
}

// ScoreMessageAgainstTerms returns the metadata routing score for message text
// against a precomputed set of routing terms.
func ScoreMessageAgainstTerms(message string, terms []string) float64 {
	return scoreAgentAgainstMessage(ExtractRoutingTerms(message), dedupeTerms(normalizeRoutingTerms(terms)))
}

func extractRoutingTerms(message string) []string {
	tokens := normalizeRoutingTerms(routingWordPattern.FindAllString(strings.ToLower(message), -1))
	filtered := make([]string, 0, len(tokens))
	for _, token := range tokens {
		if _, ok := routingStopWords[token]; !ok {
			filtered = append(filtered, token)
		}
	}
	if len(filtered) == 0 {
		return nil
	}

	var out []string
	for size := 1; size <= 3; size++ {
		if len(filtered) < size {
			break
		}
		for i := 0; i+size <= len(filtered); i++ {
			out = append(out, strings.Join(filtered[i:i+size], " "))
		}
	}
	return dedupeTerms(out)
}

func normalizeRoutingTerms(terms []string) []string {
	out := make([]string, 0, len(terms))
	for _, term := range terms {
		normalized := normalizeRoutingTerm(term)
		if normalized != "" {
			out = append(out, normalized)
		}
	}
	return out
}

func normalizeRoutingTerm(term string) string {
	parts := routingWordPattern.FindAllString(strings.ToLower(term), -1)
	if len(parts) == 0 {
		return ""
	}
	filtered := make([]string, 0, len(parts))
	for _, part := range parts {
		if _, ok := routingStopWords[part]; !ok {
			filtered = append(filtered, part)
		}
	}
	if len(filtered) == 0 {
		return ""
	}
	return strings.Join(filtered, " ")
}

func dedupeTerms(terms []string) []string {
	seen := make(map[string]struct{}, len(terms))
	out := make([]string, 0, len(terms))
	for _, term := range terms {
		if _, ok := seen[term]; ok {
			continue
		}
		seen[term] = struct{}{}
		out = append(out, term)
	}
	return out
}
