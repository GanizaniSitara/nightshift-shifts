package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/text/cases"
	"golang.org/x/text/language"
)

type claimConflictError struct{}

func (e claimConflictError) Error() string { return "" }
func (e claimConflictError) ExitCode() int { return 4 }

func claimUsage(msg string) int {
	errLine("usage: nightshift claim [-h] shift role paths [paths ...]")
	errLine("nightshift claim: error: " + msg)
	return 2
}

func releaseUsage(msg string) int {
	errLine("usage: nightshift release [-h] shift role [paths ...]")
	errLine("nightshift release: error: " + msg)
	return 2
}

func claimHelp() string {
	return "usage: nightshift claim [-h] shift role paths [paths ...]\n\n" +
		"Claim files for a role before editing them. All or nothing: if any path is\n" +
		"held by another role, nothing is claimed and the exit code is 4. Re-claiming\n" +
		"your own path refreshes its time.\n\n" +
		"positional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n" +
		"  role        who holds the claim, e.g. the Coder session name\n  paths\n\n" +
		"options:\n  -h, --help  show this help message and exit\n"
}

func releaseHelp() string {
	return "usage: nightshift release [-h] shift role [paths ...]\n\n" +
		"Release a role's claims: the given paths, or all of them when none are given.\n" +
		"Paths held by another role are left alone.\n\n" +
		"positional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n" +
		"  role\n  paths\n\noptions:\n  -h, --help  show this help message and exit\n"
}

func parseClaimArgs(args []string, release bool) ([]string, bool, string) {
	positional := []string{}
	extra := []string{}
	terminated := false
	for _, arg := range args {
		if !terminated && arg == "--" {
			terminated = true
			continue
		}
		if !terminated && (arg == "-h" || (strings.HasPrefix(arg, "--") && strings.HasPrefix("--help", arg))) {
			return nil, true, ""
		}
		if !terminated && strings.HasPrefix(arg, "-") && arg != "-" {
			extra = append(extra, arg)
			continue
		}
		positional = append(positional, arg)
	}
	requiredNames := []string{"shift", "role"}
	if !release {
		requiredNames = append(requiredNames, "paths")
	}
	if len(positional) < len(requiredNames) {
		return nil, false, "the following arguments are required: " + strings.Join(requiredNames[len(positional):], ", ")
	}
	if len(extra) != 0 {
		return nil, false, "unrecognized arguments: " + strings.Join(extra, " ")
	}
	return positional, false, ""
}

func claimRole(v any) string {
	o, ok := v.(object)
	if !ok {
		return ""
	}
	value, exists := objectGet(o, "role")
	if !exists {
		return ""
	}
	return pythonValueString(value)
}

func sameClaimRole(v any, role string) bool {
	return pythonLowerString(claimRole(v)) == pythonLowerString(role)
}

func pythonLowerString(value string) string {
	return cases.Lower(language.Und, cases.HandleFinalSigma(true)).String(value)
}

func claimsWithValue(claims object, key string, value any) object {
	for i := range claims {
		if claims[i].key == key {
			claims[i].val = value
			return claims
		}
	}
	return append(claims, pair{key: key, val: value})
}

func claimsWithout(claims object, key string) object {
	for i := range claims {
		if claims[i].key == key {
			return append(claims[:i], claims[i+1:]...)
		}
	}
	return claims
}

func writeClaims(dir string, claims object) error {
	path := filepath.Join(dir, "claims.json")
	tmp := filepath.Join(dir, fmt.Sprintf("claims.%d.tmp", os.Getpid()))
	data := strings.ReplaceAll(renderJSON(claims, true, true), "\n", "\r\n")
	if err := os.WriteFile(tmp, []byte(data), 0600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func claimPaths(args []string) ([]string, error) {
	paths := make([]string, 0, len(args))
	seen := map[string]bool{}
	for _, path := range args {
		normalized, err := normalizedClaimPath(path)
		if err != nil {
			return nil, err
		}
		if !seen[normalized] {
			seen[normalized] = true
			paths = append(paths, normalized)
		}
	}
	return paths, nil
}

func otherLiveClaims(dir string, paths []string, conflicts map[string]bool) error {
	for _, otherDir := range shiftDirs() {
		if otherDir == dir {
			continue
		}
		record, err := statusRecord(otherDir)
		if err != nil || !objectBool(record, "alive") {
			continue
		}
		claims := readClaims(otherDir)
		for _, path := range paths {
			held, ok := objectGet(claims, path)
			if !ok {
				continue
			}
			errLine(fmt.Sprintf("%s is already claimed by %s (shift %s) since %s",
				path, claimOwner(held), filepath.Base(otherDir), claimTime(held)))
			conflicts[path] = true
		}
	}
	return nil
}

func validWorktree(dir string) bool {
	o, ok := readObject(filepath.Join(dir, "worktree.json"))
	return ok && len(o) != 0
}

func claimTime(v any) string {
	o, ok := v.(object)
	if !ok {
		return "None"
	}
	value, exists := objectGet(o, "time")
	if !exists {
		return "None"
	}
	return pythonValueString(value)
}

func claimOwner(v any) string {
	o, ok := v.(object)
	if !ok {
		return "None"
	}
	value, exists := objectGet(o, "role")
	if !exists {
		return "None"
	}
	return pythonValueString(value)
}

func pythonValueString(value any) string {
	if value == nil {
		return "None"
	}
	switch x := value.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number:
		return numberText(x)
	default:
		return fmt.Sprint(x)
	}
}

func claimCommand(args []string) int {
	positional, help, parseError := parseClaimArgs(args, false)
	if help {
		out(claimHelp())
		return 0
	}
	if parseError != "" {
		return claimUsage(parseError)
	}
	dir, err := resolveShift(positional[0])
	if err != nil {
		return commandExit(err)
	}
	paths, err := claimPaths(positional[2:])
	if err != nil {
		return commandExit(err)
	}
	role := positional[1]
	claims := object{}
	err = withClaimsLock(dir, func() error {
		claims = readClaims(dir)
		conflicts := map[string]bool{}
		for _, path := range paths {
			held, ok := objectGet(claims, path)
			if !ok || sameClaimRole(held, role) {
				continue
			}
			errLine(fmt.Sprintf("%s is already claimed by %s since %s", path, claimOwner(held), claimTime(held)))
			conflicts[path] = true
		}
		if !validWorktree(dir) {
			if err := otherLiveClaims(dir, paths, conflicts); err != nil {
				return err
			}
		}
		if len(conflicts) != 0 {
			return claimConflictError{}
		}
		now := time.Now().Format("2006-01-02 15:04:05")
		for _, path := range paths {
			claims = claimsWithValue(claims, path, object{{"role", role}, {"time", now}})
		}
		return writeClaims(dir, claims)
	})
	if err != nil {
		return commandExit(err)
	}
	printResult(object{{"shift", filepath.Base(dir)}, {"role", role}, {"claimed", stringsToAny(paths)}}, false, true)
	return 0
}

func releaseCommand(args []string) int {
	positional, help, parseError := parseClaimArgs(args, true)
	if help {
		out(releaseHelp())
		return 0
	}
	if parseError != "" {
		return releaseUsage(parseError)
	}
	dir, err := resolveShift(positional[0])
	if err != nil {
		return commandExit(err)
	}
	wanted, err := claimPaths(positional[2:])
	if err != nil {
		return commandExit(err)
	}
	wantedSet := map[string]bool{}
	for _, path := range wanted {
		wantedSet[path] = true
	}
	role := positional[1]
	released := []string{}
	err = withClaimsLock(dir, func() error {
		claims := readClaims(dir)
		for _, claim := range claims {
			if !sameClaimRole(claim.val, role) || (len(wantedSet) != 0 && !wantedSet[claim.key]) {
				continue
			}
			released = append(released, claim.key)
		}
		for _, path := range released {
			claims = claimsWithout(claims, path)
		}
		if len(released) != 0 {
			return writeClaims(dir, claims)
		}
		return nil
	})
	if err != nil {
		return commandExit(err)
	}
	printResult(object{{"released", stringsToAny(released)}}, false, true)
	return 0
}

func stringsToAny(values []string) []any {
	out := make([]any, len(values))
	for i, value := range values {
		out[i] = value
	}
	return out
}
