package console

import (
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
	"unsafe"
)

func copilotScreen(draft, footer string) []string {
	return []string{"● Previous answer: Thinking. Allow access?", "────────────", "❯" + draft, "────────────", footer, "", ""}
}

func TestCopilotScreens(t *testing.T) {
	for _, tc := range []struct {
		name, draft, footer, want string
	}{
		{"idle", "", "/ commands · ? help · tab next tab", "idle"},
		{"working", "", "◎ Working esc interrupt    Auto → GPT-5.4 mini", "busy"},
		{"queued draft", " pending text", "◉ Working esc interrupt · ctrl+enter enqueue", "busy"},
		{"draft", " Thinking: Allow access?", "@ files · # issues", "prompt"},
		{"picker", " Search models…", "↑/↓ to navigate · enter to select · esc to cancel", "prompt"},
		{"permission", " Yes", "↑/↓ navigate · Enter confirm · Esc cancel", "prompt"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			lines := copilotScreen(tc.draft, tc.footer)
			if got := guessState("copilot", lines); got != tc.want {
				t.Fatalf("state=%s want %s", got, tc.want)
			}
		})
	}
	if copilotState([]string{"❯ old transcript prompt"}) != "unknown" {
		t.Fatal("transcript mistaken for composer")
	}
}

func TestCopilotPasteAndKeyRecords(t *testing.T) {
	text := "first\r\nsecond\r£漢😀\n" + strings.Repeat("long text ", 180)
	records := pasteRecords(text)
	var units []uint16
	for i := 0; i < len(records); i += 2 {
		down, up := records[i], records[i+1]
		if down.EventType != keyEvent || down.KeyDown != 1 || down.RepeatCount != 1 || down.VK != 0 || down.Ctrl != 0 {
			t.Fatalf("bad paste record: %+v", down)
		}
		down.KeyDown = 0
		if down != up {
			t.Fatal("unpaired key event")
		}
		units = append(units, down.Char)
	}
	want := "\x1b[200~first\nsecond\n£漢😀\n" + strings.Repeat("long text ", 180) + "\x1b[201~"
	if got := string(utf16.Decode(units)); got != want {
		t.Fatalf("paste changed: %q", got)
	}
	if unsafe.Sizeof(inputRecord{}) != 20 || unsafe.Offsetof(inputRecord{}.Char) != 14 {
		t.Fatal("Windows INPUT_RECORD layout changed")
	}
	for name, want := range map[string]struct{ vk, ch uint16 }{
		"enter": {0x0d, '\r'}, "escape": {0x1b, 0x1b}, "up": {0x26, 0}, "down": {0x28, 0},
	} {
		k := namedKeys[name]
		r := keyPair(k.vk, k.ch, k.ctrl)
		if r[0].VK != want.vk || r[0].Char != want.ch || r[0].Ctrl != 0 || r[0].KeyDown != 1 || r[1].KeyDown != 0 {
			t.Fatalf("%s: %+v", name, r)
		}
	}
}

func TestCopilotSendWithFakeConsole(t *testing.T) {
	for _, outcome := range []string{"busy", "idle", "menu", "unsubmitted", "no-enter", "paste-error", "peek-error", "key-error", "unsettled", "already-busy", "already-menu"} {
		t.Run(outcome, func(t *testing.T) {
			text := "long\n" + strings.Repeat("£漢😀 ", 400)
			now := time.Unix(0, 0)
			start := now
			var operations []string
			pasted, entered := false, false
			reads := 0
			c := copilotConsole{
				paste: func(pid uint32, got string) error {
					if pid != 42 || got != text {
						t.Fatal("wrong paste target/text")
					}
					operations = append(operations, "paste")
					pasted = true
					if outcome == "paste-error" {
						return errors.New("paste failed")
					}
					return nil
				},
				peek: func(pid uint32, above int) (*peekResult, error) {
					if outcome == "muted" {
						t.Fatal("muted console polled")
					}
					if pid != 42 || above != 0 {
						t.Fatal("wrong peek target")
					}
					if outcome == "peek-error" && pasted {
						return nil, errors.New("read failed")
					}
					draft, footer := "", "/ commands · ? help"
					if !pasted {
						if outcome == "already-busy" {
							footer = "◎ Working esc interrupt"
						}
						if outcome == "already-menu" {
							footer = "enter to select · esc to cancel"
						}
					} else if !entered || outcome == "unsubmitted" {
						reads++
						draft = " [Pasted text]"
						if reads == 1 || outcome == "unsettled" {
							draft += strings.Repeat("x", reads)
						}
					} else if outcome == "busy" {
						footer = "◎ Working esc interrupt"
					} else if outcome == "menu" {
						draft, footer = " Search models…", "↑/↓ to navigate · enter to select · esc to cancel"
					}
					return &peekResult{Lines: copilotScreen(draft, footer)}, nil
				},
				key: func(pid uint32, tool, name string) error {
					if pid != 42 || tool != "copilot" || name != "enter" {
						t.Fatal("wrong key")
					}
					if now.Sub(start) < 1600*time.Millisecond {
						t.Fatal("long paste submitted too early")
					}
					if entered {
						t.Fatal("blind Enter retry")
					}
					entered = true
					operations = append(operations, "enter")
					if outcome == "key-error" {
						return errors.New("key failed")
					}
					return nil
				},
				sleep: func(d time.Duration) { now = now.Add(d) }, now: func() time.Time { return now },
			}
			s := &session{PID: 42, Tool: "copilot"}
			submitted, err := c.send(s, text, outcome != "no-enter")
			wantSubmitted := outcome == "busy" || outcome == "idle" || outcome == "menu"
			wantErr := strings.HasSuffix(outcome, "-error") || outcome == "unsettled" || strings.HasPrefix(outcome, "already-")
			if submitted != wantSubmitted || (err != nil) != wantErr {
				t.Fatalf("submitted=%v err=%v", submitted, err)
			}
			wantOps := []string{"paste", "enter"}
			if strings.HasPrefix(outcome, "already-") {
				wantOps = nil
			} else if outcome == "no-enter" || outcome == "paste-error" || outcome == "peek-error" || outcome == "unsettled" {
				wantOps = []string{"paste"}
			}
			if !reflect.DeepEqual(operations, wantOps) {
				t.Fatalf("operations = %v", operations)
			}
		})
	}
}

// TECH-229: agy frames a background-task row with rules below its composer.
func TestCopilotComposerSkipsFramedTaskRow(t *testing.T) {
	rule := "───────────────────────────────────────────────"
	task := "  ● [16:05:14] Schedule 120s check on NGH-050 and NGH-052 running"
	footer := "? for shortcuts                       Gemini 4 Argon · high · 1 task(s) · /tasks"
	for _, tc := range []struct {
		name  string
		lines []string
		draft string
		ok    bool
	}{
		{"idle with task", []string{rule, ">", rule, task, rule, footer}, "", true},
		{"draft with task", []string{rule, "> ping the crew", rule, task, rule, footer}, "ping the crew", true},
		{"wrapped draft with task", []string{rule, "> first", "  second", rule, task, rule, footer, "", ""}, "first\nsecond", true},
		{"two task rows", []string{"old transcript", rule, "❯", rule, task, rule, task, rule, footer}, "", true},
		{"empty adjacent pair", []string{rule, "> x", rule, rule, footer}, "x", true},
		{"only task framed", []string{"> old transcript prompt", rule, task, rule, footer}, "", false},
		{"only empty pair", []string{"> old transcript prompt", rule, rule, footer}, "", false},
		{"no rules", []string{"> old transcript prompt", footer}, "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			draft, ok := copilotComposer(tc.lines)
			if draft != tc.draft || ok != tc.ok {
				t.Fatalf("copilotComposer = %q, %v; want %q, %v", draft, ok, tc.draft, tc.ok)
			}
		})
	}
}
