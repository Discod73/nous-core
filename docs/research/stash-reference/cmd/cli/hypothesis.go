package main

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/alash3al/stash/internal/brain"
	"github.com/urfave/cli/v3"
)

func hypothesisCreateCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("content argument is required")
	}
	content := args.First()
	verificationPlan := cmd.String("verification-plan")
	confidence := cmd.Float("confidence")
	if confidence == 0 {
		confidence = 0.5
	}
	namespace := cmd.String("namespace")

	var sourceFactIDs []int64
	if raw := cmd.String("source-fact-ids"); raw != "" {
		for _, s := range strings.Split(raw, ",") {
			s = strings.TrimSpace(s)
			if id, err := strconv.ParseInt(s, 10, 64); err == nil {
				sourceFactIDs = append(sourceFactIDs, id)
			}
		}
	}

	bc := getBootstrap(cmd)
	nsIDs, err := bc.Brain.ResolveNamespaceIDs(ctx, []string{namespace})
	if err != nil {
		return err
	}

	h, err := bc.Brain.CreateHypothesis(ctx, nsIDs[0], content, verificationPlan, float32(confidence), sourceFactIDs)
	if err != nil {
		return err
	}
	return printJSON(h)
}

func hypothesisListCmd(ctx context.Context, cmd *cli.Command) error {
	namespaces := cmd.StringSlice("namespaces")
	status := cmd.String("status")
	page := brain.Pagination{
		Offset: cmd.Int("offset"),
		Limit:  cmd.Int("limit"),
	}

	bc := getBootstrap(cmd)
	hypotheses, err := bc.Brain.ListHypotheses(ctx, namespaces, status, page)
	if err != nil {
		return err
	}
	return printJSON(hypotheses)
}

func hypothesisShowCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	bc := getBootstrap(cmd)
	h, err := bc.Brain.GetHypothesis(ctx, id)
	if err != nil {
		return err
	}
	return printJSON(h)
}

func hypothesisTestCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	bc := getBootstrap(cmd)
	h, err := bc.Brain.UpdateHypothesisStatus(ctx, id, "testing")
	if err != nil {
		return err
	}
	return printJSON(h)
}

func hypothesisConfirmCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	bc := getBootstrap(cmd)
	h, f, err := bc.Brain.ConfirmHypothesis(ctx, id)
	if err != nil {
		return err
	}
	return printJSON(map[string]any{
		"hypothesis": h,
		"fact":       f,
	})
}

func hypothesisRejectCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	reason := cmd.String("reason")

	bc := getBootstrap(cmd)
	h, err := bc.Brain.RejectHypothesis(ctx, id, reason)
	if err != nil {
		return err
	}
	return printJSON(h)
}

func hypothesisRefineCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	content := cmd.String("content")
	verificationPlan := cmd.String("verification-plan")
	confidence := float32(cmd.Float("confidence"))

	bc := getBootstrap(cmd)
	h, err := bc.Brain.RefineHypothesis(ctx, id, content, verificationPlan, confidence)
	if err != nil {
		return err
	}
	return printJSON(h)
}

func hypothesisDeleteCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("hypothesis ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid hypothesis ID: %w", err)
	}

	bc := getBootstrap(cmd)
	if err := bc.Brain.DeleteHypothesis(ctx, id); err != nil {
		return err
	}
	return printJSON(map[string]string{"message": "Hypothesis deleted successfully"})
}
