package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func readQueue(t *testing.T, dir string) []queueItem {
	t.Helper()
	var list []queueItem
	if err := withQueue(dir, func(items []queueItem) ([]queueItem, error) { list = items; return nil, nil }); err != nil {
		t.Fatal(err)
	}
	return list
}

func TestQueueAddNextDoneDrop(t *testing.T) {
	dir := t.TempDir()
	if _, item, err := queueAdd(dir, "TECH-230", ""); err != nil || item.ID != 1 || item.Ticket != "TECH-230" || item.From != "owner" {
		t.Fatalf("add 1: %+v %v", item, err)
	}
	if _, item, err := queueAdd(dir, "tidy the README", "start"); err != nil || item.ID != 2 || item.Ticket != "" {
		t.Fatalf("add 2: %+v %v", item, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "result.json"), []byte(`{"status": "done"}`), 0666); err != nil {
		t.Fatal(err)
	}
	first, err := queueNext(dir)
	if err != nil || first == nil || first.ID != 1 || first.State != "running" {
		t.Fatalf("next: %+v %v", first, err)
	}
	if !fileExists(filepath.Join(dir, "items", "0", "result.json")) || fileExists(filepath.Join(dir, "result.json")) {
		t.Fatal("next must archive the previous result.json")
	}
	if _, err := queueNext(dir); err == nil {
		t.Fatal("next must refuse while an item is running")
	}
	if code := queueCommand([]string{"--shift", dir, "drop", "1"}); code == 0 {
		t.Fatal("drop must refuse a running item")
	}
	if code := queueCommand([]string{"--shift", dir, "done", "1"}); code != 0 {
		t.Fatalf("done exit %d", code)
	}
	if code := queueCommand([]string{"--shift", dir, "drop", "2"}); code != 0 {
		t.Fatalf("drop exit %d", code)
	}
	items := readQueue(t, dir)
	if items[0].State != "landed" || items[1].State != "dropped" {
		t.Fatalf("states: %+v", items)
	}
	if next, err := queueNext(dir); err != nil || next != nil {
		t.Fatalf("empty queue next: %+v %v", next, err)
	}
}

func TestLiveShiftIdleNeedsResultEmptyQueueAndQuiet(t *testing.T) {
	dir := t.TempDir()
	if liveShiftIdle(dir) {
		t.Fatal("no result.json: not idle")
	}
	old := time.Now().Add(-10 * time.Minute)
	for _, name := range []string{"result.json", "bus.jsonl"} {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte("{}\n"), 0666); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(p, old, old); err != nil {
			t.Fatal(err)
		}
	}
	if !liveShiftIdle(dir) {
		t.Fatal("finished, empty and quiet for 10 minutes: idle")
	}
	if _, _, err := queueAdd(dir, "more work", ""); err != nil {
		t.Fatal(err)
	}
	if liveShiftIdle(dir) {
		t.Fatal("a queued item keeps the shift alive")
	}
}
