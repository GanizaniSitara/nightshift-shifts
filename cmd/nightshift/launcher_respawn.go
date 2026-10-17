package main

// `nightshift respawn` restarts a dead member in its own slot with its active assignment as the first prompt, and
// `nightshift uplift` hands an Apprentice assignment to the Coder (NGH-061). docs/ARCHITECTURE.md "Failure events
// and lifecycle commands (NGH-061)".

import (
	"database/sql"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"
)

func init() {
	extraCommands["respawn"] = respawnCommand
	extraCommands["uplift"] = upliftCommand
}

const respawnUsage = "usage: nightshift respawn [-h] shift role"

const respawnHelp = respawnUsage + `

Restart a dead crew member in the slot it last ran in. Its active assignment
(the latest assignment or uplift to it with no later result for that asg) is
stored again for the new incarnation with the same asg and becomes its first
prompt; with none it starts with the normal startup prompt. Exit 4 = the
member is running (or was never started).

positional arguments:
  shift       shift name, unique suffix, or "latest"
  role        the member's crew label (Coder, Tester)

options:
  -h, --help  show this help message and exit
`

// respawnKeys are the row columns a re-delivered assignment keeps, besides kind, asg and body.
var respawnKeys = []string{"coding_asg", "source_dispatch_id", "reason", "evidence"}

// activeAssignment is the latest assignment or uplift to label with no later result from label for that asg.
func activeAssignment(rows []object, label string) object {
	for i := len(rows) - 1; i >= 0; i-- {
		r := rows[i]
		if rowString(r, "recipient") != label || !contains([]string{"assignment", "uplift"}, rowString(r, "kind")) {
			continue
		}
		for _, later := range rows[i+1:] {
			if rowString(later, "kind") == "result" && rowString(later, "sender") == label && equalValue(rowValue(later, "asg"), rowValue(r, "asg")) {
				return nil
			}
		}
		return r
	}
	return nil
}

func respawnCommand(argv []string) int {
	args, message := parseWriteArgs(argv, nil, []string{"shift", "role"}, nil)
	if args.help {
		out(respawnHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift respawn", respawnUsage, message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return busExit(err)
	}
	label, info, err := canonicalLabel(dir, args.pos[1])
	if err != nil {
		return busExit(err)
	}
	pid := processNumber(info, "pid")
	started, startedOK := objectNumber(info, "started")
	if pid == 0 {
		return busExit(commandError{4, fmt.Sprintf("%s was never started; use nightshift spawn, consult or summon", label)})
	}
	if pidAlive(pid, started, startedOK) {
		return busExit(commandError{4, label + " is running; nothing to do"})
	}
	slot := 0
	saved, _ := readObject(filepath.Join(dir, "crew.json"))
	slots := intValue(rowValue(saved, "slots"))
	for n := 1; n <= slots; n++ {
		if objectString(spawnReadSlot(dir, n), "role") == label {
			slot = n
		}
	}
	if slot == 0 {
		return busExit(commandError{4, fmt.Sprintf("no slot is recorded for %s; start it with nightshift spawn", label)})
	}
	var active object
	err = withWriteMailbox(dir, func(db *sql.DB) error {
		rows, err := queryMessages(db, "SELECT * FROM messages ORDER BY id")
		active = activeAssignment(rows, label)
		return err
	})
	if err != nil {
		return busExit(err)
	}
	crewFile := filepath.Join(dir, "crew.json")
	if active != nil {
		pending := object{{"role", label}, {"kind", rowString(active, "kind")}, {"asg", rowValue(active, "asg")}, {"body", rowString(active, "body")},
			{"sender", rowString(active, "sender")}, {"msg", rowValue(active, "id")}}
		for _, key := range respawnKeys {
			if v := rowValue(active, key); v != nil {
				pending = append(pending, pair{key, v})
			}
		}
		if err := writeJSONFile(crewFile, setValue(append(object{}, saved...), "respawn", pending)); err != nil {
			return busExit(err)
		}
	}
	if err := runSpawn([]string{filepath.Base(dir), "--role", label, "--slot", strconv.Itoa(slot)}, true); err != nil {
		if active != nil {
			// the spawn failed: nothing is pending for a pane that never started
			if werr := writeJSONFile(crewFile, saved); werr != nil {
				errLine(werr.Error())
			}
		}
		var se spawnError
		if errors.As(err, &se) {
			errLine(se.message)
			return se.code
		}
		errLine(err.Error())
		return 1
	}
	asg := "no active assignment"
	var msg any
	if active != nil {
		asg, msg = "asg "+asgText(rowValue(active, "asg")), rowValue(active, "id")
	}
	busAppend(dir, "respawn", object{{"role", label}, {"slot", slot}, {"asg", rowValue(active, "asg")}, {"msg", msg}})
	outLine(fmt.Sprintf("respawned %s in slot %d (%s)", label, slot, asg))
	return 0
}

// respawnRecord stores the pending re-delivery for the pane's new incarnation: same kind, asg, body and metadata
// as the original row, which was validated when it was first sent.
func respawnRecord(db *sql.DB, stateDir, role string, pending object) (int64, error) {
	metadata := object{}
	for _, key := range respawnKeys {
		if v := rowValue(pending, key); v != nil {
			metadata = append(metadata, pair{key, v})
		}
	}
	sender := objectString(pending, "sender")
	if sender == "" {
		sender = humanLabel(stateDir)
	}
	return insertMessage(db, stateDir, objectString(pending, "kind"), sender, role, objectString(pending, "body"), rowValue(pending, "asg"), metadata)
}

const upliftUsage = "usage: nightshift uplift [-h] --reason {needs-senior,tester-failed-twice,timeout}\n                        shift asg text"

const upliftHelp = upliftUsage + `

Transfer the Apprentice assignment ASG to the Coder as a guarded uplift. The
trigger evidence and the Apprentice's released claims are validated as msg
send does. A Coder that is not started yet is started with summon --kind
uplift; a started one gets the row stored and a nudge, as assign does.
Exit 4 = refused (the reason is printed), nothing stored.

positional arguments:
  shift                 shift name, unique suffix, or "latest"
  asg                   the Apprentice coding assignment id
  text                  the uplift body for the Coder

options:
  -h, --help            show this help message and exit
  --reason {needs-senior,tester-failed-twice,timeout}
                        the trigger the mailbox evidence must satisfy
`

func upliftCommand(argv []string) int {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--reason", choices: upliftReasons}}, []string{"shift", "asg", "text"}, nil)
	if args.help {
		out(upliftHelp)
		return 0
	}
	if message == "" && !args.present["--reason"] {
		message = "the following arguments are required: --reason"
	}
	if message == "" && strings.TrimSpace(args.pos[2]) == "" {
		message = "the uplift text is empty"
	}
	if message != "" {
		return writeUsage("nightshift uplift", upliftUsage, message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return busExit(err)
	}
	asg, reason, text := args.pos[1], args.values["--reason"], args.pos[2]
	lead := humanLabel(dir)
	// the Coder-role member, whatever its label (Coder, Senior, Coder-1)
	coder := ""
	_, members := crewMembers(dir)
	for _, m := range members {
		if label := objectString(m, "label"); coder == "" && memberRole(dir, label) == coderLabel {
			coder = label
		}
	}
	if coder == "" {
		return busExit(commandError{4, fmt.Sprintf("shift %s has no %s to uplift to; nothing stored", filepath.Base(dir), coderLabel)})
	}
	send := busArgs("uplift", text, asg, "")
	send.values["--reason"], send.present["--reason"] = reason, true
	// msg send's validation, with an already-stored uplift refused instead of returned
	validate := func(db *sql.DB) error {
		_, existing, err := dispatchMetadata(db, dir, send, lead, coder)
		if err != nil {
			var ce commandError
			if errors.As(err, &ce) {
				return err
			}
			return commandError{4, err.Error() + "; nothing stored"}
		}
		if existing != nil {
			return commandError{4, fmt.Sprintf("%s is already uplifted to %s (msg:%v); nothing stored", asg, coder, existing)}
		}
		return nil
	}
	if status, _ := memberStatus(dir, coder); status == "not_started" {
		err := withClaimsLock(dir, func() error { return withWriteMailbox(dir, validate) })
		if err != nil {
			return busExit(err)
		}
		return summonCommand([]string{filepath.Base(dir), text, "--asg", asg, "--kind", "uplift", "--reason", reason})
	}
	row, err := storeBusMessage(dir, func(db *sql.DB, rows []object) (writeArgs, string, string, error) {
		return send, lead, coder, validate(db)
	})
	if err != nil {
		return busExit(err)
	}
	deliverBusMessage(dir, row)
	return 0
}
