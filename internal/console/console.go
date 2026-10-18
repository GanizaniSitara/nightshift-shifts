// Package console reads and types into another process's Windows console: the pane screens, nudges and keys
// nightshift needs, reworked from agents-cli's console code (NGH-068; agents-cli is no longer a dependency).
//
// A process can be attached to one console at a time and the caller's own stdout may be a console, so the
// attach work runs in a hidden, detached child: the nightshift binary itself as `nightshift _attach <op> ...`
// (NIGHTSHIFT_CONSOLE_HELPER names another helper binary; it is the test seam). Text always travels on the
// child's stdin, never in its argv; the child answers with one JSON object on stdout.
package console

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Screen is what a pane shows: its console title, visible rows (and any asked-for rows above), buffer width,
// and, from Tail, the guessed state ("busy", "idle", "prompt" or "unknown").
type Screen struct {
	Title string
	Lines []string
	Width int
	State string
}

type peekResult struct {
	Title string   `json:"title"`
	Lines []string `json:"lines"`
	Width int      `json:"width"`
	Error string   `json:"error,omitempty"`
}

// Peek reads pid's console: the visible window plus `above` rows of scrollback.
func Peek(pid uint32, above int) (Screen, error) {
	r, err := peek(pid, above)
	if r == nil {
		return Screen{}, err
	}
	return Screen{Title: r.Title, Lines: r.Lines, Width: r.Width}, err
}

// Tail is Peek with the state guessed for tool and the screen cut to its last `lines` non-blank-tailed rows
// (0 = all), as `agents-cli tail <pid> --lines N` printed it.
func Tail(pid uint32, tool string, lines int) (Screen, error) {
	r, err := peek(pid, 0)
	if err != nil {
		return Screen{}, err
	}
	return Screen{Title: r.Title, Lines: screenTail(r.Lines, lines), Width: r.Width, State: guessState(tool, r.Lines)}, nil
}

// Send types text into pid's composer the way tool reads it, presses Enter when asked, and reports whether
// the screen shows it was submitted. It never refuses a Claude target (nightshift always sent with --force).
func Send(pid uint32, tool, text string, enter bool) (bool, error) {
	s := &session{PID: pid, Tool: tool}
	switch tool {
	case "agy":
		return liveAGYConsole.send(s, text, enter, true)
	case "copilot":
		return liveCopilotConsole.send(s, text, enter)
	}
	send := inject
	if tool == "gemini" {
		send = liveGeminiConsole.inject
	}
	if err := send(pid, text, enter); err != nil {
		return false, err
	}
	if !enter {
		return false, nil
	}
	return defaultVerifier.verify(s, text), nil
}

// Inject types text as raw key events (Enter in a second write after a pause), with no verification.
func Inject(pid uint32, text string, enter bool) error { return inject(pid, text, enter) }

// Key presses a named key (enter, escape, tab, backspace, arrows, ctrl-c/u/d/l) the way tool reads it.
func Key(pid uint32, tool, key string) error { return keyFor(pid, tool, key) }

// session is the target of a send: the console-owning pid and the crew tool name.
type session struct {
	PID  uint32
	Tool string
}

// ---- parent side: run the hidden helper ----

func helperExe() (string, error) {
	if exe := os.Getenv("NIGHTSHIFT_CONSOLE_HELPER"); exe != "" {
		return exe, nil
	}
	return os.Executable()
}

func helperCommand(ctx context.Context, exe, input string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, exe, append([]string{"_attach"}, args...)...)
	cmd.Stdin = strings.NewReader(input)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.DETACHED_PROCESS, HideWindow: true}
	return cmd
}

func runHelper(args ...string) ([]byte, error) { return runHelperInput("", args...) }

func runHelperInput(input string, args ...string) ([]byte, error) {
	exe, err := helperExe()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	out, err := helperCommand(ctx, exe, input, args...).Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && len(out) > 0 {
			return out, nil // the helper reported a structured error on stdout
		}
		return nil, fmt.Errorf("console helper: %v", err)
	}
	return out, nil
}

func peek(pid uint32, above int) (*peekResult, error) {
	out, err := runHelper("peek", strconv.FormatUint(uint64(pid), 10), strconv.Itoa(above))
	if err != nil {
		return nil, err
	}
	var r peekResult
	if err := json.Unmarshal(out, &r); err != nil {
		return nil, errors.New("console helper returned invalid JSON")
	}
	if r.Error != "" {
		return &r, errors.New(r.Error)
	}
	return &r, nil
}

func inject(pid uint32, text string, enter bool) error {
	e := "0"
	if enter {
		e = "1"
	}
	out, err := runHelperInput(text, "inject", strconv.FormatUint(uint64(pid), 10), e)
	if err != nil {
		return err
	}
	return helperErr(out)
}

func paste(pid uint32, text string) error {
	out, err := runHelperInput(text, "copilot-paste", strconv.FormatUint(uint64(pid), 10))
	if err != nil {
		return err
	}
	return helperErr(out)
}

// vtKeys are typed as characters for TUIs that read VT input (Textual, i.e. Mistral Vibe): such apps ignore a
// bare arrow VK with no character and hold a lone ESC until the next byte, so escape is the CSI-u form.
var vtKeys = map[string]string{
	"escape": "\x1b[27u", "esc": "\x1b[27u",
	"up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
}

func keyFor(pid uint32, tool, key string) error {
	if seq, ok := vtKeys[strings.ToLower(key)]; ok && tool == "vibe" {
		return inject(pid, seq, false)
	}
	out, err := runHelper("key", strconv.FormatUint(uint64(pid), 10), key)
	if err != nil {
		return err
	}
	return helperErr(out)
}

func helperErr(out []byte) error {
	var r struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(out, &r); err != nil {
		return errors.New("console helper returned invalid JSON")
	}
	if r.Error != "" {
		return errors.New(r.Error)
	}
	return nil
}

func isAlive(pid uint32) bool {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return false
	}
	defer windows.CloseHandle(h)
	var code uint32
	if err := windows.GetExitCodeProcess(h, &code); err != nil {
		return false
	}
	return code == 259 // STILL_ACTIVE
}

// ---- child side: attached to the target console ----

// HelperMain is `nightshift _attach <op> <pid> ...`: peek <pid> [above] | inject <pid> 0|1 (text on stdin) |
// copilot-paste <pid> (text on stdin) | key <pid> <name>. It prints one JSON object; a failure prints
// {"error": ...} and returns 1.
func HelperMain(args []string) int {
	result, err := helperMain(args)
	if err != nil {
		encoded, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Println(string(encoded))
		return 1
	}
	encoded, _ := json.Marshal(result)
	fmt.Println(string(encoded))
	return 0
}

func helperMain(args []string) (any, error) {
	if len(args) < 2 {
		return nil, errors.New("_attach needs an op and a pid")
	}
	pid64, err := strconv.ParseUint(args[1], 10, 32)
	if err != nil {
		return nil, err
	}
	pid := uint32(pid64)
	switch args[0] {
	case "peek":
		above := 0
		if len(args) > 2 {
			above, _ = strconv.Atoi(args[2])
		}
		return doPeek(pid, above), nil
	case "copilot-paste":
		if len(args) != 2 {
			return nil, errors.New("_attach copilot-paste needs pid and text on stdin")
		}
		text, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, errors.New("cannot read helper input")
		}
		return errJSON(doPaste(pid, string(text))), nil
	case "inject":
		if len(args) != 3 {
			return nil, errors.New("_attach inject needs pid, enter and text on stdin")
		}
		text, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, errors.New("cannot read helper input")
		}
		return errJSON(doInject(pid, string(text), args[2] == "1")), nil
	case "key":
		if len(args) < 3 {
			return nil, errors.New("_attach key needs pid, key")
		}
		return errJSON(doKey(pid, args[2])), nil
	}
	return nil, fmt.Errorf("unknown helper op %q", args[0])
}

func errJSON(err error) map[string]string {
	if err != nil {
		return map[string]string{"error": err.Error()}
	}
	return map[string]string{}
}

var (
	k32                          = windows.NewLazySystemDLL("kernel32.dll")
	pAttachConsole               = k32.NewProc("AttachConsole")
	pFreeConsole                 = k32.NewProc("FreeConsole")
	pGetConsoleTitleW            = k32.NewProc("GetConsoleTitleW")
	pGetConsoleScreenBufferInfo  = k32.NewProc("GetConsoleScreenBufferInfo")
	pReadConsoleOutputCharacterW = k32.NewProc("ReadConsoleOutputCharacterW")
	pWriteConsoleInputW          = k32.NewProc("WriteConsoleInputW")
)

type coord struct{ X, Y int16 }
type smallRect struct{ Left, Top, Right, Bottom int16 }
type screenBufferInfo struct {
	Size       coord
	Cursor     coord
	Attributes uint16
	Window     smallRect
	MaxWindow  coord
}

// inputRecord is INPUT_RECORD with the KEY_EVENT_RECORD union member, 20 bytes.
type inputRecord struct {
	EventType   uint16
	_           uint16
	KeyDown     int32
	RepeatCount uint16
	VK          uint16
	Scan        uint16
	Char        uint16
	Ctrl        uint32
}

const (
	keyEvent        = 0x0001
	leftCtrlPressed = 0x0008
)

func attach(pid uint32) error {
	pFreeConsole.Call()
	if r, _, e := pAttachConsole.Call(uintptr(pid)); r == 0 {
		return fmt.Errorf("AttachConsole(%d): %v", pid, e)
	}
	return nil
}

func openConsole(name string) (windows.Handle, error) {
	p, _ := windows.UTF16PtrFromString(name)
	h, err := windows.CreateFile(p, windows.GENERIC_READ|windows.GENERIC_WRITE,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
	if err != nil {
		return 0, fmt.Errorf("open %s: %v", name, err)
	}
	return h, nil
}

func doPeek(pid uint32, above int) *peekResult {
	r := &peekResult{Lines: []string{}}
	if err := attach(pid); err != nil {
		r.Error = err.Error()
		return r
	}
	title := make([]uint16, 1024)
	if n, _, _ := pGetConsoleTitleW.Call(uintptr(unsafe.Pointer(&title[0])), uintptr(len(title))); n > 0 {
		r.Title = windows.UTF16ToString(title[:n])
	}
	h, err := openConsole("CONOUT$")
	if err != nil {
		r.Error = err.Error()
		return r
	}
	defer windows.CloseHandle(h)
	var info screenBufferInfo
	if ok, _, e := pGetConsoleScreenBufferInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&info))); ok == 0 {
		r.Error = fmt.Sprintf("GetConsoleScreenBufferInfo: %v", e)
		return r
	}
	width := max(int(info.Size.X), 1)
	r.Width = width
	top := max(int(info.Window.Top)-above, 0)
	buf := make([]uint16, width)
	for y := top; y <= int(info.Window.Bottom); y++ {
		var read uint32
		at := uint32(uint16(y)) << 16
		if ok, _, e := pReadConsoleOutputCharacterW.Call(uintptr(h), uintptr(unsafe.Pointer(&buf[0])), uintptr(width), uintptr(at), uintptr(unsafe.Pointer(&read))); ok == 0 {
			r.Error = fmt.Sprintf("ReadConsoleOutputCharacter row %d: %v", y, e)
			return r
		}
		r.Lines = append(r.Lines, strings.TrimRight(strings.ReplaceAll(windows.UTF16ToString(buf[:read]), "\x00", ""), " "))
	}
	return r
}

func writeInput(h windows.Handle, recs []inputRecord) error {
	if len(recs) == 0 {
		return nil
	}
	var written uint32
	if ok, _, e := pWriteConsoleInputW.Call(uintptr(h), uintptr(unsafe.Pointer(&recs[0])), uintptr(len(recs)), uintptr(unsafe.Pointer(&written))); ok == 0 {
		return fmt.Errorf("WriteConsoleInput: %v", e)
	}
	return nil
}

// writeToConsole attaches to pid's console and writes records to its input buffer through write.
func writeToConsole(pid uint32, write func(func([]inputRecord) error) error) error {
	if err := attach(pid); err != nil {
		return err
	}
	h, err := openConsole("CONIN$")
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	return write(func(recs []inputRecord) error { return writeInput(h, recs) })
}

func keyPair(vk, ch uint16, ctrl uint32) []inputRecord {
	return []inputRecord{
		{EventType: keyEvent, KeyDown: 1, RepeatCount: 1, VK: vk, Char: ch, Ctrl: ctrl},
		{EventType: keyEvent, KeyDown: 0, RepeatCount: 1, VK: vk, Char: ch, Ctrl: ctrl},
	}
}

// enterDelay scales the pause before Enter with the text length: TUIs (Codex especially) treat a fast burst
// as a paste and ignore or swallow an Enter inside it.
func enterDelay(textLen int) time.Duration {
	if textLen <= 0 {
		return 150 * time.Millisecond
	}
	return min(500*time.Millisecond+time.Duration(textLen/200)*100*time.Millisecond, 4*time.Second)
}

func doInject(pid uint32, text string, enter bool) error {
	return writeToConsole(pid, func(write func([]inputRecord) error) error {
		return injectInput(write, time.Sleep, text, enter)
	})
}

// injectInput writes the text as key events, then Enter in a second write after enterDelay: a trailing CR in
// the same burst becomes a newline instead of a submit.
func injectInput(write func([]inputRecord) error, sleep func(time.Duration), text string, enter bool) error {
	if text != "" {
		recs := make([]inputRecord, 0, len(text)*2)
		for _, r := range text {
			for _, u := range utf16Units(r) {
				recs = append(recs, keyPair(0xE7, u, 0)...)
			}
		}
		if err := write(recs); err != nil {
			return err
		}
	}
	if enter {
		sleep(enterDelay(len([]rune(text))))
		return write(keyPair(0x0D, '\r', 0))
	}
	return nil
}

func utf16Units(r rune) []uint16 {
	if r < 0x10000 {
		return []uint16{uint16(r)}
	}
	r -= 0x10000
	return []uint16{uint16(0xD800 + (r >> 10)), uint16(0xDC00 + (r & 0x3FF))}
}

// pasteRecords wraps text in bracketed paste so embedded newlines and slash-command completion are not acted
// on midway (Copilot, agy, Gemini CLI); Enter is a separate named key.
func pasteRecords(text string) []inputRecord {
	text = strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", "\n"), "\r", "\n")
	var records []inputRecord
	for _, r := range "\x1b[200~" + text + "\x1b[201~" {
		for _, u := range utf16Units(r) {
			records = append(records, keyPair(0, u, 0)...)
		}
	}
	return records
}

func doPaste(pid uint32, text string) error {
	return writeToConsole(pid, func(write func([]inputRecord) error) error { return write(pasteRecords(text)) })
}

var namedKeys = map[string]struct {
	vk, ch uint16
	ctrl   uint32
}{
	"enter":     {0x0D, '\r', 0},
	"escape":    {0x1B, 0x1B, 0},
	"esc":       {0x1B, 0x1B, 0},
	"tab":       {0x09, '\t', 0},
	"backspace": {0x08, '\b', 0},
	"up":        {0x26, 0, 0},
	"down":      {0x28, 0, 0},
	"left":      {0x25, 0, 0},
	"right":     {0x27, 0, 0},
	"ctrl-c":    {0x43, 0x03, leftCtrlPressed},
	"ctrl-u":    {0x55, 0x15, leftCtrlPressed},
	"ctrl-d":    {0x44, 0x04, leftCtrlPressed},
	"ctrl-l":    {0x4C, 0x0C, leftCtrlPressed},
}

func doKey(pid uint32, name string) error {
	k, ok := namedKeys[strings.ToLower(name)]
	if !ok {
		return fmt.Errorf("unknown key %q", name)
	}
	return writeToConsole(pid, func(write func([]inputRecord) error) error { return write(keyPair(k.vk, k.ch, k.ctrl)) })
}
