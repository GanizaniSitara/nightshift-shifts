package main

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"nightshift-readonly/internal/console"
)

const contextFreshSeconds = 90.0

var compactContextLeft = regexp.MustCompile(`(?i)(?:(-?\d+(?:\.\d+)?)\s*%\s*context left|context\s+(-?\d+(?:\.\d+)?)\s*%\s*left)`)

func compactThreshold(member object) float64 {
	if n, ok := objectNumber(member, "compact_at"); ok {
		return numberFloat(n)
	}
	if strings.EqualFold(objectString(member, "role"), "Foreman") {
		return 50
	}
	return 70
}

func compactionCommand(tool string) string {
	switch tool {
	case "claude", "codex", "copilot":
		return "/compact"
	case "gemini":
		return "/compress"
	}
	return ""
}

func parseContextUsed(tool, screen string) (float64, bool) {
	if tool != "codex" && tool != "agy" {
		return 0, false
	}
	matches := compactContextLeft.FindAllStringSubmatch(lastLines(screen, 3), -1)
	if len(matches) == 0 {
		return 0, false
	}
	m := matches[len(matches)-1]
	figure := m[1]
	if figure == "" {
		figure = m[2]
	}
	left, err := strconv.ParseFloat(figure, 64)
	return 100 - left, err == nil && left >= 0 && left <= 100
}

func contextPath(dir, role string) string    { return filepath.Join(dir, role+"-context.json") }
func compactionPath(dir, role string) string { return filepath.Join(dir, role+"-compaction.json") }

func samePane(saved, info object) bool {
	a, aok := objectNumber(saved, "started")
	b, bok := objectNumber(info, "started")
	return aok && bok && numberFloat(a) == numberFloat(b) && processNumber(saved, "pid") == processNumber(info, "pid")
}

func contextRecord(info object, used, at float64) object {
	return object{{"pid", rowValue(info, "pid")}, {"started", rowValue(info, "started")}, {"context_used", used}, {"context_at", at}}
}

func freshContext(saved, info object, now float64) (float64, bool) {
	used, uok := objectNumber(saved, "context_used")
	at, aok := objectNumber(saved, "context_at")
	age := now - numberFloat(at)
	value := numberFloat(used)
	return value, samePane(saved, info) && uok && aok && age >= 0 && age <= contextFreshSeconds && value >= 0 && value <= 100
}

func recordClaudeContext(dir, role string, payload object) {
	info, _ := readObject(filepath.Join(dir, role+".json"))
	id := objectString(payload, "session_id")
	if id == "" || id != objectString(info, "session_id") {
		return
	}
	window, _ := rowValue(payload, "context_window").(object)
	used, ok := objectNumber(window, "used_percentage")
	value := numberFloat(used)
	if ok && value >= 0 && value <= 100 {
		_ = writeJSONFile(contextPath(dir, role), contextRecord(info, value, epochNow()))
	}
}

func compactionBlocked(dir, label string, info object) (bool, error) {
	builds, _ := filepath.Glob(filepath.Join(dir, "builds", "*.json"))
	for _, path := range builds {
		r, _ := readObject(path)
		if contains([]string{"waiting", "running"}, objectString(r, "status")) {
			return true, nil
		}
	}
	rows, err := readMessages(dir)
	if err != nil {
		return true, err
	}
	inc, ok := objectNumber(info, "started")
	for _, row := range dispatches(dir, rows) {
		if rowString(row, "recipient") == label && sameNumber(inc, ok, rowValue(row, "recipient_incarnation")) && !writeDispatchDone(row, rows) {
			return true, nil
		}
	}
	return false, nil
}

func compactionIdle(tool string, screen console.Screen) bool {
	if screen.State != "idle" {
		return false
	}
	if tool == "copilot" {
		return true
	} // console's classifier requires an empty framed composer.
	for i := len(screen.Lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(screen.Lines[i])
		if strings.HasPrefix(line, "›") || strings.HasPrefix(line, "❯") || strings.HasPrefix(line, ">") {
			return line == "›" || line == "❯" || line == ">" || (tool == "codex" && (line == "› Ask Codex" || line == "› Ask Codex to do anything"))
		}
	}
	return false
}

// Both patrol and warden enter here. Persist intent before typing: an uncertain send is never retried.
func patrolCompaction(dir string) ([]any, error) {
	result := []any{}
	if patrolEnded(dir) {
		return result, nil
	}
	err := withMailboxMutexTimeout(filepath.Join(dir, "compaction.lock"), 5000, func() error {
		var err error
		result, err = compactionPass(dir)
		return err
	})
	return result, err
}

func compactionPass(dir string) ([]any, error) {
	result := []any{}
	for _, member := range liveMembers(dir) {
		info, _ := readObject(filepath.Join(dir, member.label+".json"))
		if processNumber(info, "pid") != member.pid {
			continue
		}
		tool := objectString(info, "tool")
		if tool == "" {
			tool = objectString(member.obj, "tool")
		}
		if tool == "" {
			tool = "claude"
		}
		screen, err := console.Tail(member.pid, tool, 40)
		screenErr := err
		now := epochNow()
		usedOnScreen, screenContext := parseContextUsed(tool, strings.Join(screen.Lines, "\n"))
		if screenContext {
			if err := writeJSONFile(contextPath(dir, member.label), contextRecord(info, usedOnScreen, now)); err != nil {
				return result, err
			}
		}
		context, _ := readObject(contextPath(dir, member.label))
		used, fresh := freshContext(context, info, now)
		if (tool == "codex" || tool == "agy") && !screenContext {
			fresh = false
		}
		path := compactionPath(dir, member.label)
		state, _ := readObject(path)
		if !samePane(state, info) {
			state = object{{"pid", rowValue(info, "pid")}, {"started", rowValue(info, "started")}}
		}
		phase := objectString(state, "phase")
		if phase == "waiting" || phase == "anchor_pending" {
			anchor, _ := objectNumber(state, "anchor_at")
			waiter, _ := readObject(waiterPath(dir, member.label))
			since, _ := objectNumber(waiter, "since")
			if liveWaiter(dir, member.label) && numberFloat(since) >= numberFloat(anchor) && numberFloat(since) <= numberFloat(anchor)+60 && numberFloat(since) <= now {
				state = setValue(state, "phase", "done")
			} else if now-numberFloat(anchor) >= 60 {
				subject := member.label + "@" + valueText(rowValue(state, "started")) + "/" + valueText(rowValue(state, "requested_at"))
				_, err := raiseEvent(dir, "member-stuck", subject, "member-stuck "+member.label+": compaction reanchor did not return to wait within 60s", nil, object{{"role", member.label}, {"reason", "compaction-reanchor"}})
				if err != nil {
					return result, err
				}
				state = setValue(state, "phase", "stuck")
			}
			if err := writeJSONFile(path, state); err != nil {
				return result, err
			}
			continue
		}
		if phase == "stuck" {
			continue
		}
		if screenErr != nil {
			continue
		}
		blocked, err := compactionBlocked(dir, member.label, info)
		if err != nil {
			return result, err
		}
		if blocked || !compactionIdle(tool, screen) || !fresh {
			continue
		}
		if phase == "pending" {
			before, _ := objectNumber(state, "before")
			requested, _ := objectNumber(state, "requested_at")
			at, _ := objectNumber(context, "context_at")
			if used >= numberFloat(before) || numberFloat(at) <= numberFloat(requested) {
				continue
			}
			state = setValue(setValue(setValue(state, "phase", "confirmed"), "after", used), "last_compacted", now)
			if err := writeJSONFile(path, state); err != nil {
				return result, err
			}
			fields := object{{"role", member.label}, {"before", numberFloat(before)}, {"after", used}}
			busAppend(dir, "compacted", fields)
			result = append(result, fields)
			phase = "confirmed"
		}
		if phase == "confirmed" {
			if tool == "codex" || tool == "copilot" {
				state = setValue(setValue(state, "phase", "anchor_pending"), "anchor_at", now)
				if err := writeJSONFile(path, state); err != nil {
					return result, err
				}
				note := fmt.Sprintf("You are %s in %s. Your brief is %s; reread it, then run nightshift wait.", member.label, filepath.Base(dir), filepath.Join(dir, member.label+".brief.md"))
				current, _ := readObject(filepath.Join(dir, member.label+".json"))
				if samePane(current, info) && !liveWaiter(dir, member.label) {
					if submitted, err := console.Send(member.pid, tool, note, true); err == nil && submitted {
						state = setValue(state, "phase", "waiting")
					}
				}
			} else {
				state = setValue(state, "phase", "done")
			}
			if err := writeJSONFile(path, state); err != nil {
				return result, err
			}
			continue
		}
		threshold := compactThreshold(member.obj)
		if phase == "done" {
			if used < threshold {
				state = setValue(state, "phase", "")
				if err := writeJSONFile(path, state); err != nil {
					return result, err
				}
			}
			continue
		}
		command := compactionCommand(tool)
		if threshold <= 0 || threshold > 100 || used < threshold || command == "" {
			continue
		}
		// A live wait owns a tool call even if the TUI happens to show an idle footer.
		if liveWaiter(dir, member.label) {
			continue
		}
		state = setValue(setValue(setValue(state, "phase", "pending"), "before", used), "requested_at", now)
		if err := writeJSONFile(path, state); err != nil {
			return result, err
		}
		current, _ := readObject(filepath.Join(dir, member.label+".json"))
		blocked, err = compactionBlocked(dir, member.label, info)
		if err != nil {
			return result, err
		}
		if samePane(current, info) && !blocked && !liveWaiter(dir, member.label) {
			_, _ = console.Send(member.pid, tool, command, true)
		}
	}
	return result, nil
}

func compactionStatus(dir, label string, info, role object) object {
	context, _ := readObject(contextPath(dir, label))
	if used, ok := freshContext(context, info, epochNow()); ok {
		role = append(role, pair{"context_used", used})
	}
	state, _ := readObject(compactionPath(dir, label))
	if samePane(state, info) {
		if at := rowValue(state, "last_compacted"); at != nil {
			role = append(role, pair{"last_compacted", at})
		}
	}
	return role
}
