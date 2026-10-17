package main

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"time"
)

const mailboxSchema = `CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, sender TEXT NOT NULL, sender_incarnation REAL,
    recipient TEXT NOT NULL, recipient_incarnation REAL, asg TEXT, body TEXT NOT NULL, created TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'stored')`

func withWriteMailbox(dir string, fn func(*sql.DB) error) error {
	return withMailboxMutex(canonicalMailboxPath(dir), func() (runErr error) {
		db, err := sql.Open("sqlite", canonicalMailboxPath(dir))
		if err != nil {
			return err
		}
		db.SetMaxOpenConns(1)
		db.SetMaxIdleConns(1)
		defer func() {
			if err := db.Close(); runErr == nil {
				runErr = err
			}
		}()
		for _, q := range []string{"PRAGMA busy_timeout=10000", "PRAGMA journal_mode=WAL", mailboxSchema} {
			if _, err = db.Exec(q); err != nil {
				return err
			}
		}
		rows, err := db.Query("PRAGMA table_info(messages)")
		if err != nil {
			return err
		}
		columns := map[string]bool{}
		for rows.Next() {
			var cid, notnull, pk int
			var name, typ string
			var def any
			if err = rows.Scan(&cid, &name, &typ, &notnull, &def, &pk); err != nil {
				rows.Close()
				return err
			}
			columns[name] = true
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, c := range [][2]string{{"delivered_epoch", "REAL"}, {"coding_asg", "TEXT"}, {"source_dispatch_id", "INTEGER"}, {"reason", "TEXT"}, {"evidence", "TEXT"}} {
			if !columns[c[0]] {
				if _, err = db.Exec("ALTER TABLE messages ADD COLUMN " + c[0] + " " + c[1]); err != nil {
					return err
				}
			}
		}
		return fn(db)
	})
}

func queryMessages(db *sql.DB, query string, args ...any) ([]object, error) {
	rows, err := db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := []object{}
	for rows.Next() {
		values := make([]any, len(columns))
		ptrs := make([]any, len(values))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		o := object{}
		for i, key := range columns {
			o = append(o, pair{key, dbValue(values[i], key)})
		}
		result = append(result, o)
	}
	return result, rows.Err()
}
func getMessage(db *sql.DB, id any) (object, error) {
	rows, err := queryMessages(db, "SELECT * FROM messages WHERE id = ?", id)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	return rows[0], nil
}

func insertMessage(db *sql.DB, dir, kind, sender, recipient, body string, asg any, metadata object) (int64, error) {
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	sinc, sok := currentIncarnation(dir, sender)
	rinc, rok := currentIncarnation(dir, recipient)
	result, err := tx.Exec("INSERT INTO messages (kind, sender, sender_incarnation, recipient, recipient_incarnation, asg, body, created, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", kind, sender, numberOrNil(sinc, sok), recipient, numberOrNil(rinc, rok), asg, body, time.Now().Format("2006-01-02 15:04:05"), "stored")
	if err != nil {
		return 0, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	for _, m := range metadata {
		if _, err = tx.Exec("UPDATE messages SET "+m.key+" = ? WHERE id = ?", m.val, id); err != nil {
			return 0, err
		}
	}
	if err = tx.Commit(); err != nil {
		return 0, err
	}
	fields := object{{"id", id}, {"kind", kind}, {"sender", sender}, {"recipient", recipient}, {"asg", asg}, {"body", body}}
	fields = append(fields, metadata...)
	busAppend(dir, "msg", fields)
	return id, nil
}

func equalValue(a, b any) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	an, aok := numericValue(a)
	bn, bok := numericValue(b)
	if aok && bok {
		return numberFloat(an) == numberFloat(bn)
	}
	return a == b
}
func messageAfter(a, b object) bool {
	an, _ := rowNumber(a, "id")
	bn, _ := rowNumber(b, "id")
	return numberFloat(an) > numberFloat(bn)
}
func writeDispatchDone(source object, rows []object) bool {
	for _, r := range rows {
		if rowString(r, "kind") == "result" && equalValue(rowValue(r, "asg"), rowValue(source, "asg")) && messageAfter(r, source) && rowString(r, "sender") == rowString(source, "recipient") && equalValue(rowValue(r, "sender_incarnation"), rowValue(source, "recipient_incarnation")) && rowString(r, "recipient") == rowString(source, "sender") {
			return true
		}
	}
	return false
}
func writeUpliftEvidence(dir string, source object, rows []object) map[string][]object {
	evidence := map[string][]object{}
	start := source
	for _, r := range rows {
		if messageAfter(r, source) && equalValue(rowValue(r, "asg"), rowValue(source, "asg")) && rowString(r, "sender") == rowString(source, "recipient") && rowString(r, "recipient") == rowString(source, "sender") && equalValue(rowValue(r, "sender_incarnation"), rowValue(source, "recipient_incarnation")) && rowString(r, "kind") == "note" && strings.HasPrefix(rowString(r, "body"), "APPRENTICE-NEEDS-SENIOR") {
			evidence["needs-senior"] = append(evidence["needs-senior"], r)
		}
	}
	for i := len(rows) - 1; i >= 0; i-- {
		r := rows[i]
		if !messageAfter(source, r) || !equalValue(rowValue(r, "asg"), rowValue(source, "asg")) || !contains([]string{"assignment", "uplift"}, rowString(r, "kind")) || !contains([]string{"Apprentice", "Coder"}, memberRole(dir, rowString(r, "recipient"))) {
			continue
		}
		if rowString(r, "recipient") != rowString(source, "recipient") || !equalValue(rowValue(r, "recipient_incarnation"), rowValue(source, "recipient_incarnation")) || rowString(r, "sender") != rowString(source, "sender") {
			break
		}
		start = r
	}
	failures := []object{}
	for _, v := range dispatches(dir, rows) {
		if memberRole(dir, rowString(v, "recipient")) != "Tester" || !equalValue(rowValue(v, "coding_asg"), rowValue(source, "asg")) || !messageAfter(v, start) || rowString(v, "sender") != rowString(source, "sender") {
			continue
		}
		for _, r := range rows {
			if rowString(r, "kind") == "result" && equalValue(rowValue(r, "asg"), rowValue(v, "asg")) && messageAfter(r, v) && rowString(r, "sender") == rowString(v, "recipient") && equalValue(rowValue(r, "sender_incarnation"), rowValue(v, "recipient_incarnation")) && rowString(r, "recipient") == rowString(source, "sender") && strings.HasPrefix(rowString(r, "body"), "TESTED-FAIL") {
				failures = append(failures, r)
				break
			}
		}
	}
	if len(failures) >= 2 {
		evidence["tester-failed-twice"] = failures
	}
	if delivered, ok := rowNumber(source, "delivered_epoch"); ok && float64(time.Now().UnixNano())/1e9-numberFloat(delivered) >= 900 && !writeDispatchDone(source, rows) {
		evidence["timeout"] = []object{{{"id", rowValue(source, "id")}, {"delivered_epoch", delivered}, {"body", rowValue(source, "body")}}}
	}
	return evidence
}
func requireReleasedClaims(dir, label string) error {
	aliases := []string{strings.ToLower(label), strings.ToLower(meshName(dir, label))}
	_, info, _ := canonicalLabel(dir, label)
	aliases = append(aliases, strings.ToLower(objectString(info, "name")))
	_, members := crewMembers(dir)
	for _, m := range members {
		if objectString(m, "label") == label {
			aliases = append(aliases, strings.ToLower(objectString(m, "identity")))
		}
	}
	held := []string{}
	for _, c := range readClaims(dir) {
		if claim, ok := c.val.(object); ok && contains(aliases, strings.ToLower(valueText(rowValue(claim, "role")))) {
			held = append(held, c.key)
		}
	}
	if len(held) > 0 {
		return commandError{4, label + " must release claims before transfer: " + strings.Join(held, ", ")}
	}
	return nil
}
func storeDispatchMessage(db *sql.DB, dir string, args writeArgs, sender, recipient string) (any, error) {
	metadata, existing, err := dispatchMetadata(db, dir, args, sender, recipient)
	if err != nil || existing != nil {
		return existing, err
	}
	var asgValue any
	if args.present["--asg"] {
		asgValue = args.values["--asg"]
	}
	return insertMessage(db, dir, args.values["--kind"], sender, recipient, args.values["--body"], asgValue, metadata)
}

// dispatchMetadata validates a msg send (uplift triggers, claims released) and returns the row metadata, or the
// id of an identical uplift already stored; summon runs it before starting the reserved Coder and again to record.
func dispatchMetadata(db *sql.DB, dir string, args writeArgs, sender, recipient string) (object, any, error) {
	rows, err := queryMessages(db, "SELECT * FROM messages ORDER BY id")
	if err != nil {
		return nil, nil, err
	}
	lead := humanLabel(dir)
	latest := dispatches(dir, rows)
	var source object
	for _, r := range latest {
		if args.present["--asg"] && rowValue(r, "asg") == args.values["--asg"] && rowString(r, "sender") == lead && contains([]string{"Apprentice", "Coder"}, memberRole(dir, rowString(r, "recipient"))) {
			source = r
			break
		}
	}
	kind, asg, reason := args.values["--kind"], args.values["--asg"], args.values["--reason"]
	metadata := object{}
	if coding := args.values["--coding-asg"]; coding != "" {
		if kind != "assignment" || sender != lead || memberRole(dir, recipient) != "Tester" || asg == "" {
			return nil, nil, fmt.Errorf("--coding-asg requires a Foreman verification assignment with --asg")
		}
		found := false
		for _, r := range latest {
			if rowValue(r, "asg") == coding && contains([]string{"Apprentice", "Coder"}, memberRole(dir, rowString(r, "recipient"))) {
				found = true
				break
			}
		}
		if !found {
			return nil, nil, fmt.Errorf("--coding-asg must name a coding assignment")
		}
		metadata = append(metadata, pair{"coding_asg", coding})
	}
	if reason != "" && kind != "uplift" {
		return nil, nil, fmt.Errorf("--reason requires --kind uplift")
	}
	if kind == "uplift" {
		if sender != lead || memberRole(dir, recipient) != "Coder" || asg == "" || reason == "" {
			return nil, nil, fmt.Errorf("uplift requires Foreman -> Coder, --asg and --reason")
		}
		if source != nil && rowString(source, "kind") == "uplift" {
			if rowString(source, "recipient") != recipient || rowString(source, "reason") != reason {
				return nil, nil, fmt.Errorf("assignment already uplifted to a different recipient or reason")
			}
			return nil, rowValue(source, "id"), nil
		}
		inc, ok := currentIncarnation(dir, rowString(source, "recipient"))
		if source == nil || memberRole(dir, rowString(source, "recipient")) != "Apprentice" || !sameNumber(inc, ok, rowValue(source, "recipient_incarnation")) {
			return nil, nil, fmt.Errorf("uplift requires a current Apprentice coding assignment")
		}
		evidence := writeUpliftEvidence(dir, source, rows)[reason]
		if len(evidence) == 0 {
			return nil, nil, fmt.Errorf("uplift trigger not satisfied: %s", reason)
		}
		if err := requireReleasedClaims(dir, rowString(source, "recipient")); err != nil {
			return nil, nil, err
		}
		items := []any{}
		for _, r := range evidence {
			items = append(items, r)
		}
		metadata = append(metadata, pair{"source_dispatch_id", rowValue(source, "id")}, pair{"reason", reason}, pair{"evidence", renderJSON(items, false, true)})
	} else if kind == "assignment" && sender == lead && source != nil && rowString(source, "recipient") != recipient && contains([]string{"Apprentice", "Coder"}, memberRole(dir, recipient)) {
		if memberRole(dir, rowString(source, "recipient")) == "Apprentice" && memberRole(dir, recipient) == "Coder" {
			return nil, nil, fmt.Errorf("use --kind uplift with a validated reason for Apprentice -> Coder transfer")
		}
		if err := requireReleasedClaims(dir, rowString(source, "recipient")); err != nil {
			return nil, nil, err
		}
	}
	return metadata, nil, nil
}

const msgUsage = "usage: nightshift msg [-h] {send,sent} ..."
const sendUsage = "usage: nightshift msg send [-h] --kind {assignment,uplift,result,note,ack}\n                           --body BODY [--asg ASG]\n                           [--reason {needs-senior,tester-failed-twice,timeout}]\n                           [--coding-asg CODING_ASG]\n                           shift FROM TO"
const sentUsage = "usage: nightshift msg sent [-h] shift id"

func msgCommand(args []string) int {
	if len(args) == 0 {
		return writeUsage("nightshift msg", msgUsage, "the following arguments are required: action")
	}
	if args[0] == "-h" || longOption(args[0], []string{"--help"}) == "--help" {
		out(msgUsage + "\n\nRecord crew messages in the shift mailbox before sending them with\nSendMessage.\n\npositional arguments:\n  {send,sent}\n    send       store a message; prints its header line for the top of the\n               SendMessage\n    sent       mark a stored message as delivered with SendMessage\n\noptions:\n  -h, --help   show this help message and exit\n")
		return 0
	}
	if args[0] != "send" && args[0] != "sent" {
		return writeUsage("nightshift msg", msgUsage, fmt.Sprintf("argument action: invalid choice: %s (choose from send, sent)", pyRepr(args[0])))
	}
	if args[0] == "sent" {
		return sentCommand(args[1:])
	}
	opts := []writeOption{{name: "--kind", choices: []string{"assignment", "uplift", "result", "note", "ack"}}, {name: "--body"}, {name: "--asg"}, {name: "--reason", choices: upliftReasons}, {name: "--coding-asg"}}
	a, parseErr := parseWriteArgs(args[1:], opts, []string{"shift", "FROM", "TO"}, nil)
	if a.help {
		out(sendUsage + "\n\npositional arguments:\n  shift                 shift name, unique suffix, or \"latest\"\n  FROM                  your crew label (Foreman, Coder) or session name\n  TO                    the peer's crew label or session name\n\noptions:\n  -h, --help            show this help message and exit\n  --kind {assignment,uplift,result,note,ack}\n  --body BODY           the message text the recipient acts on\n  --asg ASG             assignment id this message belongs to\n  --reason {needs-senior,tester-failed-twice,timeout}\n                        validated trigger for an uplift\n  --coding-asg CODING_ASG\n                        coding assignment verified by this Tester assignment\n")
		return 0
	}
	if parseErr == "" || strings.HasPrefix(parseErr, "unrecognized arguments:") || strings.HasPrefix(parseErr, "the following arguments are required:") {
		missing := []string{}
		if len(a.pos) < 3 {
			missing = append(missing, []string{"shift", "FROM", "TO"}[len(a.pos):]...)
		}
		for _, key := range []string{"--kind", "--body"} {
			if !a.present[key] {
				missing = append(missing, key)
			}
		}
		if len(missing) > 0 {
			parseErr = "the following arguments are required: " + strings.Join(missing, ", ")
		}
	}
	if parseErr != "" {
		return writeUsage("nightshift msg send", sendUsage, parseErr)
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return commandExit(err)
	}
	sender, _, err := canonicalLabel(dir, a.pos[1])
	if err != nil {
		return commandExit(err)
	}
	recipient, _, err := canonicalLabel(dir, a.pos[2])
	if err != nil {
		return commandExit(err)
	}
	lead := humanLabel(dir)
	if recipient == "Advisor" && sender != lead {
		return commandExit(fmt.Errorf("only %s may address the Advisor; nothing stored", lead))
	}
	if sender == "Advisor" && recipient != lead {
		return commandExit(fmt.Errorf("the Advisor answers only %s; nothing stored", lead))
	}
	return commandExit(withClaimsLock(dir, func() error {
		return withWriteMailbox(dir, func(db *sql.DB) error {
			id, err := storeDispatchMessage(db, dir, a, sender, recipient)
			if err != nil {
				return err
			}
			row, err := getMessage(db, id)
			if err == nil {
				outLine(fmt.Sprintf("msg:%v kind:%s asg:%s", rowValue(row, "id"), rowString(row, "kind"), asgText(rowValue(row, "asg"))))
			}
			return err
		})
	}))
}
func markMessage(db *sql.DB, id int64, sent bool) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	q := "UPDATE messages SET state = 'acknowledged' WHERE id = ?"
	if sent {
		q = "UPDATE messages SET state = 'sent' WHERE id = ? AND state = 'stored'"
	}
	if _, err = tx.Exec(q, id); err != nil {
		return err
	}
	if _, err = tx.Exec("UPDATE messages SET delivered_epoch = COALESCE(delivered_epoch, ?) WHERE id = ?", float64(time.Now().UnixNano())/1e9, id); err != nil {
		return err
	}
	return tx.Commit()
}
func sentCommand(args []string) int {
	a, pe := parseWriteArgs(args, nil, []string{"shift", "id"}, nil)
	if a.help {
		out(sentUsage + "\n\npositional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n  id\n\noptions:\n  -h, --help  show this help message and exit\n")
		return 0
	}
	if pe != "" {
		return writeUsage("nightshift msg sent", sentUsage, pe)
	}
	id, e := parseInteger(a.pos[1])
	if e != nil {
		return writeUsage("nightshift msg sent", sentUsage, "argument id: invalid int value: "+pyRepr(a.pos[1]))
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return commandExit(err)
	}
	var row object
	err = withWriteMailbox(dir, func(db *sql.DB) error {
		if err := markMessage(db, id, true); err != nil {
			return err
		}
		var e error
		row, e = getMessage(db, id)
		return e
	})
	if err != nil {
		return commandExit(err)
	}
	if row == nil {
		return commandExit(fmt.Errorf("no message msg:%d in shift %s", id, filepath.Base(dir)))
	}
	busAppend(dir, "sent", object{{"id", id}})
	printResult(row, false, true)
	return 0
}

const ackUsage = "usage: nightshift ack [-h] shift role id"

func ackCommand(args []string) int {
	a, pe := parseWriteArgs(args, nil, []string{"shift", "role", "id"}, nil)
	if a.help {
		out(ackUsage + "\n\nAcknowledge a received message and print its stored body, the only text to act\non. Unknown id, wrong recipient or a message for an earlier incarnation: a\nprotocol-violation note goes to the human contact and the exit code is 5 (do\nnot act).\n\npositional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n  role        your crew label (Foreman, Coder) or session name\n  id\n\noptions:\n  -h, --help  show this help message and exit\n")
		return 0
	}
	if pe != "" {
		return writeUsage("nightshift ack", ackUsage, pe)
	}
	id, e := parseInteger(a.pos[2])
	if e != nil {
		return writeUsage("nightshift ack", ackUsage, "argument id: invalid int value: "+pyRepr(a.pos[2]))
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return commandExit(err)
	}
	label, _, err := canonicalLabel(dir, a.pos[1])
	if err != nil {
		return commandExit(err)
	}
	inc, ok := currentIncarnation(dir, label)
	var row object
	outcome := "acknowledged"
	err = withWriteMailbox(dir, func(db *sql.DB) error {
		var err error
		row, err = getMessage(db, id)
		if err != nil {
			return err
		}
		reason := ""
		if row == nil {
			reason = fmt.Sprintf("no message msg:%d", id)
		} else if rowString(row, "recipient") != label {
			reason = fmt.Sprintf("msg:%d is addressed to %s, not %s", id, rowString(row, "recipient"), label)
		} else if !sameNumber(inc, ok, rowValue(row, "recipient_incarnation")) {
			reason = fmt.Sprintf("msg:%d was sent to an earlier %s incarnation (%s; current %s)", id, label, valueText(rowValue(row, "recipient_incarnation")), valueText(numberOrNil(inc, ok)))
		}
		if reason != "" {
			if _, err = insertMessage(db, dir, "note", label, humanLabel(dir), fmt.Sprintf("protocol violation: %s (ack of msg:%d by %s)", reason, id, label), nil, nil); err != nil {
				return err
			}
			busAppend(dir, "ack", object{{"id", id}, {"by", label}, {"outcome", "protocol violation"}, {"reason", reason}})
			errLine("protocol violation: " + reason)
			return commandError{5, reason}
		}
		if rowString(row, "state") == "acknowledged" {
			outcome = "already acknowledged"
		}
		if err = markMessage(db, id, false); err != nil {
			return err
		}
		row, err = getMessage(db, id)
		return err
	})
	if err != nil {
		return commandExit(err)
	}
	busAppend(dir, "ack", object{{"id", id}, {"by", label}, {"outcome", outcome}})
	printResult(object{{"ack", outcome}, {"row", row}}, false, true)
	return 0
}
