package main

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// NGH-066 usage sampling (contract-ngh066.md, Interface block). parseFooter is pure; recordUsage/shiftUsageTotals
// work on a temp shift dir; sampleMember reads the screen through the stubbable paneScreen var. No window, no
// agents-cli.

func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestParseFooterPerTool(t *testing.T) {
	cases := []struct {
		tool, screen, unit string
		value              float64
	}{
		{"copilot", "> working...\n Session: 12 AIC used  |  gemini-3.8-flash\n", "credits", 12},
		{"copilot", "Session: 3.5 AIC used", "credits", 3.5},
		{"claude", "Coder | claude-opus-5-5 | $1.23\n", "usd", 1.23},
		{"claude", "cost $0.07 this session", "usd", 0.07},
		{"agy", "gemini-4-argon-high   37% context left", "context_left", 37},
		{"codex", "  gpt-6.1-sol high · 82% context left  ", "context_left", 82},
	}
	for _, c := range cases {
		s, ok := parseFooter(c.tool, c.screen)
		if !ok {
			t.Errorf("%s: no figure in %q", c.tool, c.screen)
			continue
		}
		if s.Unit != c.unit || !near(s.Value, c.value) {
			t.Errorf("%s %q: got %s %v, want %s %v", c.tool, c.screen, s.Unit, s.Value, c.unit, c.value)
		}
	}
}

func TestParseFooterNoFigure(t *testing.T) {
	cases := []struct{ tool, screen string }{
		{"copilot", "no footer here"},
		{"claude", "nothing that costs anything"},
		{"codex", "context almost full"},
		{"agy", ""},
		{"copilot", "$1.23"},          // a dollar figure is not a Copilot credit count
		{"claude", "Session: 9 AIC used"}, // nor an AIC count a Claude cost
		{"nosuchtool", "Session: 9 AIC used $1.00 50% context left"},
	}
	for _, c := range cases {
		if s, ok := parseFooter(c.tool, c.screen); ok {
			t.Errorf("%s %q: unexpected figure %+v", c.tool, c.screen, s)
		}
	}
}

func TestParseFooterLatestFigureWins(t *testing.T) {
	// a screen still showing an older footer above the current one: the last (current) figure counts
	s, ok := parseFooter("copilot", "Session: 4 AIC used\n...\nSession: 9 AIC used\n")
	if !ok || !near(s.Value, 9) {
		t.Fatalf("want the last figure 9, got %+v %v", s, ok)
	}
	s, ok = parseFooter("claude", "$0.50\nlater\n$0.75")
	if !ok || !near(s.Value, 0.75) {
		t.Fatalf("want $0.75, got %+v %v", s, ok)
	}
}

type usageFile struct {
	Role    string           `json:"role"`
	Samples []map[string]any `json:"samples"`
	Latest  map[string]any   `json:"latest"`
}

func readUsageFile(t *testing.T, dir, role string) usageFile {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, role+"-usage.json"))
	if err != nil {
		t.Fatal(err)
	}
	var u usageFile
	if err := json.Unmarshal(data, &u); err != nil {
		t.Fatalf("usage file is not the contract schema: %v\n%s", err, data)
	}
	return u
}

func TestRecordUsageSchemaAndLatest(t *testing.T) {
	dir := t.TempDir()
	for i, v := range []float64{0.5, 1.25} {
		if err := recordUsage(dir, usageSample{Role: "Coder", Unit: "usd", Source: "screen", Value: v, At: 1000 + float64(i)}); err != nil {
			t.Fatal(err)
		}
	}
	u := readUsageFile(t, dir, "Coder")
	if u.Role != "Coder" || len(u.Samples) != 2 {
		t.Fatalf("schema: %+v", u)
	}
	for _, key := range []string{"at", "unit", "value", "source"} {
		if _, ok := u.Samples[0][key]; !ok {
			t.Fatalf("sample lacks %q: %+v", key, u.Samples[0])
		}
	}
	if u.Latest["unit"] != "usd" || !near(u.Latest["value"].(float64), 1.25) || u.Latest["source"] != "screen" {
		t.Fatalf("latest: %+v", u.Latest)
	}
}

func TestRecordUsageKeepsLast200(t *testing.T) {
	dir := t.TempDir()
	for i := 0; i < 205; i++ {
		if err := recordUsage(dir, usageSample{Role: "Tester", Unit: "credits", Source: "screen", Value: float64(i), At: float64(i)}); err != nil {
			t.Fatal(err)
		}
	}
	u := readUsageFile(t, dir, "Tester")
	if len(u.Samples) != 200 {
		t.Fatalf("keeps %d samples, want 200", len(u.Samples))
	}
	if !near(u.Samples[0]["value"].(float64), 5) || !near(u.Samples[199]["value"].(float64), 204) {
		t.Fatalf("keeps the newest 200: first %v last %v", u.Samples[0]["value"], u.Samples[199]["value"])
	}
}

func TestShiftUsageTotalsSumsLatestPerMemberPerUnit(t *testing.T) {
	dir := t.TempDir()
	rec := func(role, unit string, v float64) {
		if err := recordUsage(dir, usageSample{Role: role, Unit: unit, Source: "screen", Value: v, At: 1}); err != nil {
			t.Fatal(err)
		}
	}
	rec("Coder", "usd", 1.0)
	rec("Coder", "usd", 2.5) // cumulative session figure: the latest replaces, it does not add
	rec("Foreman", "usd", 0.5)
	rec("Tester", "credits", 30)
	rec("Apprentice", "context_left", 40)
	totals := shiftUsageTotals(dir)
	if !near(totals["usd"], 3.0) || !near(totals["credits"], 30) {
		t.Fatalf("totals %v", totals)
	}
	if _, ok := totals["tokens"]; ok && totals["tokens"] != 0 {
		t.Fatalf("no token samples: %v", totals)
	}
}

func TestShiftUsageTotalsEmptyDir(t *testing.T) {
	if totals := shiftUsageTotals(t.TempDir()); len(totals) != 0 && (totals["usd"] != 0 || totals["credits"] != 0) {
		t.Fatalf("no usage files: %v", totals)
	}
}

func TestSampleMemberReadsTheScreenThroughPaneScreen(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "Coder.json"), []byte(`{"pid": 4242, "started": 1.0, "tool": "copilot"}`), 0o666); err != nil {
		t.Fatal(err)
	}
	saved := paneScreen
	defer func() { paneScreen = saved }()
	var asked uint32
	paneScreen = func(pid uint32) string { asked = pid; return "Session: 7 AIC used" }
	s, ok := sampleMember(dir, object{{"label", "Coder"}, {"role", "Coder"}, {"tool", "copilot"}})
	if !ok {
		t.Fatal("no sample from the stubbed screen")
	}
	if asked != 4242 {
		t.Fatalf("screen read for pid %d, want the member's pid 4242", asked)
	}
	if s.Role != "Coder" || s.Unit != "credits" || !near(s.Value, 7) || s.At <= 0 {
		t.Fatalf("sample %+v", s)
	}
}

func TestSampleMemberNoFigureIsNoSample(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "Coder.json"), []byte(`{"pid": 4242, "started": 1.0, "tool": "claude"}`), 0o666); err != nil {
		t.Fatal(err)
	}
	saved := paneScreen
	defer func() { paneScreen = saved }()
	paneScreen = func(uint32) string { return "just a prompt" }
	if s, ok := sampleMember(dir, object{{"label", "Coder"}, {"role", "Coder"}, {"tool", "claude"}}); ok {
		t.Fatalf("unexpected sample %+v", s)
	}
}

// NGH-067 Codex footers (Coder2, from the 0.163 binary): estimated-thread-cost "~$1.82" -> usd, thread-credits
// "5.2 credits" -> credits, "Context 83% left" (or the old "83% context left") -> context_left. Order usd, credits,
// context_left; usd needs "~$"; usd and credits count only in the last 3 non-empty lines.
func TestParseFooterCodexCostCreditsContext(t *testing.T) {
	cases := []struct {
		name, screen, unit string
		value              float64
	}{
		{"all three: usd wins", "› working\n gpt-6.1-sol high · Context 83% left · ~$1.82 · 5.2 credits\n", "usd", 1.82},
		{"credits without cost", "gpt-6.1-sol high · Context 83% left · 5.2 credits", "credits", 5.2},
		{"context only (new)", "gpt-6.1-sol high · Context 83% left", "context_left", 83},
		{"context only (old)", "gpt-6.1-sol high · 83% context left", "context_left", 83},
		{"thousands separator", "Context 10% left · ~$1,234.50", "usd", 1234.50},
		{"bare dollar is not a cost", "the fix saves $3 a month\nContext 50% left", "context_left", 50},
		{"cost in conversation above the tail", "it would cost ~$5 to rerun\nline a\nline b\nline c\nline d\ngpt-6.1-sol · Context 40% left",
			"context_left", 40},
		{"credits in conversation above the tail", "we spent 9 credits yesterday\nx\ny\nz\nw\nContext 70% left", "context_left", 70},
	}
	for _, c := range cases {
		s, ok := parseFooter("codex", c.screen)
		if !ok || s.Unit != c.unit || !near(s.Value, c.value) {
			t.Errorf("%s: got %+v ok=%v, want %s %v", c.name, s, ok, c.unit, c.value)
		}
	}
}

func TestParseFooterCodexNothing(t *testing.T) {
	// (a "N credits" phrase inside the last 3 non-empty lines does count, by design; only "~$" marks a cost)
	for _, screen := range []string{"", "gpt-6.1-sol high", "costs about $3, honestly\nmore text\n\n"} {
		if s, ok := parseFooter("codex", screen); ok && s.Unit != "context_left" {
			t.Errorf("%q: unexpected %+v", screen, s)
		}
	}
}
