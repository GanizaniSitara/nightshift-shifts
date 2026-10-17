package main

import (
	"encoding/json"
	"strconv"
	"testing"
)

// waitRow builds a mailbox row the way queryMessages returns it: numbers as json.Number, NULL as nil.
func waitRow(id int, recipient, state string, inc any) object {
	return object{
		{"id", json.Number(strconv.Itoa(id))},
		{"kind", "note"},
		{"sender", "Foreman"},
		{"recipient", recipient},
		{"recipient_incarnation", inc},
		{"body", "body"},
		{"state", state},
	}
}

func matchedID(t *testing.T, got object) string {
	t.Helper()
	if got == nil {
		return "<nil>"
	}
	v, _ := objectGet(got, "id")
	return valueText(v)
}

func TestWaitMatchPicksLowestIDRegardlessOfOrder(t *testing.T) {
	rows := []object{
		waitRow(9, "Tester", "stored", json.Number("400.0")),
		waitRow(3, "Tester", "sent", json.Number("400.0")),
		waitRow(5, "Tester", "stored", json.Number("400.0")),
	}
	if id := matchedID(t, waitMatch(rows, "Tester", 400.0, true)); id != "3" {
		t.Fatalf("want lowest id 3, got %s", id)
	}
}

func TestWaitMatchOnlyStoredOrSent(t *testing.T) {
	rows := []object{
		waitRow(1, "Tester", "acknowledged", json.Number("400.0")),
		waitRow(2, "Tester", "bogus", json.Number("400.0")),
		waitRow(3, "Tester", "", json.Number("400.0")),
		waitRow(4, "Tester", "sent", json.Number("400.0")),
	}
	if id := matchedID(t, waitMatch(rows, "Tester", 400.0, true)); id != "4" {
		t.Fatalf("want 4 (first stored/sent), got %s", id)
	}
	stored := []object{waitRow(7, "Tester", "stored", json.Number("400.0"))}
	if id := matchedID(t, waitMatch(stored, "Tester", 400.0, true)); id != "7" {
		t.Fatalf("stored row must match, got %s", id)
	}
}

func TestWaitMatchSkipsAcknowledged(t *testing.T) {
	rows := []object{
		waitRow(1, "Tester", "acknowledged", json.Number("400.0")),
		waitRow(2, "Tester", "acknowledged", json.Number("400.0")),
	}
	if got := waitMatch(rows, "Tester", 400.0, true); got != nil {
		t.Fatalf("acknowledged rows must never match, got %s", matchedID(t, got))
	}
}

func TestWaitMatchSkipsOtherRecipient(t *testing.T) {
	rows := []object{
		waitRow(1, "Coder", "stored", json.Number("400.0")),
		waitRow(2, "Foreman", "sent", json.Number("400.0")),
		waitRow(3, "tester", "stored", json.Number("400.0")), // labels are canonical; no case folding here
		waitRow(4, "Tester", "stored", json.Number("400.0")),
	}
	if id := matchedID(t, waitMatch(rows, "Tester", 400.0, true)); id != "4" {
		t.Fatalf("want 4, got %s", id)
	}
}

func TestWaitMatchSkipsEarlierIncarnation(t *testing.T) {
	rows := []object{
		waitRow(1, "Tester", "stored", json.Number("399.0")),
		waitRow(2, "Tester", "sent", nil), // NULL incarnation is not the current one when the current is known
		waitRow(3, "Tester", "stored", json.Number("400.0")),
	}
	if id := matchedID(t, waitMatch(rows, "Tester", 400.0, true)); id != "3" {
		t.Fatalf("want 3, got %s", id)
	}
	if got := waitMatch(rows[:2], "Tester", 400.0, true); got != nil {
		t.Fatalf("earlier/NULL incarnation must not match, got %s", matchedID(t, got))
	}
}

func TestWaitMatchNilIncarnationMatchesNil(t *testing.T) {
	rows := []object{
		waitRow(1, "Tester", "stored", json.Number("400.0")),
		waitRow(2, "Tester", "stored", nil),
	}
	if id := matchedID(t, waitMatch(rows, "Tester", 0, false)); id != "2" {
		t.Fatalf("nil current incarnation must match only NULL rows: want 2, got %s", id)
	}
}

func TestWaitMatchIncarnationComparesNumerically(t *testing.T) {
	// SQLite REAL text forms differ ("400" vs "400.0", float64 vs json.Number); comparison is by value.
	rows := []object{waitRow(1, "Tester", "stored", float64(1792261807.715952))}
	if id := matchedID(t, waitMatch(rows, "Tester", 1792261807.715952, true)); id != "1" {
		t.Fatalf("float64 incarnation must match by value, got %s", id)
	}
	rows = []object{waitRow(1, "Tester", "stored", json.Number("400"))}
	if id := matchedID(t, waitMatch(rows, "Tester", 400, true)); id != "1" {
		t.Fatalf("400 and 400.0 are the same incarnation, got %s", id)
	}
}

func TestWaitMatchEmptyIsNil(t *testing.T) {
	if got := waitMatch(nil, "Tester", 400.0, true); got != nil {
		t.Fatalf("nil rows: want nil, got %v", got)
	}
	if got := waitMatch([]object{}, "Tester", 400.0, true); got != nil {
		t.Fatalf("empty rows: want nil, got %v", got)
	}
}
