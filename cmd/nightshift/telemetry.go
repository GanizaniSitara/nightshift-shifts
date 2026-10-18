package main

// Session discovery and transcript harvesting are deliberately separate from
// the launcher record. A live pane may update only its own current incarnation;
// down takes the final sample before it ends that pane.

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

func telemetryHome(home string) string {
	if home != "" {
		return home
	}
	if value := os.Getenv("NIGHTSHIFT_HOME"); value != "" {
		return value
	}
	return userHome()
}

func telemetrySlug(cwd string) string {
	var b strings.Builder
	for _, r := range cwd {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			b.WriteByte('-')
		}
	}
	return b.String()
}

func cleanPathEqual(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

// discoverRoleSession finds only files belonging to the given tool. Its empty
// result is intentional: compatibility records acquire no telemetry keys.
func discoverRoleSession(tool, home, cwd, roleName string, pid int, started float64) (string, string) {
	home = telemetryHome(home)
	switch strings.ToLower(tool) {
	case "claude", "":
		return discoverClaudeSession(home, cwd, pid)
	case "codex":
		return discoverCodexSession(home, cwd, started)
	case "copilot":
		return discoverCopilotSession(home, cwd, started)
	}
	return "", ""
}

func discoverClaudeSession(home, cwd string, pid int) (string, string) {
	if pid == 0 {
		return "", ""
	}
	session, ok := readObject(filepath.Join(home, ".claude", "sessions", strconv.Itoa(pid)+".json"))
	if !ok {
		return "", ""
	}
	id := objectString(session, "sessionId")
	if id == "" {
		return "", ""
	}
	if fromSession := objectString(session, "cwd"); fromSession != "" {
		cwd = fromSession
	}
	direct := filepath.Join(home, ".claude", "projects", telemetrySlug(cwd), id+".jsonl")
	if fileExists(direct) {
		return id, direct
	}
	matches, _ := filepath.Glob(filepath.Join(home, ".claude", "projects", "*", id+".jsonl"))
	if len(matches) > 0 {
		return id, matches[0]
	}
	return id, ""
}

type rolloutCandidate struct {
	id, cwd, path string
	when          time.Time
}

func startedTime(started float64) time.Time {
	return time.Unix(0, int64(started*float64(time.Second)))
}

func codexHomes(home string) []string {
	if configured := os.Getenv("CODEX_HOME"); configured != "" {
		return []string{configured}
	}
	entries, err := os.ReadDir(home)
	if err != nil {
		return []string{filepath.Join(home, ".codex")}
	}
	result := []string{}
	for _, entry := range entries {
		if entry.IsDir() && strings.HasPrefix(strings.ToLower(entry.Name()), ".codex") {
			result = append(result, filepath.Join(home, entry.Name()))
		}
	}
	if len(result) == 0 {
		result = append(result, filepath.Join(home, ".codex"))
	}
	return result
}

func firstJSONLine(path string) object {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	line, err := bufio.NewReader(f).ReadBytes('\n')
	if err != nil && len(line) == 0 {
		return nil
	}
	v, err := parseJSON(line)
	if err != nil {
		return nil
	}
	o, _ := v.(object)
	return o
}

func telemetryMetadata(path string) object {
	if filepath.Ext(path) != ".jsonl" {
		if record, ok := readObject(path); ok {
			return record
		}
	}
	return firstJSONLine(path)
}

func discoverCodexSession(home, cwd string, started float64) (string, string) {
	var candidates []rolloutCandidate
	for _, codexHome := range codexHomes(home) {
		root := filepath.Join(codexHome, "sessions")
		_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
			if err != nil || info == nil || info.IsDir() || !strings.HasPrefix(strings.ToLower(info.Name()), "rollout-") || filepath.Ext(info.Name()) != ".jsonl" {
				return nil
			}
			meta := firstJSONLine(path)
			payload, _ := rowValue(meta, "payload").(object)
			if objectString(meta, "type") != "session_meta" || !cleanPathEqual(objectString(payload, "cwd"), cwd) {
				return nil
			}
			when := info.ModTime()
			if stamp, err := time.Parse(time.RFC3339, objectString(payload, "timestamp")); err == nil {
				when = stamp
			}
			if started > 0 && (when.Before(startedTime(started).Add(-60*time.Second)) || when.After(startedTime(started).Add(5*time.Minute))) {
				return nil
			}
			if id := objectString(payload, "id"); id != "" {
				candidates = append(candidates, rolloutCandidate{id: id, cwd: cwd, path: path, when: when})
			}
			return nil
		})
	}
	sort.Slice(candidates, func(i, j int) bool {
		if started == 0 {
			return candidates[i].when.After(candidates[j].when)
		}
		a := candidates[i].when.Sub(startedTime(started))
		b := candidates[j].when.Sub(startedTime(started))
		if a < 0 {
			a = -a
		}
		if b < 0 {
			b = -b
		}
		return a < b
	})
	if len(candidates) == 0 {
		return "", ""
	}
	if started != 0 && len(candidates) > 1 {
		a := candidates[0].when.Sub(startedTime(started))
		b := candidates[1].when.Sub(startedTime(started))
		if a < 0 {
			a = -a
		}
		if b < 0 {
			b = -b
		}
		if a == b {
			return "", ""
		}
	}
	return candidates[0].id, candidates[0].path
}

func discoverCopilotSession(home, cwd string, started float64) (string, string) {
	root := filepath.Join(home, ".copilot", "session-state")
	var found string
	if info, err := os.Stat(root); err == nil && !info.IsDir() {
		if started == 0 || !info.ModTime().Before(startedTime(started).Add(-60*time.Second)) {
			meta := telemetryMetadata(root)
			candidateCwd, id := objectString(meta, "cwd"), objectString(meta, "sessionId")
			if payload, ok := rowValue(meta, "payload").(object); ok {
				candidateCwd = objectString(payload, "cwd")
				if id == "" {
					id = objectString(payload, "id")
				}
			}
			if id == "" {
				id = objectString(meta, "id")
			}
			if id != "" && cleanPathEqual(candidateCwd, cwd) {
				return id, root
			}
		}
	}
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if found != "" || err != nil || info == nil || info.IsDir() || (filepath.Ext(info.Name()) != ".json" && filepath.Ext(info.Name()) != ".jsonl") {
			return nil
		}
		if started > 0 && info.ModTime().Before(startedTime(started).Add(-60*time.Second)) {
			return nil
		}
		meta := telemetryMetadata(path)
		candidateCwd := objectString(meta, "cwd")
		id := objectString(meta, "sessionId")
		if payload, ok := rowValue(meta, "payload").(object); ok {
			candidateCwd = objectString(payload, "cwd")
			if id == "" {
				id = objectString(payload, "id")
			}
		}
		if !cleanPathEqual(candidateCwd, cwd) {
			return nil
		}
		if id == "" {
			id = objectString(meta, "id")
		}
		if id != "" {
			found = id + "\x00" + path
		}
		return nil
	})
	if found != "" {
		parts := strings.SplitN(found, "\x00", 2)
		return parts[0], parts[1]
	}
	return "", ""
}

func usageInt(o object, key string) int64 {
	n, ok := objectNumber(o, key)
	if !ok {
		return 0
	}
	v, _ := n.Int64()
	return v
}

func usageObject(input, cached, output, reasoning, total, turns int64) object {
	return object{{"input_tokens", input}, {"cached_input_tokens", cached}, {"output_tokens", output}, {"reasoning_output_tokens", reasoning}, {"total_tokens", total}, {"turns", turns}}
}

func harvestTranscript(path, tool string) object {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	if strings.EqualFold(tool, "codex") {
		return harvestCodexTranscript(f)
	}
	return harvestClaudeTranscript(f)
}

func harvestClaudeTranscript(f *os.File) object {
	last := map[string]object{}
	anonymous := 0
	reader := bufio.NewReader(f)
	for {
		line, readErr := reader.ReadBytes('\n')
		v, err := parseJSON(line)
		record, ok := v.(object)
		if err == nil && ok && objectString(record, "type") == "assistant" {
			message, messageOK := rowValue(record, "message").(object)
			usage, usageOK := rowValue(message, "usage").(object)
			if messageOK && usageOK {
				id := objectString(message, "id")
				if id == "" {
					anonymous++
					id = fmt.Sprintf("anonymous-%d", anonymous)
				}
				last[id] = usage
			}
		}
		if readErr != nil {
			break
		}
	}
	if len(last) == 0 {
		return nil
	}
	var input, cached, output int64
	for _, usage := range last {
		input += usageInt(usage, "input_tokens")
		cached += usageInt(usage, "cache_read_input_tokens") + usageInt(usage, "cache_creation_input_tokens")
		output += usageInt(usage, "output_tokens")
	}
	return usageObject(input, cached, output, 0, input+cached+output, int64(len(last)))
}

func harvestCodexTranscript(f *os.File) object {
	var latest object
	var turns int64
	reader := bufio.NewReader(f)
	for {
		line, readErr := reader.ReadBytes('\n')
		v, err := parseJSON(line)
		record, ok := v.(object)
		if err == nil && ok {
			if objectString(record, "type") == "turn_context" {
				turns++
			}
			if objectString(record, "type") == "event_msg" {
				payload, _ := rowValue(record, "payload").(object)
				info, _ := rowValue(payload, "info").(object)
				if objectString(payload, "type") == "token_count" {
					if total, totalOK := rowValue(info, "total_token_usage").(object); totalOK {
						latest = total
					}
				}
			}
		}
		if readErr != nil {
			break
		}
	}
	if latest == nil {
		return nil
	}
	return usageObject(usageInt(latest, "input_tokens"), usageInt(latest, "cached_input_tokens"), usageInt(latest, "output_tokens"), usageInt(latest, "reasoning_output_tokens"), usageInt(latest, "total_tokens"), turns)
}

func withTelemetryLock(stateDir, role string, fn func() error) error {
	// A named mutex is released by Windows if its owning pane crashes; unlike an
	// O_EXCL lock file it cannot strand subsequent respawns behind stale state.
	return withMailboxMutexTimeout(filepath.Join(stateDir, role+".telemetry"), 5000, fn)
}

func writeRoleRecord(stateDir, role string, record object, preserveHistory bool) (object, error) {
	err := withTelemetryLock(stateDir, role, func() error {
		path := filepath.Join(stateDir, role+".json")
		if preserveHistory {
			if previous, ok := readObject(path); ok && len(previous) > 0 {
				record = setValue(record, "history", earlierIncarnations(previous))
			}
		}
		return os.WriteFile(path, []byte(renderJSON(record, false, true)), 0666)
	})
	return record, err
}

func refreshRoleTelemetry(stateDir, role string, expectedPID uint32, expectedStarted float64, harvestUsage bool) error {
	return withTelemetryLock(stateDir, role, func() error {
		path := filepath.Join(stateDir, role+".json")
		record, ok := readObject(path)
		if !ok {
			return nil
		}
		pid := processNumber(record, "pid")
		started, _ := objectNumber(record, "started")
		if expectedPID != 0 && (pid != expectedPID || (expectedStarted != 0 && numberFloat(started) != expectedStarted)) {
			return nil
		}
		changed := false
		if objectString(record, "session_id") == "" || objectString(record, "transcript") == "" {
			id, transcript := discoverRoleSession(objectString(record, "tool"), "", objectString(record, "cwd"), role, int(pid), numberFloat(started))
			if id != "" && objectString(record, "session_id") == "" {
				record = setValue(record, "session_id", id)
				changed = true
			}
			if transcript != "" && objectString(record, "transcript") == "" && (objectString(record, "session_id") == "" || objectString(record, "session_id") == id) {
				record = setValue(record, "transcript", transcript)
				changed = true
			}
		}
		if harvestUsage && objectString(record, "transcript") != "" {
			if usage := harvestTranscript(objectString(record, "transcript"), objectString(record, "tool")); usage != nil {
				record = setValue(record, "usage", usage)
				changed = true
			}
		}
		if !changed {
			return nil
		}
		// Re-read under the lock so a replacement incarnation can never be overwritten.
		current, ok := readObject(path)
		if !ok || processNumber(current, "pid") != pid {
			return nil
		}
		currentStarted, _ := objectNumber(current, "started")
		if numberFloat(currentStarted) != numberFloat(started) {
			return nil
		}
		for _, key := range []string{"session_id", "transcript", "usage"} {
			if value := rowValue(record, key); value != nil && (rowValue(current, key) == nil || (key == "usage" && harvestUsage)) {
				current = setValue(current, key, value)
			}
		}
		return writeJSONFile(path, current)
	})
}

func watchRoleTelemetry(child *paneChild, stateDir, role string, pid uint32, started float64) {
	for {
		_ = refreshRoleTelemetry(stateDir, role, pid, started, false)
		select {
		case <-child.done:
			_ = refreshRoleTelemetry(stateDir, role, pid, started, false)
			return
		case <-time.After(time.Second):
		}
	}
}

func harvestShiftTelemetry(dir string, record object) {
	_ = record
	for _, member := range shiftRoleRecords(dir) {
		role := member.key
		_ = refreshRoleTelemetry(dir, role, 0, 0, true)
		_ = withTelemetryLock(dir, role, func() error {
			path := filepath.Join(dir, role+".json")
			record, ok := readObject(path)
			if !ok {
				return nil
			}
			history, _ := rowValue(record, "history").([]any)
			changed := false
			for i, entry := range history {
				prior, ok := entry.(object)
				if !ok || objectString(prior, "transcript") == "" {
					continue
				}
				if usage := harvestTranscript(objectString(prior, "transcript"), objectString(prior, "tool")); usage != nil {
					history[i] = setValue(prior, "usage", usage)
					changed = true
				}
			}
			if changed {
				record = setValue(record, "history", history)
				return writeJSONFile(path, record)
			}
			return nil
		})
	}
}

func shiftRoleRecords(stateDir string) object {
	entries, err := os.ReadDir(stateDir)
	if err != nil {
		return nil
	}
	roles := object{}
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		record, ok := readObject(filepath.Join(stateDir, entry.Name()))
		role := objectString(record, "role")
		if !ok || role == "" {
			continue
		}
		roles = append(roles, pair{role, record})
	}
	return roles
}

func appendTokenUsage(lines []string, stateDir string) []string {
	usageLines := []string{}
	for _, role := range shiftRoleRecords(stateDir) {
		record, _ := role.val.(object)
		usage, ok := rowValue(record, "usage").(object)
		if !ok {
			continue
		}
		usageLines = append(usageLines, fmt.Sprintf("- **%s**: %d total (input %d, cached %d, output %d, turns %d)", role.key,
			usageInt(usage, "total_tokens"), usageInt(usage, "input_tokens"), usageInt(usage, "cached_input_tokens"),
			usageInt(usage, "output_tokens"), usageInt(usage, "turns")))
	}
	if len(usageLines) == 0 {
		return lines
	}
	return append(lines, append([]string{"", "## Token Usage", ""}, usageLines...)...)
}
