package console

import "testing"

// Screens captured from live sessions on 2026-10-01; the heuristic must keep
// reading them the same way.
func TestGuessStateLiveScreens(t *testing.T) {
	agyIdle := []string{
		"      ▄▀▀▄        Antigravity CLI 1.2.14",
		"    ▀▀▀▀▀▀▀▀      Gemini 3.1 Pro (High)",
		"────────────────────────────────────────",
		">",
		"────────────────────────────────────────",
		"? for shortcuts                                Gemini 3.1 Pro · high",
		"",
	}
	if s := guessState("agy", agyIdle); s != "idle" {
		t.Errorf("agy idle screen -> %s", s)
	}
	agyTrust := []string{
		"Do you trust the contents of this project?",
		"> Yes, I trust this folder",
		"  No, exit",
		"  ↑/↓ Navigate · enter Confirm",
	}
	if s := guessState("agy", agyTrust); s != "prompt" {
		t.Errorf("agy trust dialog -> %s", s)
	}
	claudeBusy := []string{
		"✢ Misting… (10s · ↓ 550 tokens)",
		"",
		"> We should probably run it every day to be honest.",
		"  ctrl+x ctrl+s to send now",
	}
	if s := guessState("claude", claudeBusy); s != "busy" {
		t.Errorf("claude busy screen -> %s", s)
	}
	codexBusy := []string{
		"Working (4m 43s • esc to interrupt)",
		"",
		"› Ask Codex to do anything",
	}
	if s := guessState("codex", codexBusy); s != "busy" {
		t.Errorf("codex busy screen -> %s", s)
	}
	// A real window has dozens of blank rows under the TUI; they must not hide it.
	for i := 0; i < 40; i++ {
		agyIdle = append(agyIdle, "")
	}
	if s := guessState("agy", agyIdle); s != "idle" {
		t.Errorf("agy idle screen with blank tail -> %s", s)
	}
	// Mistral Vibe 2.26 (2026-10-07): the start-up logo is braille art, which
	// must not read as a spinner; its real spinner line says "Generating…".
	vibeLogo := []string{
		" ⢠⢢    ⡔⢄⠔⡄",
		" ⢸⢸⣀⡔⢉⠱⣃⡢⣂⡣",
		" ⠈⠒⠒⠣⠤⠵⠤⠬⠮⠆",
		"Mistral Vibe v2.26.0 · Mistral Medium 3.5[high] · unified harness · [Subscription] Pro",
		"Type /help for more information",
	}
	vibeFoot := []string{
		"──────────────────────────────────────── accept edits ─",
		">",
		"────────────────────────────────────────────────────────",
		`~\vibetest [PID 16476]                 0/200k tokens (0%)`,
	}
	if s := guessState("vibe", append(append([]string{}, vibeLogo...), vibeFoot...)); s != "idle" {
		t.Errorf("vibe idle screen with logo -> %s", s)
	}
	vibeBusy := append(append([]string{}, vibeLogo...), "> Run the shell command: echo hi",
		"⠉⠁ Generating… (0s Esc/Ctrl+C to interrupt)")
	if s := guessState("vibe", append(vibeBusy, vibeFoot...)); s != "busy" {
		t.Errorf("vibe busy screen -> %s", s)
	}
	vibeApproval := []string{
		" ⣠ Running command… (1s Esc/Ctrl+C to interrupt)",
		"│ Permission for the file_system.bash tool (mkdir *)                                                   │",
		"│ › 1. Allow once                                                                                      │",
		"│   4. Deny                                                                                            │",
		"│ ↑↓/jk navigate  Enter select  Esc reject                                                             │",
	}
	if s := guessState("vibe", vibeApproval); s != "prompt" {
		t.Errorf("vibe approval box -> %s", s)
	}
	cmdIdle := []string{"ping-from-agents-cli", "", `C:\git\agents-cli>`, ""}
	if s := guessState("other", cmdIdle); s != "idle" {
		t.Errorf("cmd prompt -> %s", s)
	}
}
