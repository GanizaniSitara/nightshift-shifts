package main

import (
	"os/exec"
	"testing"
)

func TestHideConsoleWindow(t *testing.T) {
	cmd := exec.Command("cmd.exe", "/c", "echo", "test")
	hideConsoleWindow(cmd)

	if cmd.SysProcAttr == nil {
		t.Fatal("expected SysProcAttr to be set, got nil")
	}
	if !cmd.SysProcAttr.HideWindow {
		t.Errorf("expected HideWindow to be true, got false")
	}
	const (
		createNoWindow  = 0x08000000
		detachedProcess = 0x00000008
	)
	if cmd.SysProcAttr.CreationFlags != createNoWindow {
		t.Errorf("expected CreationFlags to be 0x%08x, got 0x%08x", createNoWindow, cmd.SysProcAttr.CreationFlags)
	}
	if cmd.SysProcAttr.CreationFlags&detachedProcess != 0 {
		t.Errorf("expected CreationFlags not to set DETACHED_PROCESS (0x%08x), got 0x%08x", detachedProcess, cmd.SysProcAttr.CreationFlags)
	}
}
