package main

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

func loadDenials(stateDir string) []any {
	value, ok := readJSONFile(filepath.Join(stateDir, "denials.json"))
	if !ok {
		return []any{}
	}
	denials, ok := value.([]any)
	if !ok {
		return []any{}
	}
	return denials
}

func recordDenials(stateDir string, tails object) ([]any, error) {
	denials := loadDenials(stateDir)
	seen := map[string]bool{}
	for _, value := range denials {
		entry, ok := value.(object)
		if !ok {
			continue
		}
		seen[objectString(entry, "role")+"\x00"+objectString(entry, "line")] = true
	}
	added := false
	for _, pane := range tails {
		role := pane.key
		var lines []string
		switch values := pane.val.(type) {
		case []string:
			lines = values
		case []any:
			for _, value := range values {
				if line, ok := value.(string); ok {
					lines = append(lines, line)
				}
			}
		}
		for _, line := range lines {
			trimmed := strings.TrimSpace(line)
			if !strings.Contains(strings.ToLower(line), "denied") {
				continue
			}
			key := role + "\x00" + trimmed
			if seen[key] {
				continue
			}
			seen[key] = true
			denials = append(denials, object{{"time", time.Now().Format("2006-01-02 15:04:05")}, {"role", role}, {"line", trimmed}})
			added = true
		}
	}
	if added {
		if err := writeJSONFile(filepath.Join(stateDir, "denials.json"), denials); err != nil {
			return denials, err
		}
	}
	return denials, nil
}

func paneTail(pid uint32, lines int) []string {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var stdout bytes.Buffer
	peek := exec.CommandContext(ctx, "agents-cli", "_attach", "peek", strconv.FormatUint(uint64(pid), 10), "0")
	peek.Stdout = &stdout
	if err := peek.Run(); err != nil && ctx.Err() != nil {
		return []string{}
	}
	value, err := parseJSON(stdout.Bytes())
	if err != nil {
		return []string{}
	}
	screen, ok := value.(object)
	if !ok {
		return []string{}
	}
	value, ok = objectGet(screen, "lines")
	if !ok {
		return []string{}
	}
	all, ok := value.([]any)
	if !ok {
		return []string{}
	}
	visible := make([]string, 0, len(all))
	for _, item := range all {
		line, ok := item.(string)
		if ok && strings.TrimSpace(line) != "" {
			visible = append(visible, line)
		}
	}
	start := 0
	if lines != 0 {
		start = len(visible) - lines
		if start < 0 {
			start = 0
		}
	}
	if start > len(visible) {
		start = len(visible)
	}
	return visible[start:]
}

func handoverText(value any) string {
	switch value := value.(type) {
	case nil:
		return "None"
	case bool:
		if value {
			return "True"
		}
		return "False"
	default:
		return valueText(value)
	}
}

func handoverValue(o object, key string) string { return handoverText(rowValue(o, key)) }

func handoverDefault(o object, key, fallback string) string {
	value, ok := objectGet(o, key)
	if !ok {
		return fallback
	}
	return handoverText(value)
}

func handoverAsg(value any) string {
	if value == nil {
		return "-"
	}
	if text, ok := value.(string); ok && text == "" {
		return "-"
	}
	return handoverText(value)
}

func handoverBuilds(stateDir string) []object {
	entries, err := os.ReadDir(filepath.Join(stateDir, "builds"))
	if err != nil {
		return []object{}
	}
	type buildFile struct {
		n    int
		path string
	}
	files := make([]buildFile, 0)
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		stem := strings.TrimSuffix(entry.Name(), ".json")
		if stem == "" {
			continue
		}
		for _, r := range stem {
			if r < '0' || r > '9' {
				stem = ""
				break
			}
		}
		if stem == "" {
			continue
		}
		n, err := strconv.Atoi(stem)
		if err == nil {
			files = append(files, buildFile{n: n, path: filepath.Join(stateDir, "builds", entry.Name())})
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].n < files[j].n })
	builds := make([]object, 0, len(files))
	for _, file := range files {
		value, ok := readJSONFile(file.path)
		if !ok {
			continue
		}
		build, ok := value.(object)
		if ok {
			builds = append(builds, build)
		}
	}
	return builds
}

func handoverCommits(cwd string, since time.Time) []string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "git", "-C", cwd, "log", "--since="+since.Local().Format("2006-01-02 15:04:05"), "--oneline")
	output, err := command.Output()
	if err != nil {
		return []string{}
	}
	commits := make([]string, 0)
	for _, line := range strings.FieldsFunc(string(output), func(r rune) bool { return r == '\r' || r == '\n' }) {
		if strings.TrimSpace(line) != "" {
			commits = append(commits, line)
		}
	}
	return commits
}

func handoverReport(stateDir string, record object) (string, error) {
	lines := []string{"## Result", ""}
	resultValue, resultOK := readJSONFile(filepath.Join(stateDir, "result.json"))
	if result, ok := resultValue.(object); resultOK && ok {
		lines = append(lines,
			"Status: "+handoverDefault(result, "status", ""),
			fmt.Sprintf("Stage: %s (required: %s)", resultStage(result), requires(stateDir)))
		evidenceValue, _ := objectGet(result, "evidence")
		rawEvidence, _ := evidenceValue.([]any)
		evidence := make([]object, 0, len(rawEvidence))
		for _, value := range rawEvidence {
			if entry, ok := value.(object); ok {
				evidence = append(evidence, entry)
			}
		}
		if len(evidence) == 0 {
			lines = append(lines, "Evidence: (none)")
		} else {
			lines = append(lines, "Evidence:")
			for _, entry := range evidence {
				lines = append(lines, fmt.Sprintf("- %s: %s \u2014 %s", handoverValue(entry, "stage"), handoverValue(entry, "what"), handoverValue(entry, "outcome")))
			}
		}
		lines = append(lines, "Summary: "+handoverDefault(result, "summary", ""))
		changedValue, _ := objectGet(result, "changed")
		changed, _ := changedValue.([]any)
		if len(changed) == 0 {
			lines = append(lines, "Changed: (none)")
		} else {
			lines = append(lines, "Changed:")
			for _, value := range changed {
				lines = append(lines, "- "+handoverText(value))
			}
		}
	} else {
		lines = append(lines, "(no result.json)")
	}

	lines = append(lines, "", "## Auto-mode denials", "")
	denials := loadDenials(stateDir)
	if len(denials) == 0 {
		lines = append(lines, "(none)")
	} else {
		for _, value := range denials {
			entry, ok := value.(object)
			if ok {
				lines = append(lines, fmt.Sprintf("- %s %s: %s", handoverValue(entry, "time"), handoverValue(entry, "role"), handoverValue(entry, "line")))
			}
		}
	}

	lines = append(lines, "", "## Outstanding claims", "")
	claims := readClaims(stateDir)
	if len(claims) == 0 {
		lines = append(lines, "(none)")
	} else {
		for _, claim := range claims {
			entry, _ := claim.val.(object)
			lines = append(lines, fmt.Sprintf("- %s \u2014 %s (since %s)", claim.key, handoverValue(entry, "role"), handoverValue(entry, "time")))
		}
	}

	lines = append(lines, "", "## Unacknowledged messages", "")
	messages, err := readMessages(stateDir)
	if err != nil {
		return "", err
	}
	pending := make([]object, 0)
	for _, message := range messages {
		state := rowString(message, "state")
		if state == "stored" || state == "sent" {
			pending = append(pending, message)
		}
	}
	if len(pending) == 0 {
		lines = append(lines, "(none)")
	} else {
		for _, message := range pending {
			lines = append(lines, fmt.Sprintf("- msg:%s %s %s %s -> %s [%s]", handoverText(rowValue(message, "id")), rowString(message, "kind"), handoverAsg(rowValue(message, "asg")), rowString(message, "sender"), rowString(message, "recipient"), rowString(message, "state")))
		}
	}

	lines = append(lines, "", "## Builds", "")
	builds := handoverBuilds(stateDir)
	if len(builds) == 0 {
		lines = append(lines, "(none)")
	} else {
		for _, build := range builds {
			head := handoverValue(build, "head")
			if head == "None" || head == "" {
				head = "-"
			}
			headRunes := []rune(head)
			if len(headRunes) > 8 {
				head = string(headRunes[:8])
			}
			lines = append(lines, fmt.Sprintf("- #%s %s exit=%s head=%s dirty=%s %s", handoverValue(build, "n"), handoverValue(build, "status"), handoverValue(build, "exit"), head, handoverValue(build, "dirty"), handoverValue(build, "cmd")))
		}
	}

	lines = append(lines, "", "## Commits since shift start", "")
	launchPath := filepath.Join(stateDir, "launch.json")
	launchInfo, launchErr := os.Stat(launchPath)
	if launchErr != nil {
		lines = append(lines, "(no launch.json)")
	} else {
		rolesValue, _ := objectGet(record, "roles")
		roles, _ := rolesValue.(object)
		cwd := ""
		if foremanValue, ok := objectGet(roles, "Foreman"); ok {
			if foreman, ok := foremanValue.(object); ok {
				cwd = objectString(foreman, "cwd")
			}
		}
		if cwd == "" {
			for _, role := range roles {
				if info, ok := role.val.(object); ok {
					if candidate := objectString(info, "cwd"); candidate != "" {
						cwd = candidate
						break
					}
				}
			}
		}
		if cwd == "" {
			lines = append(lines, "(no crew cwd recorded)")
		} else {
			commits := handoverCommits(cwd, launchInfo.ModTime())
			if len(commits) == 0 {
				lines = append(lines, "(none)")
			} else {
				lines = append(lines, "```")
				lines = append(lines, commits...)
				lines = append(lines, "```")
			}
		}
	}

	lines = append(lines, "", "## Test evidence", "", "Recorded build and result evidence above; teardown does not run tests.", "")
	return strings.Join(lines, "\n"), nil
}
