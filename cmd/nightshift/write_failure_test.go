package main

import "testing"

// buildSignature (NGH-061): a stable hash of the exit code plus the log lines that name failures (pytest
// "FAILED ..." / go "--- FAIL:"), falling back to the last 5 non-empty lines with digits and timestamps normalised.

func TestBuildSignatureIsStableAndNonEmpty(t *testing.T) {
	log := "collected 3 items\nFAILED test_a.py::test_one - assert 1 == 2\n1 failed in 0.12s\n"
	a, b := buildSignature(1, log), buildSignature(1, log)
	if a == "" || a != b {
		t.Fatalf("signature must be non-empty and deterministic: %q %q", a, b)
	}
}

func TestBuildSignatureDependsOnExitCode(t *testing.T) {
	log := "FAILED test_a.py::test_one\n"
	if buildSignature(1, log) == buildSignature(2, log) {
		t.Fatal("different exit codes must give different signatures")
	}
}

func TestBuildSignaturePytestFailuresIgnoreTimingNoise(t *testing.T) {
	a := "=== 10 passed, 1 failed in 3.21s ===\nFAILED test_a.py::test_one - assert 1 == 2\nrun at 2026-10-17 20:01:02\n"
	b := "=== 11 passed, 1 failed in 9.99s ===\nFAILED test_a.py::test_one - assert 1 == 2\nrun at 2026-10-18 07:08:09\n"
	if buildSignature(1, a) != buildSignature(1, b) {
		t.Fatal("same FAILED line must give the same signature regardless of surrounding counts and times")
	}
}

func TestBuildSignatureDistinguishesDifferentFailures(t *testing.T) {
	a := "FAILED test_a.py::test_one - assert 1 == 2\n"
	b := "FAILED test_a.py::test_two - assert 1 == 2\n"
	if buildSignature(1, a) == buildSignature(1, b) {
		t.Fatal("different failing tests must give different signatures")
	}
}

func TestBuildSignatureGoFailLines(t *testing.T) {
	a := "=== RUN   TestX\n--- FAIL: TestX (0.01s)\n    x_test.go:12: boom\nFAIL\nFAIL\tpkg\t0.123s\n"
	b := "=== RUN   TestX\n--- FAIL: TestX (0.07s)\n    x_test.go:12: boom\nFAIL\nFAIL\tpkg\t4.567s\n"
	c := "=== RUN   TestY\n--- FAIL: TestY (0.01s)\nFAIL\n"
	if buildSignature(1, a) != buildSignature(1, b) {
		t.Fatal("go --- FAIL lines differing only in durations must match")
	}
	if buildSignature(1, a) == buildSignature(1, c) {
		t.Fatal("different go test failures must differ")
	}
}

func TestBuildSignatureFallbackNormalisesDigitsAndTimestamps(t *testing.T) {
	a := "starting\nstep 1 of 3 at 2026-10-17 20:01:02\nerror: compile failed after 12 tries\n\n"
	b := "starting\nstep 2 of 4 at 2026-10-18 21:11:12\nerror: compile failed after 99 tries\n"
	if buildSignature(2, a) != buildSignature(2, b) {
		t.Fatal("fallback lines that differ only in digits/timestamps must match")
	}
	c := "starting\nstep 1 of 3 at 2026-10-17 20:01:02\nerror: link failed after 12 tries\n"
	if buildSignature(2, a) == buildSignature(2, c) {
		t.Fatal("fallback lines with different text must differ")
	}
}

func TestBuildSignatureFallbackUsesOnlyLastFiveLines(t *testing.T) {
	tail := "l1\nl2\nl3\nl4\nerror: boom\n"
	a := "early noise A\n" + tail
	b := "completely different early noise\nmore\n" + tail
	if buildSignature(1, a) != buildSignature(1, b) {
		t.Fatal("only the last 5 non-empty lines count in the fallback")
	}
}

func TestBuildSignatureFailureLinesWinOverTail(t *testing.T) {
	a := "FAILED t.py::test_one\ntail line one\n"
	b := "FAILED t.py::test_one\nanother tail entirely\n"
	if buildSignature(1, a) != buildSignature(1, b) {
		t.Fatal("when FAILED lines exist the signature must not depend on the other tail lines")
	}
}

func TestBuildSignatureHandlesCRLFAndEmptyLog(t *testing.T) {
	if buildSignature(1, "FAILED t.py::a\r\n") != buildSignature(1, "FAILED t.py::a\n") {
		t.Fatal("CRLF and LF logs must give the same signature")
	}
	if buildSignature(1, "") == "" {
		t.Fatal("an empty log still has a signature (the exit code)")
	}
}
