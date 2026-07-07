package main

import (
	"context"
	"fmt"
	"time"

	"github.com/urfave/cli/v3"
)

func contextSetCmd(ctx context.Context, cmd *cli.Command) error {
	args := cmd.Args()
	if args.Len() == 0 {
		return fmt.Errorf("focus argument is required")
	}
	focus := args.First()
	namespace := cmd.String("namespace")
	expires := cmd.Duration("expires")

	bc := getBootstrap(cmd)
	if err := bc.Brain.SetContext(ctx, namespace, focus, time.Now().UTC().Add(expires)); err != nil {
		return err
	}
	return printJSON(map[string]string{"message": "Context set successfully"})
}

func contextShowCmd(ctx context.Context, cmd *cli.Command) error {
	namespace := cmd.String("namespace")

	bc := getBootstrap(cmd)
	c, err := bc.Brain.GetContext(ctx, namespace)
	if err != nil {
		return err
	}
	if c == nil {
		return printJSON(map[string]string{"message": "No context set"})
	}
	return printJSON(c)
}
