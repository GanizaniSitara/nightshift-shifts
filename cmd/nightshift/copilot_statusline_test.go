package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type copilotSettings struct {
	StatusLine *struct {
		Type    string `json:"type"`
		Command string `json:"command"`
	} `json:"statusLine"`
	OtherKey string `json:"otherKey"`
	Theme    string `json:"theme"`
}

func TestCopilotStatuslineMissingFileCreated(t *testing.T) {
	home := t.TempDir()
	t.Setenv("COPILOT_HOME", home)
	t.Setenv("USERPROFILE", t.TempDir())

	settingsPath := filepath.Join(home, "settings.json")
	if _, err := os.Stat(settingsPath); !os.IsNotExist(err) {
		t.Fatalf("expected settings.json to not exist initially")
	}

	if !installCopilotStatusline() {
		t.Fatalf("installCopilotStatusline returned false on missing file")
	}

	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("failed to read created settings.json: %v", err)
	}

	var parsed copilotSettings
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal settings.json: %v", err)
	}

	if parsed.StatusLine == nil {
		t.Fatalf("statusLine missing from created settings.json")
	}
	if parsed.StatusLine.Type != "command" {
		t.Errorf("statusLine.type = %q, want %q", parsed.StatusLine.Type, "command")
	}
	wantCmd := nightshiftCommand() + " statusline"
	if parsed.StatusLine.Command != wantCmd {
		t.Errorf("statusLine.command = %q, want %q", parsed.StatusLine.Command, wantCmd)
	}
}

func TestCopilotStatuslineIdempotent(t *testing.T) {
	home := t.TempDir()
	t.Setenv("COPILOT_HOME", home)
	t.Setenv("USERPROFILE", t.TempDir())

	if !installCopilotStatusline() {
		t.Fatalf("first installCopilotStatusline failed")
	}
	if !installCopilotStatusline() {
		t.Fatalf("second installCopilotStatusline failed; expected idempotent re-run to return true")
	}

	data, err := os.ReadFile(filepath.Join(home, "settings.json"))
	if err != nil {
		t.Fatalf("failed to read settings.json: %v", err)
	}

	var parsed copilotSettings
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal settings.json: %v", err)
	}

	wantCmd := nightshiftCommand() + " statusline"
	if parsed.StatusLine == nil || parsed.StatusLine.Command != wantCmd {
		t.Fatalf("unexpected statusLine after second run: %+v", parsed.StatusLine)
	}
}

func TestCopilotStatuslinePreservesOtherKeys(t *testing.T) {
	home := t.TempDir()
	t.Setenv("COPILOT_HOME", home)
	t.Setenv("USERPROFILE", t.TempDir())

	settingsPath := filepath.Join(home, "settings.json")
	initial := `{"otherKey": "customValue", "theme": "dark"}`
	if err := os.WriteFile(settingsPath, []byte(initial), 0666); err != nil {
		t.Fatal(err)
	}

	if !installCopilotStatusline() {
		t.Fatalf("installCopilotStatusline returned false")
	}

	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("failed to read settings.json: %v", err)
	}

	var parsed copilotSettings
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal settings.json: %v", err)
	}

	if parsed.OtherKey != "customValue" {
		t.Errorf("otherKey = %q, want %q", parsed.OtherKey, "customValue")
	}
	if parsed.Theme != "dark" {
		t.Errorf("theme = %q, want %q", parsed.Theme, "dark")
	}
	wantCmd := nightshiftCommand() + " statusline"
	if parsed.StatusLine == nil || parsed.StatusLine.Command != wantCmd {
		t.Errorf("statusLine.command = %v, want %q", parsed.StatusLine, wantCmd)
	}
}

func TestCopilotStatuslineForeignStatusLinePreservedAndReturnsFalse(t *testing.T) {
	home := t.TempDir()
	t.Setenv("COPILOT_HOME", home)
	t.Setenv("USERPROFILE", t.TempDir())

	settingsPath := filepath.Join(home, "settings.json")
	initial := `{"statusLine": {"type": "command", "command": "my-custom-statusline"}, "theme": "solarized"}`
	if err := os.WriteFile(settingsPath, []byte(initial), 0666); err != nil {
		t.Fatal(err)
	}

	if installCopilotStatusline() {
		t.Fatalf("installCopilotStatusline returned true, want false when foreign statusLine is present")
	}

	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("failed to read settings.json: %v", err)
	}

	var parsed copilotSettings
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal settings.json: %v", err)
	}

	if parsed.StatusLine == nil || parsed.StatusLine.Command != "my-custom-statusline" {
		t.Errorf("statusLine command modified: %v, want %q", parsed.StatusLine, "my-custom-statusline")
	}
	if parsed.Theme != "solarized" {
		t.Errorf("theme modified: %q, want %q", parsed.Theme, "solarized")
	}
}

func TestCopilotStatuslineDefaultHomeFallback(t *testing.T) {
	t.Setenv("COPILOT_HOME", "")
	userDir := t.TempDir()
	t.Setenv("USERPROFILE", userDir)

	if !installCopilotStatusline() {
		t.Fatalf("installCopilotStatusline returned false with default home fallback")
	}

	settingsPath := filepath.Join(userDir, ".copilot", "settings.json")
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("expected fallback settings file %s: %v", settingsPath, err)
	}

	var parsed copilotSettings
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal settings.json: %v", err)
	}

	wantCmd := nightshiftCommand() + " statusline"
	if parsed.StatusLine == nil || parsed.StatusLine.Command != wantCmd {
		t.Errorf("statusLine.command = %v, want %q", parsed.StatusLine, wantCmd)
	}
}
