package console

import (
	"errors"
	"time"
)

// submissionVerifier watches the screen after an injected send: submitted, or still in the composer, in which
// case it presses Enter once more and watches again.
type submissionVerifier struct {
	peek  func(uint32, int) (*peekResult, error)
	key   func(uint32, string, string) error
	alive func(uint32) bool
	sleep func(time.Duration)
	now   func() time.Time
}

var defaultVerifier = submissionVerifier{peek: peek, key: keyFor, alive: isAlive, sleep: time.Sleep, now: time.Now}

func (v submissionVerifier) waitFor(s *session, text string, timeout time.Duration) bool {
	v.sleep(100 * time.Millisecond)
	deadline := v.now().Add(timeout)
	for {
		r, err := v.peek(s.PID, 0)
		if err != nil {
			return !v.alive(s.PID) // a pane that exited took the input with it
		}
		if isSubmitted(s.Tool, text, r.Lines) {
			return true
		}
		if !v.now().Before(deadline) {
			return false
		}
		v.sleep(150 * time.Millisecond)
	}
}

func (v submissionVerifier) verify(s *session, text string) bool {
	if v.waitFor(s, text, 1200*time.Millisecond) {
		return true
	}
	// Re-read right before retrying, so a dialog or a failed screen read never gets an unsolicited Enter.
	r, err := v.peek(s.PID, 0)
	if err != nil || guessState(s.Tool, r.Lines) == "prompt" || !hasUnsubmittedInput(text, r.Lines) {
		return false
	}
	if err := v.key(s.PID, s.Tool, "enter"); err != nil {
		return false
	}
	return v.waitFor(s, text, 1200*time.Millisecond)
}

// pasteDelay lets a bracketed paste (including a long redraw) finish before Enter, bounded well inside the
// helper's 15-second lifetime.
func pasteDelay(text string) time.Duration {
	return min(800*time.Millisecond+time.Duration(len([]rune(text))/200)*100*time.Millisecond, 4*time.Second)
}

// copilotConsole sends to Copilot: refuse a busy pane or a menu, bracketed paste, wait for the draft to
// settle, Enter, then watch for the composer to clear. It never retries Enter blindly: a second one could
// approve a permission or pick a menu item.
type copilotConsole struct {
	paste func(uint32, string) error
	peek  func(uint32, int) (*peekResult, error)
	key   func(uint32, string, string) error
	sleep func(time.Duration)
	now   func() time.Time
}

var liveCopilotConsole = copilotConsole{paste: paste, peek: peek, key: keyFor, sleep: time.Sleep, now: time.Now}

func (c copilotConsole) send(s *session, text string, enter bool) (bool, error) {
	r, err := c.peek(s.PID, 0)
	if err != nil {
		return false, err
	}
	if copilotMenu(r.Lines) || copilotState(r.Lines) == "busy" {
		return false, errors.New("Copilot is busy or showing a menu; inspect the pane before sending")
	}
	if err := c.paste(s.PID, text); err != nil {
		return false, err
	}
	if !enter {
		return false, nil
	}
	c.sleep(pasteDelay(text))
	if err := c.settle(s.PID, nil, "Copilot input did not settle; text may remain in the composer"); err != nil {
		return false, err
	}
	if err := c.key(s.PID, "copilot", "enter"); err != nil {
		return false, err
	}
	deadline := c.now().Add(3 * time.Second)
	for {
		c.sleep(150 * time.Millisecond)
		r, err := c.peek(s.PID, 0)
		if err != nil {
			return false, err
		}
		state := copilotState(r.Lines)
		draft, ok := copilotComposer(r.Lines)
		if (ok && draft == "" && (state == "busy" || state == "idle")) || copilotMenu(r.Lines) {
			return true, nil
		}
		if !c.now().Before(deadline) {
			return false, nil
		}
	}
}

// settle waits up to 5 seconds for a non-empty composer draft to read the same twice running; bad, when set,
// fails early on a screen that changed state.
func (c copilotConsole) settle(pid uint32, bad func([]string) error, timeout string) error {
	deadline := c.now().Add(5 * time.Second)
	last, stable := "", 0
	for {
		r, err := c.peek(pid, 0)
		if err != nil {
			return err
		}
		if bad != nil {
			if err := bad(r.Lines); err != nil {
				return err
			}
		}
		draft, ok := copilotComposer(r.Lines)
		if ok && draft != "" && draft == last {
			stable++
		} else {
			stable = 0
		}
		if stable >= 2 {
			return nil
		}
		if !c.now().Before(deadline) {
			return errors.New(timeout)
		}
		last = draft
		c.sleep(150 * time.Millisecond)
	}
}

// agyConsole sends to agy (Antigravity): bracketed paste too, since plain character events redraw per
// character. force skips only the empty-composer check; a busy pane or a menu still refuses.
type agyConsole copilotConsole

var liveAGYConsole = agyConsole(liveCopilotConsole)

func (c agyConsole) send(s *session, text string, enter, force bool) (bool, error) {
	notIdle := errors.New("agy must have an empty idle composer before sending; inspect the pane")
	r, err := c.peek(s.PID, 0)
	if err != nil {
		return false, err
	}
	if agyBusy(r.Lines) || copilotMenu(r.Lines) {
		return false, notIdle
	}
	if draft, ok := copilotComposer(r.Lines); !force && (!ok || draft != "") {
		return false, notIdle
	}
	if err := c.paste(s.PID, text); err != nil {
		return false, err
	}
	if !enter {
		return false, nil
	}
	// A write only acknowledges enqueueing; wait for a stable draft before Enter, outside the paste burst.
	c.sleep(150 * time.Millisecond)
	changed := func(lines []string) error {
		if agyBusy(lines) || copilotMenu(lines) {
			return errors.New("agy changed state during paste; inspect the pane before submitting")
		}
		return nil
	}
	if err := copilotConsole(c).settle(s.PID, changed, "agy paste did not settle; text may remain in the composer"); err != nil {
		return false, err
	}
	if err := c.key(s.PID, s.Tool, "enter"); err != nil {
		return false, err
	}
	deadline := c.now().Add(3 * time.Second)
	for {
		c.sleep(150 * time.Millisecond)
		r, err := c.peek(s.PID, 0)
		if err != nil {
			return false, err
		}
		if draft, ok := copilotComposer(r.Lines); ok && draft == "" || copilotMenu(r.Lines) {
			return true, nil
		}
		if !c.now().Before(deadline) {
			return false, nil
		}
		// an uncertain result never causes a second Enter
	}
}

// geminiConsole is Google's Gemini CLI: bracketed paste, then Enter after the paste delay, no verification.
type geminiConsole struct {
	paste func(uint32, string) error
	key   func(uint32, string, string) error
	sleep func(time.Duration)
}

var liveGeminiConsole = geminiConsole{paste: paste, key: keyFor, sleep: time.Sleep}

func (c geminiConsole) inject(pid uint32, text string, enter bool) error {
	if err := c.paste(pid, text); err != nil {
		return err
	}
	if !enter {
		return nil
	}
	c.sleep(pasteDelay(text))
	return c.key(pid, "gemini", "enter")
}
