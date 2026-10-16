package main

// `nightshift consult`: start the shift's absent manifest Advisor in its
// reserved slot with the question as its durable first assignment (acc1f52).

import (
	"database/sql"
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

// `nightshift summon`: start the shift's reserved Coder (a crew with an Apprentice holds it, NGH-056) in its
// slot, with the Foreman's assignment or guarded uplift as its durable first message.

const summonUsage = "usage: nightshift summon [-h] --asg ASG [--kind {assignment,uplift}]\n" +
	"                        [--reason {needs-senior,tester-failed-twice,timeout}]\n" +
	"                        shift body"

const summonHelp = summonUsage + `

Start the reserved Coder of a crew with an Apprentice. The body is stored
exactly as msg send Foreman Coder would store it (an uplift is validated
the same way: trigger evidence and the Apprentice's claims released) and
becomes the Coder's first prompt.

positional arguments:
  shift                 shift name, unique suffix, or "latest"
  body                  the Foreman's assignment for the Coder

options:
  -h, --help            show this help message and exit
  --asg ASG             the coding assignment id
  --kind {assignment,uplift}
                        assignment (default) or a guarded uplift of an
                        Apprentice assignment
  --reason {needs-senior,tester-failed-twice,timeout}
                        validated trigger for an uplift
`

func init() { extraCommands["summon"] = summonCommand }

// summonArgs is the msg send argument set for a recorded summon.
func summonArgs(summon object) writeArgs {
	args := writeArgs{values: map[string]string{}, present: map[string]bool{}, lists: map[string][]string{}}
	for _, key := range []string{"kind", "body", "asg", "reason"} {
		if v := rowValue(summon, key); v != nil {
			args.values["--"+key], args.present["--"+key] = valueText(v), true
		}
	}
	return args
}

func summonCommand(argv []string) int {
	options := []writeOption{{name: "--asg"}, {name: "--kind", choices: []string{"assignment", "uplift"}}, {name: "--reason", choices: upliftReasons}}
	args, message := parseWriteArgs(argv, options, nil, []string{"shift", "body"})
	if args.help {
		out(summonHelp)
		return 0
	}
	if message == "" || strings.HasPrefix(message, "the following arguments are required:") {
		missing := []string{}
		if len(args.pos) < 2 {
			missing = append(missing, []string{"shift", "body"}[len(args.pos):]...)
		}
		if !args.present["--asg"] {
			missing = append(missing, "--asg")
		}
		if len(missing) > 0 {
			message = "the following arguments are required: " + strings.Join(missing, ", ")
		}
	}
	if message != "" {
		return writeUsage("nightshift summon", summonUsage, message)
	}
	body := args.pos[1]
	if strings.TrimSpace(body) == "" {
		return writeUsage("nightshift summon", summonUsage, "the assignment body is empty")
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
	var coder *crewMember
	for _, item := range members {
		if o, ok := item.(object); ok {
			if m := memberFromObject(o); isCoder(m) {
				coder = m
				break
			}
		}
	}
	coderSlot := rowValue(saved, "coder_slot")
	if coder == nil || !truthy(coderSlot) {
		errLine("shift " + filepath.Base(stateDir) + " has no reserved " + coderLabel)
		return 1
	}
	if info, _ := readObject(filepath.Join(stateDir, coder.label+".json")); truthy(rowValue(info, "pid")) {
		errLine(coderLabel + " is already started; use msg send for later assignments")
		return 1
	}
	if truthy(rowValue(saved, "summon")) {
		errLine(coderLabel + " summon is already starting")
		return 1
	}
	kind := args.values["--kind"]
	if kind == "" {
		kind = "assignment"
	}
	summon := object{{"kind", kind}, {"body", body}, {"asg", args.values["--asg"]}}
	if args.present["--reason"] {
		summon = append(summon, pair{"reason", args.values["--reason"]})
	}
	// Refuse now what the pane would refuse when it records the message (msg send's checks).
	err = withClaimsLock(stateDir, func() error {
		return withWriteMailbox(stateDir, func(db *sql.DB) error {
			_, _, err := dispatchMetadata(db, stateDir, summonArgs(summon), humanLabel(stateDir), coder.label)
			return err
		})
	})
	if err != nil {
		return commandExit(err)
	}
	crewFile := filepath.Join(stateDir, "crew.json")
	if err := writeJSONFile(crewFile, setValue(append(object{}, saved...), "summon", summon)); err != nil {
		errLine(err.Error())
		return 1
	}
	code := spawnCommand([]string{filepath.Base(stateDir), "--role", coder.label, "--slot", strconv.Itoa(intValue(coderSlot))})
	if code != 0 {
		// the spawn failed: the shift goes back to an unstarted, unsummoned Coder
		if err := writeJSONFile(crewFile, saved); err != nil {
			errLine(err.Error())
		}
	}
	return code
}
