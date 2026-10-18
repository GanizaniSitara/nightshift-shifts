package main

import (
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"unicode/utf16"
)

// Use a real harmless child to check the preflight boundary against CreateProcess,
// including argument decoding, rather than only repeating the length calculation.
func TestIndependentWindowsLimitMatchesChildLaunch(t *testing.T) {
	for _, suffix := range []string{"", " 😀 \"quoted\" \\", "\t\\\"\\\\"} {
		t.Run(fmt.Sprintf("suffix_%x", suffix), func(t *testing.T) {
			command := []string{os.Args[0], "-test.run=^TestIndependentCommandLineChild$", "--", suffix}
			quoted := make([]string, len(command))
			for i, arg := range command {
				quoted[i] = syscall.EscapeArg(arg)
			}
			units := len(utf16.Encode([]rune(strings.Join(quoted, " ")))) + 1
			// Replacing the quoted empty argument also removes its two quotes.
			padding := 32767 - units
			if suffix == "" {
				padding += 2
			}
			command[3] = strings.Repeat("x", padding) + suffix
			if err := checkMemberCommand("Tester", command); err != nil {
				t.Fatalf("last legal command rejected: %v", err)
			}
			child := exec.Command(command[0], command[1:]...)
			child.Env = append(os.Environ(), "NIGHTSHIFT_LIMIT_CHILD="+fmt.Sprintf("%x", sha256.Sum256([]byte(command[3]))))
			if output, err := child.CombinedOutput(); err != nil {
				t.Fatalf("legal child failed: %v, %s", err, output)
			}
			command[3] = "x" + command[3]
			if err := checkMemberCommand("Tester", command); err == nil {
				t.Fatal("one-unit overflow passed preflight")
			}
			child = exec.Command(command[0], command[1:]...)
			if output, err := child.CombinedOutput(); err == nil {
				t.Fatalf("one-unit overflow unexpectedly launched: %s", output)
			}
		})
	}
}

func TestIndependentCommandLineChild(t *testing.T) {
	if want := os.Getenv("NIGHTSHIFT_LIMIT_CHILD"); want != "" {
		if len(os.Args) != 4 || fmt.Sprintf("%x", sha256.Sum256([]byte(os.Args[3]))) != want {
			t.Fatal("Windows argument decoding changed the payload")
		}
	}
}
