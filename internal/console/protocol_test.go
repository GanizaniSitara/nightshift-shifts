package console

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode/utf16"

	"golang.org/x/sys/windows"
)

func TestMain(m *testing.M) {
	if os.Getenv("NIGHTSHIFT_UNIT_HELPER") == "1" && len(os.Args) > 1 && os.Args[1] == "_attach" {
		input, _ := io.ReadAll(os.Stdin)
		if p := os.Getenv("NIGHTSHIFT_UNIT_RECORD"); p != "" {
			f, _ := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
			_ = json.NewEncoder(f).Encode(struct {
				Args  []string
				Input string
			}{os.Args[1:], string(input)})
			_ = f.Close()
		}
		_, _ = io.WriteString(os.Stdout, os.Getenv("NIGHTSHIFT_UNIT_RESPONSE"))
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestHelperTransportAndClaudeForce(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("NIGHTSHIFT_CONSOLE_HELPER", exe)
	t.Setenv("NIGHTSHIFT_UNIT_HELPER", "1")
	record := filepath.Join(t.TempDir(), "calls.jsonl")
	t.Setenv("NIGHTSHIFT_UNIT_RECORD", record)
	t.Setenv("NIGHTSHIFT_UNIT_RESPONSE", `{"lines":["❯"],"width":120,"title":"fixture"}`)
	sent, err := Send(42, "claude", "hello £漢😀", true)
	if err != nil || !sent {
		t.Fatal(sent, err)
	}
	if err := Key(42, "vibe", "escape"); err != nil {
		t.Fatal(err)
	}
	if err := Key(42, "codex", "escape"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(record)
	var calls []struct {
		Args  []string
		Input string
	}
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var c struct {
			Args  []string
			Input string
		}
		if err := json.Unmarshal([]byte(line), &c); err != nil {
			t.Fatal(err)
		}
		calls = append(calls, c)
	}
	if len(calls) != 4 || !reflect.DeepEqual(calls[0].Args, []string{"_attach", "inject", "42", "1"}) || calls[0].Input != "hello £漢😀" {
		t.Fatal(calls)
	}
	if !reflect.DeepEqual(calls[2].Args, []string{"_attach", "inject", "42", "0"}) || calls[2].Input != "\x1b[27u" {
		t.Fatal(calls[2])
	}
	if !reflect.DeepEqual(calls[3].Args, []string{"_attach", "key", "42", "escape"}) {
		t.Fatal(calls[3])
	}
	t.Setenv("NIGHTSHIFT_UNIT_RESPONSE", `{"error":"fixture denied"}`)
	if _, err := Peek(42, 0); err == nil || err.Error() != "fixture denied" {
		t.Fatal(err)
	}
	if err := Inject(42, "text", false); err == nil || err.Error() != "fixture denied" {
		t.Fatal(err)
	}
	t.Setenv("NIGHTSHIFT_UNIT_RESPONSE", `not-json`)
	if _, err := Peek(42, 0); err == nil {
		t.Fatal("invalid JSON accepted")
	}
}

func TestHelperCommandKeepsTextOffArgv(t *testing.T) {
	text := "secret-like text\n£漢😀"
	c := helperCommand(context.Background(), "helper.exe", text, "inject", "42", "1")
	if !reflect.DeepEqual(c.Args, []string{"helper.exe", "_attach", "inject", "42", "1"}) {
		t.Fatal(c.Args)
	}
	b, err := io.ReadAll(c.Stdin)
	if err != nil || string(b) != text || !c.SysProcAttr.HideWindow || c.SysProcAttr.CreationFlags != windows.DETACHED_PROCESS {
		t.Fatalf("stdin=%q err=%v attrs=%+v", b, err, c.SysProcAttr)
	}
}

func TestHelperErrors(t *testing.T) {
	for _, tc := range []struct {
		data string
		want string
	}{
		{`{}`, ""}, {`{"error":"attach denied"}`, "attach denied"}, {`not json`, "invalid JSON"},
	} {
		err := helperErr([]byte(tc.data))
		if tc.want == "" {
			if err != nil {
				t.Fatal(err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Fatalf("%q: %v", tc.data, err)
		}
	}
}

func TestInjectInputSeparatesEnterAndPreservesUnicode(t *testing.T) {
	for _, enter := range []bool{false, true} {
		var events []string
		var writes [][]inputRecord
		err := injectInput(func(r []inputRecord) error { events = append(events, "write"); writes = append(writes, r); return nil },
			func(d time.Duration) {
				events = append(events, "sleep")
				if d != enterDelay(4) {
					t.Fatal(d)
				}
			}, "£漢😀\n", enter)
		if err != nil {
			t.Fatal(err)
		}
		want := []string{"write"}
		if enter {
			want = append(want, "sleep", "write")
		}
		if !reflect.DeepEqual(events, want) {
			t.Fatal(events)
		}
		var units []uint16
		for i := 0; i < len(writes[0]); i += 2 {
			down, up := writes[0][i], writes[0][i+1]
			if down.KeyDown != 1 || up.KeyDown != 0 {
				t.Fatal(down, up)
			}
			units = append(units, down.Char)
		}
		if string(utf16.Decode(units)) != "£漢😀\n" {
			t.Fatal(units)
		}
		if enter && (writes[1][0].VK != 0x0d || writes[1][0].Char != '\r') {
			t.Fatal(writes[1])
		}
	}
	writes, sleeps := 0, 0
	err := injectInput(func([]inputRecord) error { writes++; return errors.New("write failed") }, func(time.Duration) { sleeps++ }, "x", true)
	if err == nil || writes != 1 || sleeps != 0 {
		t.Fatal(err, writes, sleeps)
	}
}

func TestVibeVTKeys(t *testing.T) {
	want := map[string]string{"escape": "\x1b[27u", "esc": "\x1b[27u", "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D"}
	if !reflect.DeepEqual(vtKeys, want) {
		t.Fatal(vtKeys)
	}
}

func TestVerifierDoesNotRetryUncertainScreens(t *testing.T) {
	for _, tc := range []struct {
		name    string
		lines   []string
		readErr error
	}{
		{"unknown", []string{"unrecognized screen"}, nil},
		{"unreadable live pane", nil, errors.New("read failed")},
		{"permission with draft", []string{"Do you trust this folder?", "❯ pending text"}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Unix(0, 0)
			keys := 0
			v := submissionVerifier{
				peek:  func(uint32, int) (*peekResult, error) { return &peekResult{Lines: tc.lines}, tc.readErr },
				key:   func(uint32, string, string) error { keys++; return nil },
				alive: func(uint32) bool { return true },
				sleep: func(d time.Duration) { now = now.Add(d) }, now: func() time.Time { return now },
			}
			if v.verify(&session{PID: 42, Tool: "claude"}, "pending text") || keys != 0 {
				t.Fatal("uncertain screen retried", keys)
			}
		})
	}
}
