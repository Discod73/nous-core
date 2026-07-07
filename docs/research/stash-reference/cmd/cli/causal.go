package main

import (
	"context"
	"fmt"

	"github.com/alash3al/stash/internal/brain"
	"github.com/urfave/cli/v3"
)

func causalListCmd(ctx context.Context, cmd *cli.Command) error {
	namespaces := cmd.StringSlice("namespaces")
	page := brain.Pagination{
		Offset: cmd.Int("offset"),
		Limit:  cmd.Int("limit"),
	}

	bc := getBootstrap(cmd)
	links, err := bc.Brain.ListCausalLinks(ctx, namespaces, page)
	if err != nil {
		return err
	}
	return printJSON(links)
}

func causalCreateCmd(ctx context.Context, cmd *cli.Command) error {
	causeID := cmd.Int("cause-id")
	effectID := cmd.Int("effect-id")
	if causeID == 0 || effectID == 0 {
		return fmt.Errorf("both --cause-id and --effect-id are required")
	}

	namespace := cmd.String("namespace")
	confidence := cmd.Float("confidence")
	if confidence == 0 {
		confidence = 0.8
	}

	bc := getBootstrap(cmd)
	nsID, err := bc.Brain.ResolveNamespaceIDs(ctx, []string{namespace})
	if err != nil {
		return err
	}

	link, err := bc.Brain.CreateCausalLink(ctx, nsID[0], int64(causeID), int64(effectID), float32(confidence))
	if err != nil {
		return err
	}
	return printJSON(link)
}

func causalTraceCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("fact ID argument is required")
	}
	var factID int64
	if _, err := fmt.Sscanf(args.First(), "%d", &factID); err != nil {
		return fmt.Errorf("invalid fact ID: %w", err)
	}

	direction := cmd.String("direction")
	maxDepth := cmd.Int("depth")

	bc := getBootstrap(cmd)
	chain, err := bc.Brain.TraceCausalChain(ctx, factID, direction, maxDepth)
	if err != nil {
		return err
	}
	return printJSON(chain)
}

func causalDeleteCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("causal link ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid causal link ID: %w", err)
	}

	bc := getBootstrap(cmd)
	if err := bc.Brain.DeleteCausalLink(ctx, id); err != nil {
		return err
	}
	return printJSON(map[string]string{"message": "Causal link deleted successfully"})
}
