package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// NGH-067 Codex pane identity: the Codex command line built through toolCommand (the dispatcher every launch path
// and the spawn dry-run use). Pure: nothing is launched.

func codexArgv(t *testing.T, reasoning string) []string {
	t.Helper()
	name := "Coder (Nightshift-shift-x-coder)"
	return toolCommand("codex", "gpt-6.1-sol", "<brief>", "<opening message>", "default", reasoning, "", &name, 0)
}

// configValue returns the value of `-c key=value` in argv.
func configValue(argv []string, key string) (string, bool) {
	for i := 0; i+1 < len(argv); i++ {
		if argv[i] == "-c" && strings.HasPrefix(argv[i+1], key+"=") {
			return strings.TrimPrefix(argv[i+1], key+"="), true
		}
	}
	return "", false
}

func statusLineItems(t *testing.T, argv []string) []string {
	t.Helper()
	raw, ok := configValue(argv, "tui.status_line")
	if !ok {
		t.Fatalf("no -c tui.status_line in %q", argv)
	}
	var items []string
	if err := json.Unmarshal([]byte(raw), &items); err != nil {
		t.Fatalf("tui.status_line is not a TOML/JSON string list: %q (%v)", raw, err)
	}
	return items
}

func TestCodexStatusLineCarriesCostAndCredits(t *testing.T) {
	items := statusLineItems(t, codexArgv(t, "high"))
	has := map[string]bool{}
	for _, it := range items {
		has[it] = true
	}
	for _, want := range []string{"estimated-thread-cost", "thread-credits", "model-with-reasoning", "context-remaining"} {
		if !has[want] {
			t.Errorf("tui.status_line lacks %q: %v", want, items)
		}
	}
}

func TestCodexStatusLineItemsAreValid(t *testing.T) {
	// The 0.163 binary's valid items (NGH-067 ticket); anything else makes Codex reject the config.
	valid := map[string]bool{}
	for _, it := range strings.Split("activity project-name app-name current-dir run-state thread-title git-branch "+
		"context-remaining context-used five-hour-limit weekly-limit thread-credits estimated-thread-cost "+
		"codex-version used-tokens total-input-tokens total-output-tokens thread-id fast-mode model-with-reasoning "+
		"task-progress", " ") {
		valid[it] = true
	}
	for _, it := range statusLineItems(t, codexArgv(t, "")) {
		if !valid[it] {
			t.Errorf("invalid tui.status_line item %q", it)
		}
	}
}

func TestCodexReasoningEffortOnlyWhenGiven(t *testing.T) {
	if v, ok := configValue(codexArgv(t, "high"), "model_reasoning_effort"); !ok || v != "high" {
		t.Fatalf("reasoning high: got %q %v", v, ok)
	}
	if _, ok := configValue(codexArgv(t, ""), "model_reasoning_effort"); ok {
		t.Fatal("no reasoning given: no model_reasoning_effort")
	}
}

func TestCodexBriefAndOpeningAreTheLastArgument(t *testing.T) {
	argv := codexArgv(t, "high")
	last := argv[len(argv)-1]
	if !strings.HasPrefix(last, "<brief>") || !strings.HasSuffix(last, "<opening message>") {
		t.Fatalf("the prompt must be the final argument: %q", last)
	}
	if i := indexOf(argv, "--model"); i < 0 || argv[i+1] != "gpt-6.1-sol" {
		t.Fatalf("--model gpt-6.1-sol missing: %q", argv)
	}
}

func indexOf(xs []string, x string) int {
	for i, v := range xs {
		if v == x {
			return i
		}
	}
	return -1
}

// c14 decision (Foreman msg 186): thread-title is dropped, the status line is exactly these four items in order, and
// the terminal title stays off; the pane keeps --no-daemon.
func TestCodexStatusLineExactAndNoThreadTitle(t *testing.T) {
	argv := codexArgv(t, "high")
	items := statusLineItems(t, argv)
	want := []string{"model-with-reasoning", "context-remaining", "estimated-thread-cost", "thread-credits"}
	if strings.Join(items, ",") != strings.Join(want, ",") {
		t.Fatalf("tui.status_line %v, want %v", items, want)
	}
	if v, ok := configValue(argv, "tui.terminal_title"); !ok || v != "[]" {
		t.Fatalf("tui.terminal_title=[] missing: %q", argv)
	}
	if indexOf(argv, "--no-daemon") < 0 {
		t.Fatalf("--no-daemon missing: %q", argv)
	}
}
