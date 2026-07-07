//go:build darwin || linux

package team

import (
	"os"
	"os/exec"
	"syscall"
)

func configureHeadlessProcess(cmd *exec.Cmd) {
	if cmd == nil {
		return
	}
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

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
	if pgid, err := syscall.Getpgid(pid); err == nil && pgid > 0 {
		_ = syscall.Kill(-pgid, syscall.SIGKILL)
		return
	}
	if proc, err := os.FindProcess(pid); err == nil {
		_ = proc.Kill()
	}
}
