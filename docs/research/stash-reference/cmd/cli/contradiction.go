package main

import (
	"context"
	"fmt"

	"github.com/alash3al/stash/internal/brain"
	"github.com/urfave/cli/v3"
)

func contradictionsListCmd(ctx context.Context, cmd *cli.Command) error {
	namespaces := cmd.StringSlice("namespaces")
	page := brain.Pagination{
		Offset: cmd.Int("offset"),
		Limit:  cmd.Int("limit"),
	}

	bc := getBootstrap(cmd)
	contradictions, err := bc.Brain.ListContradictions(ctx, namespaces, page)
	if err != nil {
		return err
	}
	return printJSON(contradictions)
}

func contradictionResolveCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("contradiction ID is required")
	}
	var id int64
	if _, err := fmt.Sscanf(args.First(), "%d", &id); err != nil {
		return fmt.Errorf("invalid contradiction ID: %w", err)
	}

	resolution := cmd.String("resolution")

	bc := getBootstrap(cmd)
	if err := bc.Brain.ResolveContradiction(ctx, id, resolution); err != nil {
		return err
	}
	return printJSON(map[string]string{"message": "Contradiction resolved successfully"})
}
