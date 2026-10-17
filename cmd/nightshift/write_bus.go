package main

// Pull-based crew messaging (NGH-060): `ready`, `assign`, `note`, `report` store a mailbox row and nudge the
// recipient with one line; `wait` blocks until a row for the caller arrives, acknowledges it and prints the
// stored body. The body is never typed into a pane. docs/ARCHITECTURE.md "Pull-based delivery (NGH-060)".

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func init() {
	extraCommands["ready"] = readyCommand
	extraCommands["assign"] = func(argv []string) int { return leadCommand("assign", argv) }
	extraCommands["note"] = func(argv []string) int { return leadCommand("note", argv) }
	extraCommands["report"] = reportCommand
	extraCommands["wait"] = waitCommand
}

const (
	readyUsage  = "usage: nightshift ready [-h] shift role"
	assignUsage = "usage: nightshift assign [-h] [--asg ASG] [--coding-asg CODING_ASG]\n                         [--ready-timeout SECS]\n                         shift role text"
	noteUsage   = "usage: nightshift note [-h] [--asg ASG] [--ready-timeout SECS] shift role text"
	reportUsage = "usage: nightshift report [-h] [--asg ASG] shift role text"
	waitUsage   = "usage: nightshift wait [-h] [--timeout SECS] [--json] shift role"
)

const busPositionals = "positional arguments:\n  shift                 shift name, unique suffix, or \"latest\"\n  role                  the member's crew label (Coder, Tester) or session name\n"

var busHelp = map[string]string{
	"ready":  readyUsage + "\n\nRecord READY from ROLE to the lead and nudge the lead. A second READY from the\nsame incarnation exits 4.\n\n" + busPositionals + "\noptions:\n  -h, --help            show this help message and exit\n",
	"assign": assignUsage + "\n\nStore a lead -> ROLE assignment once ROLE is ready, then nudge it to run\nnightshift wait. Exit 3 = not ready within --ready-timeout (nothing stored),\n4 = refused.\n\n" + busPositionals + "  text                  the assignment body\n\noptions:\n  -h, --help            show this help message and exit\n  --asg ASG             assignment id (generated as <role>-<n> when omitted)\n  --coding-asg CODING_ASG\n                        coding assignment verified by this Tester assignment\n  --ready-timeout SECS  seconds to wait for ROLE's READY (default 300)\n",
	"note":   noteUsage + "\n\nStore a lead -> ROLE note once ROLE is ready, then nudge it to run nightshift\nwait. Exit 3 = not ready within --ready-timeout, 4 = refused.\n\n" + busPositionals + "  text                  the note body\n\noptions:\n  -h, --help            show this help message and exit\n  --asg ASG             assignment id this note belongs to\n  --ready-timeout SECS  seconds to wait for ROLE's READY (default 300)\n",
	"report": reportUsage + "\n\nStore ROLE's result for the lead; the first line is the verdict or summary.\nA first line starting APPRENTICE-NEEDS-SENIOR is stored as a note.\n\n" + busPositionals + "  text                  the report body\n\noptions:\n  -h, --help            show this help message and exit\n  --asg ASG             assignment id (default: ROLE's latest assignment)\n",
	"wait":   waitUsage + "\n\nBlock until a message for ROLE arrives, acknowledge it and print\n'msg:N kind:K asg:A from:SENDER' and the stored body. Exit 3 = timed out.\n\n" + busPositionals + "\noptions:\n  -h, --help            show this help message and exit\n  --timeout SECS        give up after SECS (default 540; 0 waits forever)\n  --json                print the message row as JSON\n",
}

// busExit prints the one-line reason for a non-zero exit on stderr.
func busExit(err error) int {
	if err == nil {
		return 0
	}
	var ce commandError
	if errors.As(err, &ce) {
		if ce.message != "" {
			errLine(ce.message)
		}
		return ce.code
	}
	errLine(err.Error())
	return 1
}

// busParse parses SHIFT ROLE [TEXT] plus options; it returns the resolved shift, canonical label and lead.
func busParse(command string, argv []string, opts []writeOption, positionals []string) (writeArgs, string, string, string, int, bool) {
	usage := map[string]string{"ready": readyUsage, "assign": assignUsage, "note": noteUsage, "report": reportUsage, "wait": waitUsage}[command]
	a, parseErr := parseWriteArgs(argv, opts, positionals, nil)
	if a.help {
		out(busHelp[command])
		return a, "", "", "", 0, false
	}
	if parseErr != "" {
		return a, "", "", "", writeUsage("nightshift "+command, usage, parseErr), false
	}
	if len(positionals) == 3 && strings.TrimSpace(a.pos[2]) == "" {
		return a, "", "", "", busExit(commandError{2, "nightshift " + command + ": error: the text is empty; pass the message text as the third argument"}), false
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return a, "", "", "", busExit(err), false
	}
	label, _, err := canonicalLabel(dir, a.pos[1])
	if err != nil {
		return a, "", "", "", busExit(err), false
	}
	lead := humanLabel(dir)
	if label == lead && command != "wait" {
		return a, "", "", "", busExit(commandError{4, fmt.Sprintf("%s is the lead and cannot address itself; name a crew member", lead)}), false
	}
	return a, dir, label, lead, 0, true
}

func readyCommand(argv []string) int {
	_, dir, label, lead, code, ok := busParse("ready", argv, nil, []string{"shift", "role"})
	if !ok {
		return code
	}
	if memberRole(dir, label) == "Advisor" {
		return busExit(commandError{4, "the Advisor never sends READY; its first message is the question, answer it with nightshift report"})
	}
	inc, incOK := currentIncarnation(dir, label)
	row, err := storeBusMessage(dir, func(db *sql.DB, rows []object) (writeArgs, string, string, error) {
		for _, r := range rows {
			if isReady(r, label, inc, incOK) {
				return writeArgs{}, "", "", commandError{4, fmt.Sprintf("already READY as %s (msg:%v); run nightshift wait %s %s", label, rowValue(r, "id"), filepath.Base(dir), label)}
			}
		}
		return busArgs("note", "READY", nil, ""), label, lead, nil
	})
	if err != nil {
		return busExit(err)
	}
	deliverBusMessage(dir, row)
	return 0
}

// leadCommand is assign and note: lead -> ROLE once ROLE is ready.
func leadCommand(command string, argv []string) int {
	opts := []writeOption{{name: "--asg"}, {name: "--ready-timeout", number: "float"}}
	if command == "assign" {
		opts = append(opts, writeOption{name: "--coding-asg"})
	}
	a, dir, label, lead, code, ok := busParse(command, argv, opts, []string{"shift", "role", "text"})
	if !ok {
		return code
	}
	if status, _ := memberStatus(dir, label); status == "not_started" {
		return busExit(commandError{4, fmt.Sprintf("%s is asleep; use nightshift consult|summon", label)})
	}
	timeout := 300.0
	if a.present["--ready-timeout"] {
		timeout, _ = parseNumber(a.values["--ready-timeout"], "float")
	}
	if timeout < 0 {
		return busExit(commandError{2, "nightshift " + command + ": error: argument --ready-timeout: must not be negative"})
	}
	kind := map[string]string{"assign": "assignment", "note": "note"}[command]
	if err := waitReady(dir, label, timeout, command); err != nil {
		return busExit(err)
	}
	row, err := storeBusMessage(dir, func(db *sql.DB, rows []object) (writeArgs, string, string, error) {
		var asg any
		if a.present["--asg"] {
			asg = a.values["--asg"]
		} else if kind == "assignment" {
			asg = generatedAsg(rows, label)
		}
		return busArgs(kind, a.pos[2], asg, a.values["--coding-asg"]), lead, label, nil
	})
	if err != nil {
		return busExit(err)
	}
	deliverBusMessage(dir, row)
	return 0
}

func reportCommand(argv []string) int {
	a, dir, label, lead, code, ok := busParse("report", argv, []writeOption{{name: "--asg"}}, []string{"shift", "role", "text"})
	if !ok {
		return code
	}
	text := a.pos[2]
	if strings.TrimSpace(text) == "READY" {
		return busExit(commandError{4, fmt.Sprintf("READY is not a report; run nightshift ready %s %s", filepath.Base(dir), label)})
	}
	kind := "result"
	if strings.HasPrefix(text, "APPRENTICE-NEEDS-SENIOR") {
		kind = "note"
	}
	inc, incOK := currentIncarnation(dir, label)
	row, err := storeBusMessage(dir, func(db *sql.DB, rows []object) (writeArgs, string, string, error) {
		var asg any
		if a.present["--asg"] {
			asg = a.values["--asg"]
		} else {
			for _, r := range rows {
				if rowString(r, "recipient") == label && contains([]string{"assignment", "uplift"}, rowString(r, "kind")) && sameNumber(inc, incOK, rowValue(r, "recipient_incarnation")) {
					asg = rowValue(r, "asg")
				}
			}
		}
		return busArgs(kind, text, asg, ""), label, lead, nil
	})
	if err != nil {
		return busExit(err)
	}
	deliverBusMessage(dir, row)
	return 0
}

// busArgs is the msg send argument set dispatchMetadata validates.
func busArgs(kind, body string, asg any, coding string) writeArgs {
	a := writeArgs{values: map[string]string{"--kind": kind, "--body": body}, present: map[string]bool{"--kind": true, "--body": true}, lists: map[string][]string{}}
	if asg != nil {
		a.values["--asg"], a.present["--asg"] = valueText(asg), true
	}
	if coding != "" {
		a.values["--coding-asg"], a.present["--coding-asg"] = coding, true
	}
	return a
}

// storeBusMessage validates and stores one row under the claims and mailbox locks, prints its header line and
// returns the stored row. build sees every row so far and returns the msg send arguments, sender and recipient.
func storeBusMessage(dir string, build func(*sql.DB, []object) (writeArgs, string, string, error)) (object, error) {
	var row object
	err := withClaimsLock(dir, func() error {
		return withWriteMailbox(dir, func(db *sql.DB) error {
			rows, err := queryMessages(db, "SELECT * FROM messages ORDER BY id")
			if err != nil {
				return err
			}
			args, sender, recipient, err := build(db, rows)
			if err != nil {
				return err
			}
			metadata, _, err := dispatchMetadata(db, dir, args, sender, recipient)
			if err != nil {
				var ce commandError
				if errors.As(err, &ce) {
					return err
				}
				return commandError{4, err.Error() + "; nothing stored"}
			}
			var asg any
			if args.present["--asg"] {
				asg = args.values["--asg"]
			}
			id, err := insertMessage(db, dir, args.values["--kind"], sender, recipient, args.values["--body"], asg, metadata)
			if err != nil {
				return err
			}
			row, err = getMessage(db, id)
			return err
		})
	})
	if err != nil {
		return nil, err
	}
	outLine(msgHeader(row))
	return row, nil
}

func generatedAsg(rows []object, label string) string {
	used := map[string]bool{}
	for _, r := range rows {
		used[valueText(rowValue(r, "asg"))] = true
	}
	for n := 1; ; n++ {
		if asg := fmt.Sprintf("%s-%d", strings.ToLower(label), n); !used[asg] {
			return asg
		}
	}
}

func isReady(r object, label string, inc json.Number, incOK bool) bool {
	return rowString(r, "sender") == label && rowString(r, "kind") == "note" && rowString(r, "body") == "READY" && sameNumber(inc, incOK, rowValue(r, "sender_incarnation"))
}

// memberReady: ROLE's current incarnation sent READY, or already acknowledged a row (consult/summon starts send none).
func memberReady(rows []object, label string, inc json.Number, incOK bool) bool {
	for _, r := range rows {
		if isReady(r, label, inc, incOK) || (rowString(r, "recipient") == label && rowString(r, "state") == "acknowledged" && sameNumber(inc, incOK, rowValue(r, "recipient_incarnation"))) {
			return true
		}
	}
	return false
}

func memberStatus(dir, label string) (string, error) {
	record, err := statusRecord(dir)
	if err != nil {
		return "", err
	}
	roles, _ := rowValue(record, "roles").(object)
	role, _ := rowValue(roles, label).(object)
	return rowString(role, "status"), nil
}

func waitReady(dir, label string, timeout float64, command string) error {
	deadline := time.Now().Add(time.Duration(timeout * float64(time.Second)))
	for {
		inc, incOK := currentIncarnation(dir, label)
		var ready bool
		err := withWriteMailbox(dir, func(db *sql.DB) error {
			rows, err := queryMessages(db, "SELECT * FROM messages ORDER BY id")
			ready = memberReady(rows, label, inc, incOK)
			return err
		})
		if err != nil || ready {
			return err
		}
		if !time.Now().Before(deadline) {
			return commandError{3, fmt.Sprintf("%s has not sent READY within %ss; check the pane, then rerun nightshift %s", label, secondsText(timeout), command)}
		}
		time.Sleep(time.Second)
	}
}

func secondsText(s float64) string { return strconv.FormatFloat(s, 'f', -1, 64) }

func waiterPath(dir, label string) string { return filepath.Join(dir, "waiting", label+".json") }

// deliverBusMessage marks the row sent when the recipient is blocked in wait, otherwise nudges its pane with one
// line. A failed nudge leaves the row stored for the warden and only warns.
func deliverBusMessage(dir string, row object) {
	id, _ := rowNumber(row, "id")
	n, _ := strconv.ParseInt(id.String(), 10, 64)
	recipient := rowString(row, "recipient")
	markSent := func(via string) {
		err := withWriteMailbox(dir, func(db *sql.DB) error { return markMessage(db, n, true) })
		if err != nil {
			errLine(fmt.Sprintf("warning: msg:%d not marked sent: %s", n, err))
			return
		}
		busAppend(dir, "sent", object{{"id", n}, {"via", via}})
	}
	if waiter, ok := readObject(waiterPath(dir, recipient)); ok && processRunning(processNumber(waiter, "pid")) {
		markSent("wait")
		return
	}
	if os.Getenv("NIGHTSHIFT_NUDGE") == "off" {
		return
	}
	info, _ := readObject(filepath.Join(dir, recipient+".json"))
	pid := processNumber(info, "pid")
	started, startedOK := objectNumber(info, "started")
	tool := objectString(info, "tool")
	if tool == "" {
		tool = "claude"
	}
	reason := ""
	if pid == 0 || !pidAlive(pid, started, startedOK) {
		reason = recipient + " has no live pane"
	} else if !sendHeader(pid, fmt.Sprintf("%s  run: nightshift wait %s %s", msgHeader(row), filepath.Base(dir), recipient), tool) {
		reason = "agents-cli send did not submit the nudge"
	} else {
		markSent("nudge")
		return
	}
	busAppend(dir, "nudge-failed", object{{"id", n}, {"to", recipient}, {"reason", reason}})
	errLine(fmt.Sprintf("warning: msg:%d stored but not nudged (%s); the warden retries", n, reason))
}

// waitMatch returns the lowest-id unacknowledged row for label's current incarnation, or nil.
func waitMatch(rows []object, label string, inc float64, incOK bool) object {
	var match object
	var matchID float64
	current := json.Number(strconv.FormatFloat(inc, 'g', -1, 64))
	for _, r := range rows {
		state := rowString(r, "state")
		if rowString(r, "recipient") != label || (state != "stored" && state != "sent") || !sameNumber(current, incOK, rowValue(r, "recipient_incarnation")) {
			continue
		}
		id, _ := rowNumber(r, "id")
		if match == nil || numberFloat(id) < matchID {
			match, matchID = r, numberFloat(id)
		}
	}
	return match
}

// claimWaiter makes this process the owner of the waiter file unless another running wait already holds it;
// wait calls it on every poll, so a surviving wait re-claims the file after the owner exits.
func claimWaiter(path string, since float64) error {
	if holder, ok := readObject(path); ok {
		if pid := processNumber(holder, "pid"); pid == uint32(os.Getpid()) || processRunning(pid) {
			return nil
		}
	}
	return writeJSONFile(path, object{{"pid", os.Getpid()}, {"since", since}})
}

// releaseWaiter removes the waiter file only while it still records this process.
func releaseWaiter(path string) {
	if holder, ok := readObject(path); ok && processNumber(holder, "pid") == uint32(os.Getpid()) {
		os.Remove(path)
	}
}

func waitCommand(argv []string) int {
	a, dir, label, lead, code, ok := busParse("wait", argv, []writeOption{{name: "--timeout", number: "float"}}, []string{"shift", "role"})
	if !ok {
		return code
	}
	timeout := 540.0
	if a.present["--timeout"] {
		timeout, _ = parseNumber(a.values["--timeout"], "float")
	}
	if timeout < 0 {
		return busExit(commandError{2, "nightshift wait: error: argument --timeout: must not be negative"})
	}
	waiter := waiterPath(dir, label)
	if err := os.MkdirAll(filepath.Dir(waiter), 0777); err != nil {
		return busExit(err)
	}
	since := float64(time.Now().UnixNano()) / 1e9
	defer releaseWaiter(waiter)
	interrupted := make(chan os.Signal, 1)
	signal.Notify(interrupted, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(interrupted)

	deadline := time.Now().Add(time.Duration(timeout * float64(time.Second)))
	for {
		if err := claimWaiter(waiter, since); err != nil {
			return busExit(err)
		}
		if label == lead {
			if err := raiseLeadEvents(dir, lead); err != nil {
				return busExit(err)
			}
		}
		var row object
		err := withWriteMailbox(dir, func(db *sql.DB) error {
			rows, err := queryMessages(db, "SELECT * FROM messages WHERE recipient = ? ORDER BY id", label)
			if err != nil {
				return err
			}
			inc, incOK := currentIncarnation(dir, label)
			incFloat, _ := strconv.ParseFloat(inc.String(), 64)
			match := waitMatch(rows, label, incFloat, incOK)
			if match == nil {
				return nil
			}
			id, _ := rowNumber(match, "id")
			n, _ := strconv.ParseInt(id.String(), 10, 64)
			if err = markMessage(db, n, false); err != nil {
				return err
			}
			row, err = getMessage(db, n)
			return err
		})
		if err != nil {
			return busExit(err)
		}
		if row != nil {
			busAppend(dir, "ack", object{{"id", rowValue(row, "id")}, {"by", label}, {"outcome", "acknowledged"}, {"via", "wait"}})
			if forceJSON {
				outLine(renderJSON(row, false, true))
			} else {
				outLine(fmt.Sprintf("msg:%v kind:%s asg:%s from:%s", rowValue(row, "id"), rowString(row, "kind"), asgText(rowValue(row, "asg")), rowString(row, "sender")))
				outLine(rowString(row, "body"))
			}
			return 0
		}
		if timeout > 0 && !time.Now().Before(deadline) {
			outLine(fmt.Sprintf("timeout: nothing for %s in %ss; run nightshift wait %s %s again", label, secondsText(timeout), filepath.Base(dir), label))
			return 3
		}
		select {
		case <-interrupted:
			return 130
		case <-time.After(time.Second):
		}
	}
}

// Failure events (NGH-061): durable mailbox rows of kind `event` from `Nightshift` to the lead, so the lead's
// wait and inbox see them. `reason` holds the (event, subject) key that keeps each one raised at most once.

const eventSender = "Nightshift"

// raiseEvent stores the event row and its bus line unless an event row with the same key exists; it reports
// whether it raised one.
func raiseEvent(dir, event, subject, body string, asg any, fields object) (bool, error) {
	key := event + " " + subject
	raised := false
	err := withWriteMailbox(dir, func(db *sql.DB) error {
		rows, err := queryMessages(db, "SELECT id FROM messages WHERE kind = 'event' AND reason = ?", key)
		if err != nil || len(rows) > 0 {
			return err
		}
		metadata := object{{"reason", key}, {"evidence", renderJSON(fields, false, true)}}
		if _, err = insertMessage(db, dir, "event", eventSender, humanLabel(dir), body, asg, metadata); err != nil {
			return err
		}
		raised = true
		return nil
	})
	if raised {
		busAppend(dir, event, fields)
	}
	return raised, err
}

// raiseLeadEvents runs in the lead's wait poll: member-died for a started member whose pane process is gone,
// asg-timeout for a coding assignment with no result 900 seconds after delivery. Nothing is raised after down.
func raiseLeadEvents(dir, lead string) error {
	if strings.Contains(readText(filepath.Join(dir, "bus.jsonl")), `"event": "down"`) {
		return nil
	}
	rows, err := readMessages(dir)
	if err != nil {
		return err
	}
	_, members := crewMembers(dir)
	for _, m := range members {
		label := objectString(m, "label")
		info, ok := readObject(filepath.Join(dir, label+".json"))
		pid := processNumber(info, "pid")
		started, startedOK := objectNumber(info, "started")
		if label == lead || !ok || pid == 0 || incarnationRunning(pid, started, startedOK) {
			continue
		}
		var asg any
		for _, r := range dispatches(dir, rows) {
			if rowString(r, "recipient") == label && sameNumber(started, startedOK, rowValue(r, "recipient_incarnation")) && !writeDispatchDone(r, rows) {
				asg = rowValue(r, "asg")
			}
		}
		body := "member-died " + label
		if asg != nil {
			body += " active asg " + valueText(asg)
		}
		fields := object{{"role", label}, {"pid", int64(pid)}, {"incarnation", numberOrNil(started, startedOK)}, {"asg", asg}}
		if _, err = raiseEvent(dir, "member-died", label+"@"+valueText(numberOrNil(started, startedOK)), body, asg, fields); err != nil {
			return err
		}
	}
	now := float64(time.Now().UnixNano()) / 1e9
	for _, r := range dispatches(dir, rows) {
		delivered, ok := rowNumber(r, "delivered_epoch")
		if !ok || rowString(r, "sender") != lead || !contains([]string{"Apprentice", "Coder"}, memberRole(dir, rowString(r, "recipient"))) || now-numberFloat(delivered) < 900 || writeDispatchDone(r, rows) {
			continue
		}
		asg := rowValue(r, "asg")
		fields := object{{"asg", asg}, {"recipient", rowString(r, "recipient")}, {"delivered_epoch", delivered}}
		if _, err = raiseEvent(dir, "asg-timeout", valueText(asg)+"@"+valueText(rowValue(r, "recipient_incarnation")), "asg-timeout "+asgText(asg), asg, fields); err != nil {
			return err
		}
	}
	return nil
}

// incarnationRunning: the pid exists and was created at the recorded start (any image; pidAlive also checks it).
func incarnationRunning(pid uint32, started json.Number, startedOK bool) bool {
	_, created, exists := processInfo(pid)
	return exists && (!startedOK || created == 0 || math.Abs(created-numberFloat(started)) <= 300)
}
