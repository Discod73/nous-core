package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type slackPayload struct {
	Channel   string   `json:"channel"`
	Text      string   `json:"text"`
	Checklist []string `json:"checklist"`
}

type googleDrivePayload struct {
	FolderName   string   `json:"folder_name"`
	DocumentName string   `json:"document_name"`
	Viewers      []string `json:"viewers"`
	Tags         []string `json:"tags"`
	Notes        []string `json:"notes"`
}

type notionPayload struct {
	Database   string            `json:"database"`
	Title      string            `json:"title"`
	Status     string            `json:"status"`
	Properties map[string]string `json:"properties"`
	Checklist  []string          `json:"checklist"`
}

type approverStatus struct {
	Role   string `json:"role"`
	Name   string `json:"name"`
	Status string `json:"status"`
}

type approvalPacket struct {
	RunID        string           `json:"run_id"`
	ApprovalMode string           `json:"approval_mode"`
	SourceBundle string           `json:"source_bundle"`
	LivePacket   string           `json:"live_packet_path"`
	Client       string           `json:"client,omitempty"`
	Engagement   string           `json:"engagement_slug,omitempty"`
	Approvers    []approverStatus `json:"approvers"`
	NotionStatus string           `json:"notion_status"`
	NotionDB     string           `json:"notion_database,omitempty"`
	NotionTitle  string           `json:"notion_title,omitempty"`
}

type approvalStatusArtifact struct {
	GateStatus    string           `json:"gate_status"`
	SourceStatus  string           `json:"source_status"`
	Approvers     []approverStatus `json:"approvers"`
	Blockers      []string         `json:"blockers,omitempty"`
	ReleaseWhen   []string         `json:"release_when,omitempty"`
	ForcedApprove bool             `json:"forced_approve"`
}

type bundleInputs struct {
	BundleDir       string
	Summary         string
	Slack           slackPayload
	Drive           googleDrivePayload
	Notion          notionPayload
	ApprovalPacket  *approvalPacket
	ApprovalStatus  *approvalStatusArtifact
	ApprovalSources []string
}

type contractCheck struct {
	Name    string   `json:"name"`
	Pass    bool     `json:"pass"`
	Missing []string `json:"missing,omitempty"`
}

type approvalGate struct {
	Status        string           `json:"status"`
	SourceStatus  string           `json:"source_status"`
	Approvers     []approverStatus `json:"approvers"`
	Blockers      []string         `json:"blockers,omitempty"`
	ReleaseWhen   []string         `json:"release_when,omitempty"`
	ForcedApprove bool             `json:"forced_approve"`
}

type consumerDispatch struct {
	Consumer      string         `json:"consumer"`
	Status        string         `json:"status"`
	Action        string         `json:"action"`
	PayloadSource string         `json:"payload_source"`
	PreviewOnly   bool           `json:"preview_only"`
	ApprovalGate  string         `json:"approval_gate"`
	Destination   map[string]any `json:"destination"`
	Checks        []string       `json:"checks,omitempty"`
	NextStep      string         `json:"next_step"`
	Payload       map[string]any `json:"payload"`
}

type workflowRun struct {
	BundleDir          string             `json:"bundle_dir"`
	SchemaChecks       []contractCheck    `json:"schema_checks"`
	ApprovalGate       approvalGate       `json:"approval_gate"`
	ApprovalSources    []string           `json:"approval_sources,omitempty"`
	ApprovalPacketPath string             `json:"approval_packet_path,omitempty"`
	ApprovalStatusPath string             `json:"approval_status_path,omitempty"`
	Consumers          []consumerDispatch `json:"consumers"`
	LivePacketPath     string             `json:"live_packet_path,omitempty"`
}

func main() {
	var bundleDir string
	var outDir string
	var forceApprove bool
	flag.StringVar(&bundleDir, "bundle-dir", "", "Path to the generated review bundle directory")
	flag.StringVar(&outDir, "out", "", "Directory to write the dry-run handoff artifacts")
	flag.BoolVar(&forceApprove, "force-approve", false, "Simulate a passed approval gate for dry-run dispatch previews")
	flag.Parse()

	if strings.TrimSpace(bundleDir) == "" {
		fmt.Fprintln(os.Stderr, "bundle-dir is required")
		os.Exit(2)
	}
	if strings.TrimSpace(outDir) == "" {
		outDir = resolveOutDir(bundleDir)
	}

	inputs, err := loadBundle(bundleDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "load bundle: %v\n", err)
		os.Exit(1)
	}

	schemaChecks := validateBundle(inputs)
	gate := buildApprovalGate(inputs, forceApprove)
	if hasSchemaFailure(schemaChecks) && gate.Status != "blocked" {
		gate.Status = "blocked"
		gate.Blockers = append(gate.Blockers, "Schema validation failed for one or more consumer payloads.")
	}

	run := workflowRun{
		BundleDir:          inputs.BundleDir,
		SchemaChecks:       schemaChecks,
		ApprovalGate:       gate,
		ApprovalSources:    inputs.ApprovalSources,
		ApprovalPacketPath: optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalPacket != nil, "approval-packet.json"),
		ApprovalStatusPath: optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalStatus != nil, "approval-status.json"),
		Consumers:          buildConsumers(inputs, gate, outDir),
		LivePacketPath:     livePacketPath(inputs),
	}

	if err := os.MkdirAll(outDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "mkdir output: %v\n", err)
		os.Exit(1)
	}
	if err := writeJSON(filepath.Join(outDir, "workflow-run.json"), run); err != nil {
		fmt.Fprintf(os.Stderr, "write workflow run: %v\n", err)
		os.Exit(1)
	}
	for _, consumer := range run.Consumers {
		name := consumer.Consumer + "-dispatch.json"
		if err := writeJSON(filepath.Join(outDir, name), consumer); err != nil {
			fmt.Fprintf(os.Stderr, "write %s: %v\n", name, err)
			os.Exit(1)
		}
	}
	if err := writeText(filepath.Join(outDir, "handoff-summary.md"), renderSummary(run)); err != nil {
		fmt.Fprintf(os.Stderr, "write summary: %v\n", err)
		os.Exit(1)
	}
}

func resolveOutDir(bundleDir string) string {
	clean := filepath.Clean(bundleDir)
	if strings.HasSuffix(clean, "-review-bundle") {
		return strings.TrimSuffix(clean, "-review-bundle") + "-review-handoff"
	}
	return filepath.Join(clean, "review-handoff")
}

func loadBundle(bundleDir string) (bundleInputs, error) {
	inputs := bundleInputs{BundleDir: filepath.Clean(bundleDir)}
	summaryBytes, err := os.ReadFile(filepath.Join(bundleDir, "summary.md"))
	if err != nil {
		return inputs, err
	}
	inputs.Summary = string(summaryBytes)
	if err := readJSON(filepath.Join(bundleDir, "slack-payload.json"), &inputs.Slack); err != nil {
		return inputs, err
	}
	if err := readJSON(filepath.Join(bundleDir, "google-drive-payload.json"), &inputs.Drive); err != nil {
		return inputs, err
	}
	if err := readJSON(filepath.Join(bundleDir, "notion-payload.json"), &inputs.Notion); err != nil {
		return inputs, err
	}
	if err := readOptionalJSON(filepath.Join(bundleDir, "approval-packet.json"), &inputs.ApprovalPacket); err != nil {
		return inputs, err
	}
	if err := readOptionalJSON(filepath.Join(bundleDir, "approval-status.json"), &inputs.ApprovalStatus); err != nil {
		return inputs, err
	}
	inputs.ApprovalSources = approvalSources(inputs)
	return inputs, nil
}

func validateBundle(inputs bundleInputs) []contractCheck {
	checks := []contractCheck{
		{
			Name: "slack-payload",
			Pass: strings.TrimSpace(inputs.Slack.Channel) != "" && strings.TrimSpace(inputs.Slack.Text) != "" && len(inputs.Slack.Checklist) > 0,
			Missing: collectMissing(
				requiredString("channel", inputs.Slack.Channel),
				requiredString("text", inputs.Slack.Text),
				requiredList("checklist", len(inputs.Slack.Checklist)),
			),
		},
		{
			Name: "google-drive-payload",
			Pass: strings.TrimSpace(inputs.Drive.FolderName) != "" &&
				strings.TrimSpace(inputs.Drive.DocumentName) != "" &&
				len(inputs.Drive.Viewers) > 0 &&
				len(inputs.Drive.Notes) > 0,
			Missing: collectMissing(
				requiredString("folder_name", inputs.Drive.FolderName),
				requiredString("document_name", inputs.Drive.DocumentName),
				requiredList("viewers", len(inputs.Drive.Viewers)),
				requiredList("notes", len(inputs.Drive.Notes)),
			),
		},
		{
			Name: "notion-payload",
			Pass: strings.TrimSpace(inputs.Notion.Database) != "" &&
				strings.TrimSpace(inputs.Notion.Title) != "" &&
				strings.TrimSpace(inputs.Notion.Status) != "" &&
				strings.TrimSpace(inputs.Notion.Properties["Live Packet"]) != "" &&
				strings.TrimSpace(inputs.Notion.Properties["Approval Mode"]) != "",
			Missing: collectMissing(
				requiredString("database", inputs.Notion.Database),
				requiredString("title", inputs.Notion.Title),
				requiredString("status", inputs.Notion.Status),
				requiredString("properties.Live Packet", inputs.Notion.Properties["Live Packet"]),
				requiredString("properties.Approval Mode", inputs.Notion.Properties["Approval Mode"]),
			),
		},
	}

	if inputs.ApprovalPacket != nil || inputs.ApprovalStatus != nil {
		checks = append(checks,
			contractCheck{
				Name: "approval-packet",
				Pass: inputs.ApprovalPacket != nil &&
					strings.TrimSpace(inputs.ApprovalPacket.ApprovalMode) != "" &&
					strings.TrimSpace(inputs.ApprovalPacket.LivePacket) != "" &&
					len(inputs.ApprovalPacket.Approvers) > 0,
				Missing: collectMissing(
					requiredPresent("approval-packet.json", inputs.ApprovalPacket != nil),
					requiredStringFromPtr("approval_mode", inputs.ApprovalPacket, func(v *approvalPacket) string { return v.ApprovalMode }),
					requiredStringFromPtr("live_packet_path", inputs.ApprovalPacket, func(v *approvalPacket) string { return v.LivePacket }),
					requiredListFromPtr("approvers", inputs.ApprovalPacket, func(v *approvalPacket) int { return len(v.Approvers) }),
				),
			},
			contractCheck{
				Name: "approval-status",
				Pass: inputs.ApprovalStatus != nil &&
					strings.TrimSpace(inputs.ApprovalStatus.GateStatus) != "" &&
					strings.TrimSpace(inputs.ApprovalStatus.SourceStatus) != "" &&
					len(inputs.ApprovalStatus.Approvers) > 0,
				Missing: collectMissing(
					requiredPresent("approval-status.json", inputs.ApprovalStatus != nil),
					requiredStringFromPtr("gate_status", inputs.ApprovalStatus, func(v *approvalStatusArtifact) string { return v.GateStatus }),
					requiredStringFromPtr("source_status", inputs.ApprovalStatus, func(v *approvalStatusArtifact) string { return v.SourceStatus }),
					requiredListFromPtr("approvers", inputs.ApprovalStatus, func(v *approvalStatusArtifact) int { return len(v.Approvers) }),
				),
			},
		)
	}

	return checks
}

func buildApprovalGate(inputs bundleInputs, forceApprove bool) approvalGate {
	if inputs.ApprovalStatus != nil {
		return buildApprovalGateFromStatus(inputs, forceApprove)
	}
	return buildApprovalGateFromNotion(inputs.Notion, forceApprove)
}

func buildApprovalGateFromStatus(inputs bundleInputs, forceApprove bool) approvalGate {
	status := *inputs.ApprovalStatus
	gate := approvalGate{
		Status:        normalizedGateStatus(status.GateStatus),
		SourceStatus:  strings.TrimSpace(status.SourceStatus),
		Approvers:     cloneApprovers(status.Approvers),
		Blockers:      cloneStrings(status.Blockers),
		ReleaseWhen:   cloneStrings(status.ReleaseWhen),
		ForcedApprove: forceApprove,
	}

	if forceApprove {
		gate.Status = "release_ready"
		gate.Blockers = nil
		gate.ReleaseWhen = []string{"Dry-run override supplied with --force-approve."}
		return gate
	}

	if gate.Status == "" {
		gate.Status = "blocked"
	}
	if gate.Status != "blocked" && gate.Status != "release_ready" {
		gate.Blockers = append(gate.Blockers, fmt.Sprintf("Approval status artifact has unsupported gate_status %q.", status.GateStatus))
		gate.Status = "blocked"
	}
	if len(gate.Approvers) == 0 && inputs.ApprovalPacket != nil {
		gate.Approvers = cloneApprovers(inputs.ApprovalPacket.Approvers)
	}
	if len(gate.Approvers) == 0 {
		gate.Blockers = append(gate.Blockers, "No approver states were found in the approval artifacts.")
		gate.Status = "blocked"
	}
	if inputs.ApprovalPacket != nil && strings.TrimSpace(inputs.ApprovalPacket.NotionStatus) != "" {
		packetStatus := normalizeStatus(inputs.ApprovalPacket.NotionStatus)
		sourceStatus := normalizeStatus(gate.SourceStatus)
		if packetStatus != "" && sourceStatus != "" && packetStatus != sourceStatus {
			gate.Blockers = append(gate.Blockers, fmt.Sprintf("Approval packet notion_status %q does not match approval status source_status %q.", inputs.ApprovalPacket.NotionStatus, status.SourceStatus))
			gate.Status = "blocked"
		}
	}
	if len(gate.Blockers) > 0 && len(gate.ReleaseWhen) == 0 {
		gate.ReleaseWhen = []string{
			"Update approval-status.json and approval-packet.json so the approver states match and the gate resolves cleanly.",
		}
	}
	return gate
}

func buildApprovalGateFromNotion(notion notionPayload, forceApprove bool) approvalGate {
	gate := approvalGate{
		Status:        "release_ready",
		SourceStatus:  strings.TrimSpace(notion.Status),
		Approvers:     parseApprovers(notion.Checklist),
		ForcedApprove: forceApprove,
	}

	if forceApprove {
		gate.ReleaseWhen = []string{"Dry-run override supplied with --force-approve."}
		return gate
	}

	normalizedStatus := normalizeStatus(notion.Status)
	if normalizedStatus == "" || strings.Contains(normalizedStatus, "pending") {
		gate.Blockers = append(gate.Blockers, fmt.Sprintf("Notion approval status is %q.", notion.Status))
	}
	if strings.Contains(normalizedStatus, "reject") || strings.Contains(normalizedStatus, "block") {
		gate.Blockers = append(gate.Blockers, fmt.Sprintf("Notion approval status is %q.", notion.Status))
	}

	for _, approver := range gate.Approvers {
		status := normalizeStatus(approver.Status)
		switch {
		case status == "approved":
		case strings.Contains(status, "pending"):
			gate.Blockers = append(gate.Blockers, fmt.Sprintf("%s is still pending.", approver.Role))
		case strings.Contains(status, "reject") || strings.Contains(status, "block"):
			gate.Blockers = append(gate.Blockers, fmt.Sprintf("%s is not approved.", approver.Role))
		default:
			gate.Blockers = append(gate.Blockers, fmt.Sprintf("%s has an unknown status %q.", approver.Role, approver.Status))
		}
	}

	if len(gate.Blockers) > 0 {
		gate.Status = "blocked"
		gate.ReleaseWhen = []string{
			"All named approvers move to approved.",
			"The Notion record status moves to an approval-complete state.",
		}
	}

	if len(gate.Approvers) == 0 {
		gate.Status = "blocked"
		gate.Blockers = append(gate.Blockers, "No approver states were found in the review bundle.")
		gate.ReleaseWhen = []string{
			"Add approver checklist lines in the format role: Name (status).",
			"Re-run the handoff once approvers are encoded in the bundle.",
		}
	}

	return gate
}

func buildConsumers(inputs bundleInputs, gate approvalGate, outDir string) []consumerDispatch {
	status := "staged_pending_approval"
	if gate.Status == "release_ready" {
		status = "ready_for_delivery"
	}

	return []consumerDispatch{
		{
			Consumer:      "slack",
			Status:        status,
			Action:        consumerAction("post_review_handoff", gate.Status),
			PayloadSource: filepath.Join(inputs.BundleDir, "slack-payload.json"),
			PreviewOnly:   true,
			ApprovalGate:  gate.Status,
			Destination: map[string]any{
				"channel": inputs.Slack.Channel,
			},
			Checks: []string{
				fmt.Sprintf("Checklist items: %d", len(inputs.Slack.Checklist)),
				"Send only after approval gate clears.",
			},
			NextStep: consumerNextStep(gate.Status, "Send the review-ready post into the mapped Slack channel."),
			Payload: map[string]any{
				"text":      inputs.Slack.Text,
				"checklist": inputs.Slack.Checklist,
				"approval_context": map[string]any{
					"approval_packet_path": optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalPacket != nil, "approval-packet.json"),
					"approval_status_path": optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalStatus != nil, "approval-status.json"),
					"gate_status":          gate.Status,
				},
			},
		},
		{
			Consumer:      "google-drive",
			Status:        status,
			Action:        consumerAction("stage_review_folder", gate.Status),
			PayloadSource: filepath.Join(inputs.BundleDir, "google-drive-payload.json"),
			PreviewOnly:   true,
			ApprovalGate:  gate.Status,
			Destination: map[string]any{
				"folder_name":   inputs.Drive.FolderName,
				"document_name": inputs.Drive.DocumentName,
			},
			Checks: []string{
				fmt.Sprintf("Viewers captured: %d", len(inputs.Drive.Viewers)),
				"Create only placeholder review assets in dry-run mode.",
			},
			NextStep: consumerNextStep(gate.Status, "Create the review folder/document preview and assign viewers."),
			Payload: map[string]any{
				"viewers": inputs.Drive.Viewers,
				"tags":    inputs.Drive.Tags,
				"notes":   inputs.Drive.Notes,
				"approval_context": map[string]any{
					"approval_packet_path": optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalPacket != nil, "approval-packet.json"),
					"approval_status_path": optionalBundleArtifactPath(inputs.BundleDir, inputs.ApprovalStatus != nil, "approval-status.json"),
					"gate_status":          gate.Status,
				},
			},
		},
		{
			Consumer:      "notion",
			Status:        status,
			Action:        consumerAction("upsert_approval_queue_record", gate.Status),
			PayloadSource: filepath.Join(inputs.BundleDir, "notion-payload.json"),
			PreviewOnly:   true,
			ApprovalGate:  gate.Status,
			Destination: map[string]any{
				"database": inputs.Notion.Database,
				"title":    inputs.Notion.Title,
			},
			Checks: []string{
				fmt.Sprintf("Checklist items: %d", len(inputs.Notion.Checklist)),
				"Approval record must stay aligned with named approver states.",
			},
			NextStep: consumerNextStep(gate.Status, "Upsert the approval queue record with the approved handoff metadata."),
			Payload: map[string]any{
				"status":     inputs.Notion.Status,
				"properties": inputs.Notion.Properties,
				"checklist":  inputs.Notion.Checklist,
			},
		},
	}
}

func approvalSources(inputs bundleInputs) []string {
	var sources []string
	if inputs.ApprovalPacket != nil {
		sources = append(sources, "approval-packet.json")
	}
	if inputs.ApprovalStatus != nil {
		sources = append(sources, "approval-status.json")
	}
	if len(sources) == 0 {
		sources = append(sources, "notion-payload.json")
	}
	return sources
}

func livePacketPath(inputs bundleInputs) string {
	if inputs.ApprovalPacket != nil && strings.TrimSpace(inputs.ApprovalPacket.LivePacket) != "" {
		return strings.TrimSpace(inputs.ApprovalPacket.LivePacket)
	}
	return strings.TrimSpace(inputs.Notion.Properties["Live Packet"])
}

func optionalBundleArtifactPath(bundleDir string, present bool, name string) string {
	if !present {
		return ""
	}
	return filepath.Join(bundleDir, name)
}

func consumerAction(base string, gateStatus string) string {
	if gateStatus == "release_ready" {
		return base
	}
	return "hold_for_approval"
}

func consumerNextStep(gateStatus, released string) string {
	if gateStatus == "release_ready" {
		return released
	}
	return "Hold this consumer at the approval gate and rerun after approval is complete."
}

func parseApprovers(checklist []string) []approverStatus {
	var approvers []approverStatus
	for _, item := range checklist {
		role, name, status, ok := parseApproverLine(item)
		if !ok {
			continue
		}
		approvers = append(approvers, approverStatus{
			Role:   role,
			Name:   name,
			Status: status,
		})
	}
	return approvers
}

func parseApproverLine(line string) (string, string, string, bool) {
	parts := strings.SplitN(strings.TrimSpace(line), ":", 2)
	if len(parts) != 2 {
		return "", "", "", false
	}
	role := strings.TrimSpace(parts[0])
	rest := strings.TrimSpace(parts[1])
	openIdx := strings.LastIndex(rest, "(")
	closeIdx := strings.LastIndex(rest, ")")
	if role == "" || openIdx <= 0 || closeIdx <= openIdx {
		return "", "", "", false
	}
	name := strings.TrimSpace(rest[:openIdx])
	status := strings.TrimSpace(rest[openIdx+1 : closeIdx])
	if name == "" || status == "" {
		return "", "", "", false
	}
	return role, name, status, true
}

func normalizeStatus(v string) string {
	v = strings.ToLower(strings.TrimSpace(v))
	v = strings.ReplaceAll(v, "-", "_")
	v = strings.ReplaceAll(v, " ", "_")
	return v
}

func hasSchemaFailure(checks []contractCheck) bool {
	for _, check := range checks {
		if !check.Pass {
			return true
		}
	}
	return false
}

func requiredPresent(name string, ok bool) error {
	if !ok {
		return errors.New(name)
	}
	return nil
}

func requiredString(name, value string) error {
	if strings.TrimSpace(value) == "" {
		return errors.New(name)
	}
	return nil
}

func requiredStringFromPtr[T any](name string, value *T, getter func(*T) string) error {
	if value == nil {
		return errors.New(name)
	}
	return requiredString(name, getter(value))
}

func requiredList(name string, count int) error {
	if count == 0 {
		return errors.New(name)
	}
	return nil
}

func requiredListFromPtr[T any](name string, value *T, getter func(*T) int) error {
	if value == nil {
		return errors.New(name)
	}
	return requiredList(name, getter(value))
}

func collectMissing(errs ...error) []string {
	var missing []string
	for _, err := range errs {
		if err != nil {
			missing = append(missing, err.Error())
		}
	}
	sort.Strings(missing)
	return missing
}

func readJSON(path string, target any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, target)
}

func readOptionalJSON[T any](path string, target **T) error {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	var value T
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	*target = &value
	return nil
}

func writeJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o644)
}

func writeText(path, value string) error {
	return os.WriteFile(path, []byte(value), 0o644)
}

func cloneApprovers(items []approverStatus) []approverStatus {
	if len(items) == 0 {
		return nil
	}
	out := make([]approverStatus, len(items))
	copy(out, items)
	return out
}

func cloneStrings(items []string) []string {
	if len(items) == 0 {
		return nil
	}
	out := make([]string, len(items))
	copy(out, items)
	return out
}

func normalizedGateStatus(v string) string {
	switch normalizeStatus(v) {
	case "release_ready", "released_live":
		return "release_ready"
	case "blocked", "hold_for_approval", "staged_pending_approval", "":
		return "blocked"
	default:
		return normalizeStatus(v)
	}
}

func renderSummary(run workflowRun) string {
	var b strings.Builder
	b.WriteString("# Review Bundle Handoff Dry Run\n\n")
	b.WriteString("## Approval Gate\n")
	b.WriteString(fmt.Sprintf("- Gate status: %s\n", run.ApprovalGate.Status))
	b.WriteString(fmt.Sprintf("- Source status: %s\n", run.ApprovalGate.SourceStatus))
	b.WriteString(fmt.Sprintf("- Forced approve: %t\n", run.ApprovalGate.ForcedApprove))
	if len(run.ApprovalSources) > 0 {
		b.WriteString(fmt.Sprintf("- Approval sources: %s\n", strings.Join(run.ApprovalSources, ", ")))
	}
	if len(run.ApprovalGate.Blockers) > 0 {
		b.WriteString("\n## Blockers\n")
		for _, item := range run.ApprovalGate.Blockers {
			b.WriteString(fmt.Sprintf("- %s\n", item))
		}
	}
	if len(run.ApprovalGate.ReleaseWhen) > 0 {
		b.WriteString("\n## Release Conditions\n")
		for _, item := range run.ApprovalGate.ReleaseWhen {
			b.WriteString(fmt.Sprintf("- %s\n", item))
		}
	}

	b.WriteString("\n## Schema Checks\n")
	for _, check := range run.SchemaChecks {
		status := "pass"
		if !check.Pass {
			status = "fail"
		}
		b.WriteString(fmt.Sprintf("- %s: %s\n", check.Name, status))
		for _, missing := range check.Missing {
			b.WriteString(fmt.Sprintf("  - missing %s\n", missing))
		}
	}

	b.WriteString("\n## Consumer Routing\n")
	for _, consumer := range run.Consumers {
		b.WriteString(fmt.Sprintf("- %s: %s (%s)\n", consumer.Consumer, consumer.Status, consumer.Action))
		b.WriteString(fmt.Sprintf("  - next: %s\n", consumer.NextStep))
	}
	if run.LivePacketPath != "" {
		b.WriteString("\n## Live Packet\n")
		b.WriteString(fmt.Sprintf("- %s\n", run.LivePacketPath))
	}
	if run.ApprovalPacketPath != "" || run.ApprovalStatusPath != "" {
		b.WriteString("\n## Approval Artifacts\n")
		if run.ApprovalPacketPath != "" {
			b.WriteString(fmt.Sprintf("- packet: %s\n", run.ApprovalPacketPath))
		}
		if run.ApprovalStatusPath != "" {
			b.WriteString(fmt.Sprintf("- status: %s\n", run.ApprovalStatusPath))
		}
	}
	return b.String()
}
