package main

// `nightshift loop` (one shift per ticket, wait for result.json, down, note,
// next) and `nightshift resume` (rerun tickets whose latest shift never
// reached done). Ported from nightshift_shifts.py loop() and resume().

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	mailkeepExe    = "C:/Tools/mailkeep/Mailkeep.Cli.exe"
	notifyIdentity = "owner_id"
	notifyTo       = "owner@example.com"
	loopPoll       = 30 * time.Second
)

const loopUsage = `usage: nightshift loop [-h] [--project PREFIX] [--max N] [--cwd CWD]
                       [--stall-minutes STALL_MINUTES] [--retries RETRIES]
                       [--notify] [--no-notify]
                       [tickets ...]`

const loopHelp = loopUsage + `

Run tickets one per shift: start a crew, wait for result.json (or a stall),
tear the shift down, note the ticket, continue. Stops at the first
blocked/stalled/died shift so a human can look. With --project PREFIX [--max
N], works that project's backlog tickets (lowest number first, needs-human
skipped) and moves each to in-progress when its shift starts.

positional arguments:
  tickets               ticket IDs, worked in order

options:
  -h, --help            show this help message and exit
  --project PREFIX      work this project's backlog instead of listed tickets
  --max N               work at most N tickets
  --cwd CWD             working folder for every shift (default: each ticket
                        codebase)
  --stall-minutes STALL_MINUTES
  --retries RETRIES     rerun a ticket this many times after its crew dies or
                        stalls (default 2)
  --notify              email the owner via Mailkeep when the loop halts or
                        finishes
  --no-notify           do not email the owner (default)
`

const resumeUsage = `usage: nightshift resume [-h] [--project PREFIX] [--dry-run]
                         [--stall-minutes STALL_MINUTES] [--notify]
                         [--no-notify]`

const resumeHelp = resumeUsage + `

Rerun every ticket whose latest shift never reached done (crew died, stalled,
blocked or was stopped), each continuing from that shift's handover. Done and
still-running tickets are skipped. Locks held by dead shifts are cleared
first. Prints the plan as JSON.

options:
  -h, --help            show this help message and exit
  --project PREFIX      only tickets of this project, e.g. NGH
  --dry-run             print the plan; clear no locks and start nothing
  --stall-minutes STALL_MINUTES
  --notify              email the owner via Mailkeep when the loop halts or
                        finishes
  --no-notify           do not email the owner (default)
`

func init() {
	extraCommands["loop"] = func(argv []string) int { return commandExit(loop(argv, nil)) }
	extraCommands["resume"] = func(argv []string) int { return commandExit(resume(argv)) }
}

// notifyFlag is argparse's shared dest for --notify/--no-notify: the last one given wins.
func notifyFlag(argv []string, options []writeOption) bool {
	names := []string{"--help"}
	for _, o := range options {
		names = append(names, o.name)
	}
	notify := false
	for _, a := range argv {
		if a == "--" {
			break
		}
		name, _, _ := strings.Cut(a, "=")
		switch longOption(name, names) {
		case "--notify":
			notify = true
		case "--no-notify":
			notify = false
		}
	}
	return notify
}

// manyPositionals stands in for nargs='*': parseWriteArgs takes optional positionals by label.
func manyPositionals(label string) []string {
	labels := make([]string, 4096)
	for i := range labels {
		labels[i] = label
	}
	return labels
}

func intOption(args writeArgs, name string, fallback int) int {
	if s, ok := args.values[name]; ok {
		n, _ := parseInteger(s)
		return int(n)
	}
	return fallback
}

// emit prints one loop event on stdout as a compact JSON line.
func emit(event object) { outLine(renderJSON(event, false, true)) }

// pyText is str() of a JSON value as the oracle's f-strings print it.
func pyText(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case string:
		return x
	}
	return valueText(v)
}

func loop(argv []string, continueFrom map[string]string) error {
	options := []writeOption{{name: "--project"}, {name: "--max", number: "int"}, {name: "--cwd"},
		{name: "--stall-minutes", number: "int"}, {name: "--retries", number: "int"}, {name: "--notify", flag: true},
		{name: "--no-notify", flag: true}}
	args, parseError := parseWriteArgs(argv, options, nil, manyPositionals("tickets"))
	if args.help {
		out(loopHelp)
		return commandError{0, ""}
	}
	if parseError != "" {
		return commandError{writeUsage("nightshift loop", loopUsage, parseError), ""}
	}
	notify := notifyFlag(argv, options)
	stallMinutes, retries := intOption(args, "--stall-minutes", 20), intOption(args, "--retries", 2)
	project := args.values["--project"]
	if (len(args.pos) > 0) == (project != "") {
		return commandError{writeUsage("nightshift loop", loopUsage, "give either ticket IDs or --project PREFIX, not both"), ""}
	}
	tickets := args.pos
	if len(tickets) == 0 {
		var err error
		if tickets, err = projectTickets(project); err != nil {
			return err
		}
	}
	if _, ok := args.values["--max"]; ok {
		if n := intOption(args, "--max", 0); n < len(tickets) {
			tickets = tickets[:pySliceEnd(n, len(tickets))]
		}
	}
	if project != "" {
		emit(object{{"event", "selected"}, {"project", project}, {"tickets", stringsToAny(tickets)}})
	}
	_, underPytest := os.LookupEnv("PYTEST_CURRENT_TEST")
	if !underPytest {
		if title, err := windows.UTF16PtrFromString(loopTitle(tickets)); err == nil {
			windows.NewLazySystemDLL("kernel32.dll").NewProc("SetConsoleTitleW").Call(uintptr(unsafe.Pointer(title)))
		}
	}
	sendNotice := func(subject, status string, records []object) {
		if notify {
			outcome := notifyOwner(subject, notifyBody(status, records))
			errLine(renderJSON(append(object{{"event", "notified"}, {"subject", subject}}, outcome...), false, true))
		}
	}
	records := []object{}
	for _, ticket := range tickets {
		previous := continueFrom[ticket]
		var result object
		stateDir := ""
		for attempt := 1; attempt <= retries+1; attempt++ {
			extra := []string{}
			if cwd := args.values["--cwd"]; cwd != "" {
				extra = append(extra, "--cwd", cwd)
			}
			if previous != "" {
				extra = append(extra, "--continue-from", previous)
			}
			dir, err := runStart(append([]string{ticket}, extra...), true)
			if err != nil {
				var exit commandError
				if !errors.As(err, &exit) || exit.code != 3 {
					return err
				}
				// Another live shift holds the folder: a human has to look, so no down and no retry.
				summary := exit.message
				if summary == "" {
					summary = "3"
				}
				result, stateDir = object{{"status", "blocked"}, {"summary", summary}}, ""
				runQuiet(60*time.Second, "tasks-cli", "note", ticket, "--heading", "Nightshift loop", "--note",
					fmt.Sprintf("Nightshift loop: attempt %d could not start: %s", attempt, summary))
				emit(append(object{{"event", "ended"}, {"ticket", ticket}, {"attempt", attempt}}, result...))
				break
			}
			stateDir = dir
			shift := filepath.Base(stateDir)
			if project != "" && attempt == 1 {
				if _, stderr, code := runQuiet(60*time.Second, "tasks-cli", "move", ticket, "in-progress"); code != 0 {
					emit(object{{"event", "move_failed"}, {"ticket", ticket}, {"error", strings.TrimSpace(stderr)}})
				}
			}
			emit(object{{"event", "started"}, {"ticket", ticket}, {"shift", shift}, {"attempt", attempt}})
			result = waitForResult(stateDir, stallMinutes)
			if objectString(result, "status") == "done" {
				stage, rung := resultStage(result), requires(stateDir)
				if stagesIndex(stage) < stagesIndex(rung) {
					// Done below the ticket's rung is not done: it stops the loop for a human, like blocked.
					summary := pyText(rowValue(result, "summary"))
					if _, ok := objectGet(result, "summary"); !ok {
						summary = ""
					}
					result = setValue(setValue(setValue(setValue(append(object{}, result...), "status", "short"), "stage", stage), "requires", rung),
						"summary", "stage "+stage+" below required "+rung+": "+summary)
				}
			}
			if code := downCommand([]string{shift, "--close"}); code != 0 {
				return commandError{code, ""}
			}
			note := fmt.Sprintf("Nightshift loop: shift %s (attempt %d) ended with status %s: %s", shift, attempt,
				pyText(rowValue(result, "status")), resultSummary(result))
			if stage := rowValue(result, "stage"); truthy(stage) {
				note += " Stage: " + pyText(stage) + "."
			}
			denials := loadDenials(stateDir)
			if status := objectString(result, "status"); (status == "blocked" || status == "short") && len(denials) > 0 {
				note += " Denials: " + denialText(denials, "; ")
			}
			runQuiet(60*time.Second, "tasks-cli", "note", ticket, "--heading", "Nightshift loop", "--note", note)
			ended := object{{"event", "ended"}, {"ticket", ticket}, {"shift", shift}, {"attempt", attempt}}
			for _, p := range result {
				ended = setValue(ended, p.key, p.val)
			}
			emit(ended)
			// An interrupted shift (died, or sat idle) is rerun with a fresh crew that gets the handover;
			// blocked is the crew asking for a human, so that stops the loop.
			if status := objectString(result, "status"); (status == "died" || status == "stalled") && attempt <= retries {
				previous = stateDir
				emit(object{{"event", "retrying"}, {"ticket", ticket}, {"after", status}})
				continue
			}
			break
		}
		handover, denials := "no shift started", []any{}
		if stateDir != "" {
			handover, denials = filepath.Join(stateDir, "handover.md"), loadDenials(stateDir)
		}
		records = append(records, object{{"ticket", ticket}, {"status", rowValue(result, "status")}, {"summary", resultSummaryValue(result)},
			{"handover", handover}, {"denials", denials}})
		if status := objectString(result, "status"); status != "done" {
			emit(object{{"event", "stopped"}, {"reason", rowValue(result, "status")}, {"ticket", ticket}})
			sendNotice("Nightshift: "+pyText(rowValue(result, "status"))+" "+ticket, pyText(rowValue(result, "status")), records[len(records)-1:])
			return commandError{2, ""}
		}
	}
	emit(object{{"event", "finished"}, {"tickets", stringsToAny(tickets)}})
	last := "batch"
	if len(tickets) > 0 {
		last = tickets[len(tickets)-1]
	}
	sendNotice("Nightshift: finished "+last, "finished", records)
	// A finished loop closes its own window; a stopped one stays open so the human can read why.
	// Never under pytest: a crew running the suite closed the live loop's window (15 Oct).
	if !underPytest {
		closeTerminalWindows(loopTitle(tickets))
	}
	return nil
}

// pySliceEnd is the end index of tickets[:n] for a possibly negative n.
func pySliceEnd(n, length int) int {
	if n < 0 {
		n += length
		if n < 0 {
			n = 0
		}
	}
	return n
}

func resultSummaryValue(result object) any {
	if v, ok := objectGet(result, "summary"); ok {
		return v
	}
	return ""
}

func resultSummary(result object) string { return pyText(resultSummaryValue(result)) }

func denialText(denials []any, sep string) string {
	parts := []string{}
	for _, d := range denials {
		o, _ := d.(object)
		parts = append(parts, pyText(rowValue(o, "role"))+": "+pyText(rowValue(o, "line")))
	}
	return strings.Join(parts, sep)
}

// runQuiet runs a command with captured output; stdout, stderr and exit code (-1 when it could not run).
func runQuiet(timeout time.Duration, name string, args ...string) (string, string, int) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	data, err := cmd.Output()
	stdout := strings.ToValidUTF8(string(data), "\ufffd")
	errText := strings.ToValidUTF8(stderr.String(), "\ufffd")
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return stdout, errText, exit.ExitCode()
	}
	if err != nil {
		return stdout, err.Error(), -1
	}
	return stdout, errText, 0
}

// waitForResult blocks until result.json appears, the crew dies, or every session sits idle past the stall.
func waitForResult(stateDir string, stallMinutes int) object {
	resultPath := filepath.Join(stateDir, "result.json")
	for {
		if fileExists(resultPath) {
			if v, ok := readJSONFile(resultPath); ok {
				if o, ok := v.(object); ok {
					return o
				}
			}
			return object{{"status", "blocked"}, {"summary", "result.json is not valid JSON"}}
		}
		record, err := statusRecord(stateDir)
		if err != nil || !objectBool(record, "alive") {
			return object{{"status", "died"}, {"summary", "crew processes ended without writing result.json"}}
		}
		roles, _ := rowValue(record, "roles").(object)
		tails := object{}
		for _, r := range roles {
			if info, ok := r.val.(object); ok && objectBool(info, "alive") {
				tails = append(tails, pair{r.key, paneTail(processNumber(info, "pid"), 40)})
			}
		}
		_, _ = recordDenials(stateDir, tails)
		// only live roles count towards a stall (a not-started Advisor has no session)
		stalled := false
		for _, r := range roles {
			info, _ := r.val.(object)
			if !objectBool(info, "alive") {
				continue
			}
			stalled = true
			idle := sessionIdleSince(processNumber(info, "pid"))
			if idle == nil || *idle < float64(stallMinutes*60) {
				stalled = false
				break
			}
		}
		if stalled {
			return object{{"status", "stalled"}, {"summary", fmt.Sprintf("both sessions idle for %d minutes with no result.json", stallMinutes)}}
		}
		time.Sleep(loopPoll)
	}
}

// projectTickets lists a project's backlog ticket IDs, lowest number first, skipping needs-human.
func projectTickets(prefix string) ([]string, error) {
	stdout, stderr, code := runQuiet(60*time.Second, "tasks-cli", "search", "--prefix", prefix, "--status", "backlog", "--all")
	if code != 0 {
		return nil, startExit(fmt.Sprintf("tasks-cli search --prefix %s failed: %s", prefix, strings.TrimSpace(stderr)))
	}
	value, err := parseJSON([]byte(stdout))
	data, ok := value.(object)
	if err != nil || !ok {
		return nil, startExit("tasks-cli search --prefix " + prefix + " printed no JSON object")
	}
	results, _ := rowValue(data, "results").([]any)
	type task struct {
		id     string
		number float64
	}
	tasks := []task{}
	for _, item := range results {
		t, _ := item.(object)
		tags, _ := rowValue(t, "tags").([]any)
		human := false
		for _, tag := range tags {
			human = human || tag == "needs-human"
		}
		if !human {
			n, _ := objectNumber(t, "number")
			tasks = append(tasks, task{valueText(rowValue(t, "task_id")), numberFloat(n)})
		}
	}
	sort.SliceStable(tasks, func(i, j int) bool { return tasks[i].number < tasks[j].number })
	ids := []string{}
	for _, t := range tasks {
		ids = append(ids, t.id)
	}
	return ids, nil
}

// notifyOwner emails the owner through Mailkeep (prepare, queue, send); it never fails the loop.
func notifyOwner(subject, body string) object {
	outcome := object{{"sent", false}, {"draft_id", nil}, {"error", nil}}
	run := func(argv ...string) (object, error) {
		stdout, stderr, code := runQuiet(120*time.Second, mailkeepExe, argv...)
		if code != 0 {
			return nil, fmt.Errorf("mailkeep %s exited %d: %s", argv[0], code, strings.TrimSpace(stderr))
		}
		value, err := parseJSON([]byte(stdout))
		if err != nil {
			return nil, fmt.Errorf("mailkeep %s printed no JSON", argv[0])
		}
		o, _ := value.(object)
		return o, nil
	}
	err := func() error {
		tmp, err := os.MkdirTemp("", "nightshift-notify-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(tmp)
		bodyFile := filepath.Join(tmp, "body.txt")
		if err := writeTextFile(bodyFile, body); err != nil {
			return err
		}
		prepared, err := run("prepare", "--identity", notifyIdentity, "--to", notifyTo, "--subject", subject, "--body-file", bodyFile, "--execute")
		if err != nil {
			return err
		}
		draft := rowValue(prepared, "id")
		if !truthy(draft) {
			return errors.New("mailkeep prepare returned no id")
		}
		outcome = setValue(outcome, "draft_id", draft)
		id := valueText(draft)
		queued, err := run("queue", id, "--execute")
		if err != nil {
			return err
		}
		if v, ok := objectGet(queued, "ok"); ok && v == false {
			return fmt.Errorf("mailkeep queue %s reported ok:false", id)
		}
		sent, err := run("send", id, "--execute")
		if err != nil {
			return err
		}
		confirmed := rowValue(sent, "ok") == true
		results, _ := rowValue(sent, "results").([]any)
		for _, r := range results {
			if o, ok := r.(object); ok && valueText(rowValue(o, "id")) == id && rowValue(o, "ok") == true {
				confirmed = true
			}
		}
		if !confirmed {
			return fmt.Errorf("mailkeep send did not confirm %s", id)
		}
		outcome = setValue(outcome, "sent", true)
		return nil
	}()
	if err != nil {
		outcome = setValue(outcome, "error", err.Error())
	}
	return outcome
}

// notifyBody is the owner email: overall status, then each ticket's result, handover and denials.
func notifyBody(status string, records []object) string {
	lines := []string{"Status: " + status, ""}
	for _, r := range records {
		lines = append(lines, "Ticket: "+pyText(rowValue(r, "ticket")),
			"Result: "+pyText(rowValue(r, "status"))+": "+pyText(rowValue(r, "summary")),
			"Handover: "+pyText(rowValue(r, "handover")), "Denials:")
		denials, _ := rowValue(r, "denials").([]any)
		if len(denials) == 0 {
			lines = append(lines, "  (none)")
		}
		for _, d := range denials {
			lines = append(lines, "  "+denialText([]any{d}, ""))
		}
		lines = append(lines, "")
	}
	return strings.Join(lines, "\n")
}

// closeTerminalWindows posts WM_CLOSE to every Terminal window whose title starts with prefix.
func closeTerminalWindows(prefix string) int {
	n := 0
	for _, w := range terminalWindows() {
		if strings.HasPrefix(w.title, prefix) {
			postWindowClose(w.hwnd)
			n++
		}
	}
	return n
}

// staleLocks: folder locks whose shift folder is missing or whose crew is no longer alive.
func staleLocks() []lifecycleLock {
	stale := []lifecycleLock{}
	for _, lock := range lifecycleLocks() {
		if lockIsStale(lock.record) {
			stale = append(stale, lock)
		}
	}
	return stale
}

func lockIsStale(lock object) bool {
	if objectString(lock, "kind") == "landing" && processNumber(lock, "pid") != 0 && processRunningSince(lock, "started_epoch") {
		return false
	}
	holder := lockShiftDir(lock)
	if holder == "" {
		return true
	}
	if info, err := os.Stat(holder); err != nil || !info.IsDir() {
		return true
	}
	record, err := statusRecord(holder)
	return err != nil || !objectBool(record, "alive")
}

type ticketKey struct {
	prefix string
	number int
}

// ticketSortKey: (prefix, number) for PREFIX-N, else (ticket, -1).
func ticketSortKey(ticket string) ticketKey {
	i := strings.LastIndex(ticket, "-")
	number := ticket[i+1:]
	if n, err := strconv.Atoi(number); err == nil && number != "" && strings.Trim(number, "0123456789") == "" {
		return ticketKey{ticket[:max(i, 0)], n}
	}
	return ticketKey{ticket, -1}
}

// resumePlan: which tickets' latest shift (newest ticket.txt) never reached done.
func resumePlan(project string) (object, error) {
	type latest struct {
		mtime time.Time
		dir   string
	}
	found := map[string]latest{}
	for _, d := range shiftDirs() {
		ticketFile := filepath.Join(d, "ticket.txt")
		info, err := os.Stat(ticketFile)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		text, err := readTextUniversal(ticketFile)
		if err != nil {
			return nil, err
		}
		ticket := strings.TrimSpace(text)
		if ticket == "" || (project != "" && strings.SplitN(ticket, "-", 2)[0] != project) {
			continue
		}
		if l, ok := found[ticket]; !ok || info.ModTime().After(l.mtime) {
			found[ticket] = latest{info.ModTime(), d}
		}
	}
	tickets := make([]string, 0, len(found))
	for t := range found {
		tickets = append(tickets, t)
	}
	sort.Slice(tickets, func(i, j int) bool {
		a, b := ticketSortKey(tickets[i]), ticketSortKey(tickets[j])
		if a.prefix != b.prefix {
			return a.prefix < b.prefix
		}
		return a.number < b.number
	})
	resumeList, skipped := []any{}, []any{}
	for _, ticket := range tickets {
		d := found[ticket].dir
		record, err := statusRecord(d)
		if err != nil {
			return nil, err
		}
		result := objectString(record, "result")
		if result == "done" {
			skipped = append(skipped, object{{"ticket", ticket}, {"last_shift", filepath.Base(d)}, {"reason", "done"}})
			continue
		}
		if objectBool(record, "alive") {
			skipped = append(skipped, object{{"ticket", ticket}, {"last_shift", filepath.Base(d)}, {"reason", "running"}})
			continue
		}
		reason := "result " + result + " without handover"
		if fileExists(filepath.Join(d, "handover.md")) {
			reason = "handover without done (result " + result + ")"
		} else if result == "none" {
			reason = "no handover"
		}
		resumeList = append(resumeList, object{{"ticket", ticket}, {"continue_from", d}, {"last_shift", filepath.Base(d)},
			{"last_result", result}, {"reason", reason}})
	}
	locks := []any{}
	for _, lock := range staleLocks() {
		locks = append(locks, object{{"lock", lock.path}, {"shift", rowValue(lock.record, "shift")}, {"cwd", rowValue(lock.record, "cwd")}})
	}
	return object{{"resume", resumeList}, {"skipped", skipped}, {"stale_locks", locks}}, nil
}

func resume(argv []string) error {
	options := []writeOption{{name: "--project"}, {name: "--dry-run", flag: true}, {name: "--stall-minutes", number: "int"},
		{name: "--notify", flag: true}, {name: "--no-notify", flag: true}}
	args, parseError := parseWriteArgs(argv, options, nil, nil)
	if args.help {
		out(resumeHelp)
		return commandError{0, ""}
	}
	if parseError != "" {
		return commandError{writeUsage("nightshift resume", resumeUsage, parseError), ""}
	}
	dryRun := args.present["--dry-run"]
	plan, err := resumePlan(args.values["--project"])
	if err != nil {
		return startExit(err.Error())
	}
	outLine(renderJSON(append(object{{"dry_run", dryRun}}, plan...), true, true))
	if dryRun {
		return nil
	}
	locks, _ := rowValue(plan, "stale_locks").([]any)
	for _, entry := range locks {
		path := objectString(entry.(object), "lock")
		_ = withMailboxMutex(path, func() error {
			if held, ok := readObject(path); ok && lockIsStale(held) {
				_ = os.Remove(path)
			}
			return nil
		})
	}
	entries, _ := rowValue(plan, "resume").([]any)
	if len(entries) == 0 {
		return nil
	}
	tickets := []string{}
	continueFrom := map[string]string{}
	for _, e := range entries {
		o := e.(object)
		tickets = append(tickets, objectString(o, "ticket"))
		continueFrom[objectString(o, "ticket")] = objectString(o, "continue_from")
	}
	flags := []string{"--stall-minutes", strconv.Itoa(intOption(args, "--stall-minutes", 20))}
	if notifyFlag(argv, options) {
		flags = append(flags, "--notify")
	}
	return loop(append(tickets, flags...), continueFrom)
}
