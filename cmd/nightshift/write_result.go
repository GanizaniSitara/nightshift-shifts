package main

// `nightshift result` (NGH-061): the Foreman's result.json written by the tool, with the bus path, build records,
// uplift counts and Copilot usage attached, so it is never hand-written. `down` still appends the result event.

import (
	"fmt"
	"path/filepath"
	"strings"
)

func init() { extraCommands["result"] = resultCommand }

const resultUsage = "usage: nightshift result [-h] --stage {implemented,tested,deployed,validated}\n" +
	"                        --summary SUMMARY [--changed PATH] [--evidence EVIDENCE]\n" +
	"                        shift {done,blocked}"

const resultHelp = resultUsage + `

Write <state_dir>/result.json: status, stage, summary, changed, evidence, the
bus path, every builds/<n>.json record (n, head, dirty, exit, asg), the
Foreman's uplift counts and Copilot usage from <label>-usage.json files.
done above implemented needs a passed build on a clean tree at the
worktree's current HEAD (exit 4).
Prints the path.

positional arguments:
  shift                 shift name, unique suffix, or "latest"
  {done,blocked}        the shift outcome

options:
  -h, --help            show this help message and exit
  --stage {implemented,tested,deployed,validated}
                        the rung reached
  --summary SUMMARY     three sentences at most
  --changed PATH        a changed path (repeatable)
  --evidence EVIDENCE   'stage|what|outcome' (repeatable)
`

func resultCommand(argv []string) int {
	options := []writeOption{{name: "--stage", choices: stages}, {name: "--summary"}, {name: "--changed", multi: true}, {name: "--evidence", multi: true}}
	args, message := parseWriteArgs(argv, options, []string{"shift", "status"}, nil)
	if args.help {
		out(resultHelp)
		return 0
	}
	if message == "" {
		missing := []string{}
		for _, name := range []string{"--stage", "--summary"} {
			if !args.present[name] {
				missing = append(missing, name)
			}
		}
		if len(missing) > 0 {
			message = "the following arguments are required: " + strings.Join(missing, ", ")
		} else if !contains([]string{"done", "blocked"}, args.pos[1]) {
			message = fmt.Sprintf("argument status: invalid choice: %s (choose from done, blocked)", pyRepr(args.pos[1]))
		}
	}
	evidence := []any{}
	for _, e := range args.lists["--evidence"] {
		parts := strings.SplitN(e, "|", 3)
		if len(parts) != 3 && message == "" {
			message = fmt.Sprintf("argument --evidence: expected 'stage|what|outcome', got %s", pyRepr(e))
		}
		if len(parts) == 3 {
			evidence = append(evidence, object{{"stage", parts[0]}, {"what", parts[1]}, {"outcome", parts[2]}})
		}
	}
	if message != "" {
		return writeUsage("nightshift result", resultUsage, message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return busExit(err)
	}
	status, stage := args.pos[1], args.values["--stage"]
	// a passed build counts only on the current HEAD of the shift's worktree (else of the build's own cwd)
	worktree, _ := readObject(filepath.Join(dir, "worktree.json"))
	heads := map[string]string{}
	currentHead := func(cwd string) string {
		if _, ok := heads[cwd]; !ok && cwd != "" {
			heads[cwd], _ = lifecycleGitText(cwd, "rev-parse", "HEAD")
		}
		return heads[cwd]
	}
	builds, passed := []any{}, false
	for _, b := range handoverBuilds(dir) {
		builds = append(builds, object{{"n", rowValue(b, "n")}, {"head", rowValue(b, "head")}, {"dirty", rowValue(b, "dirty")}, {"exit", rowValue(b, "exit")}, {"asg", rowValue(b, "asg")}})
		exit, ok := objectNumber(b, "exit")
		cwd := objectString(worktree, "worktree_dir")
		if cwd == "" {
			cwd = objectString(b, "cwd")
		}
		if objectString(b, "status") == "passed" && ok && numberFloat(exit) == 0 && rowValue(b, "dirty") == false && objectString(b, "head") != "" && objectString(b, "head") == currentHead(cwd) {
			passed = true
		}
	}
	if status == "done" && stagesIndex(stage) > 0 && !passed {
		return busExit(commandError{4, fmt.Sprintf("done at %s needs a passed nightshift build on a clean tree on the current HEAD; run nightshift build first, or report --stage implemented or blocked", stage)})
	}
	view, err := inboxView(dir, humanLabel(dir))
	if err != nil {
		return busExit(err)
	}
	result := object{{"status", status}, {"stage", stage}, {"summary", args.values["--summary"]},
		{"changed", stringsToAny(args.lists["--changed"])}, {"evidence", evidence},
		{"bus", filepath.Join(dir, "bus.jsonl")}, {"builds", builds}, {"uplift_counts", rowValue(view, "uplift_counts")}}
	if usage := resultUsageRecord(dir); usage != nil {
		result = append(result, pair{"usage", usage})
	}
	path := filepath.Join(dir, "result.json")
	if err := writeJSONFile(path, result); err != nil {
		return busExit(err)
	}
	outLine(path)
	return 0
}

// resultUsageRecord is {"budget", "total_used", "panes": {label: {"credits_used", "cap"}}} from the members'
// <label>-usage.json files (credits_used read from the file's credits_used), or nil when there are none.
func resultUsageRecord(dir string) object {
	saved, _ := readObject(filepath.Join(dir, "crew.json"))
	members, _ := rowValue(saved, "members").([]any)
	panes := object{}
	total := 0.0
	for _, item := range members {
		m, _ := item.(object)
		label := objectString(m, "label")
		file, ok := readObject(filepath.Join(dir, label+"-usage.json"))
		if label == "" || !ok {
			continue
		}
		used := rowValue(file, "credits_used")
		if n, ok := objectNumber(file, "credits_used"); ok {
			total += numberFloat(n)
		}
		panes = append(panes, pair{label, object{{"credits_used", used}, {"cap", rowValue(m, "max_credits")}}})
	}
	if len(panes) == 0 {
		return nil
	}
	return object{{"budget", rowValue(saved, "max_credits")}, {"total_used", total}, {"panes", panes}}
}
