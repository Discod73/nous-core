//go:build windows

package team

import (
	"os"
	"os/exec"
)

func configureHeadlessProcess(cmd *exec.Cmd) {}

func terminateHeadlessProcess(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	terminateHeadlessProcessPID(cmd.Process.Pid)
}

func terminateHeadlessProcessPID(pid int) {
	if pid <= 0 {
		return
	}
	if proc, err := os.FindProcess(pid); err == nil {
		_ = proc.Kill()
	}
}
