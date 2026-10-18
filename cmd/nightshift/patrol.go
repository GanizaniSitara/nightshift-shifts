package main

// `nightshift patrol SHIFT [--once]` (NGH-066, docs/instructions-review.md §11): the shift's own warden. `start`
// launches it as a hidden child and records patrol.json; it ticks every 30 seconds (NIGHTSHIFT_PATROL_INTERVAL
// overrides) until the shift is down, paused or gone. Each tick: liveness and asg-timeout events, delivery and
// re-nudges, cost samples per live member, and the budget (warning at 80%, a deterministic stop at 100%).

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func init() { extraCommands["patrol"] = patrolCommand }

const patrolUsage = "usage: nightshift patrol [-h] [--once] shift"

const patrolHelp = patrolUsage + `

The shift's patrol: every 30 seconds (NIGHTSHIFT_PATROL_INTERVAL seconds overrides)
raise member-died and asg-timeout, deliver and re-nudge unacked rows, sample
each live member's cost, and enforce the crew budget. Exits when the shift is
down, paused or gone. start launches it; down stops it.

positional arguments:
  shift       shift name, unique suffix, or "latest"

options:
  -h, --help  show this help message and exit
  --once      run one tick and exit
`

// patrolBudgets: crew.json key -> the unit shiftUsageTotals sums it in.
var patrolBudgets = [][2]string{{"budget_usd", "usd"}, {"max_credits", "credits"}, {"budget_tokens", "tokens"}}

func patrolCommand(argv []string) int {
	opts := []writeOption{{name: "--once", flag: true}, {name: "--slot", number: "int"}}
	args, message := parseWriteArgs(argv, opts, []string{"shift"}, nil)
	if args.help {
		out(patrolHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift patrol", patrolUsage, message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return busExit(err)
	}
	interval := 30 * time.Second
	if v := os.Getenv("NIGHTSHIFT_PATROL_INTERVAL"); v != "" {
		if secs, err := strconv.ParseFloat(v, 64); err == nil && secs > 0 {
			interval = time.Duration(secs * float64(time.Second))
		}
	}
	if !args.present["--once"] {
		pid := os.Getpid()
		_, created, _ := processInfo(uint32(pid))
		rec := object{{"pid", pid}, {"started", created}}
		if args.present["--slot"] {
			if n, err := parseInteger(args.values["--slot"]); err == nil {
				rec = append(rec, pair{"slot", int(n)})
			}
		}
		_ = writeJSONFile(filepath.Join(dir, "patrol.json"), rec)
		out(wardenBanner(dir))
	}
	for {
		report, stop, err := patrolTick(dir)
		if err != nil {
			errLine("patrol: " + err.Error())
		}
		if args.present["--once"] {
			printResult(report, false, true)
			return busExit(err)
		}
		out(wardenTickLine(dir, report))
		if stop {
			return 0
		}
		time.Sleep(interval)
	}
}

func wardenBanner(dir string) string {
	shift := filepath.Base(dir)
	label := strings.TrimSpace(readText(filepath.Join(dir, "ticket.txt")))
	if label == "" {
		label = shift
	} else {
		label = shift + " | " + label
	}
	const bold, dim, reset = "\x1b[1;37;45m", "\x1b[90m", "\x1b[0m"
	return "\n" + bold + " NIGHTSHIFT WARDEN  " + label + " " + reset + "\n" +
		dim + "  Every 30s: liveness, nudges, compaction (Foreman 50%, others 70%), cost & budget guard" + reset + "\n\n"
}

func wardenTickLine(dir string, report object) string {
	const dim, yellow, green, red, reset = "\x1b[90m", "\x1b[33m", "\x1b[32m", "\x1b[31m", "\x1b[0m"
	ts := time.Now().Format("15:04:05")
	_, members := crewMembers(dir)
	parts := []string{}
	now := epochNow()
	for _, m := range members {
		label := objectString(m, "label")
		info, _ := readObject(filepath.Join(dir, label+".json"))
		pid := processNumber(info, "pid")
		started, ok := objectNumber(info, "started")
		if pid == 0 || !incarnationRunning(pid, started, ok) {
			continue
		}
		thresh := int(compactThreshold(m))
		ctxStr := "?%"
		if ctxObj, ok := readObject(contextPath(dir, label)); ok {
			if used, fresh := freshContext(ctxObj, info, now); fresh {
				ctxStr = fmt.Sprintf("%.0f%%", used)
			}
		}
		costStr := ""
		if uObj, ok := readObject(usagePath(dir, label)); ok {
			if latest, ok := rowValue(uObj, "latest").(object); ok {
				unit := objectString(latest, "unit")
				val, _ := objectNumber(latest, "value")
				v := numberFloat(val)
				switch unit {
				case "usd":
					costStr = fmt.Sprintf(" $%.2f", v)
				case "credits":
					costStr = fmt.Sprintf(" %.0fAIC", v)
				case "tokens":
					costStr = fmt.Sprintf(" %.0fk", v/1000.0)
				}
			}
		}
		parts = append(parts, fmt.Sprintf("%s(%s/%d%%%s)", label, ctxStr, thresh, costStr))
	}
	if len(parts) == 0 {
		parts = append(parts, "no live panes")
	}
	budgetParts := []string{}
	if bObj, ok := rowValue(report, "budget").(object); ok {
		for _, p := range bObj {
			if u, ok := p.val.(object); ok {
				tot, _ := objectNumber(u, "total")
				lim, _ := objectNumber(u, "budget")
				budgetParts = append(budgetParts, fmt.Sprintf("%s %s/%s", p.key, secondsText(numberFloat(tot)), secondsText(numberFloat(lim))))
			}
		}
	}
	budgetStr := ""
	if len(budgetParts) > 0 {
		budgetStr = " | budget: " + strings.Join(budgetParts, ", ")
	}
	actions := []string{}
	for _, k := range []string{"delivered", "nudged", "renudged"} {
		if list := anyList(rowValue(report, k)); len(list) > 0 {
			actions = append(actions, fmt.Sprintf("%s:%d", k, len(list)))
		}
	}
	if list := anyList(rowValue(report, "compacted")); len(list) > 0 {
		for _, item := range list {
			if c, ok := item.(object); ok {
				b, _ := objectNumber(c, "before")
				a, _ := objectNumber(c, "after")
				actions = append(actions, green+fmt.Sprintf("compacted:%s(%.0f%%->%.0f%%)", objectString(c, "role"), numberFloat(b), numberFloat(a))+reset)
			}
		}
	}
	actStr := ""
	if len(actions) > 0 {
		actStr = " | " + yellow + strings.Join(actions, " ") + reset
	}
	if truthy(rowValue(report, "ended")) {
		actStr += " | " + red + "shift ended" + reset
	}
	return fmt.Sprintf("%s[%s]%s %s%s%s\n", dim, ts, reset, strings.Join(parts, "  "), budgetStr, actStr)
}

// patrolEnded: the shift is down, paused by a budget stop, or its folder is gone.
func patrolEnded(dir string) bool {
	if !isFile(filepath.Join(dir, "crew.json")) {
		return true
	}
	bus := readText(filepath.Join(dir, "bus.jsonl"))
	return strings.Contains(bus, `"event": "down"`) || strings.Contains(bus, `"event": "paused"`)
}

// patrolTick runs one patrol pass and reports what it did; stop says the patrol should exit.
func patrolTick(dir string) (object, bool, error) {
	report := object{{"shift", filepath.Base(dir)}}
	if patrolEnded(dir) {
		return append(report, pair{"ended", true}), true, nil
	}
	lead := humanLabel(dir)
	// 1-2. liveness and stalls (member-died, asg-timeout), first delivery, the idle lead, re-nudges
	if err := raiseLeadEvents(dir, lead); err != nil {
		return report, false, err
	}
	delivered, err := deliverPending(dir, false)
	if err != nil {
		return report, false, err
	}
	nudged, err := nudgeIdleForemen(dir, false)
	if err != nil {
		return report, false, err
	}
	renudged, err := retryNudges(dir, false)
	if err != nil {
		return report, false, err
	}
	report = append(report, pair{"delivered", delivered}, pair{"nudged", nudged}, pair{"renudged", renudged})
	// 3. costs: one sample per live member
	sampled := []any{}
	for _, m := range liveMembers(dir) {
		if s, ok := sampleMember(dir, m.obj); ok {
			if err := recordUsage(dir, s); err != nil {
				return report, false, err
			}
			sampled = append(sampled, object{{"role", s.Role}, {"unit", s.Unit}, {"value", s.Value}, {"source", s.Source}})
		}
	}
	report = append(report, pair{"sampled", sampled})
	// 4. budget
	budget, stopped, err := patrolBudget(dir)
	report = append(report, pair{"budget", budget})
	if err == nil && !stopped {
		var compacted []any
		compacted, err = patrolCompaction(dir)
		if len(compacted) > 0 {
			report = append(report, pair{"compacted", compacted})
		}
	}
	return report, stopped, err
}

type liveMember struct {
	label string
	pid   uint32
	obj   object
}

// liveMembers are the crew members whose recorded pid runs in their current incarnation.
func liveMembers(dir string) []liveMember {
	live := []liveMember{}
	_, members := crewMembers(dir)
	for _, m := range members {
		label := objectString(m, "label")
		info, _ := readObject(filepath.Join(dir, label+".json"))
		pid := processNumber(info, "pid")
		started, ok := objectNumber(info, "started")
		if pid != 0 && incarnationRunning(pid, started, ok) {
			live = append(live, liveMember{label, pid, m})
		}
	}
	return live
}

// patrolBudget compares each unit's shift total with its budget: budget-warning at 80%, and at 100% budget-stop
// followed by the deterministic stop. It reports {unit: {total, budget}} for every budgeted unit.
func patrolBudget(dir string) (object, bool, error) {
	crew, _ := readObject(filepath.Join(dir, "crew.json"))
	totals := shiftUsageTotals(dir)
	status := object{}
	for _, b := range patrolBudgets {
		limit, ok := objectNumber(crew, b[0])
		budget := numberFloat(limit)
		if !ok || budget <= 0 {
			continue
		}
		unit, total := b[1], totals[b[1]]
		status = append(status, pair{unit, object{{"total", total}, {"budget", budget}}})
		figures := fmt.Sprintf("%s %s/%s", unit, secondsText(total), secondsText(budget)) // shortest form: 10.5/10
		fields := object{{"unit", unit}, {"total", total}, {"budget", budget}}
		if total >= budget {
			raised, err := raiseEvent(dir, "budget-stop", unit, "budget-stop "+figures, nil, fields)
			if err != nil || !raised {
				return status, err == nil && raised, err
			}
			return status, true, budgetStop(dir, unit, total, budget)
		}
		if total >= 0.8*budget {
			if _, err := raiseEvent(dir, "budget-warning", unit, "budget-warning "+figures, nil, fields); err != nil {
				return status, false, err
			}
		}
	}
	return status, false, nil
}

// budgetStop (the NGH-034 decision: stop at budget, never kill): interrupt every live member's turn, write the
// handover, append bus paused and write result.json status paused; the shift stays resumable with
// `start --continue-from`.
func budgetStop(dir, unit string, total, budget float64) error {
	for _, m := range liveMembers(dir) {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		_ = exec.CommandContext(ctx, "agents-cli", "key", strconv.FormatUint(uint64(m.pid), 10), "escape").Run()
		cancel()
	}
	if err := writeHandover(dir); err != nil {
		errLine("patrol: handover not written during budget stop: " + err.Error())
	}
	busAppend(dir, "paused", object{{"reason", "budget-stop"}, {"unit", unit}, {"total", total}, {"budget", budget}})
	stage := "implemented"
	if result, ok := readObject(filepath.Join(dir, "result.json")); ok && objectString(result, "stage") != "" {
		stage = objectString(result, "stage")
	}
	summary := fmt.Sprintf("budget stop: %s %s/%s", unit, secondsText(total), secondsText(budget))
	return writeJSONFile(filepath.Join(dir, "result.json"), object{{"status", "paused"}, {"stage", stage}, {"summary", summary}})
}

// writeHandover writes handover.md from live state exactly as `nightshift handover` does, without printing.
func writeHandover(dir string) error {
	record, err := statusRecord(dir)
	if err != nil {
		return err
	}
	report, err := handoverReport(dir, record)
	if err != nil {
		return err
	}
	live, err := handoverLive(dir)
	if err != nil {
		return err
	}
	lines := []string{"# Handover: " + objectString(record, "shift"), "", "Task: " + objectString(record, "task"), "Written: " + time.Now().Format("2006-01-02 15:04"), ""}
	lines = append(append(lines, live...), strings.Split(report, "\n")...)
	return os.WriteFile(filepath.Join(dir, "handover.md"), []byte(strings.ReplaceAll(strings.Join(lines, "\n"), "\n", "\r\n")), 0666)
}

// wardenPaneSlot returns (slot, cmdPid) for Cell 2 of the bottom row (Slot 5 when slots <= 7, Slot 6 when slots == 8)
// if that slot has a live cmd.exe shell and no started agent role.
func wardenPaneSlot(dir string) (int, uint32) {
	crew, ok := readObject(filepath.Join(dir, "crew.json"))
	if !ok {
		return 0, 0
	}
	slots := intValue(rowValue(crew, "slots"))
	if slots < 5 {
		return 0, 0
	}
	preferred := 5
	if slots == 8 {
		preferred = 6
	}
	candidates := []int{preferred}
	for n := 4; n < slots; n++ {
		if n != preferred {
			candidates = append(candidates, n)
		}
	}
	for _, slot := range candidates {
		rec := spawnReadSlot(dir, slot)
		if rec == nil || truthy(rowValue(rec, "role")) {
			continue
		}
		cmdPid := processNumber(rec, "cmd_pid")
		if cmdPid != 0 && processRunning(cmdPid) {
			return slot, cmdPid
		}
	}
	return 0, 0
}

// startPatrol launches the shift's patrol in Cell 2 of the bottom row when a live container slot is available,
// or as a hidden detached child otherwise, and records <shift>/patrol.json. NIGHTSHIFT_PATROL=off skips it.
func startPatrol(dir string) error {
	if os.Getenv("NIGHTSHIFT_PATROL") == "off" {
		return nil
	}
	if slot, cmdPid := wardenPaneSlot(dir); slot != 0 && cmdPid != 0 {
		line := fmt.Sprintf("\"%s\" patrol %s --slot %d", launcherExe(), filepath.Base(dir), slot)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		cmd := exec.CommandContext(ctx, "agents-cli", "_attach", "inject", strconv.FormatUint(uint64(cmdPid), 10), "1")
		cmd.Stdin = strings.NewReader(line)
		err := cmd.Run()
		cancel()
		if err == nil {
			for i := 0; i < 30; i++ {
				if patrolLive(dir) {
					return nil
				}
				time.Sleep(100 * time.Millisecond)
			}
		}
	}
	return startPatrolDetached(dir)
}

func startPatrolDetached(dir string) error {
	if os.Getenv("NIGHTSHIFT_PATROL") == "off" {
		return nil
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "patrol", filepath.Base(dir))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000 | 0x00000008} // CREATE_NO_WINDOW | DETACHED_PROCESS
	if err := cmd.Start(); err != nil {
		return err
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	_, created, _ := processInfo(uint32(pid))
	return writeJSONFile(filepath.Join(dir, "patrol.json"), object{{"pid", pid}, {"started", created}})
}

// patrolSlot returns the slot number where the live patrol is running visibly, or 0 if detached/not running.
func patrolSlot(dir string) int {
	if !patrolLive(dir) {
		return 0
	}
	info, ok := readObject(filepath.Join(dir, "patrol.json"))
	if !ok {
		return 0
	}
	return intValue(rowValue(info, "slot"))
}

// evacuatePatrolSlot moves a visible pane patrol out of `slot` to a detached background patrol so `consult` or
// `spawn` can use the slot's cmd.exe shell.
func evacuatePatrolSlot(dir string, slot int) {
	if slot == 0 || patrolSlot(dir) != slot {
		return
	}
	stopPatrol(dir)
	time.Sleep(250 * time.Millisecond)
	_ = startPatrolDetached(dir)
}

// patrolLive: the shift's recorded patrol process is still running.
func patrolLive(dir string) bool {
	info, ok := readObject(filepath.Join(dir, "patrol.json"))
	if !ok {
		return false
	}
	pid := processNumber(info, "pid")
	started, startedOK := objectNumber(info, "started")
	return pid != 0 && incarnationRunning(pid, started, startedOK)
}

// stopPatrol ends the shift's patrol (down); it would also exit by itself on the down event.
func stopPatrol(dir string) {
	if !patrolLive(dir) {
		return
	}
	info, _ := readObject(filepath.Join(dir, "patrol.json"))
	if p, err := os.FindProcess(int(processNumber(info, "pid"))); err == nil {
		_ = p.Kill()
	}
}
