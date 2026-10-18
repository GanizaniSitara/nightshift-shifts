package console

import (
	"regexp"
	"strings"
)

var (
	// Claude: "✢ Misting… (10s · ↓ 550 tokens)" / "esc to interrupt"; Codex: "Working (4m 43s • esc to
	// interrupt)"; braille spinners elsewhere.
	spinnerRe = regexp.MustCompile(`[\x{2800}-\x{28FF}]|esc to interrupt|Esc to cancel|\(esc\)|Thinking|Working \(|\(\d+[smh][\d\w ]* · |…\s*\(`)
	promptRe  = regexp.MustCompile(`(?i)\(y/n\)|\[y/n\]|Yes, |Do you want|Do you trust|Allow |Approve|press enter to confirm|enter Confirm|❯ 1\.|› 1\.|\? for shortcuts.*\n.*\b1\. `)
	// A bare input prompt, or a TUI's empty-input placeholder.
	brailleOnlyRe   = regexp.MustCompile(`^[\s\x{2800}-\x{28FF}]+$`)
	idleRe          = regexp.MustCompile(`^\s*(>|›|❯|\$|%)\s*$|^› Ask Codex|^> Try "|^>\s+Type your message|^[A-Za-z]:\\[^>]*>\s*$`)
	pastedContentRe = regexp.MustCompile(`(?i)\[pasted (content|text)`)
)

// guessState reads the last screen rows: spinner text means a turn is running, a visible question means the
// TUI waits on a human, a bare input prompt means idle, anything else is "unknown".
func guessState(tool string, lines []string) string {
	if tool == "copilot" {
		return copilotState(lines)
	}
	// TUIs draw at the top of a tall window and leave the rest blank; trailing blank rows carry no signal.
	tail := trimBlankTail(lines)
	if tool == "vibe" {
		// Vibe's start-up logo is braille art; its live spinner row always carries text, so glyph-only rows are logo.
		kept := tail[:0:0]
		for _, l := range tail {
			if !brailleOnlyRe.MatchString(l) {
				kept = append(kept, l)
			}
		}
		tail = kept
	}
	if len(tail) > 18 {
		tail = tail[len(tail)-18:]
	}
	joined := strings.Join(tail, "\n")
	if promptRe.MatchString(joined) {
		return "prompt"
	}
	if spinnerRe.MatchString(joined) {
		return "busy"
	}
	for _, l := range tail {
		if idleRe.MatchString(strings.TrimSpace(l)) {
			return "idle"
		}
	}
	return "unknown"
}

func trimBlankTail(lines []string) []string {
	end := len(lines)
	for end > 0 && strings.TrimSpace(lines[end-1]) == "" {
		end--
	}
	return lines[:end]
}

// screenTail is the last count rows after the blank tail is trimmed; count 0 keeps every row untrimmed.
func screenTail(lines []string, count int) []string {
	if count == 0 {
		return lines
	}
	lines = trimBlankTail(lines)
	if len(lines) > count {
		return lines[len(lines)-count:]
	}
	return lines
}

// copilotState: Copilot keeps its live status below the composer; transcript text (echoed prompts, old
// permission questions) is not a status.
func copilotState(lines []string) string {
	tail := trimBlankTail(lines)
	if len(tail) == 0 {
		return "unknown"
	}
	footer := strings.ToLower(strings.TrimSpace(tail[len(tail)-1]))
	if copilotMenu(tail) {
		return "prompt"
	}
	if strings.Contains(footer, "esc interrupt") || strings.Contains(footer, "esc to interrupt") {
		return "busy"
	}
	if draft, ok := copilotComposer(tail); ok {
		if draft == "" {
			return "idle"
		}
		return "prompt"
	}
	return "unknown"
}

// copilotComposer finds the composer between two horizontal rules, from the bottom up (agy frames other rows,
// such as background tasks, below it), and returns its draft; a transcript's prompt glyph is not the composer.
func copilotComposer(lines []string) (string, bool) {
	bottom := -1
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if len([]rune(line)) < 3 || strings.Trim(line, "─━") != "" {
			continue
		}
		if bottom < 0 || i+1 >= bottom {
			bottom = i
			continue
		}
		first := strings.TrimSpace(lines[i+1])
		if !strings.HasPrefix(first, "❯") && !strings.HasPrefix(first, ">") {
			bottom = i
			continue
		}
		draft := []string{strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(first, "❯"), ">"))}
		for _, row := range lines[i+2 : bottom] {
			draft = append(draft, strings.TrimSpace(row))
		}
		return strings.TrimSpace(strings.Join(draft, "\n")), true
	}
	return "", false
}

func copilotMenu(lines []string) bool {
	tail := trimBlankTail(lines)
	if len(tail) == 0 {
		return false
	}
	footer := strings.ToLower(tail[len(tail)-1])
	return strings.Contains(footer, "enter") && (strings.Contains(footer, "select") || strings.Contains(footer, "confirm"))
}

func agyBusy(lines []string) bool {
	tail := trimBlankTail(lines)
	return len(tail) > 0 && strings.Contains(strings.ToLower(tail[len(tail)-1]), "esc to cancel")
}

// hasUnsubmittedInput: the last input prompt still holds our text or a paste pill. Transcript text, unknown
// screens and permission menus are no evidence that Enter was swallowed.
func hasUnsubmittedInput(text string, lines []string) bool {
	prefix := []rune(strings.TrimSpace(strings.SplitN(text, "\n", 2)[0]))
	if len(prefix) > 30 {
		prefix = prefix[:30]
	}
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if !strings.HasPrefix(line, "❯") && !strings.HasPrefix(line, "›") && !strings.HasPrefix(line, ">") {
			continue
		}
		if idleRe.MatchString(line) {
			return false
		}
		return pastedContentRe.MatchString(line) || (len(prefix) > 0 && strings.Contains(line, string(prefix)))
	}
	return false
}

// isSubmitted: the injected input was submitted when a busy marker or a dialog shows, or the composer is back
// to an idle prompt without the text or a paste pill.
func isSubmitted(tool, text string, lines []string) bool {
	tail := trimBlankTail(lines)
	if len(tail) == 0 {
		return false
	}
	if len(tail) > 18 {
		tail = tail[len(tail)-18:]
	}
	if hasUnsubmittedInput(text, tail) {
		return false
	}
	for _, l := range tail {
		if pastedContentRe.MatchString(l) {
			return false
		}
	}
	if st := guessState(tool, lines); st == "busy" || st == "prompt" {
		return true
	}
	hasIdle := false
	for _, l := range tail {
		if idleRe.MatchString(strings.TrimSpace(l)) {
			hasIdle = true
			break
		}
	}
	if !hasIdle {
		return false
	}
	if trimmed := strings.TrimSpace(text); trimmed != "" {
		runes := []rune(trimmed)
		prefix := string(runes)
		if len(runes) > 30 {
			prefix = string(runes[:30])
		}
		start := max(len(tail)-4, 0)
		for _, l := range tail[start:] {
			if strings.Contains(l, prefix) {
				return false
			}
		}
	}
	return true
}
