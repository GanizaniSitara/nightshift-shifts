package main

import (
	"reflect"
	"testing"
)

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
