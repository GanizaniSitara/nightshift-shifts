package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

func init() {
	extraCommands["warden"] = wardenCommand
	extraCommands["gaffer"] = gafferCommand
}

const fileLockStale = 30 * time.Second
const nudgeIdleSeconds = 300.0
const copilotEnterTries = 3

func staleFileLock(path string) bool {
	stat, err := os.Stat(path)
	if err != nil {
		return false
	}
	if time.Since(stat.ModTime()) > fileLockStale {
		return true
	}
	holder, ok := readObject(path)
	if !ok || len(holder) == 0 {
		return false
	}
	pid := processNumber(holder, "pid")
	return pid != 0 && !processRunning(pid)
}

func agentTail(pid uint32) object {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "agents-cli", "tail", strconv.FormatUint(uint64(pid), 10), "--lines", "40")
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		return object{}
	}
	val, err := parseJSON(stdout.Bytes())
	if err != nil {
		return object{}
	}
	obj, ok := val.(object)
	if !ok {
		return object{}
	}
	return obj
}

var promptRegex = regexp.MustCompile(`^\s*[›❯>]`)

func headerSubmitted(pid uint32, header string) bool {
	tail := agentTail(pid)
	state := objectString(tail, "state")
	if state != "idle" && state != "busy" {
		return false
	}
	linesVal, ok := objectGet(tail, "lines")
	if !ok {
		return false
	}
	linesList, ok := linesVal.([]any)
	if !ok {
		return false
	}
	var lines []string
	for _, item := range linesList {
		if s, ok := item.(string); ok {
			lines = append(lines, s)
		} else {
			lines = append(lines, "")
		}
	}
	var prompts []int
	for i, line := range lines {
		if promptRegex.MatchString(line) {
			prompts = append(prompts, i)
		}
	}
	if len(prompts) == 0 {
		return false
	}
	last := prompts[len(prompts)-1]
	lastLine := strings.TrimSpace(lines[last])
	validLast := lastLine == "›" || lastLine == "❯" || lastLine == ">" || lastLine == "› Ask Codex to do anything"
	if !validLast {
		return false
	}
	for i := 0; i < last; i++ {
		if strings.Contains(lines[i], header) {
			return true
		}
	}
	return false
}

var whitespaceRegex = regexp.MustCompile(`\s+`)

func headerInComposer(pid uint32, header string) bool {
	tail := agentTail(pid)
	linesVal, _ := objectGet(tail, "lines")
	linesList, _ := linesVal.([]any)
	var lines []string
	for _, item := range linesList {
		if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
			lines = append(lines, s)
		}
	}
	if len(lines) == 0 {
		return false
	}
	footer := strings.ToLower(lines[len(lines)-1])
	if strings.Contains(footer, "enter") && (strings.Contains(footer, "select") || strings.Contains(footer, "confirm")) {
		return false
	}
	var prompts []int
	for i, line := range lines {
		if promptRegex.MatchString(line) {
			prompts = append(prompts, i)
		}
	}
	if len(prompts) == 0 {
		return false
	}
	joined := strings.Join(lines[prompts[len(prompts)-1]:], "")
	cleanHeader := whitespaceRegex.ReplaceAllString(header, "")
	cleanJoined := whitespaceRegex.ReplaceAllString(joined, "")
	return strings.Contains(cleanJoined, cleanHeader)
}

func sendHeader(pid uint32, header, tool string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "agents-cli", "send", strconv.FormatUint(uint64(pid), 10), header)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		return false
	}
	val, err := parseJSON(stdout.Bytes())
	if err != nil {
		return false
	}
	receipt, ok := val.(object)
	if !ok {
		return false
	}
	if submittedVal, hasSubmitted := objectGet(receipt, "submitted"); hasSubmitted {
		if submittedVal == true || tool != "copilot" {
			return submittedVal == true
		}
		for i := 0; i < copilotEnterTries; i++ {
			if !headerInComposer(pid, header) {
				break
			}
			keyCtx, keyCancel := context.WithTimeout(context.Background(), 30*time.Second)
			_ = exec.CommandContext(keyCtx, "agents-cli", "key", strconv.FormatUint(uint64(pid), 10), "enter").Run()
			keyCancel()
			time.Sleep(1 * time.Second)
			if headerSubmitted(pid, header) {
				return true
			}
		}
		return headerSubmitted(pid, header)
	}
	enterVal, hasEnter := objectGet(receipt, "enter")
	sentVal, hasSent := objectGet(receipt, "sent")
	return hasEnter && enterVal == true && hasSent && sentVal == header && headerSubmitted(pid, header)
}

func msgHeader(row object) string {
	asg := rowString(row, "asg")
	if asg == "" {
		asg = "-"
	}
	return fmt.Sprintf("msg:%v kind:%s asg:%s", rowValue(row, "id"), rowString(row, "kind"), asg)
}

func deliverPending(dir string, dryRun bool) ([]any, error) {
	delivered := []any{}
	attemptsPath := filepath.Join(dir, "warden-delivery.json")
	attempts := object{}
	if fileExists(attemptsPath) {
		if obj, err := lifecycleReadObject(attemptsPath); err == nil {
			attempts = obj
		}
	}
	messages, err := readMessages(dir)
	if err != nil {
		return delivered, err
	}
	for _, row := range messages {
		state := rowString(row, "state")
		if state != "stored" {
			continue
		}
		recipient := rowString(row, "recipient")
		info, _ := readObject(filepath.Join(dir, recipient+".json"))
		started, startedOK := objectNumber(info, "started")
		if !sameNumber(started, startedOK, rowValue(row, "recipient_incarnation")) {
			continue
		}
		pid := processNumber(info, "pid")
		if pid == 0 || !pidAlive(pid, started, startedOK) {
			continue
		}
		incVal := rowValue(row, "recipient_incarnation")
		var incStr string
		if incVal == nil {
			incStr = "None"
		} else {
			switch v := incVal.(type) {
			case float64:
				incStr = floatText(v)
			case float32:
				incStr = floatText(float64(v))
			case json.Number:
				if f, err := v.Float64(); err == nil {
					incStr = floatText(f)
				} else {
					incStr = v.String()
				}
			case int:
				incStr = floatText(float64(v))
			case int64:
				incStr = floatText(float64(v))
			case uint32:
				incStr = floatText(float64(v))
			case uint64:
				incStr = floatText(float64(v))
			default:
				if f, err := strconv.ParseFloat(fmt.Sprint(v), 64); err == nil {
					incStr = floatText(f)
				} else {
					incStr = fmt.Sprint(v)
				}
			}
		}
		key := fmt.Sprintf("%v:%s", rowValue(row, "id"), incStr)

		confirmed := false
		if dryRun {
			confirmed = true
		} else if _, inAttempts := objectGet(attempts, key); inAttempts {
			confirmed = headerSubmitted(pid, msgHeader(row))
		} else {
			attempts = setValue(attempts, key, object{
				{"pid", pid},
				{"at", float64(time.Now().UnixNano()) / 1e9},
			})
			_ = writeJSONFile(attemptsPath, attempts)
			confirmed = sendHeader(pid, msgHeader(row), objectString(info, "tool"))
		}
		if confirmed {
			idVal := rowValue(row, "id")
			idInt, _ := parseInteger(fmt.Sprint(idVal))
			delivered = append(delivered, idInt)
			if !dryRun {
				_ = withWriteMailbox(dir, func(db *sql.DB) error {
					_, err := db.Exec("UPDATE messages SET state = 'sent' WHERE id = ? AND state = 'stored'", idInt)
					return err
				})
				busAppend(dir, "sent", object{{"id", idInt}})
			}
		}
	}
	return delivered, nil
}

func sessionIdleSince(pid uint32) *float64 {
	sessionPath := filepath.Join(envOr("NIGHTSHIFT_HOME", userHome()), ".claude", "sessions", strconv.FormatUint(uint64(pid), 10)+".json")
	info, ok := readObject(sessionPath)
	if !ok {
		return nil
	}
	if objectString(info, "status") != "idle" {
		return nil
	}
	statusUpdatedAt, _ := objectNumber(info, "statusUpdatedAt")
	diff := (float64(time.Now().UnixNano()) / 1e9) - (numberFloat(statusUpdatedAt) / 1000)
	if diff < 0 {
		diff = 0
	}
	return &diff
}

func nudgeIdleForemen(dir string, dryRun bool) ([]any, error) {
	lead := humanLabel(dir)
	info, _ := readObject(filepath.Join(dir, lead+".json"))
	pid := processNumber(info, "pid")
	started, startedOK := objectNumber(info, "started")
	if pid == 0 || !pidAlive(pid, started, startedOK) {
		return []any{}, nil
	}
	tool := objectString(info, "tool")
	if tool == "" {
		tool = "claude"
	}
	var idle *float64
	if tool == "claude" {
		idle = sessionIdleSince(pid)
	} else {
		tail := agentTail(pid)
		if objectString(tail, "state") != "idle" {
			return []any{}, nil
		}
		lastActivity := float64(time.Now().UnixNano()) / 1e9
		if startedOK {
			lastActivity = numberFloat(started)
		}
		busPath := filepath.Join(dir, "bus.jsonl")
		if fileExists(busPath) {
			data, _ := os.ReadFile(busPath)
			lines := strings.Split(string(data), "\n")
			for _, line := range lines {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				val, err := parseJSON([]byte(line))
				if err != nil {
					continue
				}
				event, ok := val.(object)
				if !ok {
					continue
				}
				sender := objectString(event, "sender")
				by := objectString(event, "by")
				if sender == lead || by == lead {
					tsStr := objectString(event, "ts")
					t, err := time.ParseInLocation("2006-01-02T15:04:05", tsStr, time.Local)
					if err == nil {
						tEpoch := float64(t.Unix())
						if tEpoch > lastActivity {
							lastActivity = tEpoch
						}
					}
				}
			}
		}
		diff := (float64(time.Now().UnixNano()) / 1e9) - lastActivity
		idle = &diff
	}
	if idle == nil || *idle < nudgeIdleSeconds {
		return []any{}, nil
	}
	markerPath := filepath.Join(dir, "warden-nudge.json")
	marker, _ := readObject(markerPath)
	if atVal, ok := objectNumber(marker, "at"); ok {
		if (float64(time.Now().UnixNano())/1e9 - numberFloat(atVal)) < nudgeIdleSeconds {
			return []any{}, nil
		}
	}
	if !dryRun {
		_ = writeJSONFile(markerPath, object{{"at", float64(time.Now().UnixNano()) / 1e9}})
	}
	nudgeMsg := fmt.Sprintf("Warden: inspect nightshift inbox %s %s and continue the active assignment.", filepath.Base(dir), lead)
	if dryRun || sendHeader(pid, nudgeMsg, tool) {
		return []any{lead}, nil
	}
	return []any{}, nil
}

func recoverPane(dir string, slot int, dryRun bool) object {
	saved, _ := readObject(filepath.Join(dir, "slots", strconv.Itoa(slot)+".json"))
	var roleVal any = nil
	var role string
	if saved != nil {
		if r, ok := objectGet(saved, "role"); ok {
			roleVal = r
			role = objectString(saved, "role")
		}
	}
	result := object{
		{"slot", slot},
		{"role", roleVal},
		{"recovered", false},
	}
	state, _ := slotState(dir, saved)
	if role == "" || state != "dead" {
		return result
	}
	if dryRun {
		return append(result, pair{"would_recover", true})
	}
	if spawnHook != nil {
		if err := spawnHook([]string{filepath.Base(dir), "--role", role, "--slot", strconv.Itoa(slot)}); err != nil {
			return append(result, pair{"error", err.Error()})
		}
		result[2].val = true
		return result
	}
	return append(result, pair{"error", "spawnHook not registered"})
}

const wardenUsage = "usage: nightshift warden [-h] [--dry-run] [shift]"
const wardenHelp = wardenUsage + "\n\nPatrol live shifts without tearing them down.\n\npositional arguments:\n  shift\n\noptions:\n  -h, --help  show this help message and exit\n  --dry-run\n"

func runWarden(argv []string) (object, int) {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--dry-run", flag: true}}, nil, []string{"shift"})
	if args.help {
		out(wardenHelp)
		return nil, 0
	}
	if message != "" {
		return nil, writeUsage("nightshift warden", wardenUsage, message)
	}
	dryRun := args.present["--dry-run"]
	var shiftArg string
	if len(args.pos) > 0 {
		shiftArg = args.pos[0]
	}
	report := object{
		{"shifts", []any{}},
		{"dry_run", dryRun},
	}
	err := withFloorLock(dryRun, func() error {
		floor, err := readFloor()
		if err != nil {
			return err
		}
		var stateDirs []string
		if shiftArg != "" {
			d, err := resolveShift(shiftArg)
			if err != nil {
				return err
			}
			stateDirs = []string{d}
		} else {
			stateDirs = shiftDirs()
		}
		shiftsList := []any{}
		idleClosed := []any{}
		defer func() {
			if len(idleClosed) > 0 {
				report = setValue(report, "idle_closed", idleClosed)
			}
		}()
		for _, dir := range stateDirs {
			record, err := statusRecord(dir)
			if err != nil {
				return err
			}
			// Live shift idle timeout (docs/design-live-shift.md, review note c): an idle prompt cannot time out by
			// itself, so the warden closes a live shift whose queue is empty and quiet for idle_minutes.
			if objectBool(record, "alive") && liveShiftIdle(dir) {
				idleClosed = append(idleClosed, filepath.Base(dir))
				if !dryRun {
					if err := stopShift(dir, true, &floor, false); err != nil {
						return err
					}
				}
				continue
			}
			if !objectBool(record, "alive") || contains([]string{"done", "halted"}, objectString(record, "result")) {
				continue
			}
			locksReleased := []any{}
			claimsNormalized := []any{}
			recovered := []any{}

			for _, name := range []string{"claims.lock", "bus.lock"} {
				p := filepath.Join(dir, name)
				if staleFileLock(p) {
					locksReleased = append(locksReleased, p)
					if !dryRun {
						_ = os.Remove(p)
					}
				}
			}

			normErr := func() error {
				claims := readClaims(dir)
				names := map[string]string{}
				_, members := crewMembers(dir)
				rolesVal, _ := objectGet(record, "roles")
				rolesObj, _ := rolesVal.(object)
				for _, m := range members {
					label := objectString(m, "label")
					rInfoVal, _ := objectGet(rolesObj, label)
					if rInfo, ok := rInfoVal.(object); ok && objectBool(rInfo, "alive") {
						names[strings.ToLower(label)] = objectString(m, "identity")
					}
				}
				for _, p := range claims {
					claimInfo, ok := p.val.(object)
					if !ok {
						continue
					}
					role := objectString(claimInfo, "role")
					identity, has := names[strings.ToLower(role)]
					if has && identity != role {
						claimInfo = setValue(claimInfo, "role", identity)
						claims = setValue(claims, p.key, claimInfo)
						claimsNormalized = append(claimsNormalized, p.key)
					}
				}
				if len(claimsNormalized) > 0 && !dryRun {
					return writeClaims(dir, claims)
				}
				return nil
			}

			if dryRun {
				if err := normErr(); err != nil {
					return err
				}
			} else {
				if err := withClaimsLock(dir, normErr); err != nil {
					return err
				}
			}

			delivered, err := deliverPending(dir, dryRun)
			if err != nil {
				return err
			}
			nudged, err := nudgeIdleForemen(dir, dryRun)
			if err != nil {
				return err
			}

			if objectString(record, "result") == "none" {
				core := map[string]bool{}
				_, members := crewMembers(dir)
				for _, m := range members {
					role := objectString(m, "role")
					if role == "" {
						role = objectString(m, "label")
					}
					if contains([]string{"Foreman", "Advisor", "Coder", "Tester"}, role) {
						core[objectString(m, "label")] = true
					}
				}
				if slotsVal, ok := objectGet(record, "slots"); ok {
					if slotsList, ok := slotsVal.([]any); ok {
						for _, s := range slotsList {
							if sObj, ok := s.(object); ok {
								slotRole := objectString(sObj, "role")
								slotStateStr := objectString(sObj, "state")
								if core[slotRole] && slotStateStr == "dead" {
									slotNum := int(processNumber(sObj, "slot"))
									recovered = append(recovered, recoverPane(dir, slotNum, dryRun))
								}
							}
						}
					}
				}
			}

			patrol := object{
				{"shift", filepath.Base(dir)},
				{"locks_released", locksReleased},
				{"claims_normalized", claimsNormalized},
				{"recovered", recovered},
				{"delivered", delivered},
				{"nudged", nudged},
			}
			shiftsList = append(shiftsList, patrol)
		}
		report = setValue(report, "shifts", shiftsList)
		if !dryRun {
			floor = setValue(floor, "last_warden_at", time.Now().Format("2006-01-02 15:04:05"))
			health := object{}
			for _, item := range shiftsList {
				if p, ok := item.(object); ok {
					health = append(health, pair{objectString(p, "shift"), p})
				}
			}
			floor = setValue(floor, "health", health)
			return writeJSONFile(floorPath(), floor)
		}
		return nil
	})
	if err != nil {
		return nil, commandExit(err)
	}
	return report, 0
}

func wardenCommand(argv []string) int {
	report, code := runWarden(argv)
	if code != 0 || report == nil {
		return code
	}
	printResult(report, true, true)
	return 0
}

const gafferUsage = "usage: nightshift gaffer [-h] [--max-shifts MAX_SHIFTS] [--crew CREW]\n                         [--ticket TICKET] [--repo REPO] [--tick] [--dry-run]"
const gafferHelp = gafferUsage + "\n\nDispatch a ticket below the live-shift cap.\n\noptions:\n  -h, --help            show this help message and exit\n  --max-shifts MAX_SHIFTS\n  --crew CREW\n  --ticket TICKET\n  --repo REPO\n  --tick\n  --dry-run\n"

func runCommandCapturingJSON(fn func() int) (obj object, err error) {
	oldStdout := os.Stdout
	r, w, pipeErr := os.Pipe()
	if pipeErr != nil {
		return nil, pipeErr
	}
	os.Stdout = w
	defer func() {
		os.Stdout = oldStdout
	}()

	var outBuf bytes.Buffer
	readDone := make(chan struct{})
	go func() {
		_, _ = io.Copy(&outBuf, r)
		close(readDone)
	}()

	code := fn()

	_ = w.Close()
	<-readDone
	_ = r.Close()

	if code != 0 {
		return nil, fmt.Errorf("command failed with code %d", code)
	}
	val, parseErr := parseJSON(outBuf.Bytes())
	if parseErr != nil {
		return nil, parseErr
	}
	res, ok := val.(object)
	if !ok {
		return nil, fmt.Errorf("command output was not a JSON object")
	}
	return res, nil
}

func gafferCommand(argv []string) int {
	opts := []writeOption{
		{name: "--max-shifts", number: "int"},
		{name: "--crew"},
		{name: "--tick", flag: true},
		{name: "--ticket"},
		{name: "--repo"},
		{name: "--dry-run", flag: true},
	}
	args, message := parseWriteArgs(argv, opts, nil, nil)
	if args.help {
		out(gafferHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift gaffer", gafferUsage, message)
	}
	maxShifts := int64(2)
	if args.present["--max-shifts"] {
		var err error
		maxShifts, err = parseInteger(args.values["--max-shifts"])
		if err != nil {
			return writeUsage("nightshift gaffer", gafferUsage, fmt.Sprintf("argument --max-shifts: invalid int value: %s", pyRepr(args.values["--max-shifts"])))
		}
	}
	if maxShifts < 1 {
		return writeUsage("nightshift gaffer", gafferUsage, "--max-shifts must be positive")
	}
	dryRun := args.present["--dry-run"]
	report := object{
		{"dry_run", dryRun},
		{"dispatched", false},
	}
	if args.present["--tick"] {
		cleanerArgs := []string{}
		if dryRun {
			cleanerArgs = []string{"--dry-run"}
		}
		cleanerRep, err := runCommandCapturingJSON(func() int { return cleanerCommand(cleanerArgs) })
		if err != nil {
			return commandExit(err)
		}
		wardenArgs := []string{}
		if dryRun {
			wardenArgs = []string{"--dry-run"}
		}
		wardenRep, code := runWarden(wardenArgs)
		if code != 0 {
			return code
		}
		report = append(report, pair{"cleaner", cleanerRep}, pair{"warden", wardenRep})
	}
	err := withFloorLock(dryRun, func() error {
		floor, err := readFloor()
		if err != nil {
			return err
		}
		var live []string
		for _, d := range shiftDirs() {
			rec, err := statusRecord(d)
			if err == nil && objectBool(rec, "alive") {
				live = append(live, d)
			}
		}
		report = append(report, pair{"live_shifts", len(live)})

		queueVal, _ := objectGet(floor, "queue")
		queue, _ := queueVal.([]any)
		var entry object
		ticketArg := args.values["--ticket"]
		if ticketArg != "" {
			entry = object{{"ticket", ticketArg}}
		} else if len(queue) > 0 {
			first := queue[0]
			if s, ok := first.(string); ok {
				entry = object{{"ticket", s}}
			} else if o, ok := first.(object); ok {
				entry = o
			}
		}
		ticket := objectString(entry, "ticket")

		if int64(len(live)) >= maxShifts {
			report = append(report, pair{"reason", "at_capacity"})
		} else if ticket == "" {
			report = append(report, pair{"reason", "no_ticket"})
		} else {
			alreadyRunning := false
			for _, d := range live {
				ticketPath := filepath.Join(d, "ticket.txt")
				if fileExists(ticketPath) {
					content := strings.TrimSpace(readText(ticketPath))
					if content == ticket {
						alreadyRunning = true
						break
					}
				}
			}
			if alreadyRunning {
				report = append(report, pair{"reason", "already_running"})
			} else {
				crew := args.values["--crew"]
				if crew == "" {
					crew = objectString(entry, "crew")
				}
				if crew == "" {
					crew = "default"
				}
				var repo any = nil
				if args.present["--repo"] {
					repo = args.values["--repo"]
				} else if r, ok := objectGet(entry, "repo"); ok && r != nil {
					repo = r
				}
				report = append(report, pair{"ticket", ticket}, pair{"crew", crew}, pair{"repo", repo})
				if dryRun {
					report = append(report, pair{"reason", "would_dispatch"})
				} else {
					command := []string{ticket, "--crew", crew}
					if repoStr, ok := repo.(string); ok && repoStr != "" {
						command = append(command, "--cwd", repoStr)
					}
					if startHook == nil {
						return fmt.Errorf("startHook not registered")
					}
					stateDir, err := startHook(command)
					if err != nil {
						return err
					}
					shiftName := filepath.Base(stateDir)
					dispatch := object{
						{"shift", shiftName},
						{"ticket", ticket},
						{"crew", crew},
						{"repo", repo},
						{"dispatched_at", time.Now().Format("2006-01-02 15:04:05")},
					}
					dispatchesVal, _ := objectGet(floor, "dispatches")
					dispatches, _ := dispatchesVal.([]any)
					dispatches = append(dispatches, dispatch)
					floor = setValue(floor, "dispatches", dispatches)
					if ticketArg == "" && len(queue) > 0 {
						floor = setValue(floor, "queue", queue[1:])
					}
					if err := writeJSONFile(floorPath(), floor); err != nil {
						return err
					}
					report = setValue(report, "dispatched", true)
					report = append(report, pair{"reason", "dispatched"}, pair{"shift", shiftName})
				}
			}
		}
		return nil
	})
	if err != nil {
		return commandExit(err)
	}
	printResult(report, true, true)
	return 0
}
