package commands

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDispatchYouTubePackSingleTopicWritesRunManifest(t *testing.T) {
	restore := youtubePackGenerator
	youtubePackGenerator = func(systemPrompt, prompt, cwd string) (string, error) {
		if !strings.Contains(prompt, `"topic":"topic one"`) {
			return "", fmt.Errorf("unexpected prompt: %s", prompt)
		}
		return `{"title":"Topic One","hook":"Hook One","outline":["Beat 1","Beat 2"],"script":"Script One","cta":"CTA One","metadata":{"difficulty":"easy"}}`, nil
	}
	defer func() { youtubePackGenerator = restore }()

	outDir := t.TempDir()
	result := Dispatch("/youtube-pack --topic 'topic one' --channel 'Founders' --audience 'Operators' --voice 'Crisp' --out '"+outDir+"'", "", "text", 0)
	if result.ExitCode != 0 {
		t.Fatalf("expected success, got exit=%d error=%q output=%q", result.ExitCode, result.Error, result.Output)
	}
	if !strings.Contains(result.Output, "Generated 1 package(s):") {
		t.Fatalf("expected success summary, got %q", result.Output)
	}

	pkgDir := filepath.Join(outDir, "01-topic-one")
	if _, err := os.Stat(filepath.Join(pkgDir, "script.md")); err != nil {
		t.Fatalf("expected script.md in %s: %v", pkgDir, err)
	}
	if _, err := os.Stat(filepath.Join(pkgDir, "metadata.json")); err != nil {
		t.Fatalf("expected metadata.json in %s: %v", pkgDir, err)
	}

	metaRaw, err := os.ReadFile(filepath.Join(pkgDir, "metadata.json"))
	if err != nil {
		t.Fatalf("read metadata: %v", err)
	}
	var meta map[string]any
	if err := json.Unmarshal(metaRaw, &meta); err != nil {
		t.Fatalf("decode metadata: %v", err)
	}
	if got := meta["topic"]; got != "topic one" {
		t.Fatalf("unexpected topic in metadata: %#v", got)
	}
	if got := meta["channel"]; got != "Founders" {
		t.Fatalf("unexpected channel in metadata: %#v", got)
	}

	manifestRaw, err := os.ReadFile(filepath.Join(outDir, "run-manifest.json"))
	if err != nil {
		t.Fatalf("read run manifest: %v", err)
	}
	var manifest youtubePackRunManifest
	if err := json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatalf("decode run manifest: %v", err)
	}
	if len(manifest.Results) != 1 {
		t.Fatalf("expected 1 manifest result, got %d", len(manifest.Results))
	}
	got := manifest.Results[0]
	if got.Topic != "topic one" || got.Slug != "topic-one" || got.OutputDir != "01-topic-one" || got.Status != "success" || got.Message != "ok" {
		t.Fatalf("unexpected manifest entry: %#v", got)
	}
}

func TestDispatchYouTubePackPartialFailureFromInputFile(t *testing.T) {
	restore := youtubePackGenerator
	youtubePackGenerator = func(systemPrompt, prompt, cwd string) (string, error) {
		switch {
		case strings.Contains(prompt, `"topic":"alpha topic"`):
			return `{"title":"Alpha","hook":"Alpha Hook","outline":["One"],"script":"Alpha Script","cta":"Alpha CTA","metadata":{"source":"test"}}`, nil
		case strings.Contains(prompt, `"topic":"broken topic"`):
			return `{"title":"","hook":"Broken","outline":[],"script":"","cta":"","metadata":{}}`, nil
		default:
			return "", fmt.Errorf("unexpected prompt: %s", prompt)
		}
	}
	defer func() { youtubePackGenerator = restore }()

	tmp := t.TempDir()
	inputPath := filepath.Join(tmp, "topics.txt")
	if err := os.WriteFile(inputPath, []byte("alpha topic\n\nbroken topic\n"), 0o644); err != nil {
		t.Fatalf("write input file: %v", err)
	}

	result := Dispatch("/youtube-pack --input '"+inputPath+"' --out '"+tmp+"'", "", "text", 0)
	if result.ExitCode != 1 {
		t.Fatalf("expected partial failure exit code 1, got %d output=%q error=%q", result.ExitCode, result.Output, result.Error)
	}
	if result.Error != "youtube-pack completed with 1 failure(s)" {
		t.Fatalf("unexpected error: %q", result.Error)
	}
	if !strings.Contains(result.Output, "Generated 1 package(s):") {
		t.Fatalf("expected generated summary, got %q", result.Output)
	}
	if !strings.Contains(result.Output, "Failed 1 package(s):") {
		t.Fatalf("expected failure summary, got %q", result.Output)
	}
	if !strings.Contains(result.Output, "broken topic: missing title") {
		t.Fatalf("expected validation detail, got %q", result.Output)
	}

	if _, err := os.Stat(filepath.Join(tmp, "01-alpha-topic", "script.md")); err != nil {
		t.Fatalf("expected successful package output: %v", err)
	}
	if _, err := os.Stat(filepath.Join(tmp, "02-broken-topic")); !os.IsNotExist(err) {
		t.Fatalf("expected failed topic output dir to be absent, got err=%v", err)
	}

	manifestRaw, err := os.ReadFile(filepath.Join(tmp, "run-manifest.json"))
	if err != nil {
		t.Fatalf("read run manifest: %v", err)
	}
	var manifest youtubePackRunManifest
	if err := json.Unmarshal(manifestRaw, &manifest); err != nil {
		t.Fatalf("decode run manifest: %v", err)
	}
	if len(manifest.Results) != 2 {
		t.Fatalf("expected 2 manifest results, got %d", len(manifest.Results))
	}
	first := manifest.Results[0]
	if first.Topic != "alpha topic" || first.OutputDir != "01-alpha-topic" || first.Status != "success" || first.Message != "ok" {
		t.Fatalf("unexpected first manifest entry: %#v", first)
	}
	second := manifest.Results[1]
	if second.Topic != "broken topic" || second.Slug != "broken-topic" || second.OutputDir != "" || second.Status != "failed" || second.Message != "missing title" {
		t.Fatalf("unexpected second manifest entry: %#v", second)
	}
}
