package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"nightshift-readonly/internal/console"
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
	screen, err := console.Peek(pid, 0)
	if err != nil {
		return []string{}
	}
	visible := make([]string, 0, len(screen.Lines))
	for _, line := range screen.Lines {
		if strings.TrimSpace(line) != "" {
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

	lines = appendTokenUsage(lines, stateDir)

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

// `nightshift handover` (NGH-061): handover.md from live state, without capturing or stopping any pane, so a member
// or Foreman about to run out of context gets it for free. `down` keeps writing its own.

const handoverUsage = "usage: nightshift handover [-h] shift"

const handoverHelp = handoverUsage + `

Write <state_dir>/handover.md from live state: the result, claims, unacked
messages and builds that down records, plus active assignments, live waiters
and the last report of each member. Prints the path.

positional arguments:
  shift       shift name, unique suffix, or "latest"

options:
  -h, --help  show this help message and exit
`

func init() { extraCommands["handover"] = handoverCommand }

func handoverCommand(argv []string) int {
	args, message := parseWriteArgs(argv, nil, []string{"shift"}, nil)
	if args.help {
		out(handoverHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift handover", handoverUsage, message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return busExit(err)
	}
	record, err := statusRecord(dir)
	if err != nil {
		return busExit(err)
	}
	report, err := handoverReport(dir, record)
	if err != nil {
		return busExit(err)
	}
	live, err := handoverLive(dir)
	if err != nil {
		return busExit(err)
	}
	lines := []string{"# Handover: " + objectString(record, "shift"), "", "Task: " + objectString(record, "task"), "Written: " + time.Now().Format("2006-01-02 15:04"), ""}
	lines = append(lines, live...)
	lines = append(lines, strings.Split(report, "\n")...)
	path := filepath.Join(dir, "handover.md")
	if err := os.WriteFile(path, []byte(strings.ReplaceAll(strings.Join(lines, "\n"), "\n", "\r\n")), 0666); err != nil {
		return busExit(err)
	}
	outLine(path)
	return 0
}

// handoverLive is the live-state part of a handover: active assignments, waiters and each member's last report.
func handoverLive(dir string) ([]string, error) {
	rows, err := readMessages(dir)
	if err != nil {
		return nil, err
	}
	now := epochNow()
	age := func(r object) string {
		at, ok := rowNumber(r, "delivered_epoch")
		if !ok {
			return "not delivered"
		}
		return (time.Duration(now-numberFloat(at)) * time.Second).Round(time.Second).String()
	}
	lines := []string{"## Active assignments", ""}
	n := len(lines)
	for _, r := range dispatches(dir, rows) {
		if !dispatchDone(r, rows) {
			lines = append(lines, fmt.Sprintf("- %s -> %s (msg:%s %s, age %s)", handoverAsg(rowValue(r, "asg")), rowString(r, "recipient"), handoverText(rowValue(r, "id")), rowString(r, "kind"), age(r)))
		}
	}
	if len(lines) == n {
		lines = append(lines, "(none)")
	}

	lines = append(lines, "", "## Live waiters", "")
	n = len(lines)
	entries, _ := os.ReadDir(filepath.Join(dir, "waiting"))
	for _, entry := range entries {
		waiter, ok := readObject(filepath.Join(dir, "waiting", entry.Name()))
		if !ok || filepath.Ext(entry.Name()) != ".json" || !processRunning(processNumber(waiter, "pid")) {
			continue
		}
		sinceNumber, _ := objectNumber(waiter, "since")
		sinceEpoch := numberFloat(sinceNumber)
		since := time.Unix(int64(sinceEpoch), 0).Format("2006-01-02 15:04:05")
		lines = append(lines, fmt.Sprintf("- %s (pid %s, since %s)", strings.TrimSuffix(entry.Name(), ".json"), handoverValue(waiter, "pid"), since))
	}
	if len(lines) == n {
		lines = append(lines, "(none)")
	}

	lines = append(lines, "", "## Last report per member", "")
	last := map[string]object{}
	order := []string{}
	for _, r := range rows {
		if rowString(r, "kind") != "result" {
			continue
		}
		sender := rowString(r, "sender")
		if _, seen := last[sender]; !seen {
			order = append(order, sender)
		}
		last[sender] = r
	}
	if len(order) == 0 {
		lines = append(lines, "(none)")
	}
	for _, sender := range order {
		r := last[sender]
		first, _, _ := strings.Cut(rowString(r, "body"), "\n")
		lines = append(lines, fmt.Sprintf("- %s msg:%s asg:%s %s: %s", sender, handoverText(rowValue(r, "id")), handoverAsg(rowValue(r, "asg")), rowString(r, "created"), strings.TrimSpace(first)))
	}
	return append(lines, ""), nil
}
