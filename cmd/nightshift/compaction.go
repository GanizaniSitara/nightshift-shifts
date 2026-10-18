package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
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

func dispatchCounts(dir, label string, info object) (openShift, completedShift, completedRole int) {
	rows, err := readMessages(dir)
	if err != nil {
		return 0, 0, 0
	}
	inc, ok := objectNumber(info, "started")
	for _, row := range dispatches(dir, rows) {
		if writeDispatchDone(row, rows) {
			completedShift++
			if rowString(row, "recipient") == label && sameNumber(inc, ok, rowValue(row, "recipient_incarnation")) {
				completedRole++
			}
		} else {
			openShift++
		}
	}
	return openShift, completedShift, completedRole
}

// situationalCompactThreshold returns the Bridge Warden's situational compaction threshold and reason.
// Explicit compact_at overrides always win. Otherwise:
//   - Foreman with open crew dispatches in flight defers compaction up to 80% ("awaiting-dispatch") so it never loses
//     in-flight context mid-assignment; at a phase boundary (0 open dispatches after completed work) it compacts early at 40% ("phase-boundary").
//   - Workers (Coder/Apprentice/Tester) at an assignment boundary (after completing an assignment and before the next)
//     compact early at 35% ("between-assignments") instead of starting the next coding task at 60-69% context.
func situationalCompactThreshold(dir, label string, member, info object) (float64, string) {
	if n, ok := objectNumber(member, "compact_at"); ok {
		v := numberFloat(n)
		if v <= 0 {
			return 0, "disabled"
		}
		return v, "explicit"
	}
	openShift, completedShift, completedRole := dispatchCounts(dir, label, info)
	isForeman := strings.EqualFold(objectString(member, "role"), "Foreman") || truthy(rowValue(member, "human_contact"))
	if isForeman {
		if openShift > 0 {
			return 80, "awaiting-dispatch"
		}
		if completedShift > 0 {
			return 40, "phase-boundary"
		}
		return 50, "foreman-default"
	}
	if completedRole > 0 {
		return 35, "between-assignments"
	}
	return 70, "worker-default"
}

func wardenJudgePath(dir string) string { return filepath.Join(dir, "warden-judge.json") }

func wardenJudgeCompaction(dir, label string, member, info object, used, threshold float64, reason string, now float64) (bool, string) {
	judges, _ := readObject(wardenJudgePath(dir))
	decision := "compact"
	crew, _ := readObject(filepath.Join(dir, "crew.json"))
	llmEnabled := os.Getenv("NIGHTSHIFT_WARDEN_LLM") == "1" || truthy(rowValue(crew, "warden_llm"))
	if llmEnabled && reason != "explicit" && used < 80 {
		prev, _ := rowValue(judges, label).(object)
		lastAt, ok := objectNumber(prev, "llm_at")
		if ok && now-numberFloat(lastAt) < 300 && samePane(prev, info) {
			if objectString(prev, "decision") == "defer" {
				return false, objectString(prev, "reason")
			}
		} else if compact, llmReason, ok := runWardenLLMJudge(dir, label, used, threshold, reason); ok {
			reason = llmReason
			if !compact {
				decision = "defer"
			}
			entry := object{{"pid", rowValue(info, "pid")}, {"started", rowValue(info, "started")}, {"decision", decision}, {"reason", reason}, {"used", used}, {"threshold", threshold}, {"at", now}, {"llm_at", now}}
			_ = writeJSONFile(wardenJudgePath(dir), setValue(judges, label, entry))
			return compact, reason
		}
	}
	entry := object{{"pid", rowValue(info, "pid")}, {"started", rowValue(info, "started")}, {"decision", decision}, {"reason", reason}, {"used", used}, {"threshold", threshold}, {"at", now}}
	_ = writeJSONFile(wardenJudgePath(dir), setValue(judges, label, entry))
	return true, reason
}

func runWardenLLMJudge(dir, label string, used, threshold float64, reason string) (bool, string, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	prompt := fmt.Sprintf("You are Nightshift Bridge Warden for %s. Role %s is idle at %.0f%% context used (situational threshold %.0f%%, trigger %s). Reply with one line starting COMPACT or DEFER followed by a short 3-word reason.", filepath.Base(dir), label, used, threshold, reason)
	cmd := exec.CommandContext(ctx, toolExe("codex"), "exec", "--skip-git-repo-check", "-m", "gpt-6.1-sol", "-c", `model_reasoning_effort="low"`, prompt)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	out, err := cmd.Output()
	if err != nil {
		return true, reason, false
	}
	line := strings.TrimSpace(firstSplitLine(string(out)))
	upper := strings.ToUpper(line)
	if strings.HasPrefix(upper, "DEFER") {
		return false, "llm-defer", true
	}
	if strings.HasPrefix(upper, "COMPACT") {
		return true, "llm-compact", true
	}
	return true, reason, false
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

func compactionIdle(tool, state string, lines []string) bool {
	if state != "idle" {
		return false
	}
	if tool == "copilot" {
		return true
	}
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
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
		tail := agentTail(member.pid)
		screenOK := len(tail) > 0
		var screenLines []string
		if rawLines, ok := rowValue(tail, "lines").([]any); ok {
			for _, item := range rawLines {
				screenLines = append(screenLines, valueText(item))
			}
		}
		screenState := objectString(tail, "state")
		now := epochNow()
		usedOnScreen, screenContext := parseContextUsed(tool, strings.Join(screenLines, "\n"))
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
		if !screenOK {
			continue
		}
		blocked, err := compactionBlocked(dir, member.label, info)
		if err != nil {
			return result, err
		}
		if blocked || !compactionIdle(tool, screenState, screenLines) || !fresh {
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
					if sendHeader(member.pid, note, tool) {
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
		threshold, reason := situationalCompactThreshold(dir, member.label, member.obj, info)
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
		if allow, judgeReason := wardenJudgeCompaction(dir, member.label, member.obj, info, used, threshold, reason, now); !allow {
			_ = judgeReason
			continue
		}
		state = setValue(setValue(setValue(setValue(state, "phase", "pending"), "before", used), "requested_at", now), "reason", reason)
		if err := writeJSONFile(path, state); err != nil {
			return result, err
		}
		current, _ := readObject(filepath.Join(dir, member.label+".json"))
		blocked, err = compactionBlocked(dir, member.label, info)
		if err != nil {
			return result, err
		}
		if samePane(current, info) && !blocked && !liveWaiter(dir, member.label) {
			_ = sendHeader(member.pid, command, tool)
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
