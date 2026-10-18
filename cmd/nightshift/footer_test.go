package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestShortShiftNamesAndTitles(t *testing.T) {
	for _, tc := range []struct{ input, want string }{
		{"Nightshift-shift-1234abcd", "shift-1234abcd"},
		{"shift-1234abcd", "shift-1234abcd"},
		{"Nightshift-other", "Nightshift-other"},
		{"fixture", "fixture"},
		{"", ""},
	} {
		if got := shortShiftName(tc.input); got != tc.want {
			t.Errorf("shortShiftName(%q)=%q want %q", tc.input, got, tc.want)
		}
	}
	if got := shiftTitle("Nightshift-shift-1234abcd", "NGH-070"); got != "shift | NGH-070" {
		t.Fatal(got)
	}
	if got := shiftTitle("shift-1234abcd", "LAP-044"); got != "shift | LAP-044" {
		t.Fatal(got)
	}
	if got := shiftTitle("shift-1234abcd", ""); got != "shift-1234abcd" {
		t.Fatal(got)
	}
	for _, tc := range []struct{ prompt, want string }{
		{"Ticket NGH-070: Shorten footer", "NGH-070"},
		{"LAP-044: Fix face crop clustering\nBody text", "LAP-044"},
		{"LAP-044 — Fix face crop clustering", "LAP-044"},
		{"Read ticket with tasks-cli get LAP-044 and implement", "LAP-044"},
		{"Plain task with no ticket", ""},
	} {
		if got := promptTicket(tc.prompt); got != tc.want {
			t.Errorf("promptTicket(%q)=%q want %q", tc.prompt, got, tc.want)
		}
	}
}

func TestClaudeFooterPipesAndOptionalValues(t *testing.T) {
	for _, tc := range []struct{ name, payload, want string }{
		{"all values", `{"model":{"display_name":"Opus 5.5","id":"fallback"},"context_window":{"remaining_percentage":41},"cost":{"total_cost_usd":12.3}}`, "shift-1234abcd | Opus 5.5 | ctx 41% left | $12.30"},
		{"zero remaining and cost", `{"model":{"display_name":"Opus 5.5"},"context_window":{"remaining_percentage":0},"cost":{"total_cost_usd":0}}`, "shift-1234abcd | Opus 5.5 | ctx 0% left | $0.00"},
		{"derive from used", `{"model":{"display_name":"Opus 5.5"},"context_window":{"used_percentage":58.5}}`, "shift-1234abcd | Opus 5.5 | ctx 41.5% left"},
		{"remaining preferred", `{"model":{"display_name":"Opus 5.5"},"context_window":{"remaining_percentage":41,"used_percentage":70}}`, "shift-1234abcd | Opus 5.5 | ctx 41% left"},
		{"model id fallback", `{"model":{"id":"claude-opus"},"context_window":{"remaining_percentage":100}}`, "shift-1234abcd | claude-opus | ctx 100% left"},
		{"no model", `{"context_window":{"remaining_percentage":41}}`, "shift-1234abcd | ctx 41% left"},
		{"missing all", `{}`, "shift-1234abcd"},
		{"unavailable context", `{"model":{"display_name":"Opus 5.5"}}`, "shift-1234abcd | Opus 5.5"},
		{"invalid context", `{"model":{"display_name":"Opus 5.5"},"context_window":{"remaining_percentage":101}}`, "shift-1234abcd | Opus 5.5"},
		{"negative cost omitted", `{"model":{"display_name":"Opus 5.5"},"context_window":{"remaining_percentage":41},"cost":{"total_cost_usd":-1}}`, "shift-1234abcd | Opus 5.5 | ctx 41% left"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "Nightshift-shift-1234abcd")
			if err := os.MkdirAll(dir, 0700); err != nil {
				t.Fatal(err)
			}
			got := claudeStatusline(dir, "Foreman", []byte(tc.payload))
			if got != tc.want {
				t.Fatalf("footer=%q want %q", got, tc.want)
			}
			if sample, ok := parseFooter("claude", got); strings.Contains(tc.want, "$") {
				if !ok || sample.Unit != "usd" {
					t.Fatal("footer cost not readable by warden", sample, ok)
				}
			} else if ok {
				t.Fatal("absent cost invented a budget figure", sample)
			}
			if strings.Contains(got, "Foreman") || strings.Contains(got, filepath.Dir(dir)) || strings.Contains(got, "tokens") {
				t.Fatal("role/cwd/token fallback leaked", got)
			}
		})
	}
}

func TestClaudePaneSettingsDoNotMutateGlobalSettings(t *testing.T) {
	home := t.TempDir()
	t.Setenv("USERPROFILE", home)
	t.Setenv("NIGHTSHIFT_HOME", home)
	global := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(global), 0700); err != nil {
		t.Fatal(err)
	}
	const sentinel = `{"statusLine":{"command":"personal-footer"},"theme":"dark"}`
	if err := os.WriteFile(global, []byte(sentinel), 0600); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	var files []string
	for _, role := range []string{"Foreman", "Coder"} {
		path, err := claudeSettingsFile(dir, role, "Nightshift-shift-1234abcd-"+strings.ToLower(role), "red", dir, role == "Foreman")
		if err != nil {
			t.Fatal(err)
		}
		if filepath.Dir(path) != dir {
			t.Fatal("settings outside pane state", path)
		}
		settings, ok := readObject(path)
		if !ok {
			t.Fatal(path)
		}
		status, _ := rowValue(settings, "statusLine").(object)
		command := objectString(status, "command")
		if !strings.Contains(command, "statusline") || !strings.Contains(command, "--state-dir") || !strings.Contains(command, "--role "+role) {
			t.Fatal(command)
		}
		files = append(files, path)
	}
	if files[0] == files[1] {
		t.Fatal("panes share settings")
	}
	after, err := os.ReadFile(global)
	if err != nil || string(after) != sentinel {
		t.Fatal("global settings changed", err, string(after))
	}
}
