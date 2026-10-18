package main

// `nightshift init` and `nightshift doctor`: set up and check a machine. init creates the state folders and
// offers to refresh machine crew copies (<state root>/crews) that have drifted from the crews built into this
// binary; doctor reports the same things and changes nothing.

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/mattn/go-isatty"
)

const initUsage = `usage: nightshift init [-h] [--yes]`

const initHelp = initUsage + `

Set up this machine: create the state folders under the state root, check the
agent tools are on PATH, and compare each machine crew (<state root>/crews/NAME.yaml)
with the built-in crew of the same name. A machine crew shadows the built-in one,
so an old copy keeps old settings after an update. For each one that differs,
init shows the changed lines and asks whether to replace it; the old file is kept
as NAME.yaml.old. Run it again after every pull and rebuild.

options:
  -h, --help  show this help message and exit
  --yes       replace every differing machine crew without asking
`

const doctorUsage = `usage: nightshift doctor [-h] [--cwd REPO] [--crew CREW]`

const doctorHelp = doctorUsage + `

Check this machine without changing anything: state folders, agent tools on
PATH, and machine crews that differ from the built-in crews. nightshift init
fixes what it reports.

With --cwd, check a repository instead: render every role's brief for REPO
and print the words per layer (role, repository, site) against each budget,
then the over-budget warnings. Exit 0 even when over budget; exit 4 when
site.yaml does not allow a member's tool or model.

options:
  -h, --help   show this help message and exit
  --cwd REPO   render the briefs for this repository
  --crew CREW  with --cwd: the crew to render (default: the repository's
               default_crew, else default)
`

func init() {
	extraCommands["init"] = func(argv []string) int { return setupCommand(argv, true) }
	extraCommands["doctor"] = func(argv []string) int { return setupCommand(argv, false) }
}

func setupCommand(argv []string, fix bool) int {
	name, usage, help := "nightshift doctor", doctorUsage, doctorHelp
	options := []writeOption{}
	if fix {
		name, usage, help = "nightshift init", initUsage, initHelp
		options = append(options, writeOption{name: "--yes", flag: true})
	} else {
		options = append(options, writeOption{name: "--cwd"}, writeOption{name: "--crew"})
	}
	args, parseError := parseWriteArgs(argv, options, nil, nil)
	if args.help {
		out(help)
		return 0
	}
	if parseError == "" && args.present["--crew"] && !args.present["--cwd"] {
		parseError = "argument --crew: requires --cwd"
	}
	if parseError != "" {
		return writeUsage(name, usage, parseError)
	}
	if args.present["--cwd"] {
		return doctorBriefs(args.values["--cwd"], args.values["--crew"])
	}
	yes := args.present["--yes"]
	human := humanOutput()
	interactive := fix && !yes && human && isatty.IsTerminal(os.Stdin.Fd())
	stdin := bufio.NewReader(os.Stdin)
	say := func(s string) {
		if human {
			out(s + "\n")
		}
	}
	problems := 0

	root := stateRoot()
	say("State root  " + root)
	folders := []any{}
	for _, sub := range []string{"", "crews", "shifts", "floor", "locks"} {
		dir := filepath.Join(root, sub)
		state := "ok"
		if info, err := os.Stat(dir); err != nil || !info.IsDir() {
			state = "missing"
			if fix {
				if err := os.MkdirAll(dir, 0777); err != nil {
					state = "error: " + err.Error()
				} else {
					state = "created"
				}
			}
		}
		if state != "ok" && state != "created" {
			problems++
		}
		if state != "ok" {
			say("  " + dir + "  " + state)
		}
		folders = append(folders, object{{"path", dir}, {"state", state}})
	}

	exe := launcherExe()
	onPath := false
	if p, err := exec.LookPath("nightshift"); err == nil && strings.EqualFold(filepath.Clean(p), filepath.Clean(exe)) {
		onPath = true
	}
	if onPath {
		say("Binary      " + exe + " (on PATH)")
	} else {
		problems++
		say("Binary      " + exe + "  NOT the nightshift on PATH: add " + filepath.Dir(exe) + " to PATH")
	}

	tools := object{}
	say("Tools")
	for _, tool := range []string{"claude", "copilot", "codex", "agy"} {
		p := toolExe(tool)
		tools = append(tools, pair{tool, p})
		if p == "" {
			p = "not found"
		}
		say(fmt.Sprintf("  %-11s %s", tool, p))
	}

	builtIn := map[string]bool{}
	for _, stem := range assetStems("crews") {
		builtIn[stem] = true
	}
	crews := []any{}
	say("Machine crews  " + filepath.Join(root, "crews"))
	matches, _ := filepath.Glob(filepath.Join(root, "crews", "*.yaml"))
	if len(matches) == 0 {
		say("  none (the built-in crews are used)")
	}
	for _, path := range matches {
		stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		entry := object{{"name", stem}, {"path", path}}
		if !builtIn[stem] {
			say(fmt.Sprintf("  %-16s own crew, no built-in to compare", stem))
			crews = append(crews, append(entry, pair{"state", "own"}))
			continue
		}
		mine, err1 := readTextUniversal(path)
		theirs, err2 := assetRead("crews", stem+".yaml")
		if err1 != nil || err2 != nil {
			problems++
			say(fmt.Sprintf("  %-16s cannot read: %v %v", stem, err1, err2))
			crews = append(crews, append(entry, pair{"state", "unreadable"}))
			continue
		}
		if mine == theirs {
			say(fmt.Sprintf("  %-16s same as built-in", stem))
			crews = append(crews, append(entry, pair{"state", "same"}))
			continue
		}
		removed, added := lineChanges(mine, theirs)
		say(fmt.Sprintf("  %-16s DIFFERS from built-in (overrides it)", stem))
		for _, l := range removed {
			say("      - " + humanLine(l))
		}
		for _, l := range added {
			say("      + " + humanLine(l))
		}
		state := "differs"
		replace := yes
		if interactive {
			out(fmt.Sprintf("    Replace %s with the built-in? The old one is kept as %s.yaml.old [y/N] ", stem, stem))
			answer, _ := stdin.ReadString('\n')
			replace = strings.HasPrefix(strings.ToLower(strings.TrimSpace(answer)), "y")
		}
		if fix && replace {
			if err := os.WriteFile(path+".old", []byte(mine), 0666); err == nil {
				err = writeTextFile(path, theirs)
				if err == nil {
					state = "replaced"
					say("    replaced; old copy at " + path + ".old")
				} else {
					say("    error: " + err.Error())
				}
			} else {
				say("    error: " + err.Error())
			}
		} else if fix {
			state = "kept"
		}
		if state == "differs" {
			problems++
		}
		crews = append(crews, append(entry, pair{"state", state}))
	}

	if human {
		if problems == 0 {
			out("All good.\n")
		} else if !fix {
			out(fmt.Sprintf("%d thing(s) to look at; nightshift init fixes folders and crews.\n", problems))
		} else {
			out(fmt.Sprintf("Done; %d thing(s) above still need you.\n", problems))
		}
		return 0
	}
	printResult(object{{"state_root", root}, {"binary", exe}, {"on_path", onPath}, {"folders", folders},
		{"tools", tools}, {"crews", crews}, {"problems", problems}}, true, false)
	return 0
}

// doctorBriefs renders every role's brief for a repository, as start would, with no shift: words per layer per role
// and the over-budget warnings.
func doctorBriefs(cwd, crewName string) int {
	abs, err := filepath.Abs(cwd)
	if err != nil {
		return commandExit(err)
	}
	if info, err := os.Stat(abs); err != nil || !info.IsDir() {
		errLine("doctor --cwd: " + abs + " is not a folder")
		return 1
	}
	repoRoot := canonicalRepoRoot(abs, "")
	config, err := loadRepoConfig(repoRoot)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	if crewName == "" {
		crewName = configString(config, "default_crew")
	}
	if crewName == "" {
		crewName = "default"
	}
	crew, err := loadCrew(crewName, repoRoot)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	members := crewMembersOf(crew, "Nightshift-shift-doctor")
	if err := startSiteCheck(members); err != nil {
		return commandExit(err)
	}
	_, _, advisorSlot, coderSlot, slots := crewCells(crew, members)
	crewJSON := crewObject(crew, members, slots, advisorSlot, coderSlot, abs)
	roles, warnings := []any{}, []string{}
	human := humanOutput()
	if human {
		out(fmt.Sprintf("Briefs for %s (crew %s)\n", repoRoot, crew.name))
	}
	for _, m := range members {
		rendered, err := renderRoleBrief("", m.label, abs, repoRoot, crewJSON, m.object())
		if err != nil {
			errLine(fmt.Sprintf("brief %s: %s", m.label, err))
			return 1
		}
		layers, cells := []any{}, []string{}
		for _, l := range rendered.Layers {
			layers = append(layers, object{{"name", l.Name}, {"words", l.Words}, {"budget", l.Budget}})
			cells = append(cells, fmt.Sprintf("%s %d/%d", l.Name, l.Words, l.Budget))
		}
		if human {
			out(fmt.Sprintf("  %-12s %-9s %s\n", m.label, rendered.Mode, strings.Join(cells, "  ")))
		}
		roles = append(roles, object{{"role", m.label}, {"mode", rendered.Mode}, {"layers", layers}})
		warnings = append(warnings, rendered.Warnings...)
	}
	if !human {
		printResult(object{{"repo", repoRoot}, {"crew", crew.name}, {"roles", roles}, {"warnings", stringsToAny(warnings)}}, true, false)
		return 0
	}
	if len(warnings) == 0 {
		out("All within budget.\n")
	}
	for _, w := range warnings {
		out("warning: " + w + "\n")
	}
	return 0
}

// lineChanges is a rough diff: lines only in a, and lines only in b, in order, ignoring blank lines.
func lineChanges(a, b string) (removed, added []string) {
	count := func(s string) map[string]int {
		m := map[string]int{}
		for _, l := range strings.Split(s, "\n") {
			m[strings.TrimRight(l, " \t")]++
		}
		return m
	}
	inA, inB := count(a), count(b)
	for _, l := range strings.Split(a, "\n") {
		l = strings.TrimRight(l, " \t")
		if strings.TrimSpace(l) != "" && inB[l] == 0 {
			removed = append(removed, l)
		}
	}
	for _, l := range strings.Split(b, "\n") {
		l = strings.TrimRight(l, " \t")
		if strings.TrimSpace(l) != "" && inA[l] == 0 {
			added = append(added, l)
		}
	}
	return removed, added
}
