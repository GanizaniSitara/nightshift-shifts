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
