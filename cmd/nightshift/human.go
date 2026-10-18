package main

// Plain-text output for people. Commands print JSON for agents and scripts; when stdout is a console window
// (someone typed the command) the commands below print readable text instead. NIGHTSHIFT_JSON=1 or --json
// keeps JSON on a console too.

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/mattn/go-isatty"
)

var forceJSON = os.Getenv("NIGHTSHIFT_JSON") == "1"

// humanOutput reports whether to print text: stdout is a console (or NIGHTSHIFT_TEXT=1) and JSON was not asked for.
func humanOutput() bool {
	return !forceJSON && (os.Getenv("NIGHTSHIFT_TEXT") == "1" || isatty.IsTerminal(os.Stdout.Fd()))
}

// stripJSONFlag removes --json from argv and remembers it.
func stripJSONFlag(argv []string) []string {
	out := argv[:0:0]
	for _, a := range argv {
		if a == "--json" {
			forceJSON = true
			continue
		}
		out = append(out, a)
	}
	return out
}

func humanValue(v any) string {
	switch t := v.(type) {
	case nil:
		return "-"
	case string:
		return t
	case bool:
		if t {
			return "yes"
		}
		return "no"
	case []any:
		parts := make([]string, 0, len(t))
		for _, e := range t {
			parts = append(parts, humanValue(e))
		}
		if len(parts) == 0 {
			return "none"
		}
		return strings.Join(parts, ", ")
	case object:
		parts := []string{}
		for _, p := range t {
			parts = append(parts, p.key+" "+humanValue(p.val))
		}
		return strings.Join(parts, ", ")
	}
	return fmt.Sprint(v)
}

// humanFields prints a flat record as aligned "label  value" lines.
func humanFields(o object) {
	width := 0
	for _, p := range o {
		if len(p.key) > width {
			width = len(p.key)
		}
	}
	for _, p := range o {
		label := strings.ReplaceAll(p.key, "_", " ")
		out(fmt.Sprintf("  %-*s  %s\n", width, label, humanValue(p.val)))
	}
}

func humanStatus(records []any) {
	if len(records) == 0 {
		out("No live shifts. (nightshift status --all includes finished ones.)\n")
		return
	}
	for i, r := range records {
		rec, _ := r.(object)
		if i > 0 {
			out("\n")
		}
		state := "live"
		if !objectBool(rec, "alive") {
			state = "crew gone"
		}
		out(fmt.Sprintf("%s  (%s)\n", objectString(rec, "shift"), state))
		out("  task     " + humanLine(objectString(rec, "task")) + "\n")
		if result := objectString(rec, "result"); result != "" && result != "none" {
			out("  result   " + result + "\n")
		}
		if msgs, ok := rowValue(rec, "messages").(object); ok {
			out(fmt.Sprintf("  mailbox  %s unsent, %s unacked\n", humanValue(rowValue(msgs, "unsent")), humanValue(rowValue(msgs, "unacked"))))
		}
		roles, _ := rowValue(rec, "roles").(object)
		for _, p := range roles {
			role, _ := p.val.(object)
			line := "not started"
			if objectString(role, "status") != "not_started" {
				line = "gone"
				if objectBool(role, "alive") {
					line = "running"
				}
				if tool := objectString(role, "tool"); tool != "" {
					line += ", " + tool
				}
				if pid := rowValue(role, "pid"); pid != nil {
					line += fmt.Sprintf(", pid %v", pid)
				}
			}
			if used := rowValue(role, "context_used"); used != nil {
				line += fmt.Sprintf(", context %v%% used", used)
			}
			if at, ok := objectNumber(role, "last_compacted"); ok {
				line += ", compacted " + time.Unix(int64(numberFloat(at)), 0).Format("2006-01-02 15:04")
			}
			out(fmt.Sprintf("    %-11s %s\n", p.key, line))
		}
	}
}

func humanQueue(shift string, items []queueItem) {
	out(shift + " queue:\n")
	if len(items) == 0 {
		out("  (empty)\n")
		return
	}
	for _, it := range items {
		out(fmt.Sprintf("  %3d  %-8s %s\n", it.ID, it.State, humanLine(it.Task)))
	}
}

func humanCrews(entries []any) {
	for _, e := range entries {
		o, _ := e.(object)
		note := ""
		if !objectBool(o, "active") {
			note = "  (shadowed)"
		}
		out(fmt.Sprintf("  %-18s %-9s %s%s\n", objectString(o, "name"), objectString(o, "source"), objectString(o, "path"), note))
	}
}

func humanLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexAny(s, "\r\n"); i >= 0 {
		s = s[:i]
	}
	if len(s) > 100 {
		s = s[:97] + "..."
	}
	return s
}

// printResult is how a command prints its result: JSON for agents and pipes, readable text at a console.
func printResult(v any, pretty, ascii bool) {
	if !humanOutput() {
		outLine(renderJSON(v, pretty, ascii))
		return
	}
	humanPrint(v, "")
}

func isRecord(v any) bool {
	switch t := v.(type) {
	case object:
		return true
	case []any:
		for _, e := range t {
			if isRecord(e) {
				return true
			}
		}
	}
	return false
}

// humanPrint writes nested records as indented "label  value" lines and lists of records as blocks.
func humanPrint(v any, indent string) {
	switch t := v.(type) {
	case object:
		width := 0
		for _, p := range t {
			if !isRecord(p.val) && len(p.key) > width {
				width = len(p.key)
			}
		}
		for _, p := range t {
			label := strings.ReplaceAll(p.key, "_", " ")
			if isRecord(p.val) {
				out(indent + label + ":\n")
				humanPrint(p.val, indent+"  ")
				continue
			}
			out(fmt.Sprintf("%s%-*s  %s\n", indent, width, label, humanScalar(p.val)))
		}
	case []any:
		if len(t) == 0 {
			out(indent + "(none)\n")
		}
		for i, e := range t {
			if isRecord(e) {
				if i > 0 && indent == "" {
					out("\n")
				}
				humanPrint(e, indent)
				continue
			}
			out(indent + "- " + humanScalar(e) + "\n")
		}
	default:
		out(indent + humanScalar(v) + "\n")
	}
}

// humanScalar is a value on one line; multi-line text is indented under its label.
func humanScalar(v any) string {
	s := humanValue(v)
	if strings.Contains(s, "\n") {
		return strings.ReplaceAll(strings.TrimRight(s, "\n"), "\n", "\n      ")
	}
	return s
}
