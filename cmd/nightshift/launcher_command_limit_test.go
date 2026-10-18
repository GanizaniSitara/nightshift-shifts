package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMemberCommandWindowsLimit(t *testing.T) {
	for _, tc := range []struct {
		name string
		arg  string
		ok   bool
	}{
		{"last legal unit", strings.Repeat("x", 32766), true},
		{"NUL exceeds limit", strings.Repeat("x", 32767), false},
		// x + space + quoted argument + NUL: 1 + 1 + 2 + 32762 + 1 = 32767.
		{"quoted boundary", " " + strings.Repeat("x", 32761), true},
		{"quoted overflow", " " + strings.Repeat("x", 32762), false},
		{"astral boundary", strings.Repeat("😀", 16383), true},
		{"astral overflow", strings.Repeat("😀", 16384), false},
		{"escaped quotes overflow", strings.Repeat(`"`, 16384), false},
		{"trailing slashes quoted overflow", " " + strings.Repeat(`\`, 16384), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			command := []string{tc.arg}
			if strings.HasPrefix(tc.name, "quoted") || strings.Contains(tc.name, "escaped") || strings.Contains(tc.name, "slashes") {
				command = []string{"x", tc.arg}
			}
			err := checkMemberCommand("Coder-2", command)
			if (err == nil) != tc.ok {
				t.Fatalf("ok=%v, error=%v", tc.ok, err)
			}
			if err != nil && (!strings.Contains(err.Error(), "Coder-2") || !strings.Contains(err.Error(), "32767")) {
				t.Fatalf("error must identify member and limit: %v", err)
			}
		})
	}
}

func TestCodexLongBriefAndDeferredMessageUseFile(t *testing.T) {
	f := tbNewRenderFixture(t)
	m := memberFromObject(tbCrewMember("Coder", "Coder", "codex", "gpt-6.1-sol"))
	brief := strings.Repeat("Task café 😀 \"quoted\" \\\r\n", 2200)
	if len(brief) < 40*1024 {
		t.Fatal("fixture must exceed 40KB")
	}
	for _, first := range []string{openingMessage(m, f.stateDir), "msg:123 kind:assignment asg:retry\n\n" + strings.Repeat("assignment ", 5000)} {
		command, err := memberLaunchCommand(m, brief, first, "auto", f.stateDir, f.repo, true, false)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(f.stateDir, "Coder.brief.md")
		got, err := os.ReadFile(path)
		if err != nil || string(got) != brief+"\n\n"+first {
			t.Fatalf("brief/opening message not preserved byte for byte: %v", err)
		}
		want := "Read the file \"" + filepath.ToSlash(path) + "\" and follow its instructions."
		if command[len(command)-1] != want || len(strings.Join(command, " ")) > 4096 {
			t.Fatalf("expected short read-file argv, got %q", command)
		}
	}
}

func TestStartRejectsOversizedMemberBeforeWindow(t *testing.T) {
	f := tbNewRenderFixture(t)
	t.Setenv("LOCALAPPDATA", filepath.Join(f.root, "local"))
	t.Setenv("PATH", f.root) // Even a regression must never reach the real wt.exe.
	stderr, err := os.CreateTemp(f.root, "stderr")
	if err != nil {
		t.Fatal(err)
	}
	previousStderr := os.Stderr
	os.Stderr = stderr
	t.Cleanup(func() { os.Stderr = previousStderr; stderr.Close() })
	crewFile := filepath.Join(f.repo, "oversized.yaml")
	f.write(crewFile, "roles:\n  - role: Foreman\n    brief: foreman.md\n    human_contact: true\n    tool: codex\n    model: gpt-6.1-sol\n  - role: Advisor\n    brief: advisor.md\n    tool: codex\n    model: "+strings.Repeat("m", 33000)+"\n")
	_, err = runStart([]string{strings.Repeat("ticket text ", 4000), "--cwd", f.repo, "--crew-file", crewFile, "--no-worktree"}, false)
	diagnostic, readErr := os.ReadFile(stderr.Name())
	if err == nil || readErr != nil || !strings.Contains(string(diagnostic), "Advisor") || !strings.Contains(string(diagnostic), "32767") {
		t.Fatalf("expected reserved Advisor rejection before launch, got %v, %s", err, diagnostic)
	}
	// installProfile and launchCrew both leave files before opening a window. Neither may run.
	for _, pattern := range []string{"launch.json", "terminal-settings-before.json"} {
		matches, err := filepath.Glob(filepath.Join(f.root, "shifts", "*", pattern))
		if err != nil || len(matches) != 0 {
			t.Fatalf("preflight reached window/profile setup: %v, %v", matches, err)
		}
	}
}
