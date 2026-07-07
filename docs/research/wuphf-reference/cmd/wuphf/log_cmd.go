package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/nex-crm/wuphf/internal/agent"
)

// runLogCmd prints task receipts from the local agent log directory.
// No server required — reads directly from ~/.wuphf/office/tasks/.
//
// Usage:
//
//	wuphf log              — list the 20 most recent tasks
//	wuphf log <taskID>     — dump the full JSONL for a single task as pretty lines
//	wuphf log --agent eng  — list recent tasks for a specific agent
//	wuphf log --limit 50   — override the default list size
func runLogCmd(args []string) {
	fs := flag.NewFlagSet("log", flag.ExitOnError)
	agentFilter := fs.String("agent", "", "Filter the list by agent slug (e.g. eng, ceo)")
	limit := fs.Int("limit", 20, "Maximum number of tasks to list")
	jsonOut := fs.Bool("json", false, "Emit raw JSON instead of the pretty table")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "wuphf log — show agent task receipts")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Usage:")
		fmt.Fprintln(os.Stderr, "  wuphf log                 List recent tasks across all agents")
		fmt.Fprintln(os.Stderr, "  wuphf log <taskID>        Dump one task's full tool-call history")
		fmt.Fprintln(os.Stderr, "  wuphf log --agent eng     Filter the list to one agent")
		fmt.Fprintln(os.Stderr, "  wuphf log --limit 50      Override default list size")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Reads from ~/.wuphf/office/tasks/{taskID}/output.log.")
	}
	_ = fs.Parse(args)

	root := agent.DefaultTaskLogRoot()
	positional := fs.Args()
	if len(positional) > 0 {
		taskID := positional[0]
		entries, err := agent.ReadTaskLog(root, taskID)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			os.Exit(1)
		}
		if *jsonOut {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			_ = enc.Encode(entries)
			return
		}
		printTaskEntries(taskID, entries)
		return
	}

	tasks, err := agent.ListRecentTasks(root, *limit)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	if slug := strings.TrimSpace(*agentFilter); slug != "" {
		filtered := tasks[:0]
		for _, t := range tasks {
			if t.AgentSlug == slug {
				filtered = append(filtered, t)
			}
		}
		tasks = filtered
	}
	if *jsonOut {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(tasks)
		return
	}
	printTaskList(tasks, root)
}

func printTaskList(tasks []agent.TaskLogSummary, root string) {
	if len(tasks) == 0 {
		fmt.Println("No task receipts yet. (Logs land in " + root + " after agents run.)")
		return
	}
	fmt.Printf("%-20s  %-8s  %-6s  %-16s  %s\n", "TASK", "AGENT", "TOOLS", "LAST", "FLAGS")
	for _, t := range tasks {
		last := "-"
		if t.LastToolAt > 0 {
			last = time.UnixMilli(t.LastToolAt).Format("2006-01-02 15:04")
		}
		flag := ""
		if t.HasError {
			flag = "error"
		}
		fmt.Printf("%-20s  %-8s  %6d  %-16s  %s\n", t.TaskID, t.AgentSlug, t.ToolCallCount, last, flag)
	}
	fmt.Println("")
	fmt.Println("Dig into one with: wuphf log <taskID>")
}

func printTaskEntries(taskID string, entries []agent.TaskLogEntry) {
	fmt.Printf("== %s (%d tool calls) ==\n\n", taskID, len(entries))
	for i, e := range entries {
		when := "-"
		if e.StartedAt > 0 {
			when = time.UnixMilli(e.StartedAt).Format("15:04:05")
		}
		outcome := "ok"
		if e.Error != "" {
			outcome = "err: " + shortenErr(e.Error)
		}
		fmt.Printf("#%d  %s  %-20s  %s\n", i+1, when, e.ToolName, outcome)
		if len(e.Params) > 0 {
			raw, _ := json.Marshal(e.Params)
			fmt.Printf("      params: %s\n", truncate(string(raw), 200))
		}
	}
}

func shortenErr(s string) string {
	return truncate(strings.ReplaceAll(s, "\n", " "), 120)
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}
