package main

import (
	"database/sql"
	"path/filepath"
	"testing"
)

func TestCompactionThresholdDefaultsAndDisable(t *testing.T) {
	for _, tc := range []struct {
		name   string
		member object
		want   float64
	}{
		{"Foreman default", object{{"label", "Foreman"}, {"role", "Foreman"}}, 50},
		{"Coder default", object{{"label", "Coder"}, {"role", "Coder"}}, 70},
		{"Tester default", object{{"label", "Tester-2"}, {"role", "Tester"}}, 70},
		{"explicit disable", object{{"label", "Foreman"}, {"role", "Foreman"}, {"compact_at", 0}}, 0},
		{"override", object{{"label", "Coder"}, {"role", "Coder"}, {"compact_at", 42}}, 42},
		{"100 percent", object{{"label", "Coder"}, {"compact_at", 100}}, 100},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := compactThreshold(tc.member); got != tc.want {
				t.Fatalf("threshold=%v want %v", got, tc.want)
			}
		})
	}
}

func TestCompactionCommandsSkipUnverifiedTools(t *testing.T) {
	for tool, want := range map[string]string{"claude": "/compact", "codex": "/compact", "copilot": "/compact", "gemini": "/compress", "agy": "", "vibe": "", "unknown": ""} {
		if got := compactionCommand(tool); got != want {
			t.Errorf("%s command=%q want %q", tool, got, want)
		}
	}
}

func TestCompactionContextIndependentOfCost(t *testing.T) {
	for _, tc := range []struct {
		name, screen string
		want         float64
		ok           bool
	}{
		{"cost does not mask context", "› Ask Codex to do anything\n~$12.34 · Context 30% left", 70, true},
		{"credits do not mask context", "› Ask Codex to do anything\n5.2 credits · 20% context left", 80, true},
		{"decimal context", "› Ask Codex to do anything\n83.5% context left", 16.5, true},
		{"empty context", "› Ask Codex to do anything\nContext 100% left", 0, true},
		{"full context", "› Ask Codex to do anything\n0% context left", 100, true},
		{"cost only", "› Ask Codex to do anything\n~$50.00", 0, false},
		{"credits only", "› Ask Codex to do anything\n50 credits", 0, false},
		{"invalid percentage", "› Ask Codex to do anything\n130% context left", 0, false},
		{"negative percentage", "› Ask Codex to do anything\n-5% context left", 0, false},
		{"old transcript is not footer", "20% context left\nold answer\nmore answer\n› Ask Codex to do anything\n~$1.23", 0, false},
		{"no signal", "", 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := parseContextUsed("codex", tc.screen)
			if ok != tc.ok || (ok && got != tc.want) {
				t.Fatalf("context=%v,%v want %v,%v", got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestCompactionContextFreshnessAndStatusFencing(t *testing.T) {
	info := object{{"pid", 42}, {"started", 100.5}}
	for _, tc := range []struct {
		name  string
		saved object
		fresh bool
	}{
		{"current", contextRecord(info, 80, 1000), true},
		{"90 seconds", contextRecord(info, 80, 910), true},
		{"stale", contextRecord(info, 80, 909), false},
		{"future", contextRecord(info, 80, 1001), false},
		{"wrong pid", contextRecord(object{{"pid", 43}, {"started", 100.5}}, 80, 1000), false},
		{"wrong start", contextRecord(object{{"pid", 42}, {"started", 99}}, 80, 1000), false},
		{"invalid value", contextRecord(info, 101, 1000), false},
		{"missing", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, fresh := freshContext(tc.saved, info, 1000)
			if fresh != tc.fresh {
				t.Fatal(fresh, tc.saved)
			}
		})
	}
	base := t.TempDir()
	if err := writeJSONFile(compactionPath(base, "Coder"), object{{"pid", 43}, {"started", 100.5}, {"last_compacted", 999}}); err != nil {
		t.Fatal(err)
	}
	if status := compactionStatus(base, "Coder", info, object{}); rowValue(status, "last_compacted") != nil {
		t.Fatal("old incarnation leaked", status)
	}
}

func TestCompactionCallbackRequiresMatchingSession(t *testing.T) {
	for _, tc := range []struct {
		name, id string
		used     any
		want     bool
	}{
		{"matching", "current-session", 72.5, true},
		{"zero is valid", "current-session", 0, true},
		{"wrong session", "previous-session", 72.5, false},
		{"missing session", "", 72.5, false},
		{"negative", "current-session", -1, false},
		{"too large", "current-session", 101, false},
		{"missing percentage", "current-session", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			info := object{{"pid", 42}, {"started", 100.5}, {"session_id", "current-session"}}
			if err := writeJSONFile(filepath.Join(dir, "Coder.json"), info); err != nil {
				t.Fatal(err)
			}
			recordClaudeContext(dir, "Coder", object{{"session_id", tc.id}, {"context_window", object{{"used_percentage", tc.used}}}})
			saved, ok := readObject(contextPath(dir, "Coder"))
			if ok != tc.want {
				t.Fatal(ok, saved)
			}
			want, _ := numericValue(tc.used)
			if ok && (!samePane(saved, info) || !numberIs(rowValue(saved, "context_used"), numberFloat(want))) {
				t.Fatal(saved)
			}
		})
	}
}

func TestSituationalCompactThreshold(t *testing.T) {
	dir := t.TempDir()
	foreman := object{{"label", "Foreman"}, {"role", "Foreman"}, {"human_contact", true}}
	coder := object{{"label", "Coder"}, {"role", "Coder"}}
	info := object{{"pid", 42}, {"started", 100.0}}
	_ = writeJSONFile(filepath.Join(dir, "Foreman.json"), info)
	_ = writeJSONFile(filepath.Join(dir, "Coder.json"), info)

	// Initial state: no dispatches yet -> Foreman 50%, Coder 70%
	if got, reason := situationalCompactThreshold(dir, "Foreman", foreman, info); got != 50 || reason != "foreman-default" {
		t.Fatalf("initial Foreman: %v %q", got, reason)
	}
	if got, reason := situationalCompactThreshold(dir, "Coder", coder, info); got != 70 || reason != "worker-default" {
		t.Fatalf("initial Coder: %v %q", got, reason)
	}

	// Open dispatch in flight to Coder -> Foreman defers to 80% ("awaiting-dispatch")
	if err := withWriteMailbox(dir, func(db *sql.DB) error {
		_, err := insertMessage(db, dir, "assignment", "Foreman", "Coder", "implement feature", "asg-1", nil)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if got, reason := situationalCompactThreshold(dir, "Foreman", foreman, info); got != 80 || reason != "awaiting-dispatch" {
		t.Fatalf("in-flight Foreman: %v %q", got, reason)
	}

	// Coder completes the assignment -> Foreman is at phase boundary (40%), Coder is between assignments (35%)
	if err := withWriteMailbox(dir, func(db *sql.DB) error {
		_, err := insertMessage(db, dir, "result", "Coder", "Foreman", "DONE: implemented", "asg-1", nil)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if got, reason := situationalCompactThreshold(dir, "Foreman", foreman, info); got != 40 || reason != "phase-boundary" {
		t.Fatalf("phase-boundary Foreman: %v %q", got, reason)
	}
	if got, reason := situationalCompactThreshold(dir, "Coder", coder, info); got != 35 || reason != "between-assignments" {
		t.Fatalf("between-assignments Coder: %v %q", got, reason)
	}
}
