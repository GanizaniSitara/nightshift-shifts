package console

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func agyScreen(draft, footer string) []string {
	return []string{"old transcript: esc to cancel", "────────", ">" + draft, "────────", footer, "", ""}
}

func TestAGYSendWithFakeConsole(t *testing.T) {
	for _, outcome := range []string{"busy", "completed", "menu", "unsubmitted", "no-enter", "paste-error", "peek-error", "key-error", "unsettled", "already-busy", "already-menu", "existing-draft", "trust", "changed-state"} {
		t.Run(outcome, func(t *testing.T) {
			now := time.Unix(0, 0)
			start := now
			pasted, entered := false, false
			keys, reads := 0, 0
			text := strings.Repeat("£漢😀\r\n", 400)
			c := agyConsole{
				paste: func(pid uint32, got string) error {
					if pid != 42 || got != text {
						t.Fatal("wrong target/text")
					}
					pasted = true
					if outcome == "paste-error" {
						return errors.New("paste failed")
					}
					return nil
				},
				peek: func(pid uint32, above int) (*peekResult, error) {
					if pid != 42 || above != 0 || outcome == "muted" {
						t.Fatal("wrong or muted peek")
					}
					if pasted && outcome == "peek-error" {
						return nil, errors.New("read failed")
					}
					draft, footer := "", "? for shortcuts   Gemini 4 Argon · high"
					if !pasted {
						switch outcome {
						case "already-busy":
							footer = "esc to cancel   Gemini 4 Argon · high"
						case "already-menu":
							footer = "enter Confirm · esc Cancel"
						case "existing-draft":
							draft = " user draft"
						case "trust":
							return &peekResult{Lines: []string{"Do you trust the contents of this project?", "> Yes, I trust this folder", "enter Confirm"}}, nil
						}
					} else if !entered || outcome == "unsubmitted" {
						reads++
						draft = " [Pasted text #1 2000 chars]"
						if reads == 1 || outcome == "unsettled" {
							draft = fmt.Sprintf(" growing %d", reads)
						}
						if outcome == "changed-state" {
							footer = "esc to cancel"
						}
					} else if outcome == "busy" {
						footer = "esc to cancel   Gemini 4 Argon · high"
					} else if outcome == "menu" {
						draft, footer = " Yes", "enter Confirm · esc Cancel"
					}
					return &peekResult{Lines: agyScreen(draft, footer)}, nil
				},
				key: func(pid uint32, tool, name string) error {
					if pid != 42 || tool != "agy" || name != "enter" || entered {
						t.Fatal("wrong or repeated key")
					}
					if outcome != "muted" && now.Sub(start) < 600*time.Millisecond {
						t.Fatal("Enter preceded stable draft")
					}
					entered = true
					keys++
					if outcome == "key-error" {
						return errors.New("key failed")
					}
					return nil
				},
				sleep: func(d time.Duration) { now = now.Add(d) }, now: func() time.Time { return now },
			}
			s := &session{PID: 42, Tool: "agy"}
			submitted, err := c.send(s, text, outcome != "no-enter", false)
			preflight := strings.HasPrefix(outcome, "already-") || outcome == "existing-draft" || outcome == "trust"
			wantErr := preflight || strings.HasSuffix(outcome, "-error") || outcome == "unsettled" || outcome == "changed-state"
			wantSubmitted := outcome == "busy" || outcome == "completed" || outcome == "menu"
			wantKey := !preflight && outcome != "paste-error" && outcome != "peek-error" && outcome != "unsettled" && outcome != "no-enter" && outcome != "changed-state"
			if submitted != wantSubmitted || (err != nil) != wantErr || pasted == preflight || (keys == 1) != wantKey {
				t.Fatalf("submitted=%v err=%v pasted=%v keys=%d", submitted, err, pasted, keys)
			}
		})
	}
}

// TECH-229: --force skips only the empty-composer precheck; busy and menu
// screens are still refused, and a task line no longer hides an idle composer.
func TestAGYSendForceAndTaskLine(t *testing.T) {
	rule := "───────────────────────────────────────────────"
	task := "  ● [16:05:14] Schedule 120s check on NGH-050 and NGH-052 running"
	footer := "? for shortcuts                       Gemini 4 Argon · high · 1 task(s) · /tasks"
	taskScreen := func(draft, footer string) []string { return []string{rule, ">" + draft, rule, task, rule, footer} }
	for _, tc := range []struct {
		name         string
		before       []string
		force, enter bool
		wantPaste    bool
		wantSubmit   bool
		wantErr      bool
	}{
		{"idle with task line", taskScreen("", footer), false, false, true, false, false},
		{"idle with task line submits", taskScreen("", footer), false, true, true, true, false},
		{"existing draft refused", taskScreen(" user draft", footer), false, false, false, false, true},
		{"existing draft forced", taskScreen(" user draft", footer), true, false, true, false, false},
		{"existing draft forced submits", agyScreen(" user draft", "? for shortcuts"), true, true, true, true, false},
		{"no composer forced", []string{rule, task, rule, footer}, true, false, true, false, false},
		{"busy forced", taskScreen("", "esc to cancel   Gemini 4 Argon · high"), true, false, false, false, true},
		{"menu forced", taskScreen("", "enter Confirm · esc Cancel"), true, false, false, false, true},
		{"trust forced", []string{"Do you trust the contents of this project?", "> Yes, I trust this folder", "enter Confirm"}, true, false, false, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Unix(0, 0)
			pasted, entered := false, false
			c := agyConsole{
				paste: func(pid uint32, got string) error {
					if pid != 42 || got != "ping" {
						t.Fatal("wrong target/text")
					}
					pasted = true
					return nil
				},
				peek: func(pid uint32, above int) (*peekResult, error) {
					switch {
					case !pasted:
						return &peekResult{Lines: tc.before}, nil
					case !entered:
						return &peekResult{Lines: taskScreen(" user draft ping", footer)}, nil
					default:
						return &peekResult{Lines: taskScreen("", footer)}, nil
					}
				},
				key: func(pid uint32, tool, name string) error {
					if pid != 42 || tool != "agy" || name != "enter" || entered {
						t.Fatal("wrong or repeated key")
					}
					entered = true
					return nil
				},
				sleep: func(d time.Duration) { now = now.Add(d) }, now: func() time.Time { return now },
			}
			submitted, err := c.send(&session{PID: 42, Tool: "agy"}, "ping", tc.enter, tc.force)
			if pasted != tc.wantPaste || submitted != tc.wantSubmit || (err != nil) != tc.wantErr || entered != (tc.enter && tc.wantPaste) {
				t.Fatalf("pasted=%v submitted=%v entered=%v err=%v", pasted, submitted, entered, err)
			}
		})
	}
}
