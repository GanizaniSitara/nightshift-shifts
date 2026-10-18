package main

// `nightshift start`: plan a shift, write its state folder and open the crew's
// Windows Terminal window. Ported from nightshift_shifts.py start() and its
// helpers; --dry-run prints the plan and writes nothing.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/windows"
)

const startUsage = `usage: nightshift start [-h] [--prompt-file PROMPT_FILE] [--cwd CWD]
                        [--no-worktree] [--target-branch TARGET_BRANCH]
                        [--continue-from SHIFT_DIR] [--crew CREW]
                        [--crew-file CREW_FILE] [--without ROLE]
                        [--role ROLE=TOOL:MODEL[:REASONING]]
                        [--max-credits N] [--dry-run]
                        [prompt]`

const startHelp = startUsage + `

Start a Nightshift crew: Claude Foreman, Advisor, Coder and Tester side by
side in one Windows Terminal tab (the default crew; --crew NAME uses
crews/NAME.yaml for other roles and pane counts). The task is saved to
<state_dir>/prompt.txt and given to Foreman only; Coder and Tester send READY
and wait for Foreman to delegate work; the Advisor stays dormant until Foreman
consults it. Git shifts use an isolated worktree; --no-worktree keeps in-place
execution. Each start is a new shift with its own <shift>-foreman /
<shift>-advisor / <shift>-coder / <shift>-tester session names; there is no
restart or resume, and a closed pane is not relaunched. A folder holds one
live shift at a time: start exits with code 3 while another live shift holds
the working folder (nightshift down releases it).

positional arguments:
  prompt                the task for Foreman (quote it; it is treated as plain
                        text), or a ticket ID such as NGH-005: the ticket body
                        becomes the task and its codebase: field the working
                        folder

options:
  -h, --help            show this help message and exit
  --prompt-file PROMPT_FILE
                        read the task from this UTF-8 file instead (safest for
                        text with quotes or & | < > ^ %)
  --cwd CWD             folder the crew works in (default: the ticket
                        codebase, else the current directory)
  --no-worktree         work in place with the existing folder lock
  --target-branch TARGET_BRANCH
                        branch to start from and land onto (repository
                        default, else main)
  --continue-from SHIFT_DIR
                        an earlier shift folder for the same task: its
                        handover.md is appended so the crew carries on
  --crew CREW           crew manifest CREW.yaml, first found in
                        <state root>/crews, <repo>/.nightshift/crews, then the
                        built-in crews/: roles, briefs, colours, counts, model
                        (default: default = one Foreman, one Advisor, one
                        Coder, one Tester); nightshift crews lists them
  --crew-file CREW_FILE
                        use this crew manifest file instead of --crew
  --without ROLE        drop ROLE from the crew (repeatable; not the
                        human_contact role)
  --role ROLE=TOOL:MODEL[:REASONING]
                        run ROLE on TOOL with MODEL and optional reasoning
                        (repeatable)
  --max-credits N       the shift's Copilot credit budget, split across the
                        Copilot panes (replaces the manifest's max_credits)
  --dry-run             print the intended command, roles and prompt as JSON;
                        launch and write nothing

Example: nightshift start "Add a --verbose flag to report.py" | nightshift
start --dry-run --prompt-file task.txt
`

var (
	ticketPattern    = `[A-Z][A-Z0-9]+-\d+`
	ticketFull       = regexp.MustCompile(`^` + ticketPattern + `$`)
	ticketHeader     = regexp.MustCompile(`^Ticket (` + ticketPattern + `):`)
	requiresLine     = regexp.MustCompile(`(?mi)^requires:\s*(\w+)\s*$`)
	codebaseLine     = regexp.MustCompile(`(?m)^codebase:\s*(.+?)\s*$`)
	branchSlugUnsafe = regexp.MustCompile(`[^A-Za-z0-9_-]+`)
)

func init() {
	extraCommands["start"] = startCommand
	startHook = func(argv []string) (string, error) { return runStart(argv, false) }
}

func startCommand(argv []string) int {
	_, err := runStart(argv, true)
	return commandExit(err)
}

// startExit reports an error the way the oracle's SystemExit(message) does.
func startExit(message string) error {
	errLine(message)
	return commandError{1, ""}
}

func startUsageError(message string) error {
	return commandError{writeUsage("nightshift start", startUsage, message), ""}
}

// runStart is start(); print=false is the oracle's redirect_stdout (gaffer).
func runStart(argv []string, print bool) (string, error) {
	options := []writeOption{{name: "--prompt-file"}, {name: "--cwd"}, {name: "--no-worktree", flag: true}, {name: "--target-branch"},
		{name: "--continue-from"}, {name: "--crew"}, {name: "--crew-file"}, {name: "--without", multi: true}, {name: "--role", multi: true},
		{name: "--max-credits"}, {name: "--dry-run", flag: true}}
	args, parseError := parseWriteArgs(argv, options, nil, []string{"prompt"})
	if args.help {
		if print {
			out(startHelp)
		}
		return "", commandError{0, ""}
	}
	if parseError != "" {
		return "", startUsageError(parseError)
	}
	var prompt *string
	if len(args.pos) > 0 {
		prompt = &args.pos[0]
	}
	if args.present["--crew"] && args.present["--crew-file"] {
		return "", startUsageError("give either --crew or --crew-file, not both")
	}
	if prompt != nil && args.present["--prompt-file"] {
		return "", startUsageError("give the task either as an argument or with --prompt-file, not both")
	}
	// `start work` reads as "start the work crew" but would start the default crew with the task "work".
	if prompt != nil && !args.present["--crew"] && !args.present["--crew-file"] {
		word := strings.TrimSpace(*prompt)
		for _, src := range crewSources(canonicalRepoRoot(args.values["--cwd"], "")) {
			if strings.EqualFold(src.name, word) {
				return "", startUsageError(fmt.Sprintf("%q is a crew name, not a task: use nightshift start --crew %s \"<task or ticket>\"", word, src.name))
			}
		}
	}
	if file := args.values["--prompt-file"]; file != "" {
		text, err := readTextUniversal(file)
		if err != nil {
			return "", startExit(fmt.Sprintf("[Errno 2] No such file or directory: %s", pyRepr(file)))
		}
		prompt = &text
	}
	ticket, ticketCwd := "", ""
	if prompt != nil && ticketFull.MatchString(strings.TrimSpace(*prompt)) {
		id, text, codebase, err := ticketPrompt(strings.TrimSpace(*prompt))
		if err != nil {
			return "", err
		}
		ticket, prompt, ticketCwd = id, &text, codebase
	}
	if prompt == nil || strings.TrimSpace(*prompt) == "" {
		return "", startUsageError(`the task prompt is empty; give the work for Foreman, e.g. nightshift start "Fix the failing test"`)
	}
	task := *prompt
	if ticket == "" {
		if m := ticketHeader.FindStringSubmatch(firstSplitLine(task)); m != nil {
			ticket = m[1]
		}
	}
	required := parseRequires(task)
	if from := args.values["--continue-from"]; from != "" {
		handover := filepath.Join(from, "handover.md")
		if fileExists(handover) {
			text, err := readTextUniversal(handover)
			if err != nil {
				return "", startExit(err.Error())
			}
			if runes := []rune(text); len(runes) > 6000 {
				text = string(runes[len(runes)-6000:])
			}
			task += "\n\nAn earlier crew was interrupted on this task. Its handover follows; check what it already " +
				"committed before redoing anything.\n\n" + text
		}
	}
	name := "shift-" + randomHex(8)
	stateDir := filepath.Join(stateRoot(), "shifts", name)
	cwd := args.values["--cwd"]
	if cwd == "" {
		cwd = ticketCwd
	}
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	if info, err := os.Stat(cwd); err != nil || !info.IsDir() {
		return "", startUsageError("working folder does not exist: " + cwd)
	}
	cwd = pyResolve(cwd)
	// One live shift per repository (docs/design-live-shift.md): attach instead of starting a second crew.
	if live := liveShiftFor(cwd); live != "" && !args.present["--dry-run"] {
		item := task
		if ticket != "" {
			item = ticket
		}
		woken, added, err := queueAdd(live, item, "start")
		if err != nil {
			return "", startExit(err.Error())
		}
		focusShiftWindow(live)
		line := renderJSON(object{{"attached", filepath.Base(live)}, {"queued", added.ID}, {"foreman_woken", woken}}, false, false)
		if print && humanOutput() {
			out(fmt.Sprintf("This repo already has a live shift, %s: queued item %d there (Foreman woken: %s).\n",
				filepath.Base(live), added.ID, humanValue(woken)))
		} else if print {
			out(line + "\n")
		}
		return line, nil
	}
	repoRoot := canonicalRepoRoot(cwd, "")
	config, err := loadRepoConfig(repoRoot)
	if err != nil {
		return "", startExit(err.Error())
	}
	crewName := args.values["--crew"]
	if crewName == "" {
		crewName = configString(config, "default_crew")
	}
	if crewName == "" {
		crewName = "default"
	}
	var overrides *crewOverrides
	if args.present["--crew-file"] || args.present["--without"] || args.present["--role"] || args.present["--max-credits"] {
		overrides = &crewOverrides{without: args.lists["--without"], roles: args.lists["--role"], maxCredits: args.values["--max-credits"]}
		if file := args.values["--crew-file"]; args.present["--crew-file"] {
			if abs, err := filepath.Abs(file); err == nil {
				file = abs
			}
			overrides.crewFile = file
			crewName = strings.TrimSuffix(filepath.Base(file), filepath.Ext(file))
		}
	}
	crew, err := loadCrewWith(crewName, repoRoot, overrides)
	if err != nil {
		return "", startExit(err.Error())
	}
	if strings.Contains(cwd, ";") {
		return "", startUsageError(`the working folder contains ";", which Windows Terminal treats as a command separator: ` + cwd)
	}
	target := args.values["--target-branch"]
	if target == "" {
		target = configString(config, "target_branch")
	}
	if target == "" {
		target = "main"
	}
	var worktree object
	if !args.present["--no-worktree"] {
		if worktree, err = planWorktree(cwd, name, ticket, target); err != nil {
			return "", startExit(err.Error())
		}
	}
	if worktree != nil {
		cwd = objectString(worktree, "worktree_dir")
	}
	members := crewMembersOf(crew, name)
	if err := startSiteCheck(members); err != nil {
		return "", err
	}
	labels, cells, advisorSlot, coderSlot, slots := crewCells(crew, members)
	colours := map[string]string{}
	for _, m := range members {
		if m.tool == "agy" || m.tool == "codex" || m.tool == "copilot" {
			colours[m.label] = m.colour
		}
	}
	title := shiftTitle(name, ticket)
	reserved := map[int]string{}
	for _, slot := range []int{advisorSlot, coderSlot} {
		if slot > 0 && slot <= len(members) {
			reserved[slot-1] = members[slot-1].label
		}
	}
	command := wtCommand(wtSpec{name: name, stateDir: stateDir, agents: true, modeFlag: "--start", cwd: cwd, members: cells,
		crew: crew.name, ticket: ticket, slots: slots, opsWidth: crew.opsWidth, foremanWidth: crew.foremanWidth, colours: colours, opsDir: repoRoot,
		reserved: reserved})
	crewJSON := crewObject(crew, members, slots, advisorSlot, coderSlot, cwd)
	briefs, warnings := renderBriefs(stateDir, cwd, repoRoot, crewJSON, members)
	if print {
		for _, warning := range warnings {
			errLine(warning)
		}
	}
	if args.present["--dry-run"] {
		if print {
			outLine(renderJSON(startPlan(name, stateDir, cwd, ticket, title, task, required, crew, members, cells, slots, advisorSlot,
				coderSlot, command, worktree, config, briefs), true, false))
		}
		return "", nil
	}
	if worktree == nil {
		if err := takeLock(cwd, name, stateDir); err != nil {
			return "", err
		}
	}
	if err := os.MkdirAll(filepath.Dir(stateDir), 0777); err != nil {
		return "", startExit(err.Error())
	}
	if err := os.Mkdir(stateDir, 0777); err != nil {
		return "", startExit(err.Error())
	}
	if worktree != nil {
		if err := os.MkdirAll(filepath.Join(stateRoot(), "worktrees"), 0777); err != nil {
			return "", startExit(err.Error())
		}
		if _, _, err := lifecycleGit(objectString(worktree, "repo_root"), "worktree", "add", "-b", objectString(worktree, "branch"), cwd, objectString(worktree, "base_sha")); err != nil {
			return "", startExit(err.Error())
		}
		if err := writeJSONFile(filepath.Join(stateDir, "worktree.json"), worktree); err != nil {
			return "", startExit(err.Error())
		}
		if err := mirrorRepoLayer(repoRoot, cwd); err != nil {
			return "", startExit(err.Error())
		}
	}
	promptFile := filepath.Join(stateDir, "prompt.txt")
	writes := [][2]string{{promptFile, task}, {filepath.Join(stateDir, "requires.txt"), required},
		{filepath.Join(stateDir, "crew.json"), renderJSON(crewJSON, true, true)}}
	if ticket != "" {
		writes = append(writes, [2]string{filepath.Join(stateDir, "ticket.txt"), ticket})
	}
	for _, w := range writes {
		if err := writeTextFile(w[0], w[1]); err != nil {
			return "", startExit(err.Error())
		}
	}
	// Validate every member, including reserved roles, before profile installation or any WT call.
	for _, m := range members {
		if err := preflightMember(stateDir, cwd, repoRoot, crewJSON, m); err != nil {
			return "", startExit(err.Error())
		}
	}
	labelValues := stringsToAny(labels)
	startEvent := object{{"shift", name}, {"ticket", nullable(ticket)}, {"cwd", cwd}, {"crew", crew.name}}
	if crew.overrides != nil {
		startEvent = append(startEvent, pair{"overrides", crew.overrides})
	}
	busAppend(stateDir, "start", append(startEvent, pair{"members", labelValues}))
	if err := installProfile(stateDir); err != nil {
		return "", startExit(err.Error())
	}
	if err := launchCrew(command, name, stateDir, object{{"mode", "start"}, {"prompt_file", promptFile}}, labels, slots, print); err != nil {
		return "", err
	}
	// NGH-066: the shift's patrol, a hidden child; a shift without one still has the floor warden
	if err := startPatrol(stateDir); err != nil {
		errLine("warning: patrol not started (" + err.Error() + "); run nightshift patrol " + name)
	}
	note := "started shift " + name
	if ticket != "" {
		note += " ticket " + ticket
	}
	if floorAppendHook != nil {
		if _, err := floorAppendHook("launcher", note, "all"); err != nil {
			return "", startExit(err.Error())
		}
	}
	return stateDir, nil
}

// crewCells places the members in slots. The manifest Advisor is not started with the crew: its cell is an empty,
// reserved slot (acc1f52). With an Apprentice in the crew the Coder is held the same way until summon (NGH-056).
func crewCells(crew *crewManifest, members []*crewMember) (labels, cells []string, advisorSlot, coderSlot, slots int) {
	labels, cells = []string{}, []string{}
	reserveCoder := crewHasRole(crew, "Apprentice")
	for i, m := range members {
		if isAdvisor(m) && advisorSlot == 0 {
			advisorSlot = i + 1
			cells = append(cells, "")
		} else if reserveCoder && isCoder(m) && coderSlot == 0 {
			coderSlot = i + 1
			cells = append(cells, "")
		} else {
			labels = append(labels, m.label)
			cells = append(cells, m.label)
		}
	}
	slots = crew.slots
	if slots == 0 && (advisorSlot != 0 || coderSlot != 0) {
		slots = len(members)
	}
	for len(cells) < slots {
		cells = append(cells, "")
	}
	return labels, cells, advisorSlot, coderSlot, slots
}

// crewObject is the crew.json-shaped crew the brief renderer takes (start writes the same fields to crew.json).
func crewObject(crew *crewManifest, members []*crewMember, slots, advisorSlot, coderSlot int, cwd string) object {
	memberObjects := []any{}
	for _, m := range members {
		memberObjects = append(memberObjects, m.object())
	}
	o := object{{"crew", crew.name}}
	if crew.overrides != nil {
		o = append(o, pair{"overrides", crew.overrides})
	}
	o = append(o, object{{"members", memberObjects}, {"slots", slotsValue(slots)}, {"advisor_slot", slotsValue(advisorSlot)}}...)
	if coderSlot != 0 {
		o = append(o, pair{"coder_slot", coderSlot})
	}
	o = append(o, object{{"permission_mode", crew.permissionMode}, {"model", crew.model}, {"cwd", cwd}}...)
	if crew.maxCredits != nil {
		o = append(o, pair{"max_credits", crew.maxCredits})
	}
	if crew.briefs != "" {
		o = append(o, pair{"briefs", crew.briefs})
	}
	for _, b := range []pair{{"budget_usd", crew.budgetUSD}, {"budget_tokens", crew.budgetTokens}} {
		if b.val != nil {
			o = append(o, b)
		}
	}
	return o
}

// startSiteCheck refuses a crew whose tool or model site.yaml does not allow (exit 4, one line on stderr).
func startSiteCheck(members []*crewMember) error {
	site, err := loadSite()
	if err != nil {
		return startExit(err.Error())
	}
	list := [][3]string{}
	for _, m := range members {
		list = append(list, [3]string{m.label, m.tool, m.model})
	}
	if err := siteCheckMembers(site, list); err != nil {
		errLine(err.Error())
		return commandError{4, ""}
	}
	return nil
}

// renderBriefs renders every member's brief: the results by label, and the renderer's over-budget warnings (one
// line each; a brief that cannot be rendered is a warning too).
func renderBriefs(stateDir, cwd, repoRoot string, crew object, members []*crewMember) (map[string]renderedBrief, []string) {
	briefs, warnings := map[string]renderedBrief{}, []string{}
	for _, m := range members {
		rendered, err := renderRoleBrief(stateDir, m.label, cwd, repoRoot, crew, m.object())
		if err != nil {
			warnings = append(warnings, fmt.Sprintf("brief %s: %s", m.label, err))
			continue
		}
		briefs[m.label] = rendered
		warnings = append(warnings, rendered.Warnings...)
	}
	return briefs, warnings
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func slotsValue(slots int) any {
	if slots == 0 {
		return nil
	}
	return slots
}

func randomHex(n int) string {
	b := make([]byte, (n+1)/2)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)[:n]
}

// firstSplitLine is prompt.splitlines()[0].
func firstSplitLine(s string) string {
	if i := strings.IndexFunc(s, func(r rune) bool {
		return strings.ContainsRune("\n\r\v\f\x1c\x1d\x1e\u0085  ", r)
	}); i >= 0 {
		return s[:i]
	}
	return s
}

func startPlan(name, stateDir, cwd, ticket, title, prompt, required string, crew *crewManifest, members []*crewMember, cells []string,
	slotCount, advisorSlot, coderSlot int, command []string, worktree, config object, briefs map[string]renderedBrief) object {
	var lock any
	if worktree == nil {
		lock = lockPath(cwd)
	}
	roles := object{}
	for _, m := range members {
		permissions := m.optString("permissions")
		if permissions == "" {
			permissions = crew.permissionMode
		}
		role := object{{"session_name", m.identity}, {"pane_title", title}, {"peer", m.peer}, {"receives_prompt", m.human},
			{"first_message", openingMessage(m, "")}}
		for _, reserved := range []int{advisorSlot, coderSlot} {
			if reserved != 0 && reserved <= len(cells) && cells[reserved-1] == "" && members[reserved-1] == m {
				role = append(role, pair{"status", "not_started"}, pair{"reserved_slot", reserved})
			}
		}
		role = append(role, pair{"compaction", compactionControl(m.tool, m.role)})
		if m.tool != "claude" {
			role = append(role, pair{"tool", m.tool}, pair{"model", m.model}, pair{"exe", toolExe(m.tool)})
		} else if m.model != crew.model {
			role = append(role, pair{"model", m.model})
		}
		for _, key := range []string{"reasoning", "context", "account", "max_credits", "compact_at"} {
			if m.has(key) {
				role = append(role, pair{key, m.opt(key)})
			}
		}
		if m.tool != "claude" {
			session := m.label + " (" + m.identity + ")"
			credits, _ := m.opt("max_credits").(int)
			role = append(role, pair{"command", stringsToAny(toolCommand(m.tool, m.model, "<brief>", "<first message>", permissions,
				m.optString("reasoning"), m.optString("context"), &session, credits))})
		}
		role = append(role, pair{"permission_flags", stringsToAny(permissionFlags(m.tool, permissions))})
		if b, ok := briefs[m.label]; ok {
			layers := object{}
			for _, l := range b.Layers {
				layers = append(layers, pair{l.Name, l.Words})
			}
			role = append(role, pair{"brief_mode", b.Mode}, pair{"brief_layers", layers}, pair{"brief_warnings", stringsToAny(b.Warnings)})
		}
		roles = append(roles, pair{m.label, role})
	}
	plan := object{{"dry_run", true}, {"shift", name}, {"state_dir", stateDir}, {"cwd", cwd}, {"ticket", nullable(ticket)},
		{"prompt_file", filepath.Join(stateDir, "prompt.txt")}, {"model", crew.model}, {"profile", launcherProfile},
		{"permission_mode", crew.permissionMode}, {"lock", lock}, {"roles", roles}}
	if slotCount != 0 {
		slots := []any{}
		for n := 1; n <= slotCount; n++ {
			var role any
			if n <= len(cells) && cells[n-1] != "" {
				role = cells[n-1]
			}
			slots = append(slots, object{{"slot", n}, {"role", role}, {"pane_title", title}})
		}
		plan = append(plan, pair{"slots", slots})
	}
	plan = append(plan, pair{"wt_argv", stringsToAny(command)}, pair{"prompt", prompt})
	if worktree != nil {
		plan = append(plan, pair{"worktree", worktree})
	}
	if config != nil {
		plan = append(plan, pair{"repo_config", config})
	}
	if crew.name != "default" {
		plan = append(plan, pair{"crew", crew.name})
	}
	if crew.overrides != nil {
		plan = append(plan, pair{"overrides", crew.overrides})
	}
	if crew.maxCredits != nil {
		plan = append(object{{"max_credits", crew.maxCredits}}, plan...)
	}
	if ticket != "" {
		plan = append(plan, pair{"requires", required})
	}
	return plan
}

func parseRequires(text string) string {
	if m := requiresLine.FindStringSubmatch(text); m != nil {
		value := strings.ToLower(m[1])
		if contains(stages, value) {
			return value
		}
	}
	return defaultRequires
}

// ticketPrompt turns a tasks-cli ticket into the Foreman task: (id, prompt, codebase).
func ticketPrompt(id string) (string, string, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "tasks-cli", "get", id)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	data, err := cmd.Output()
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			return "", "", "", startExit(err.Error())
		}
		return "", "", "", startExit(fmt.Sprintf("tasks-cli get %s failed: %s", id, strings.TrimSpace(strings.ToValidUTF8(stderr.String(), "�"))))
	}
	value, err := parseJSON([]byte(strings.ToValidUTF8(string(data), "�")))
	task, ok := value.(object)
	if err != nil || !ok {
		return "", "", "", startExit("tasks-cli get " + id + " printed no JSON object")
	}
	body := objectString(task, "body")
	codebase := ""
	if front, ok := rowValue(task, "frontmatter").(object); ok {
		codebase = valueText(rowValue(front, "codebase"))
		if !truthy(rowValue(front, "codebase")) {
			codebase = ""
		}
	}
	if codebase == "" {
		if m := codebaseLine.FindStringSubmatch(body); m != nil {
			codebase = m[1]
		}
	}
	taskID := valueText(rowValue(task, "task_id"))
	prompt := "Ticket " + taskID + ": " + valueText(rowValue(task, "title")) + "\n\n" + strings.TrimSpace(body) + "\n\n" +
		"Record progress with `tasks-cli note " + taskID + " --note \"...\"`; never edit task files directly, " +
		"and leave the ticket open for the owner to review."
	return taskID, prompt, codebase, nil
}

// lockPath is LOCKS_DIR/<resolved folder, lower case, separators as _>.json.
func lockPath(cwd string) string {
	key := strings.NewReplacer(":", "_", `\`, "_", "/", "_").Replace(strings.ToLower(pyResolve(cwd)))
	return filepath.Join(stateRoot(), "locks", key+".json")
}

func takeLock(cwd, name, stateDir string) error {
	var result error
	err := withMailboxMutex(lockPath(cwd), func() error {
		result = takeFolderLock(cwd, name, stateDir)
		return nil
	})
	if err != nil {
		return startExit(err.Error())
	}
	return result
}

// takeFolderLock claims cwd for shift name; exit 3 while a live shift holds it.
func takeFolderLock(cwd, name, stateDir string) error {
	path := lockPath(cwd)
	held := func(shift, dir string) error {
		message := fmt.Sprintf("folder %s is held by live shift %s (state dir %s); stop it with `nightshift down %s` first",
			cwd, shift, dir, strings.TrimPrefix(shortShiftName(shift), "shift-"))
		errLine(message)
		return commandError{3, message}
	}
	target, _ := canonicalPath(cwd)
	for _, lock := range lifecycleLocks() {
		lockCwd := objectString(lock.record, "cwd")
		same := false
		if lockCwd != "" {
			other, _ := canonicalPath(lockCwd)
			same = other == target
		}
		if objectString(lock.record, "kind") == "landing" && same && processRunningSince(lock.record, "started_epoch") {
			return commandError{3, "repository is currently landing: " + cwd}
		}
		other := lockShiftDir(lock.record)
		if same && other != "" {
			if info, err := os.Stat(other); err == nil && info.IsDir() {
				if record, err := statusRecord(other); err == nil && objectBool(record, "alive") {
					return held(filepath.Base(other), other)
				}
			}
		}
	}
	if record, ok := readObject(path); ok {
		if holder := lockShiftDir(record); holder != "" {
			if info, err := os.Stat(holder); err == nil && info.IsDir() {
				if status, err := statusRecord(holder); err == nil && objectBool(status, "alive") {
					return held(objectString(record, "shift"), holder)
				}
			}
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0777); err != nil {
		return startExit(err.Error())
	}
	lock := object{{"shift", name}, {"cwd", cwd}, {"state_dir", stateDir}, {"launcher_pid", os.Getpid()},
		{"started", time.Now().Format("2006-01-02 15:04:05")}}
	if err := os.WriteFile(path, []byte(renderJSON(lock, false, true)), 0666); err != nil {
		return startExit(err.Error())
	}
	return nil
}

// planWorktree is the shift's worktree record, or nil outside a git repository.
func planWorktree(cwd, name, ticket, target string) (object, error) {
	if _, code, _ := lifecycleGit(cwd, "rev-parse", "--show-toplevel"); code != 0 {
		return nil, nil
	}
	list, err := lifecycleGitText(cwd, "worktree", "list", "--porcelain")
	if err != nil {
		return nil, err
	}
	repo := strings.TrimPrefix(strings.SplitN(list, "\n", 2)[0], "worktree ")
	base, err := lifecycleGitText(repo, "rev-parse", "--verify", "refs/heads/"+target+"^{commit}")
	if err != nil {
		return nil, err
	}
	slugSource := ticket
	if slugSource == "" {
		slugSource = name
	}
	slug := strings.Trim(branchSlugUnsafe.ReplaceAllString(slugSource, "-"), "-")
	suffix := name[strings.LastIndex(name, "-")+1:]
	return object{{"repo_root", repo}, {"worktree_dir", filepath.Join(stateRoot(), "worktrees", name)},
		{"branch", "shift/" + slug + "-" + suffix}, {"target_branch", target}, {"base_sha", base},
		{"created_at", time.Now().Format("2006-01-02 15:04:05")}}, nil
}

// mirrorRepoLayer copies the canonical .nightshift layer onto the worktree exactly.
func mirrorRepoLayer(repoRoot, worktreeDir string) error {
	repo, work := pyResolve(repoRoot), pyResolve(worktreeDir)
	if strings.EqualFold(repo, work) {
		return errors.New("cannot mirror repository instructions onto the canonical checkout")
	}
	source, destination := filepath.Join(repo, ".nightshift"), filepath.Join(work, ".nightshift")
	if info, err := os.Stat(source); err != nil || !info.IsDir() {
		return nil
	}
	if _, err := os.Lstat(destination); err == nil {
		if err := os.RemoveAll(destination); err != nil {
			return err
		}
	}
	return copyTree(source, destination)
}

// copyTree is shutil.copytree(symlinks=True): links copied as links, files with their times.
func copyTree(source, destination string) error {
	return filepath.WalkDir(source, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(source, path)
		target := filepath.Join(destination, rel)
		if entry.Type()&os.ModeSymlink != 0 {
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.MkdirAll(target, 0777)
		}
		in, err := os.Open(path)
		if err != nil {
			return err
		}
		defer in.Close()
		outFile, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
		if err != nil {
			return err
		}
		if _, err := io.Copy(outFile, in); err != nil {
			outFile.Close()
			return err
		}
		if err := outFile.Close(); err != nil {
			return err
		}
		return os.Chtimes(target, info.ModTime(), info.ModTime())
	})
}

func layoutDescription(n int) string {
	if n <= 4 {
		return fmt.Sprintf("1 row x %d columns", n)
	}
	top := (n + 1) / 2
	return fmt.Sprintf("2 rows: %d columns over %d", top, n-top)
}

// paneTitle is the title of every pane and slot of a start container.
func paneTitle(label string) string { return label + " shift" }

func shortShiftName(name string) string {
	if strings.HasPrefix(name, "Nightshift-shift-") {
		return strings.TrimPrefix(name, "Nightshift-")
	}
	return name
}

func shiftTitle(name, ticket string) string {
	title := shortShiftName(name)
	if ticket != "" {
		title += " | " + ticket
	}
	return title
}

func loopTitle(tickets []string) string {
	if len(tickets) == 0 {
		return "Nightshift loop"
	}
	title := "Nightshift " + tickets[0]
	if len(tickets) > 1 {
		title += ".." + tickets[len(tickets)-1]
	}
	return title
}

// wtSpec describes one wt.exe window: the start container, or a legacy trial.
type wtSpec struct {
	name, stateDir, modeFlag, briefFile, cwd, crew, ticket string
	opsDir                                                 string // the last container slot (Ops pane) opens here
	agents                                                 bool
	members                                                []string
	slots                                                  int
	opsWidth                                               int // cells the last slot spans (0 or 1: one)
	foremanWidth                                           int // cells the first slot spans (0 or 1: one)
	colours                                                map[string]string
	reserved                                               map[int]string // cell index -> the role reserved there (consult/summon start it later)
}

// wtCommand is the wt.exe argv: members fill cells 1..k; with slots the rest
// are empty cmd shells `spawn` can fill. 2 panes side by side, 3-4 one row,
// 5-8 two rows (top ceil(n/2)).
func wtCommand(w wtSpec) []string {
	exe := launcherExe()
	start := w.modeFlag == "--start"
	dir := userHome()
	if start && w.cwd != "" {
		dir = w.cwd
	}
	label := w.name
	if w.ticket != "" {
		label = w.ticket
	}
	if start {
		label = shiftTitle(w.name, w.ticket)
	}
	command := []string{"wt.exe", "-w", "-1", "--pos", "0,0", "--maximized"}
	separate := func(parts []string) {
		if len(command) > 6 {
			command = append(command, ";")
		}
		command = append(command, parts...)
	}
	add := func(parts []string, role string) {
		separate(parts)
		if role == "" {
			return
		}
		if w.agents {
			command = append(command, "--profile", launcherProfile)
			if colour := w.colours[role]; start && backgroundFor(colour) != "" {
				command = append(command, "--colorScheme", "Nightshift "+colour)
			}
		}
		title := "Nightshift | " + role + " | " + w.name
		if start {
			title = label
		}
		command = append(command, "--title", title, "--suppressApplicationTitle", "-d", dir, exe, "--pane", role,
			"--state-dir", w.stateDir)
		if w.modeFlag != "" {
			command = append(command, w.modeFlag)
		}
		if w.briefFile != "" {
			command = append(command, "--brief-file", pyResolve(w.briefFile))
		}
		if w.cwd != "" {
			command = append(command, "--cwd", w.cwd)
		}
		if w.crew != "default" {
			command = append(command, "--crew", w.crew)
		}
	}
	place := func(parts []string, i int) {
		if i < len(w.members) && w.members[i] != "" {
			add(parts, w.members[i])
			if w.slots != 0 {
				command = append(command, "--slot", strconv.Itoa(i+1))
			}
			return
		}
		separate(parts)
		if w.agents {
			command = append(command, "--profile", launcherProfile)
			// a reserved slot opens now as a plain shell; its later occupant keeps the scheme the slot gets here
			if colour := w.colours[w.reserved[i]]; start && w.reserved[i] != "" && backgroundFor(colour) != "" {
				command = append(command, "--colorScheme", "Nightshift "+colour)
			}
		}
		cellDir := dir
		if w.opsDir != "" && w.slots != 0 && i == w.slots-1 {
			cellDir = w.opsDir
		}
		cellTitle := paneTitle(label)
		if start {
			cellTitle = label
		}
		command = append(command, "--title", cellTitle, "--suppressApplicationTitle", "-d", cellDir,
			"cmd.exe", "/k", exe, "--slot", strconv.Itoa(i+1), "--state-dir", w.stateDir)
	}
	n := w.slots
	if n == 0 {
		n = len(w.members)
	}
	// width is the cells a slot spans: the Ops pane (last slot) spans opsWidth, the Foreman (first slot)
	// foremanWidth, every other one cell.
	width := func(i int) int {
		if w.opsWidth > 1 && w.slots != 0 && i == w.slots-1 {
			return w.opsWidth
		}
		if w.foremanWidth > 1 && w.slots != 0 && i == 0 {
			return w.foremanWidth
		}
		return 1
	}
	// columns splits the row's first pane (slot from-1) into slots from-1..to-1: each split gives the new
	// pane the share of the remaining width that its slots take.
	columns := func(from, to int) {
		for i := from; i < to; i++ {
			rest, after := 0, 0
			for j := i - 1; j < to; j++ {
				rest += width(j)
			}
			after = rest - width(i-1)
			place([]string{"split-pane", "-V", "-s", strconv.FormatFloat(float64(after)/float64(rest), 'g', 4, 64)}, i)
		}
	}
	place([]string{"new-tab"}, 0)
	if n == 2 {
		place([]string{"split-pane", "-V", "-s", "0.5"}, 1)
		add([]string{"focus-pane", "-t", "0"}, "")
		return command
	}
	if n <= 4 {
		columns(1, n)
	} else {
		// Focus panes by id (creation order: 0 the first pane, 1 the bottom row), not move-focus: move-focus
		// looks for a neighbour by geometry, and while Terminal is still laying out it finds none, so every
		// later split lands in the bottom row.
		// The top row holds half the cells (rounded up); a wide Foreman there leaves room for one slot fewer.
		cells := 0
		for i := 0; i < n; i++ {
			cells += width(i)
		}
		top := (cells+1)/2 - (width(0) - 1)
		place([]string{"split-pane", "-H", "-s", "0.5"}, top)
		add([]string{"focus-pane", "-t", "0"}, "")
		columns(1, top)
		add([]string{"focus-pane", "-t", "1"}, "")
		columns(top+1, n)
	}
	add([]string{"focus-pane", "-t", "0"}, "")
	return command
}

func backgroundFor(colour string) string {
	for _, p := range roleBackgrounds {
		if p.key == colour {
			return p.val.(string)
		}
	}
	return ""
}

// installProfile writes the Nightshift Crew profile and role colour schemes
// into Windows Terminal's settings, keeping the original beside the shift.
func installProfile(stateDir string) error {
	settings := filepath.Join(os.Getenv("LOCALAPPDATA"), "Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe", "LocalState", "settings.json")
	data, err := os.ReadFile(settings)
	if err != nil {
		return err
	}
	original := strings.TrimPrefix(string(data), string(rune(0xFEFF)))
	original = strings.ReplaceAll(strings.ReplaceAll(original, "\r\n", "\n"), "\r", "\n")
	value, err := parseJSON([]byte(original))
	if err != nil {
		return err
	}
	config, ok := value.(object)
	if !ok {
		return errors.New("Windows Terminal settings are not a JSON object")
	}
	profilesObject, _ := rowValue(config, "profiles").(object)
	list, ok := rowValue(profilesObject, "list").([]any)
	if !ok {
		return errors.New("Windows Terminal settings have no profiles.list")
	}
	index := -1
	for i, p := range list {
		if o, ok := p.(object); ok && objectString(o, "guid") == terminalProfileGUID {
			index = i
		}
		if index >= 0 {
			break
		}
	}
	profile := object{{"guid", terminalProfileGUID}}
	if index < 0 {
		list = append(list, nil)
		index = len(list) - 1
	} else {
		profile = list[index].(object)
	}
	for _, p := range []pair{{"name", launcherProfile}, {"commandline", "cmd.exe"}, {"hidden", false},
		{"font", object{{"face", "Cascadia Mono"}, {"size", 12}}}, {"antialiasingMode", "grayscale"}, {"colorScheme", "Campbell"}} {
		profile = setValue(profile, p.key, p.val)
	}
	list[index] = profile
	profilesObject = setValue(profilesObject, "list", list)
	config = setValue(config, "profiles", profilesObject)
	schemes, ok := rowValue(config, "schemes").([]any)
	if _, present := objectGet(config, "schemes"); !present {
		schemes, ok = []any{}, true
	}
	if !ok {
		return errors.New("Windows Terminal settings schemes is not a list")
	}
	campbell := append(object{}, campbellScheme...)
	for _, s := range schemes {
		if o, ok := s.(object); ok && objectString(o, "name") == "Campbell" {
			for _, p := range o {
				campbell = setValue(campbell, p.key, p.val)
			}
			break
		}
	}
	for _, rb := range roleBackgrounds {
		desired := setValue(setValue(append(object{}, campbell...), "name", "Nightshift "+rb.key), "background", rb.val)
		found := false
		for i, s := range schemes {
			if o, ok := s.(object); ok && objectString(o, "name") == "Nightshift "+rb.key {
				schemes[i], found = desired, true
				break
			}
		}
		if !found {
			schemes = append(schemes, desired)
		}
	}
	config = setValue(config, "schemes", schemes)
	if err := writeTextFile(filepath.Join(stateDir, "terminal-settings-before.json"), original); err != nil {
		return err
	}
	if err := writeTextFile(settings, indentFour(renderJSON(config, true, true))+"\n"); err != nil {
		return err
	}
	_, members := crewMembers(stateDir)
	hasAgy, hasCopilot := false, false
	for _, m := range members {
		switch objectString(m, "tool") {
		case "agy":
			hasAgy = true
		case "copilot":
			hasCopilot = true
		}
	}
	if hasAgy {
		installAgyStatusline()
	}
	if hasCopilot {
		installCopilotStatusline()
	}
	return nil
}

// indentFour turns renderJSON's two-space indent into json.dumps(indent=4).
func indentFour(text string) string {
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		trimmed := strings.TrimLeft(line, " ")
		lines[i] = strings.Repeat(" ", 2*(len(line)-len(trimmed))) + trimmed
	}
	return strings.Join(lines, "\n")
}

// wtStepPause is the wait between two wt.exe calls of one layout.
var wtStepPause = 600 * time.Millisecond

// wtSteps turns wtCommand's single argv into one wt.exe call per subcommand, all
// aimed at a window named after the shift. Windows Terminal handles a long `;`
// chain asynchronously and drops focus steps (move-focus and focus-pane alike)
// while it is still creating panes, so every later split landed in the bottom
// row (16 Oct). Separate calls are applied in order. The first call keeps the
// window options; "-w -1" becomes "-w <name>" so later calls find the window.
func wtSteps(command []string, name string) [][]string {
	if len(command) < 3 || command[1] != "-w" {
		return [][]string{command}
	}
	head := []string{command[0], "-w", name}
	var steps [][]string
	current := append([]string{}, head...)
	current = append(current, command[3:]...)
	first := true
	cut := func(parts []string) {
		if first {
			steps = append(steps, parts)
			first = false
			return
		}
		steps = append(steps, append(append([]string{}, head...), parts...))
	}
	start := len(head)
	for i := start; i < len(current); i++ {
		if current[i] == ";" {
			if first {
				cut(append([]string{}, current[:i]...))
			} else {
				cut(append([]string{}, current[start:i]...))
			}
			start = i + 1
		}
	}
	if first {
		cut(append([]string{}, current...))
	} else {
		cut(append([]string{}, current[start:]...))
	}
	return steps
}

// launchCrew runs wt.exe, remembers the new window, hands focus back to the
// human and waits for every pane and slot to report; then it exits.
func launchCrew(command []string, name, stateDir string, extra object, labels []string, slots int, print bool) error {
	before := map[uintptr]bool{}
	for _, w := range terminalWindows() {
		before[w.hwnd] = true
	}
	human, _, _ := lifecycleUser32.NewProc("GetForegroundWindow").Call()
	launchFile := filepath.Join(stateDir, "launch.json")
	record := object{{"argv", stringsToAny(command)}, {"launcher_pid", os.Getpid()}}
	if err := os.WriteFile(launchFile, []byte(renderJSON(record, false, true)), 0666); err != nil {
		return startExit(err.Error())
	}
	var created uintptr
	findCreated := func(tries int) {
		for i := 0; i < tries && created == 0; i++ {
			for _, w := range terminalWindows() {
				if !before[w.hwnd] {
					created = w.hwnd
					break
				}
			}
			if created == 0 {
				time.Sleep(250 * time.Millisecond)
			}
		}
	}
	// windowClosed: the crew window was seen and is gone. Later steps address the window by name, and
	// Windows Terminal would open a fresh one for them, so a window the human closed must stop the launch.
	windowClosed := func() bool {
		if created == 0 {
			return false
		}
		for _, w := range terminalWindows() {
			if w.hwnd == created {
				return false
			}
		}
		return true
	}
	closedError := func() error {
		busAppend(stateDir, "launch_aborted", object{{"reason", "window closed during launch"}})
		return startExit("the crew window was closed during launch; nothing more was started")
	}
	for i, step := range wtSteps(command, name) {
		if windowClosed() {
			return closedError()
		}
		cmd := exec.Command(step[0], step[1:]...)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			return startExit(fmt.Sprintf("Command %s returned non-zero exit status: %s", pyRepr(step[0]), err))
		}
		time.Sleep(wtStepPause)
		if i == 0 {
			findCreated(8)
		}
		// Every wt.exe call raises the crew window; hand focus straight back so the human's typing is not
		// captured by a pane while the layout builds.
		restoreForeground(human)
	}
	findCreated(20)
	if created != 0 {
		record = append(record, pair{"hwnd", int64(created)})
		if err := os.WriteFile(launchFile, []byte(renderJSON(record, false, true)), 0666); err != nil {
			return startExit(err.Error())
		}
		time.Sleep(time.Second)
		restoreForeground(human)
	}
	type expectation struct{ label, path string }
	expected := []expectation{}
	for _, label := range labels {
		expected = append(expected, expectation{label, filepath.Join(stateDir, label+".json")})
	}
	for n := 1; n <= slots; n++ {
		expected = append(expected, expectation{fmt.Sprintf("slot %d", n), filepath.Join(stateDir, "slots", strconv.Itoa(n)+".json")})
	}
	pending := []string{}
	for _, e := range expected {
		pending = append(pending, e.label)
	}
	for i := 0; i < 60; i++ {
		pending = pending[:0]
		for _, e := range expected {
			if !fileExists(e.path) {
				pending = append(pending, e.label)
			}
		}
		if len(pending) == 0 {
			break
		}
		if windowClosed() {
			return closedError()
		}
		time.Sleep(500 * time.Millisecond)
	}
	cells := slots
	if cells == 0 {
		cells = len(labels)
	}
	report := object{{"window_name", name}, {"state_dir", stateDir}, {"requested_layout", layoutDescription(cells)}}
	report = append(report, extra...)
	report = append(report, pair{"panes_missing", stringsToAny(pending)})
	if print {
		printResult(report, false, true)
	}
	if len(pending) > 0 {
		return startExit("WT activation returned success, but these panes never reported: " + strings.Join(pending, ", ") + ".")
	}
	return nil
}

// focusShiftWindow brings a live shift's window to the front with its Foreman pane (pane 0) focused.
func focusShiftWindow(dir string) {
	launch, ok := readObject(filepath.Join(dir, "launch.json"))
	if !ok {
		return
	}
	if n, ok := objectNumber(launch, "hwnd"); ok {
		if v, err := strconv.ParseUint(n.String(), 10, 64); err == nil && v != 0 {
			lifecycleUser32.NewProc("ShowWindow").Call(uintptr(v), 9) // SW_RESTORE
			restoreForeground(uintptr(v))
		}
	}
	_ = exec.Command("wt.exe", "-w", filepath.Base(dir), "focus-pane", "-t", "0").Run()
}

// restoreForeground gives focus back to the human's window (AttachThreadInput
// lets a background process hand focus back).
func restoreForeground(hwnd uintptr) {
	user32 := lifecycleUser32
	if hwnd == 0 {
		return
	}
	if ok, _, _ := user32.NewProc("IsWindow").Call(hwnd); ok == 0 {
		return
	}
	foreground, _, _ := user32.NewProc("GetForegroundWindow").Call()
	if foreground == hwnd {
		return
	}
	fgThread, _, _ := user32.NewProc("GetWindowThreadProcessId").Call(foreground, 0)
	me := uintptr(windows.GetCurrentThreadId())
	attach := user32.NewProc("AttachThreadInput")
	attach.Call(fgThread, me, 1)
	user32.NewProc("SetForegroundWindow").Call(hwnd)
	attach.Call(fgThread, me, 0)
}
