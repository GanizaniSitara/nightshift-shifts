package main

// `nightshift consult`: start the shift's absent manifest Advisor in its
// reserved slot with the question as its durable first assignment (acc1f52).

import (
	"path/filepath"
	"strconv"
	"strings"
)

const consultUsage = "usage: nightshift consult [-h] --asg ASG shift question"

const consultHelp = consultUsage + `

positional arguments:
  shift       shift name, unique suffix, or "latest"
  question    the Foreman question for the Advisor

options:
  -h, --help  show this help message and exit
  --asg ASG   the durable consultation assignment id
`

func init() { extraCommands["consult"] = consultCommand }

func consultCommand(argv []string) int {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--asg"}}, nil, []string{"shift", "question"})
	if args.help {
		out(consultHelp)
		return 0
	}
	// argparse names missing positionals first, then missing required options.
	if message == "" || strings.HasPrefix(message, "the following arguments are required:") {
		missing := []string{}
		if len(args.pos) < 2 {
			missing = append(missing, []string{"shift", "question"}[len(args.pos):]...)
		}
		if !args.present["--asg"] {
			missing = append(missing, "--asg")
		}
		if len(missing) > 0 {
			message = "the following arguments are required: " + strings.Join(missing, ", ")
		}
	}
	if message != "" {
		return writeUsage("nightshift consult", consultUsage, message)
	}
	question := args.pos[1]
	if strings.TrimSpace(question) == "" {
		return writeUsage("nightshift consult", consultUsage, "the consultation question is empty")
	}
	stateDir, err := resolveShift(args.pos[0])
	if err != nil {
		errLine(err.Error())
		return 1
	}
	saved, ok := readObject(filepath.Join(stateDir, "crew.json"))
	if _, isList := rowValue(saved, "members").([]any); !ok || !isList {
		saved = object{}
	}
	members, _ := rowValue(saved, "members").([]any)
	var advisor *crewMember
	for _, item := range members {
		if o, ok := item.(object); ok {
			if m := memberFromObject(o); isAdvisor(m) {
				advisor = m
				break
			}
		}
	}
	advisorSlot := rowValue(saved, "advisor_slot")
	if advisor == nil || !truthy(advisorSlot) {
		errLine("shift " + filepath.Base(stateDir) + " has no reserved " + advisorLabel)
		return 1
	}
	if info, _ := readObject(filepath.Join(stateDir, advisor.label+".json")); truthy(rowValue(info, "pid")) {
		errLine(advisorLabel + " is already started; use msg send for later questions")
		return 1
	}
	if truthy(rowValue(saved, "consult")) {
		errLine(advisorLabel + " consultation is already starting")
		return 1
	}
	crewFile := filepath.Join(stateDir, "crew.json")
	consult := object{{"question", question}, {"asg", args.values["--asg"]}}
	if err := writeJSONFile(crewFile, setValue(append(object{}, saved...), "consult", consult)); err != nil {
		errLine(err.Error())
		return 1
	}
	code := spawnCommand([]string{filepath.Base(stateDir), "--role", advisor.label, "--slot", strconv.Itoa(intValue(advisorSlot))})
	if code != 0 {
		// the spawn failed: the shift goes back to an unstarted, unconsulted Advisor
		if err := writeJSONFile(crewFile, saved); err != nil {
			errLine(err.Error())
		}
	}
	return code
}
