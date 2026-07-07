package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildScriptPacketAndReviewBundle(t *testing.T) {
	brief := channelBrief{
		Metadata: metadataBlock{
			ID:        "brief_01",
			Version:   1,
			UpdatedAt: "2026-04-14",
			Source:    "docs/youtube-factory/episode-launch-packets/vid_01-inbox-operator.yaml",
		},
		Channel: channelBlock{
			BrandName:          "Back Office AI",
			Thesis:             "AI Back Office for Small Teams",
			Tagline:            "Build the AI back office before you hire for manual chaos.",
			NarrationDirection: "Warm storyteller with dramatic confidence.",
			WritingStyle:       []string{"plainspoken", "operator-grade"},
		},
		Render: renderBlock{
			TargetDurationMinutes: "8-12",
			SceneOrder:            []string{"cold_open", "system_map", "cta_endcard"},
			MusicDirection:        "restrained orchestral tension",
			VisualMotifs:          []string{"inboxes", "workflow arrows"},
		},
		Episode: episodeBlock{
			EpisodeID:    "vid_01",
			WorkingSlug:  "ai-inbox-operator-5-person-business",
			Pillar:       "role_replacement",
			Audience:     "agency owners",
			Workflow:     "inbox triage and routing",
			SearchIntent: "AI assistant for founders",
			Promise:      "Show a 5-person business how to turn an overloaded inbox into a routed operating queue.",
			ProofAsset: episodeAssetRef{
				Name:    "Hidden Margin Weekly Teardown Checklist",
				Type:    "checklist",
				OfferID: "workflow_checklists",
			},
		},
		Packaging: packagingBlock{
			FinalTitle:   "I Built an AI Inbox Operator for a 5-Person Business",
			BackupTitles: []string{"Replace Inbox Chaos With This AI Ops System"},
			TitleFamily:  "role_build",
			HookPromise:  "If your inbox is acting like your project manager, this episode shows the system that takes that job back.",
			Thumbnail: thumbnailBlock{
				Family:      "chaos_vs_system",
				Text:        "INBOX FIXED",
				FocalObject: "split-screen inbox transforming into a routed triage board",
				VisualNotes: []string{"messy left side", "clean right side"},
				Avoid:       []string{"robot face"},
			},
		},
		CTA: ctaBlock{
			PrimaryOfferID:     "workflow_checklists",
			PrimaryOfferName:   "Hidden Margin Weekly Teardown Checklist",
			SecondaryOfferID:   "ai_back_office_starter_pack",
			SecondaryOfferName: "AI Back Office Starter Pack",
			OnScreenLine:       "Grab the Hidden Margin Weekly Teardown Checklist below if you want to score your own workflow before you automate it.",
		},
		Publish: publishBlock{
			PlaylistID: "ai_roles",
			Tags:       []string{"ai automation", "inbox management"},
			Chapters: []chapterBeat{
				{Time: "00:00", Label: "Why inbox chaos gets expensive fast"},
				{Time: "02:16", Label: "How the AI inbox operator routes work"},
				{Time: "07:01", Label: "The checklist and next workflow"},
			},
		},
		QA: qaBlock{
			MustPass: []string{
				"Primary CTA is the Hidden Margin Weekly Teardown Checklist and appears before the starter pack.",
				"Human review stays explicit for customer-facing replies and escalations.",
			},
			BlockIf: []string{
				"The asset linked is not real.",
				"The video drifts into a generic AI tools roundup.",
			},
		},
		Approval: approvalBlock{
			Mode:           "live_client_pilot",
			Status:         "pending_external_approval",
			ClientName:     "Pilot Client Alpha",
			LivePacketPath: "docs/youtube-factory/generated/live-client-pilot/script-packet-inbox-operator.json",
			Approvers: []approverBlock{
				{Role: "loopsmith_reviewer", Name: "Reviewer", Status: "approved"},
				{Role: "client_operator", Name: "Pilot Client Alpha", Status: "pending"},
			},
		},
	}

	packet, err := buildScriptPacket(brief)
	if err != nil {
		t.Fatalf("buildScriptPacket() error = %v", err)
	}

	dir := t.TempDir()
	packetPath := filepath.Join(dir, "script-packet.json")
	if err := writePacket(packetPath, packet); err != nil {
		t.Fatalf("writePacket() error = %v", err)
	}
	if err := writeReviewBundle(filepath.Join(dir, "review-bundle"), brief, packet); err != nil {
		t.Fatalf("writeReviewBundle() error = %v", err)
	}

	summaryBytes, err := os.ReadFile(filepath.Join(dir, "review-bundle", "summary.md"))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	summary := string(summaryBytes)
	if !strings.Contains(summary, "Live Approval Review Bundle") {
		t.Fatalf("summary missing title: %s", summary)
	}
	if !strings.Contains(summary, brief.Packaging.FinalTitle) {
		t.Fatalf("summary missing packet title: %s", summary)
	}
	if !strings.Contains(summary, brief.Approval.LivePacketPath) {
		t.Fatalf("summary missing live packet path: %s", summary)
	}

	for _, name := range []string{"slack-payload.json", "google-drive-payload.json", "notion-payload.json"} {
		path := filepath.Join(dir, "review-bundle", name)
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("expected %s to exist: %v", name, err)
		}
	}
}

func TestResolveBundleDirDefaultsNextToPacket(t *testing.T) {
	got := resolveBundleDir("", "/tmp/output/script-packet.json")
	want := "/tmp/output/script-packet-review-bundle"
	if got != want {
		t.Fatalf("resolveBundleDir() = %q, want %q", got, want)
	}
}
