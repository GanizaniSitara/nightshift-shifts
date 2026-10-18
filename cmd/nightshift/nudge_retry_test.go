package main

import "testing"

// NGH-065 (Foreman msg 137): nudgeRetryDue(now, lastAttempt, attempts) says whether the warden may re-nudge an
// unacked row now: the first retry is due at once (the row is already 10 minutes old), later ones only when at
// least 600 seconds have passed since the last attempt.

func TestNudgeRetryDueFirstRetryImmediately(t *testing.T) {
	if !nudgeRetryDue(10_000, 0, 0) {
		t.Fatal("no attempt yet: due")
	}
}

func TestNudgeRetryDueSpacing(t *testing.T) {
	cases := []struct {
		since    float64
		attempts int
		want     bool
	}{
		{0, 1, false}, {59, 1, false}, {599.9, 1, false}, {600, 1, true}, {601, 1, true},
		{300, 2, false}, {600, 2, true}, {3600, 2, true},
	}
	for _, c := range cases {
		now := 100_000.0
		if got := nudgeRetryDue(now, now-c.since, c.attempts); got != c.want {
			t.Errorf("since %v s, attempts %d: due=%v, want %v", c.since, c.attempts, got, c.want)
		}
	}
}

func TestNudgeRetryDueClockSkewIsNotDue(t *testing.T) {
	// a last attempt recorded in the future (clock moved back) must not trigger an immediate retry
	if nudgeRetryDue(1_000, 2_000, 1) {
		t.Fatal("negative elapsed time is not 10 minutes")
	}
}

func TestNudgeRetryStopsAfterThree(t *testing.T) {
	// Foreman msg 148: the third retry raises member-stalled; there is no fourth.
	for _, since := range []float64{600, 3600, 86400} {
		if nudgeRetryDue(100_000, 100_000-since, 3) || nudgeRetryDue(100_000, 100_000-since, 4) {
			t.Fatalf("attempts >= 3 must never be due (since %v s)", since)
		}
	}
}
