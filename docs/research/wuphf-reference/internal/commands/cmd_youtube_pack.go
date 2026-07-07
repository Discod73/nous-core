package commands

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/nex-crm/wuphf/internal/provider"
)

var (
	youtubePackGenerator = provider.RunConfiguredOneShot
	nonSlugChars         = regexp.MustCompile(`[^a-z0-9]+`)
)

type youtubePackSpec struct {
	Title    string         `json:"title"`
	Hook     string         `json:"hook"`
	Outline  []string       `json:"outline"`
	Script   string         `json:"script"`
	CTA      string         `json:"cta"`
	Metadata map[string]any `json:"metadata"`
}

type youtubePackOptions struct {
	Topics   []string
	Channel  string
	Audience string
	Voice    string
	OutDir   string
	Input    string
}

type youtubePackRunManifest struct {
	Results []youtubePackRunManifestEntry `json:"results"`
}

type youtubePackRunManifestEntry struct {
	Topic     string `json:"topic"`
	Slug      string `json:"slug"`
	OutputDir string `json:"output_dir"`
	Status    string `json:"status"`
	Message   string `json:"message"`
}

func cmdYouTubePack(ctx *SlashContext, args string) error {
	opts, err := parseYouTubePackArgs(args)
	if err != nil {
		ctx.AddMessage("system", youtubePackUsage())
		return err
	}

	var (
		successes []string
		failures  []string
		manifest  youtubePackRunManifest
	)

	for idx, topic := range opts.Topics {
		entry := youtubePackRunManifestEntry{
			Topic:  topic,
			Slug:   slugifyYouTubePackTopic(topic),
			Status: "failed",
		}
		pkg, err := generateYouTubePack(topic, opts)
		if err != nil {
			entry.Message = err.Error()
			failures = append(failures, fmt.Sprintf("%s: %v", topic, err))
			manifest.Results = append(manifest.Results, entry)
			continue
		}
		dir, err := writeYouTubePack(opts.OutDir, idx+1, topic, pkg, opts)
		if err != nil {
			entry.Message = err.Error()
			failures = append(failures, fmt.Sprintf("%s: %v", topic, err))
			manifest.Results = append(manifest.Results, entry)
			continue
		}
		entry.OutputDir = filepath.Base(dir)
		entry.Status = "success"
		entry.Message = "ok"
		manifest.Results = append(manifest.Results, entry)
		successes = append(successes, fmt.Sprintf("%s -> %s", topic, dir))
	}
	if err := writeYouTubePackManifest(opts.OutDir, manifest); err != nil {
		return err
	}

	ctx.AddMessage("system", formatYouTubePackSummary(successes, failures))
	if len(failures) > 0 {
		return fmt.Errorf("youtube-pack completed with %d failure(s)", len(failures))
	}
	return nil
}

func parseYouTubePackArgs(args string) (youtubePackOptions, error) {
	tokens := tokenize(args)
	if len(tokens) == 0 {
		return youtubePackOptions{}, fmt.Errorf("missing topic input")
	}

	var (
		opts       = youtubePackOptions{OutDir: "youtube-pack"}
		inline     []string
		positional []string
	)

	for i := 0; i < len(tokens); i++ {
		token := tokens[i]
		if !strings.HasPrefix(token, "--") {
			positional = append(positional, token)
			continue
		}
		key := strings.TrimPrefix(token, "--")
		if i+1 >= len(tokens) || strings.HasPrefix(tokens[i+1], "--") {
			return youtubePackOptions{}, fmt.Errorf("flag --%s requires a value", key)
		}
		value := strings.TrimSpace(tokens[i+1])
		i++
		switch key {
		case "topic":
			inline = append(inline, value)
		case "input":
			opts.Input = value
		case "channel":
			opts.Channel = value
		case "audience":
			opts.Audience = value
		case "voice":
			opts.Voice = value
		case "out":
			opts.OutDir = value
		default:
			return youtubePackOptions{}, fmt.Errorf("unknown flag: --%s", key)
		}
	}

	if len(positional) > 0 {
		inline = append([]string{strings.Join(positional, " ")}, inline...)
	}
	if opts.Input != "" {
		fileTopics, err := readYouTubePackTopics(opts.Input)
		if err != nil {
			return youtubePackOptions{}, err
		}
		inline = append(inline, fileTopics...)
	}

	opts.Topics = normalizeYouTubePackTopics(inline)
	if len(opts.Topics) == 0 {
		return youtubePackOptions{}, fmt.Errorf("no topics provided")
	}
	if strings.TrimSpace(opts.OutDir) == "" {
		return youtubePackOptions{}, fmt.Errorf("--out must not be empty")
	}
	return opts, nil
}

func readYouTubePackTopics(path string) ([]string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read input file: %w", err)
	}
	lines := strings.Split(string(raw), "\n")
	topics := make([]string, 0, len(lines))
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		topics = append(topics, trimmed)
	}
	return topics, nil
}

func normalizeYouTubePackTopics(topics []string) []string {
	out := make([]string, 0, len(topics))
	for _, topic := range topics {
		if trimmed := strings.TrimSpace(topic); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func generateYouTubePack(topic string, opts youtubePackOptions) (youtubePackSpec, error) {
	systemPrompt := strings.TrimSpace(`You generate structured faceless YouTube video packages.
Return valid JSON only. No markdown fences. No prose outside JSON.
The top-level object must contain exactly:
- title
- hook
- outline
- script
- cta
- metadata`)

	payload, _ := json.Marshal(map[string]any{
		"topic":    topic,
		"channel":  opts.Channel,
		"audience": opts.Audience,
		"voice":    opts.Voice,
	})
	prompt := strings.TrimSpace(`Generate a production-ready faceless YouTube package.

Rules:
- Keep claims specific and realistic.
- Outline must be a JSON array of section strings.
- Metadata must be a JSON object with stable scalar or array values only.
- The response must be valid JSON and include every required key.

Input JSON:
` + string(payload))

	raw, err := youtubePackGenerator(systemPrompt, prompt, "")
	if err != nil {
		return youtubePackSpec{}, err
	}
	return decodeYouTubePack(raw)
}

func decodeYouTubePack(raw string) (youtubePackSpec, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return youtubePackSpec{}, fmt.Errorf("empty generator response")
	}
	if strings.HasPrefix(trimmed, "```") {
		lines := strings.Split(trimmed, "\n")
		if len(lines) >= 3 {
			lines = lines[1:]
			if last := len(lines) - 1; last >= 0 && strings.HasPrefix(strings.TrimSpace(lines[last]), "```") {
				lines = lines[:last]
			}
			trimmed = strings.TrimSpace(strings.Join(lines, "\n"))
		}
	}
	start := strings.Index(trimmed, "{")
	end := strings.LastIndex(trimmed, "}")
	if start >= 0 && end >= start {
		trimmed = trimmed[start : end+1]
	}
	var spec youtubePackSpec
	if err := json.Unmarshal([]byte(trimmed), &spec); err != nil {
		return youtubePackSpec{}, fmt.Errorf("invalid JSON: %w", err)
	}
	if err := validateYouTubePack(spec); err != nil {
		return youtubePackSpec{}, err
	}
	return spec, nil
}

func validateYouTubePack(spec youtubePackSpec) error {
	if strings.TrimSpace(spec.Title) == "" {
		return fmt.Errorf("missing title")
	}
	if strings.TrimSpace(spec.Hook) == "" {
		return fmt.Errorf("missing hook")
	}
	if strings.TrimSpace(spec.Script) == "" {
		return fmt.Errorf("missing script")
	}
	if strings.TrimSpace(spec.CTA) == "" {
		return fmt.Errorf("missing cta")
	}
	if len(spec.Outline) == 0 {
		return fmt.Errorf("missing outline")
	}
	for _, item := range spec.Outline {
		if strings.TrimSpace(item) == "" {
			return fmt.Errorf("outline items must not be empty")
		}
	}
	if len(spec.Metadata) == 0 {
		return fmt.Errorf("missing metadata")
	}
	return nil
}

func writeYouTubePack(baseDir string, index int, topic string, spec youtubePackSpec, opts youtubePackOptions) (string, error) {
	dir := filepath.Join(baseDir, fmt.Sprintf("%02d-%s", index, slugifyYouTubePackTopic(topic)))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("create output dir: %w", err)
	}

	scriptPath := filepath.Join(dir, "script.md")
	metadataPath := filepath.Join(dir, "metadata.json")

	if err := os.WriteFile(scriptPath, []byte(renderYouTubeScript(spec)), 0o644); err != nil {
		return "", fmt.Errorf("write script.md: %w", err)
	}

	metadata := map[string]any{
		"topic":    topic,
		"channel":  opts.Channel,
		"audience": opts.Audience,
		"voice":    opts.Voice,
		"title":    spec.Title,
		"hook":     spec.Hook,
		"outline":  spec.Outline,
		"cta":      spec.CTA,
		"metadata": spec.Metadata,
	}
	b, err := json.MarshalIndent(metadata, "", "  ")
	if err != nil {
		return "", fmt.Errorf("encode metadata.json: %w", err)
	}
	if err := os.WriteFile(metadataPath, append(b, '\n'), 0o644); err != nil {
		return "", fmt.Errorf("write metadata.json: %w", err)
	}
	return dir, nil
}

func writeYouTubePackManifest(baseDir string, manifest youtubePackRunManifest) error {
	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		return fmt.Errorf("create output root: %w", err)
	}
	path := filepath.Join(baseDir, "run-manifest.json")
	b, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return fmt.Errorf("encode run-manifest.json: %w", err)
	}
	if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
		return fmt.Errorf("write run-manifest.json: %w", err)
	}
	return nil
}

func renderYouTubeScript(spec youtubePackSpec) string {
	var sb strings.Builder
	sb.WriteString("# ")
	sb.WriteString(strings.TrimSpace(spec.Title))
	sb.WriteString("\n\n")
	sb.WriteString("## Hook\n")
	sb.WriteString(strings.TrimSpace(spec.Hook))
	sb.WriteString("\n\n")
	sb.WriteString("## Outline\n")
	for _, item := range spec.Outline {
		sb.WriteString("- ")
		sb.WriteString(strings.TrimSpace(item))
		sb.WriteString("\n")
	}
	sb.WriteString("\n## Script\n")
	sb.WriteString(strings.TrimSpace(spec.Script))
	sb.WriteString("\n\n## CTA\n")
	sb.WriteString(strings.TrimSpace(spec.CTA))
	sb.WriteString("\n")
	return sb.String()
}

func slugifyYouTubePackTopic(topic string) string {
	slug := strings.ToLower(strings.TrimSpace(topic))
	slug = nonSlugChars.ReplaceAllString(slug, "-")
	slug = strings.Trim(slug, "-")
	if slug == "" {
		return "topic"
	}
	return slug
}

func formatYouTubePackSummary(successes, failures []string) string {
	var lines []string
	if len(successes) > 0 {
		lines = append(lines, fmt.Sprintf("Generated %d package(s):", len(successes)))
		sort.Strings(successes)
		lines = append(lines, successes...)
	}
	if len(failures) > 0 {
		lines = append(lines, fmt.Sprintf("Failed %d package(s):", len(failures)))
		lines = append(lines, failures...)
	}
	return strings.Join(lines, "\n")
}

func youtubePackUsage() string {
	return "Usage: /youtube-pack <topic> [--topic <topic> ...] [--input <file>] [--channel <name>] [--audience <audience>] [--voice <voice>] [--out <dir>]"
}
