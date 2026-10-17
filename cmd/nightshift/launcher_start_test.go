package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestCopilotCommandDisablesBuiltinAndUserMCPServers(t *testing.T) {
	home := t.TempDir()
	t.Setenv("COPILOT_HOME", home)
	config := `{"mcpServers": {"zeta": {"type": "http", "headers": {"Authorization": "x"}}, "alpha": {}}}`
	if err := os.WriteFile(filepath.Join(home, "mcp-config.json"), []byte(config), 0666); err != nil {
		t.Fatal(err)
	}
	argv := strings.Join(copilotCommand("gemini-3.8-flash", "B", "F", "", "", "", nil, 0), " ")
	want := "--model gemini-3.8-flash --disable-builtin-mcps --no-remote-export " +
		"--disable-mcp-server alpha --disable-mcp-server zeta -i"
	if !strings.Contains(argv, want) {
		t.Fatalf("argv %q lacks %q", argv, want)
	}
	if strings.Contains(argv, "Authorization") {
		t.Fatalf("argv leaks MCP config fields: %q", argv)
	}
	t.Setenv("COPILOT_HOME", t.TempDir())
	argv = strings.Join(copilotCommand("m", "B", "F", "", "", "", nil, 0), " ")
	if !strings.Contains(argv, "--disable-builtin-mcps --no-remote-export -i") || strings.Contains(argv, "--disable-mcp-server") {
		t.Fatalf("missing config: %q", argv)
	}
}

func TestWtStepsSplitsTheChainIntoOneCallPerSubcommand(t *testing.T) {
	command := []string{"wt.exe", "-w", "-1", "--pos", "0,0", "--maximized", "new-tab", "--title", "T",
		";", "split-pane", "-H", "-s", "0.5", "x",
		";", "focus-pane", "-t", "0",
		";", "split-pane", "-V", "-s", "0.5", "y"}
	got := wtSteps(command, "Nightshift-shift-abc")
	want := [][]string{
		{"wt.exe", "-w", "Nightshift-shift-abc", "--pos", "0,0", "--maximized", "new-tab", "--title", "T"},
		{"wt.exe", "-w", "Nightshift-shift-abc", "split-pane", "-H", "-s", "0.5", "x"},
		{"wt.exe", "-w", "Nightshift-shift-abc", "focus-pane", "-t", "0"},
		{"wt.exe", "-w", "Nightshift-shift-abc", "split-pane", "-V", "-s", "0.5", "y"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("wtSteps:\n got %q\nwant %q", got, want)
	}
}

func TestWtStepsLeavesASingleCallAlone(t *testing.T) {
	command := []string{"wt.exe", "-w", "-1", "new-tab", "x"}
	got := wtSteps(command, "n")
	want := [][]string{{"wt.exe", "-w", "n", "new-tab", "x"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestWtCommandWideOpsPaneTakesTwoBottomCells(t *testing.T) {
	splits := func(opsWidth, slots int) []string {
		command := wtCommand(wtSpec{name: "n", stateDir: "s", modeFlag: "--start", members: []string{"Foreman"}, slots: slots,
			opsWidth: opsWidth, opsDir: "R"})
		out := []string{}
		for i, part := range command {
			if part == "split-pane" {
				out = append(out, command[i+1]+" "+command[i+3])
			}
		}
		return out
	}
	// Bottom row 1, 1 and 2 cells: slot 6 takes 3/4 of slot 5's pane, then Ops takes 2/3 of slot 6's.
	if got, want := splits(2, 7), []string{"-H 0.5", "-V 0.75", "-V 0.6667", "-V 0.5", "-V 0.75", "-V 0.6667"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("ops_width 2: got %q want %q", got, want)
	}
	if got, want := splits(1, 8), []string{"-H 0.5", "-V 0.75", "-V 0.6667", "-V 0.5", "-V 0.75", "-V 0.6667", "-V 0.5"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("ops_width 1: got %q want %q", got, want)
	}
}

func TestWtCommandWideForemanTakesTwoTopCells(t *testing.T) {
	command := wtCommand(wtSpec{name: "n", stateDir: "s", modeFlag: "--start", members: []string{"Foreman"}, slots: 6,
		opsWidth: 2, foremanWidth: 2, opsDir: "R"})
	got, slotAfterH := []string{}, ""
	for i, part := range command {
		if part == "split-pane" {
			got = append(got, command[i+1]+" "+command[i+3])
		}
		if part == "--slot" && slotAfterH == "" && len(got) > 0 && got[0] == "-H 0.5" && len(got) == 1 {
			slotAfterH = command[i+1]
		}
	}
	// Top row Foreman (2 cells), slots 2 and 3; bottom row slots 4 and 5, then Ops (2 cells, slot 6).
	if want := []string{"-H 0.5", "-V 0.5", "-V 0.5", "-V 0.75", "-V 0.6667"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q want %q", got, want)
	}
	if slotAfterH != "4" {
		t.Fatalf("bottom row starts at slot %s, want 4", slotAfterH)
	}
}

func TestRealExeSwapsAnNpmShimForItsPackagedBinary(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("PATH", dir)
	shim := filepath.Join(dir, "claude.cmd")
	exe := filepath.Join(dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
	for _, f := range []string{shim, exe} {
		if err := os.MkdirAll(filepath.Dir(f), 0777); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(f, []byte("x"), 0666); err != nil {
			t.Fatal(err)
		}
	}
	if got := realExe("claude", shim); got != exe {
		t.Fatalf("realExe = %q, want %q", got, exe)
	}
	if got := realExe("agy", filepath.Join(dir, "agy.cmd")); got != filepath.Join(dir, "agy.cmd") {
		t.Fatalf("a shim with nothing behind it must stay: %q", got)
	}
	if got := realExe("codex", `C:\x\codex.exe`); got != `C:\x\codex.exe` {
		t.Fatalf("an executable must stay: %q", got)
	}
}

func TestRealExePrefersThePackageBehindAShimOverAnOlderExeOnPath(t *testing.T) {
	shims, old := t.TempDir(), t.TempDir()
	t.Setenv("PATH", shims+string(os.PathListSeparator)+old)
	shim := filepath.Join(shims, "codex.cmd")
	packaged := filepath.Join(shims, "node_modules", "@openai", "codex", "node_modules", "@openai", "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin", "codex.exe")
	for _, f := range []string{shim, packaged, filepath.Join(old, "codex.exe")} {
		if err := os.MkdirAll(filepath.Dir(f), 0777); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(f, []byte("x"), 0666); err != nil {
			t.Fatal(err)
		}
	}
	if got := realExe("codex", shim); got != packaged {
		t.Fatalf("realExe = %q, want the npm package's %q", got, packaged)
	}
}
