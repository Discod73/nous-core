package commands

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/nex-crm/wuphf/internal/provider"
)

// captureMessages returns a SlashContext that records system messages into a
// slice and a pointer to that slice so tests can assert on what the command
// printed.
func captureMessages() (*SlashContext, *[]string) {
	out := []string{}
	ctx := &SlashContext{
		AddMessage: func(role, content string) {
			out = append(out, role+":"+content)
		},
		SetLoading:  func(bool) {},
		ShowPicker:  nil,
		ShowConfirm: nil,
		SendResult:  func(string, error) {},
	}
	return ctx, &out
}

func TestBuildProviderPayload_Openclaw(t *testing.T) {
	got := buildProviderPayload(provider.KindOpenclaw, map[string]string{
		"model":       "openai-codex/gpt-5.4",
		"session-key": "agent:test:x",
		"agent-id":    "main",
	})
	if got["kind"] != provider.KindOpenclaw {
		t.Fatalf("kind=%v", got["kind"])
	}
	if got["model"] != "openai-codex/gpt-5.4" {
		t.Fatalf("model=%v", got["model"])
	}
	oc, ok := got["openclaw"].(map[string]any)
	if !ok {
		t.Fatalf("openclaw block missing: %+v", got)
	}
	if oc["session_key"] != "agent:test:x" || oc["agent_id"] != "main" {
		t.Fatalf("openclaw block wrong: %+v", oc)
	}
}

func TestBuildProviderPayload_ClaudeNoOpenclawBlock(t *testing.T) {
	got := buildProviderPayload(provider.KindClaudeCode, map[string]string{"model": "sonnet"})
	if _, has := got["openclaw"]; has {
		t.Fatalf("claude payload should not include openclaw block")
	}
}

func TestCmdAgentCreate_RejectsInvalidProvider(t *testing.T) {
	ctx, out := captureMessages()
	if err := cmdAgentCreate(ctx, "pm --provider gemini"); err != nil {
		t.Fatalf("cmdAgentCreate: %v", err)
	}
	joined := strings.Join(*out, "|")
	if !strings.Contains(joined, "unknown provider kind") {
		t.Fatalf("expected provider validation error, got %q", joined)
	}
}

func TestCmdAgentCreate_MissingSlug(t *testing.T) {
	ctx, out := captureMessages()
	if err := cmdAgentCreate(ctx, "--provider codex"); err != nil {
		t.Fatalf("cmdAgentCreate: %v", err)
	}
	if !strings.Contains(strings.Join(*out, "|"), "usage:") {
		t.Fatalf("expected usage message, got %q", *out)
	}
}

func TestCmdAgentCreate_PostsToBroker(t *testing.T) {
	var gotBody map[string]any
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/office-members" || r.Method != http.MethodPost {
			http.Error(w, "wrong route", http.StatusNotFound)
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"member":{"slug":"pm-bot"}}`))
	}))
	defer ts.Close()

	t.Setenv("WUPHF_TEAM_BROKER_URL", ts.URL)
	t.Setenv("WUPHF_BROKER_TOKEN", "test-token")
	t.Setenv("WUPHF_BROKER_TOKEN_FILE", "")

	ctx, out := captureMessages()
	if err := cmdAgentCreate(ctx, "pm-bot --name 'PM Bot' --provider codex --model gpt-5.4 --role 'Product Manager'"); err != nil {
		t.Fatalf("cmdAgentCreate: %v", err)
	}
	if !strings.Contains(strings.Join(*out, "|"), "Created @pm-bot") {
		t.Fatalf("expected success message, got %q", *out)
	}
	if gotBody["slug"] != "pm-bot" || gotBody["name"] != "PM Bot" {
		t.Fatalf("body fields wrong: %+v", gotBody)
	}
	prov, ok := gotBody["provider"].(map[string]any)
	if !ok {
		t.Fatalf("provider missing: %+v", gotBody)
	}
	if prov["kind"] != "codex" || prov["model"] != "gpt-5.4" {
		t.Fatalf("provider wrong: %+v", prov)
	}
}

func TestCmdAgentRemove_PostsToBroker(t *testing.T) {
	var gotBody map[string]any
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer ts.Close()

	t.Setenv("WUPHF_TEAM_BROKER_URL", ts.URL)
	t.Setenv("WUPHF_BROKER_TOKEN", "test-token")

	ctx, out := captureMessages()
	if err := cmdAgentRemove(ctx, "pm-bot"); err != nil {
		t.Fatalf("cmdAgentRemove: %v", err)
	}
	if gotBody["action"] != "remove" || gotBody["slug"] != "pm-bot" {
		t.Fatalf("remove body wrong: %+v", gotBody)
	}
	if !strings.Contains(strings.Join(*out, "|"), "Removed @pm-bot") {
		t.Fatalf("expected remove confirmation, got %q", *out)
	}
}

func TestCmdAgentEdit_ProviderSwitch(t *testing.T) {
	var gotBody map[string]any
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"member":{"slug":"pm-bot"}}`))
	}))
	defer ts.Close()

	t.Setenv("WUPHF_TEAM_BROKER_URL", ts.URL)
	t.Setenv("WUPHF_BROKER_TOKEN", "test-token")

	ctx, out := captureMessages()
	if err := cmdAgentEdit(ctx, "pm-bot --provider openclaw --session-key agent:test:pm"); err != nil {
		t.Fatalf("cmdAgentEdit: %v", err)
	}
	if gotBody["action"] != "update" {
		t.Fatalf("expected update action, got %v", gotBody["action"])
	}
	prov := gotBody["provider"].(map[string]any)
	if prov["kind"] != "openclaw" {
		t.Fatalf("edit did not set provider kind: %+v", prov)
	}
	oc := prov["openclaw"].(map[string]any)
	if oc["session_key"] != "agent:test:pm" {
		t.Fatalf("session_key not threaded: %+v", oc)
	}
	if !strings.Contains(strings.Join(*out, "|"), "Updated @pm-bot") {
		t.Fatalf("expected update confirmation, got %q", *out)
	}
}
