package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildApprovalGateBlockedFromPendingBundle(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "pending_external_approval", []string{
		"loopsmith_reviewer: Reviewer (approved)",
		"client_operator: Pilot Client Alpha (pending)",
	}, true)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	checks := validateBundle(inputs)
	if hasSchemaFailure(checks) {
		t.Fatalf("validateBundle() unexpectedly failed: %#v", checks)
	}

	gate := buildApprovalGate(inputs, false)
	if gate.Status != "blocked" {
		t.Fatalf("gate.Status = %q, want blocked", gate.Status)
	}
	if len(gate.Blockers) == 0 {
		t.Fatal("expected blockers for pending approval state")
	}
}

func TestBuildApprovalGateReleaseReadyWithForceApprove(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "pending_external_approval", []string{
		"loopsmith_reviewer: Reviewer (pending)",
		"client_operator: Pilot Client Alpha (pending)",
	}, true)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	gate := buildApprovalGate(inputs, true)
	if gate.Status != "release_ready" {
		t.Fatalf("gate.Status = %q, want release_ready", gate.Status)
	}
	if !gate.ForcedApprove {
		t.Fatal("expected ForcedApprove to be true")
	}
}

func TestRenderHandoffArtifacts(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "approved", []string{
		"loopsmith_reviewer: Reviewer (approved)",
		"client_operator: Pilot Client Alpha (approved)",
	}, true)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	run := workflowRun{
		BundleDir:          dir,
		SchemaChecks:       validateBundle(inputs),
		ApprovalGate:       buildApprovalGate(inputs, false),
		ApprovalSources:    inputs.ApprovalSources,
		ApprovalPacketPath: optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalPacket != nil, "approval-packet.json"),
		ApprovalStatusPath: optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalStatus != nil, "approval-status.json"),
		Consumers:          buildConsumers(inputs, buildApprovalGate(inputs, false), dir),
		LivePacketPath:     livePacketPath(inputs),
	}

	if run.ApprovalGate.Status != "release_ready" {
		t.Fatalf("gate.Status = %q, want release_ready", run.ApprovalGate.Status)
	}

	for _, consumer := range run.Consumers {
		if consumer.Status != "ready_for_delivery" {
			t.Fatalf("consumer %s status = %q, want ready_for_delivery", consumer.Consumer, consumer.Status)
		}
	}

	summary := renderSummary(run)
	if !strings.Contains(summary, "Gate status: release_ready") {
		t.Fatalf("renderSummary() missing release state: %s", summary)
	}
	if !strings.Contains(summary, "Approval sources: approval-packet.json, approval-status.json") {
		t.Fatalf("renderSummary() missing approval sources: %s", summary)
	}
}

func TestResolveOutDir(t *testing.T) {
	got := resolveOutDir("/tmp/script-packet-review-bundle")
	want := "/tmp/script-packet-review-handoff"
	if got != want {
		t.Fatalf("resolveOutDir() = %q, want %q", got, want)
	}
}

func TestCheckedInBundleStaysBlockedUntilClientApproval(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "pending_external_approval", []string{
		"loopsmith_reviewer: Reviewer (approved)",
		"client_operator: Pilot Client Alpha (pending)",
	}, true)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	checks := validateBundle(inputs)
	if hasSchemaFailure(checks) {
		t.Fatalf("validateBundle() unexpectedly failed: %#v", checks)
	}

	gate := buildApprovalGate(inputs, false)
	if gate.Status != "blocked" {
		t.Fatalf("gate.Status = %q, want blocked", gate.Status)
	}
	if len(gate.Approvers) != 2 {
		t.Fatalf("len(gate.Approvers) = %d, want 2", len(gate.Approvers))
	}
	if gate.Approvers[0].Role != "loopsmith_reviewer" || gate.Approvers[0].Status != "approved" {
		t.Fatalf("unexpected first approver: %#v", gate.Approvers[0])
	}
	if gate.Approvers[1].Role != "client_operator" || gate.Approvers[1].Status != "pending" {
		t.Fatalf("unexpected second approver: %#v", gate.Approvers[1])
	}

	consumers := buildConsumers(inputs, gate, resolveOutDir(inputs.BundleDir))
	for _, consumer := range consumers {
		if consumer.Status != "staged_pending_approval" {
			t.Fatalf("consumer %s status = %q, want staged_pending_approval", consumer.Consumer, consumer.Status)
		}
		if consumer.Action != "hold_for_approval" {
			t.Fatalf("consumer %s action = %q, want hold_for_approval", consumer.Consumer, consumer.Action)
		}
	}
}

func TestBuildApprovalGateBlocksContradictoryArtifacts(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "pending_external_approval", []string{
		"loopsmith_reviewer: Reviewer (approved)",
		"client_operator: Pilot Client Alpha (pending)",
	}, true)

	mustWriteFile(t, filepath.Join(dir, "approval-packet.json"), `{
  "run_id": "client-intake-approval-dry-run",
  "approval_mode": "live_client_pilot",
  "source_bundle": "`+dir+`",
  "live_packet_path": "docs/youtube-factory/generated/live-client-pilot/script-packet-inbox-operator.json",
  "client": "Pilot Client Alpha",
  "engagement_slug": "ai-inbox-operator-5-person-business",
  "notion_status": "approved",
  "notion_database": "Client Approval Queue",
  "notion_title": "I Built an AI Inbox Operator for a 5-Person Business",
  "approvers": [
    {"role":"loopsmith_reviewer","name":"Reviewer","status":"approved"},
    {"role":"client_operator","name":"Pilot Client Alpha","status":"pending"}
  ]
}
`)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	gate := buildApprovalGate(inputs, false)
	if gate.Status != "blocked" {
		t.Fatalf("gate.Status = %q, want blocked", gate.Status)
	}
	if len(gate.Blockers) == 0 {
		t.Fatal("expected blockers for contradictory approval artifacts")
	}
}

func TestCheckedInApprovedBundleReleasesConsumers(t *testing.T) {
	dir := t.TempDir()
	writeFixtureBundle(t, dir, "approved", []string{
		"loopsmith_reviewer: Reviewer (approved)",
		"client_operator: Pilot Client Alpha (approved)",
	}, true)

	inputs, err := loadBundle(dir)
	if err != nil {
		t.Fatalf("loadBundle() error = %v", err)
	}

	checks := validateBundle(inputs)
	if hasSchemaFailure(checks) {
		t.Fatalf("validateBundle() unexpectedly failed: %#v", checks)
	}

	gate := buildApprovalGate(inputs, false)
	if gate.Status != "release_ready" {
		t.Fatalf("gate.Status = %q, want release_ready", gate.Status)
	}
	if len(gate.Blockers) != 0 {
		t.Fatalf("gate.Blockers = %#v, want none", gate.Blockers)
	}

	consumers := buildConsumers(inputs, gate, resolveOutDir(inputs.BundleDir))
	for _, consumer := range consumers {
		if consumer.Status != "ready_for_delivery" {
			t.Fatalf("consumer %s status = %q, want ready_for_delivery", consumer.Consumer, consumer.Status)
		}
		if consumer.Action == "hold_for_approval" {
			t.Fatalf("consumer %s action = %q, want released action", consumer.Consumer, consumer.Action)
		}
	}
}

func writeFixtureBundle(t *testing.T, dir, status string, approvers []string, includeApprovalArtifacts bool) {
	t.Helper()

	mustWriteFile(t, filepath.Join(dir, "summary.md"), "# Live Approval Review Bundle\n")
	mustWriteFile(t, filepath.Join(dir, "slack-payload.json"), `{
  "channel": "#client-intake-pilot",
  "text": "Approval packet ready.",
  "checklist": ["Approval status: pending_external_approval"]
}
`)
	mustWriteFile(t, filepath.Join(dir, "google-drive-payload.json"), `{
  "folder_name": "review-ai-inbox-operator-5-person-business",
  "document_name": "I Built an AI Inbox Operator for a 5-Person Business review bundle",
  "viewers": ["consultant@loopsmith.example"],
  "tags": ["ai automation"],
  "notes": ["Live packet path: docs/youtube-factory/generated/live-client-pilot/script-packet-inbox-operator.json"]
}
`)
	mustWriteFile(t, filepath.Join(dir, "notion-payload.json"), `{
  "database": "Client Approval Queue",
  "title": "I Built an AI Inbox Operator for a 5-Person Business",
  "status": "`+status+`",
  "properties": {
    "Approval Mode": "live_client_pilot",
    "Live Packet": "docs/youtube-factory/generated/live-client-pilot/script-packet-inbox-operator.json"
  },
  "checklist": [
    "`+strings.Join(approvers, `",
    "`)+`"
  ]
}
`)
	if includeApprovalArtifacts {
		mustWriteFile(t, filepath.Join(dir, "approval-packet.json"), `{
  "run_id": "client-intake-approval-dry-run",
  "approval_mode": "live_client_pilot",
  "source_bundle": "`+dir+`",
  "live_packet_path": "docs/youtube-factory/generated/live-client-pilot/script-packet-inbox-operator.json",
  "client": "Pilot Client Alpha",
  "engagement_slug": "ai-inbox-operator-5-person-business",
  "notion_status": "`+status+`",
  "notion_database": "Client Approval Queue",
  "notion_title": "I Built an AI Inbox Operator for a 5-Person Business",
  "approvers": [
    {"role":"loopsmith_reviewer","name":"Reviewer","status":"`+approverStatusAt(approvers, 0)+`"},
    {"role":"client_operator","name":"Pilot Client Alpha","status":"`+approverStatusAt(approvers, 1)+`"}
  ]
}
`)
		gateStatus := "blocked"
		blockers := `[
    "Notion approval status is \"` + status + `\".",
    "client_operator is still pending."
  ]`
		releaseWhen := `[
    "All named approvers move to approved.",
    "The Notion record status moves to an approval-complete state."
  ]`
		if status == "approved" && strings.Contains(strings.Join(approvers, " "), "(approved)") {
			gateStatus = "release_ready"
			blockers = `[]`
			releaseWhen = `[]`
		}
		mustWriteFile(t, filepath.Join(dir, "approval-status.json"), `{
  "gate_status": "`+gateStatus+`",
  "source_status": "`+status+`",
  "approvers": [
    {"role":"loopsmith_reviewer","name":"Reviewer","status":"`+approverStatusAt(approvers, 0)+`"},
    {"role":"client_operator","name":"Pilot Client Alpha","status":"`+approverStatusAt(approvers, 1)+`"}
  ],
  "blockers": `+blockers+`,
  "release_when": `+releaseWhen+`,
  "forced_approve": false
}
`)
	}
}

func mustWriteFile(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func approverStatusAt(lines []string, idx int) string {
	if idx >= len(lines) {
		return ""
	}
	_, _, status, ok := parseApproverLine(lines[idx])
	if !ok {
		return ""
	}
	return status
}
