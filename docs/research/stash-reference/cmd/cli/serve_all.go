package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/alash3al/stash/internal/bootstrap"
	"github.com/urfave/cli/v3"
)

func serveAllCmd(ctx context.Context, cmd *cli.Command) error {
	bc := getBootstrap(cmd)

	httpHost := cmd.String("http-host")
	httpPort := cmd.String("http-port")
	mcpHost := cmd.String("mcp-host")
	mcpPort := cmd.String("mcp-port")
	consolidateInterval := cmd.Duration("consolidate-interval")
	consolidateNamespaces := cmd.StringSlice("consolidate-namespaces")

	ctx, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()

	var wg sync.WaitGroup
	wg.Add(3)

	go func() {
		defer wg.Done()
		c := cmdWithBootstrap(bc,
			&cli.StringFlag{Name: "host", Value: httpHost},
			&cli.StringFlag{Name: "port", Value: httpPort},
		)
		log.Println("starting HTTP server")
		if err := serveHTTP(ctx, c); err != nil {
			log.Printf("HTTP server stopped: %v", err)
		}
	}()

	go func() {
		defer wg.Done()
		c := cmdWithBootstrap(bc,
			&cli.StringFlag{Name: "host", Value: mcpHost},
			&cli.StringFlag{Name: "port", Value: mcpPort},
		)
		log.Println("starting MCP SSE server")
		if err := mcpServeCmd(ctx, c); err != nil {
			log.Printf("MCP server stopped: %v", err)
		}
	}()

	go func() {
		defer wg.Done()
		c := cmdWithBootstrap(bc,
			&cli.DurationFlag{Name: "interval", Value: consolidateInterval},
			&cli.StringSliceFlag{Name: "namespaces", Value: consolidateNamespaces},
		)
		log.Println("starting consolidation service")
		if err := consolidateServeCmd(ctx, c); err != nil {
			log.Printf("consolidation service stopped: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("signal received, waiting for services to stop...")

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(10 * time.Second):
		log.Println("timed out waiting for services to stop")
	}

	return nil
}

func cmdWithBootstrap(bc *bootstrap.Context, flags ...cli.Flag) *cli.Command {
	return &cli.Command{
		Flags:    flags,
		Metadata: map[string]any{"bootstrapCtx": bc},
	}
}
