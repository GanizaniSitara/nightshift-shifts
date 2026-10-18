package console

import (
	"errors"
	"testing"
	"time"
)

func TestEnterDelay(t *testing.T) {
	cases := []struct {
		length int
		want   time.Duration
	}{
		{0, 150 * time.Millisecond},
		{-10, 150 * time.Millisecond},
		{50, 500 * time.Millisecond},
		{100, 500 * time.Millisecond},
		{200, 600 * time.Millisecond},
		{1500, 1200 * time.Millisecond},
		{2000, 1500 * time.Millisecond},
		{3000, 2000 * time.Millisecond},
	}
	for _, c := range cases {
		if got := enterDelay(c.length); got != c.want {
			t.Errorf("enterDelay(%d) = %v, want %v", c.length, got, c.want)
		}
	}
}

func TestIsSubmitted(t *testing.T) {
	// Codex: unsubmitted paste box
	codexPasted := []string{
		"› [Pasted Content 1500 chars]",
		"  Ctrl+x Ctrl+s to submit",
	}
	if isSubmitted("codex", "func longCode() {}", codexPasted) {
		t.Error("unsubmitted paste box must not report submitted")
	}

	// Codex: working turn
	codexWorking := []string{
		"Working (4m 43s • esc to interrupt)",
		"",
		"› Ask Codex to do anything",
	}
	if !isSubmitted("codex", "func longCode() {}", codexWorking) {
		t.Error("working turn must report submitted")
	}

	// Codex: composer cleared after submission
	codexCleared := []string{
		"Previous output line 1",
		"Previous output line 2",
		"› Ask Codex to do anything",
	}
	if !isSubmitted("codex", "hello world", codexCleared) {
		t.Error("cleared composer must report submitted")
	}

	// Codex: unsubmitted prompt text matching placeholder prefix
	codexUnsubmittedText := []string{
		"Previous output line 1",
		"› Ask Codex to refactor this function immediately",
	}
	if isSubmitted("codex", "Ask Codex to refactor this function immediately", codexUnsubmittedText) {
		t.Error("unsubmitted text in composer must not report submitted")
	}

	// Claude: busy state
	claudeBusy := []string{
		"✢ Misting… (10s · ↓ 550 tokens)",
		"",
		"> We should probably run it every day to be honest.",
		"❯",
	}
	if !isSubmitted("claude", "run it every day", claudeBusy) {
		t.Error("claude busy must report submitted")
	}

	// Claude: unsubmitted composer
	claudeUnsubmitted := []string{
		"> run it every day",
		"  ctrl+x ctrl+s to send now",
	}
	if isSubmitted("claude", "run it every day", claudeUnsubmitted) {
		t.Error("claude unsubmitted must not report submitted")
	}

	// Vibe: busy state
	vibeBusy := []string{
		"⠉⠁ Generating… (0s Esc/Ctrl+C to interrupt)",
	}
	if !isSubmitted("vibe", "echo hi", vibeBusy) {
		t.Error("vibe generating must report submitted")
	}

	// Approval prompt dialog
	promptScreen := []string{
		"Do you trust the contents of this project?",
		"> Yes, I trust this folder",
		"  No, exit",
	}
	if !isSubmitted("agy", "trust project", promptScreen) {
		t.Error("approval prompt must report submitted")
	}

	// cmd.exe: unsubmitted
	cmdUnsubmitted := []string{
		`C:\git\agents-cli> echo AGENTS_TRANSPORT_OK`,
	}
	if isSubmitted("other", "echo AGENTS_TRANSPORT_OK", cmdUnsubmitted) {
		t.Error("cmd unsubmitted line must not report submitted")
	}

	// cmd.exe: submitted with cleared prompt
	cmdSubmitted := []string{
		"AGENTS_TRANSPORT_OK",
		`C:\git\agents-cli>`,
	}
	if !isSubmitted("other", "echo AGENTS_TRANSPORT_OK", cmdSubmitted) {
		t.Error("cmd cleared prompt must report submitted")
	}
}

func TestSubmissionVerifier(t *testing.T) {
	// Case 1: First try succeeds immediately
	keyCalls := 0
	peekCalls := 0
	now := time.Now()
	v1 := submissionVerifier{
		peek: func(pid uint32, above int) (*peekResult, error) {
			peekCalls++
			return &peekResult{Lines: []string{"Working (1s • esc to interrupt)"}}, nil
		},
		key: func(pid uint32, tool, key string) error {
			keyCalls++
			return nil
		},
		alive: func(pid uint32) bool { return true },
		sleep: func(d time.Duration) { now = now.Add(d) },
		now:   func() time.Time { return now },
	}
	s := &session{PID: 100, Tool: "codex"}
	if !v1.verify(s, "some text") {
		t.Error("verify should return true on first try")
	}
	if keyCalls != 0 {
		t.Errorf("keyCalls = %d, want 0", keyCalls)
	}

	// Case 2: Swallowed on first try, retry succeeds
	keyCalls = 0
	peekCalls = 0
	now = time.Now()
	v2 := submissionVerifier{
		peek: func(pid uint32, above int) (*peekResult, error) {
			peekCalls++
			if keyCalls == 0 {
				return &peekResult{Lines: []string{"› [Pasted Content 1500 chars]"}}, nil
			}
			return &peekResult{Lines: []string{"Working (0s • esc to interrupt)"}}, nil
		},
		key: func(pid uint32, tool, key string) error {
			keyCalls++
			return nil
		},
		alive: func(pid uint32) bool { return true },
		sleep: func(d time.Duration) { now = now.Add(d) },
		now:   func() time.Time { return now },
	}
	if !v2.verify(s, "some text") {
		t.Error("verify should return true after retry")
	}
	if keyCalls != 1 {
		t.Errorf("keyCalls = %d, want 1", keyCalls)
	}

	// Case 3: Complete failure even after retry
	keyCalls = 0
	peekCalls = 0
	now = time.Now()
	v3 := submissionVerifier{
		peek: func(pid uint32, above int) (*peekResult, error) {
			peekCalls++
			return &peekResult{Lines: []string{"› [Pasted Content 1500 chars]"}}, nil
		},
		key: func(pid uint32, tool, key string) error {
			keyCalls++
			return nil
		},
		alive: func(pid uint32) bool { return true },
		sleep: func(d time.Duration) { now = now.Add(d) },
		now:   func() time.Time { return now },
	}
	if v3.verify(s, "some text") {
		t.Error("verify should return false on persistent failure")
	}
	if keyCalls != 1 {
		t.Errorf("keyCalls = %d, want 1", keyCalls)
	}

	// Case 5: Target exits during command (e.g. exit)
	peekCalls = 0
	vExit := submissionVerifier{
		peek: func(pid uint32, above int) (*peekResult, error) {
			return nil, errors.New("attach failed: process exited")
		},
		key: func(pid uint32, tool, key string) error {
			return nil
		},
		alive: func(pid uint32) bool { return false },
		sleep: func(d time.Duration) { now = now.Add(d) },
		now:   func() time.Time { return now },
	}
	if !vExit.verify(s, "exit") {
		t.Error("exited process should report submitted")
	}
}
