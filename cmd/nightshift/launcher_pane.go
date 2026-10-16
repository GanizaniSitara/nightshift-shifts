package main

// The hidden child entry points wt.exe runs in each pane (--pane, --slot) and
// the top-level parser behind them, with the legacy layout and --live trials.
// Ported from nightshift_shifts.py pane(), slot_shell() and main().

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"golang.org/x/sys/windows"
)

const (
	claudeExe      = `C:\nvm4w\nodejs\node_modules\@anthropic-ai\claude-code\bin\claude.exe`
	paneDefaultCwd = "C:/git/nightshift"
	trialMessage   = "Begin the Nightshift communication exercise described in your role instructions."
)

var defaultLabels = []string{"Foreman", "Coder"}

// trustPrompts: (dialog text, accepting option highlighted, text proving the CLI is past it).
var trustPrompts = map[string][3]string{
	"claude":  {"Yes, I trust this folder", `❯\s*Yes, I trust this folder`, "Claude Code v"},
	"agy":     {"Do you trust the contents of this project?", `[>❯›]\s*Yes, I trust this folder`, ""},
	"codex":   {"Trust this folder? Codex can read", `[›❯>]\s*(?:\d+\.\s*)?Trust and continue`, ""},
	"copilot": {"Do you trust the files in this folder?", `❯\s*1\. Yes`, ""},
}

var (
	trustElsewhere   = []string{"Trusting will apply to the repository root"}
	trustBoxChars    = regexp.MustCompile(`[│┃║]`)
	codexDialogHints = regexp.MustCompile(`(?im)(?:\(y/n\)|\[y/n\]|press enter to confirm|do you trust|would you like to|allow once|approve|select an option|^\s*[›❯]?\s*\d+\. )`)
	permissionGlob   = regexp.MustCompile(`([*?\[\]])`)
)

// topLevel is the oracle's main() parser: no command, or arguments that start with "-".
func topLevel(argv []string) int {
	options := []writeOption{{name: "--pane"}, {name: "--state-dir"}, {name: "--start", flag: true}, {name: "--cwd"},
		{name: "--crew"}, {name: "--slot", number: "int"}, {name: "--live", flag: true}, {name: "--brief-file"}}
	args, parseError := parseWriteArgs(argv, options, nil, nil)
	prog := filepath.Base(os.Args[0])
	usage := "usage: " + prog + " [-h] [--live] [--brief-file BRIEF_FILE]"
	if args.help {
		out(usage + "\n\n" + topLevelHelpBody)
		return 0
	}
	if parseError != "" {
		return writeUsage(prog, usage, parseError)
	}
	cwd := args.values["--cwd"]
	if !args.present["--cwd"] {
		cwd = paneDefaultCwd
	}
	slot := 0
	if s := args.values["--slot"]; s != "" {
		n, _ := parseInteger(s)
		slot = int(n)
	}
	stateDir := args.values["--state-dir"]
	if role := args.values["--pane"]; role != "" {
		return commandExit(pane(role, stateDir, args.present["--live"], args.values["--brief-file"], args.present["--start"], cwd, args.values["--crew"], slot))
	}
	if slot != 0 {
		return commandExit(slotShell(slot, stateDir))
	}
	return commandExit(layoutTrial(args.present["--live"], args.values["--brief-file"]))
}

// layoutTrial is the legacy two-pane window: placeholders, or --live ping/pong agents.
func layoutTrial(live bool, briefFile string) error {
	name := "Nightshift-trial-" + randomHex(8)
	stateDir := filepath.Join(stateRoot(), "layout-trials", name)
	if err := os.MkdirAll(stateDir, 0777); err != nil {
		return startExit(err.Error())
	}
	if live {
		if err := installProfile(stateDir); err != nil {
			return startExit(err.Error())
		}
	}
	mode := ""
	if live {
		mode = "--live"
	}
	command := wtCommand(wtSpec{name: name, stateDir: stateDir, agents: live, modeFlag: mode, briefFile: briefFile,
		members: defaultLabels, crew: "default"})
	return launchCrew(command, name, stateDir, object{{"live_requested", live}}, defaultLabels, 0, true)
}

// slotShell records the cmd.exe running an empty container slot (our parent) and returns to its prompt.
func slotShell(slot int, stateDir string) error {
	old, _ := readObject(filepath.Join(stateDir, "slots", strconv.Itoa(slot)+".json"))
	kept := object{}
	for _, p := range old {
		if p.key == "role" || p.key == "identity" || p.key == "spawned" {
			kept = setValue(kept, p.key, p.val)
		}
	}
	return writeSlot(stateDir, slot, append(kept, pair{"cmd_pid", os.Getppid()}))
}

// writeSlot is write_slot: {started, role: None, **fields, slot} with fields winning in place.
func writeSlot(stateDir string, slot int, fields object) error {
	dir := filepath.Join(stateDir, "slots")
	if err := os.MkdirAll(dir, 0777); err != nil {
		return err
	}
	record := object{{"started", epochNow()}, {"role", nil}}
	for _, p := range fields {
		record = setValue(record, p.key, p.val)
	}
	record = setValue(record, "slot", slot)
	return writeJSONFile(filepath.Join(dir, strconv.Itoa(slot)+".json"), record)
}

func epochNow() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// memberFromObject reads one crew.json member back into a crewMember.
func memberFromObject(o object) *crewMember {
	m := &crewMember{role: objectString(o, "role"), label: objectString(o, "label"), identity: objectString(o, "identity"),
		brief: objectString(o, "brief"), colour: objectString(o, "colour"), tool: objectString(o, "tool"),
		model: objectString(o, "model"), peer: objectString(o, "peer"), persona: rowValue(o, "persona"),
		human: truthy(rowValue(o, "human_contact")), replacement: truthy(rowValue(o, "replacement"))}
	for _, key := range []string{"reasoning", "context", "account", "permissions", "max_credits"} {
		if v, ok := objectGet(o, key); ok {
			m.opts = append(m.opts, pair{key, v})
		}
	}
	return m
}

func numberIs(v any, want float64) bool {
	switch x := v.(type) {
	case int:
		return float64(x) == want
	case int64:
		return float64(x) == want
	case float64:
		return x == want
	case json.Number:
		f, err := x.Float64()
		return err == nil && f == want
	}
	return false
}

func intValue(v any) int {
	switch x := v.(type) {
	case int:
		return x
	case int64:
		return int(x)
	case json.Number:
		n, _ := x.Int64()
		return int(n)
	}
	return 0
}

// pane runs one crew member's CLI in this Terminal pane and records it.
func pane(role, stateDir string, live bool, briefFile string, start bool, cwd, crewName string, slot int) error {
	if !live && !start {
		return placeholderPane(role, stateDir)
	}
	model, permissionMode, tool := launcherModel, startPermissionMode, "claude"
	reasoning, context := "", ""
	var account any
	maxCredits := 0
	colour := "green"
	if role == "Foreman" {
		colour = "red"
	}
	shift := filepath.Base(stateDir)
	peer := "Foreman"
	if role == "Foreman" {
		peer = "Coder"
	}
	identity := shift + "-" + strings.ToLower(role)
	peerName := shift + "-" + strings.ToLower(peer)
	firstMessage := trialMessage
	brief := "You are " + role + " in the Nightshift two-agent proof of concept. Your session name is " + identity + ". " +
		"Your only crew peer is " + peerName + ". Foreman is the master and sole human contact. " +
		"Remain an interactive session. This first exercise is communication only: do not edit repository files, " +
		"run builds, launch additional agents or change settings. Use native ListAgents and SendMessage if available. " +
		"Report honestly if either tool is unavailable or peer discovery fails. Do not contact unrelated sessions. "
	if role == "Foreman" {
		brief += "Discover the Coder and send it NIGHTSHIFT-PING-" + shift + ". Ask it to reply with " +
			"NIGHTSHIFT-PONG-" + shift + " and its role. Report success only after receiving its reply. " +
			"If the Coder is not registered yet, state that and remain ready for its READY message."
	} else {
		brief += "Discover Foreman and send READY from " + identity + ". When it sends NIGHTSHIFT-PING-" + shift + ", " +
			"reply to it with NIGHTSHIFT-PONG-" + shift + " and your role. Then await assignments from Foreman."
	}
	if briefFile != "" {
		text, err := readTextUniversal(briefFile)
		if err != nil {
			return startExit(err.Error())
		}
		brief = "You are " + role + " in Nightshift. Your unique session name is " + identity + "; your only crew peer is " +
			peerName + ". Foreman is master and sole human contact. Use native ListAgents and SendMessage " +
			"to coordinate only with that peer. Remain interactive. Read the following authorized assignment " +
			"and act according to your role. Coder sends READY to Foreman, then waits for its assignment. " +
			"Foreman delegates implementation to Coder, reviews and checks the result, then reports. " +
			"Do not spawn additional agents.\n" + text
	}
	var member *crewMember
	var savedCrew object
	envRole := role
	if start {
		var members []*crewMember
		crewLabel := ""
		if saved, ok := readObject(filepath.Join(stateDir, "crew.json")); ok {
			if items, ok := rowValue(saved, "members").([]any); ok {
				savedCrew = saved
				for _, item := range items {
					if o, ok := item.(object); ok {
						members = append(members, memberFromObject(o))
					}
				}
				crewLabel = "default"
				if v, ok := objectGet(saved, "crew"); ok {
					crewLabel = valueText(v)
				}
				permissionMode = valueText(rowValue(saved, "permission_mode"))
				if rowValue(saved, "permission_mode") == nil {
					permissionMode = ""
				}
				goto found
			}
		}
		{
			repoRoot := canonicalRepoRoot(cwd, stateDir)
			config, err := loadRepoConfig(repoRoot)
			if err != nil {
				return startExit(err.Error())
			}
			if crewName == "" {
				crewName = configString(config, "default_crew")
			}
			if crewName == "" {
				crewName = "default"
			}
			crew, err := loadCrew(crewName, repoRoot)
			if err != nil {
				return startExit(err.Error())
			}
			members = crewMembersOf(crew, shift)
			crewLabel, permissionMode = crew.name, crew.permissionMode
		}
	found:
		for _, m := range members {
			if m.label == role {
				member = m
				break
			}
		}
		if member == nil {
			return startExit("pane " + role + " is not a member of crew " + crewLabel)
		}
		identity, peerName, colour = member.identity, member.peer, member.colour
		model, tool = member.model, member.tool
		reasoning, context, account = member.optString("reasoning"), member.optString("context"), member.opt("account")
		maxCredits = intValue(member.opt("max_credits"))
		if p := member.optString("permissions"); p != "" {
			permissionMode = p
		} else if permissionMode == "" {
			permissionMode = startPermissionMode
		}
		firstMessage = openingMessage(member, stateDir)
		persona, _ := member.persona.(string)
		var err error
		if brief, err = roleBrief(member.role, identity, peerName, stateDir, member.brief, persona, cwd, ""); err != nil {
			return startExit(err.Error())
		}
		if member.human {
			text, err := readTextUniversal(filepath.Join(stateDir, "prompt.txt"))
			if err != nil {
				return startExit(err.Error())
			}
			brief += text
		}
		envRole = member.role
	}
	var command []string
	if toolExe(tool) != "" {
		session := role + " (" + identity + ")"
		command = toolCommand(tool, model, brief, firstMessage, permissionMode, reasoning, context, &session, maxCredits)
	} else {
		command = append([]string{claudeExe, "--name", identity, "--model", model}, permissionFlags("claude", permissionMode)...)
		command = append(command, "--append-system-prompt", brief)
		if start && strings.ToLower(member.role) == "foreman" {
			command = append(command, "--settings", renderJSON(foremanClaudeSettings(cwd), false, true))
		}
		if reasoning != "" {
			command = append(command, "--effort", reasoning)
		}
	}
	env := crewEnv(os.Environ(), tool, account, envRole)
	if tool == "agy" {
		// agy has no --name: the Nightshift status line shows the role from these (acc1f52)
		env = append(env, "NIGHTSHIFT_ROLE="+role, "NIGHTSHIFT_IDENTITY="+identity, "NIGHTSHIFT_ROLE_COLOUR="+colour)
	}
	tinted := tool == "agy" || tool == "codex" || tool == "copilot"
	background := campbellBackground
	if tinted {
		if b := backgroundFor(colour); b != "" {
			background = b
		}
	}
	setPaneBackground(background)
	var preRecord object
	if consult, _ := rowValue(savedCrew, "consult").(object); start && member != nil && isAdvisor(member) && truthy(consult) {
		// The consultation is recorded only after this Advisor incarnation exists, then becomes the exact first
		// prompt, so mailbox fencing and the visible first message stay in lockstep.
		var err error
		if preRecord, firstMessage, err = consultFirstMessage(stateDir, role, identity, peerName, model, cwd, tool, consult, savedCrew); err != nil {
			return startExit(err.Error())
		}
		if toolExe(tool) != "" {
			session := role + " (" + identity + ")"
			command = toolCommand(tool, model, brief, firstMessage, permissionMode, reasoning, context, &session, maxCredits)
		} else {
			// as the oracle: this rebuild carries no --settings or --effort
			command = append([]string{claudeExe, "--name", identity, "--model", model}, permissionFlags("claude", permissionMode)...)
			command = append(command, "--append-system-prompt", brief)
		}
	}
	code, err := runPaneChild(command, cwd, env, role, identity, peerName, model, tool, colour, firstMessage, stateDir, start, slot, preRecord)
	if tinted {
		setPaneBackground(campbellBackground)
	}
	if err != nil {
		return startExit(err.Error())
	}
	out(fmt.Sprintf("%s exited with code %d.\n", role, code))
	if start && slot != 0 {
		record, _ := readObject(filepath.Join(stateDir, "slots", strconv.Itoa(slot)+".json"))
		cmdPid := processNumber(record, "cmd_pid")
		if !(cmdPid != 0 && processRunning(cmdPid)) {
			shell := exec.Command("cmd.exe", "/k", launcherExe(), "--slot", strconv.Itoa(slot), "--state-dir", stateDir)
			shell.Stdin, shell.Stdout, shell.Stderr = os.Stdin, os.Stdout, os.Stderr
			_ = shell.Run()
		}
		return nil
	}
	out("Press Enter to close this pane.")
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	return nil
}

// paneChild is a started CLI whose exit can be polled without blocking.
type paneChild struct {
	cmd  *exec.Cmd
	pid  int
	done chan struct{}
}

func (c *paneChild) exited() bool {
	select {
	case <-c.done:
		return true
	default:
		return false
	}
}

// consultFirstMessage records the Advisor's pre-start incarnation and its first assignment (the consultation),
// marks it sent, and returns that record and the first message: the header, a blank line, the question.
func consultFirstMessage(stateDir, role, identity, peerName, model, cwd, tool string, consult, saved object) (object, string, error) {
	pre := object{{"wrapper_pid", os.Getpid()}, {"role", role}, {"started", epochNow()}, {"name", identity}, {"peer", peerName},
		{"model", model}, {"cwd", cwd}, {"tool", tool}}
	var writeErr error
	if pre, writeErr = writeRoleRecord(stateDir, role, pre, true); writeErr != nil {
		return nil, "", writeErr
	}
	question := valueText(rowValue(consult, "question"))
	var row object
	err := withWriteMailbox(stateDir, func(db *sql.DB) error {
		id, err := insertMessage(db, stateDir, "assignment", humanLabel(stateDir), role, question, rowValue(consult, "asg"), nil)
		if err != nil {
			return err
		}
		if _, err = db.Exec("UPDATE messages SET state = 'sent', delivered_epoch = ? WHERE id = ?", epochNow(), id); err != nil {
			return err
		}
		row, err = getMessage(db, id)
		return err
	})
	if err != nil {
		return nil, "", err
	}
	busAppend(stateDir, "sent", object{{"id", rowValue(row, "id")}})
	if err := writeJSONFile(filepath.Join(stateDir, "crew.json"), setValue(append(object{}, saved...), "consult", nil)); err != nil {
		return nil, "", err
	}
	return pre, msgHeader(row) + "\n\n" + question, nil
}

// earlierIncarnations is a <role>.json's history plus the record itself, without its own history.
func earlierIncarnations(previous object) []any {
	history, _ := rowValue(previous, "history").([]any)
	earlier := object{}
	for _, p := range previous {
		if p.key != "history" {
			earlier = append(earlier, p)
		}
	}
	return append(append([]any{}, history...), earlier)
}

func runPaneChild(command []string, cwd string, env []string, role, identity, peerName, model, tool, colour, firstMessage, stateDir string,
	start bool, slot int, preRecord object) (int, error) {
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Dir, cmd.Env = cwd, env
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	child := &paneChild{cmd: cmd, pid: cmd.Process.Pid, done: make(chan struct{})}
	var waitErr error
	go func() { waitErr = cmd.Wait(); close(child.done) }()
	// {**pre_record, pid, wrapper_pid, role, started, name, peer, model, cwd}: pre_record's keys keep their places
	started := any(epochNow())
	if preRecord != nil {
		started = rowValue(preRecord, "started")
	}
	record := append(object{}, preRecord...)
	for _, p := range []pair{{"pid", child.pid}, {"wrapper_pid", os.Getpid()}, {"role", role}, {"started", started},
		{"name", identity}, {"peer", peerName}, {"model", model}, {"cwd", cwd}} {
		record = setValue(record, p.key, p.val)
	}
	if start {
		record = setValue(record, "tool", tool)
	}
	var recordErr error
	if record, recordErr = writeRoleRecord(stateDir, role, record, preRecord == nil); recordErr != nil {
		return 0, recordErr
	}
	// Discovery is best-effort and fenced to this exact child incarnation. It
	// never replaces the launch record or an incarnation written by a respawn.
	if start {
		recordStarted, _ := objectNumber(record, "started")
		go watchRoleTelemetry(child, stateDir, role, uint32(child.pid), numberFloat(recordStarted))
	}
	if slot != 0 {
		old, _ := readObject(filepath.Join(stateDir, "slots", strconv.Itoa(slot)+".json"))
		kept := object{}
		for _, p := range old {
			if p.key != "slot" {
				kept = append(kept, p)
			}
		}
		for _, p := range []pair{{"role", role}, {"identity", identity}, {"wrapper_pid", os.Getpid()}} {
			kept = setValue(kept, p.key, p.val)
		}
		if err := writeSlot(stateDir, slot, kept); err != nil {
			return 0, err
		}
	}
	if tool != "claude" {
		registerName(child.pid, identity)
		acceptTrustPrompt(child, tool)
		if tool == "codex" {
			go func() {
				for i := 0; i < 6 && !child.exited(); i++ {
					if setCodexRoleName(child, role, identity) {
						return
					}
				}
			}()
		}
	} else {
		acceptTrustPrompt(child, "claude")
		if setRoleColour(child, colour, cwd) {
			if !injectCommand(child.pid, firstMessage, 60) {
				out(fmt.Sprintf("%s: Claude opening message injection failed; startup stopped.\n", role))
			}
		} else {
			out(fmt.Sprintf("%s: Claude role colour was not confirmed; opening message not sent.\n", role))
		}
	}
	<-child.done
	var exit *exec.ExitError
	if waitErr != nil && !errors.As(waitErr, &exit) {
		return 0, waitErr
	}
	return int(uint32(cmd.ProcessState.ExitCode())), nil
}

// foremanClaudeSettings denies built-in edits inside the repository (not a sandbox).
func foremanClaudeSettings(cwd string) object {
	repo := strings.TrimRight(filepath.ToSlash(pyResolve(cwd)), "/")
	if len(repo) >= 2 && repo[1] == ':' && unicode.IsLetter(rune(repo[0])) {
		repo = "/" + strings.ToLower(repo[:1]) + repo[2:]
	}
	repo = permissionGlob.ReplaceAllString(repo, `\$1`)
	return object{{"permissions", object{{"deny", []any{"Edit(/" + repo + "/**)"}}}}}
}

// agentsCLI runs agents-cli with a timeout; stdout, exit code (-1 when it could not run).
func agentsCLI(timeout time.Duration, stdin []byte, args ...string) ([]byte, int) {
	if timeout <= 0 {
		return nil, -1
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "agents-cli", args...)
	if stdin != nil {
		cmd.Stdin = strings.NewReader(string(stdin))
	}
	data, err := cmd.Output()
	if ctx.Err() != nil {
		return data, -1
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return data, exit.ExitCode()
	}
	if err != nil {
		return data, -1
	}
	return data, 0
}

func registerName(pid int, identity string) {
	agentsCLI(15*time.Second, nil, "name", strconv.Itoa(pid), identity)
}

// peekLines is the pane's visible lines via agents-cli; nil when unreadable.
func peekLines(pid int, timeout time.Duration) []any {
	data, code := agentsCLI(timeout, nil, "_attach", "peek", strconv.Itoa(pid), "0")
	if code != 0 {
		return nil
	}
	if len(data) == 0 {
		data = []byte("{}")
	}
	value, err := parseJSON([]byte(strings.ToValidUTF8(string(data), "\ufffd")))
	result, ok := value.(object)
	if err != nil || !ok || truthy(rowValue(result, "error")) {
		return nil
	}
	if v, ok := objectGet(result, "lines"); ok {
		lines, _ := v.([]any)
		return lines
	}
	return []any{}
}

func lineStrings(lines []any) ([]string, bool) {
	out := make([]string, 0, len(lines))
	all := true
	for _, line := range lines {
		s, ok := line.(string)
		if !ok {
			all = false
			s = valueText(line)
		}
		out = append(out, s)
	}
	return out, all
}

// trustScreen is the pane's visible text: agents-cli peek lines, else its raw output.
func trustScreen(pid int) string {
	data, _ := agentsCLI(10*time.Second, nil, "_attach", "peek", strconv.Itoa(pid), "0")
	raw := strings.ToValidUTF8(string(data), "\ufffd")
	if value, err := parseJSON([]byte(raw)); err == nil {
		if o, ok := value.(object); ok {
			if lines, ok := rowValue(o, "lines").([]any); ok {
				text, _ := lineStrings(lines)
				return strings.Join(text, "\n")
			}
		}
	}
	return raw
}

// trustPromptState: accept, dialog, past, or "" (keep polling).
func trustPromptState(tool, screen string) string {
	spec := trustPrompts[tool]
	flat := strings.Join(strings.Fields(trustBoxChars.ReplaceAllString(screen, " ")), " ")
	if strings.Contains(flat, spec[0]) {
		for _, other := range trustElsewhere {
			if strings.Contains(flat, other) {
				return "past"
			}
		}
		if trustAcceptHighlighted(tool, spec[1], screen) {
			return "accept"
		}
		return "dialog"
	}
	if spec[2] != "" && strings.Contains(screen, spec[2]) {
		return "past"
	}
	return ""
}

// trustAcceptHighlighted is re.search(accept, screen); Copilot's "1. Yes" must not run on into "Yes, and..." (no lookahead in Go).
func trustAcceptHighlighted(tool, pattern, screen string) bool {
	matches := regexp.MustCompile(pattern).FindAllStringIndex(screen, -1)
	if tool != "copilot" {
		return len(matches) > 0
	}
	for _, m := range matches {
		rest := []rune(screen[m[1]:])
		if len(rest) == 0 || !(rest[0] == ',' || rest[0] == '_' || unicode.IsLetter(rest[0]) || unicode.IsDigit(rest[0])) {
			return true
		}
	}
	return false
}

func acceptTrustPrompt(child *paneChild, tool string) {
	if _, ok := trustPrompts[tool]; !ok {
		return
	}
	pid := strconv.Itoa(child.pid)
	for i := 0; i < 40; i++ {
		if child.exited() {
			return
		}
		state := trustPromptState(tool, trustScreen(child.pid))
		if state == "accept" || state == "dialog" {
			for attempt := 0; attempt < 6; attempt++ {
				if state == "accept" {
					agentsCLI(10*time.Second, nil, "key", pid, "enter")
					return
				}
				if state != "dialog" {
					return
				}
				agentsCLI(10*time.Second, nil, "key", pid, "down")
				time.Sleep(time.Second)
				state = trustPromptState(tool, trustScreen(child.pid))
			}
			return
		}
		if state == "past" {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// promptIsEmpty: Claude's input box (the last ❯ line) holds no text.
func promptIsEmpty(lines []any) bool {
	text, _ := lineStrings(lines)
	last := ""
	found := false
	for _, line := range text {
		if strings.HasPrefix(strings.TrimLeftFunc(line, unicode.IsSpace), "❯") {
			last, found = line, true
		}
	}
	return found && strings.TrimSpace(last) == "❯"
}

// injectCommand types a line into the pane only when its input box is empty.
func injectCommand(pid int, command string, tries int) bool {
	for i := 0; i < tries; i++ {
		if promptIsEmpty(peekLines(pid, 10*time.Second)) {
			data, code := agentsCLI(10*time.Second, []byte(command), "_attach", "inject", strconv.Itoa(pid), "1")
			if code != 0 {
				return false
			}
			if len(data) == 0 {
				data = []byte("{}")
			}
			value, err := parseJSON([]byte(strings.ToValidUTF8(string(data), "\ufffd")))
			result, ok := value.(object)
			return err != nil || !ok || !truthy(rowValue(result, "error"))
		}
		time.Sleep(time.Second)
	}
	return false
}

// codexPromptIsEmpty recognises Codex's bottom composer, empty and ready; unknown hints fail closed.
func codexPromptIsEmpty(raw []any) bool {
	lines, all := lineStrings(raw)
	if raw == nil || !all {
		return false
	}
	for i := range lines {
		lines[i] = strings.TrimRightFunc(lines[i], unicode.IsSpace)
	}
	for len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) < 3 || !strings.HasPrefix(lines[len(lines)-1], "  ") {
		return false
	}
	prompt := -1
	for i, line := range lines {
		if strings.HasPrefix(strings.TrimLeftFunc(line, unicode.IsSpace), "›") {
			prompt = i
		}
	}
	if prompt < 0 || prompt < len(lines)-6 || prompt >= len(lines)-2 {
		return false
	}
	if s := strings.TrimSpace(lines[prompt]); s != "›" && s != "› Ask Codex to do anything" {
		return false
	}
	for _, line := range lines[prompt+1 : len(lines)-1] {
		if strings.TrimSpace(line) != "" {
			return false
		}
	}
	if prompt > 4 {
		worked, busy := false, false
		for _, line := range lines[:prompt] {
			worked = worked || strings.Contains(line, "Worked for ")
			busy = busy || strings.Contains(line, "esc to interrupt")
		}
		if !worked || busy {
			return false
		}
	}
	from := prompt - 4
	if from < 0 {
		from = 0
	}
	return !codexDialogHints.MatchString(strings.Join(lines[from:], "\n"))
}

// setCodexRoleName sends /rename once after two empty-composer reads, within 30 seconds.
func setCodexRoleName(child *paneChild, role, identity string) bool {
	deadline := time.Now().Add(30 * time.Second)
	left := func() time.Duration {
		d := time.Until(deadline)
		if d > 10*time.Second {
			d = 10 * time.Second
		}
		return d
	}
	for i := 0; i < 30; i++ {
		if child.exited() || !time.Now().Before(deadline) {
			return false
		}
		if codexPromptIsEmpty(peekLines(child.pid, left())) {
			if child.exited() || !time.Now().Before(deadline) {
				return false
			}
			if codexPromptIsEmpty(peekLines(child.pid, left())) {
				if child.exited() || !time.Now().Before(deadline) {
					return false
				}
				_, code := agentsCLI(left(), []byte("/rename "+role+" ("+identity+")"), "_attach", "inject", strconv.Itoa(child.pid), "1")
				return code == 0
			}
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return false
		}
		if remaining > 500*time.Millisecond {
			remaining = 500 * time.Millisecond
		}
		time.Sleep(remaining)
	}
	return false
}

// enableVT turns on virtual terminal processing for stdout; false when it is not a console.
func enableVT() bool {
	handle := windows.Handle(os.Stdout.Fd())
	var mode uint32
	if windows.GetConsoleMode(handle, &mode) != nil {
		return false
	}
	return windows.SetConsoleMode(handle, mode|windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING) == nil
}

// setPaneBackground sets this pane's default background (OSC 11), console only.
func setPaneBackground(background string) bool {
	if !enableVT() {
		return false
	}
	out("\x1b]11;" + background + "\x1b\\")
	return true
}

// setRoleColour sends /color and waits for Claude's transcript to record it.
func setRoleColour(child *paneChild, colour, cwd string) bool {
	if child.exited() {
		return false
	}
	if !injectCommand(child.pid, "/color "+colour, 60) {
		return false
	}
	home := userHome()
	registry := filepath.Join(home, ".claude", "sessions", strconv.Itoa(child.pid)+".json")
	project := strings.TrimRight(filepath.FromSlash(cwd), `\`)
	if strings.HasSuffix(project, ":") {
		project += `\`
	}
	projectDir := filepath.Join(home, ".claude", "projects", strings.NewReplacer(":", "-", `\`, "-", "/", "-").Replace(project))
	for i := 0; i < 120; i++ {
		if child.exited() {
			return false
		}
		if session, ok := readObject(registry); ok {
			if id, ok := rowValue(session, "sessionId").(string); ok {
				if data, err := os.ReadFile(filepath.Join(projectDir, id+".jsonl")); err == nil {
					lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
					for j := len(lines) - 1; j >= 0; j-- {
						value, err := parseJSON([]byte(lines[j]))
						record, ok := value.(object)
						if err == nil && ok && objectString(record, "type") == "agent-color" && objectString(record, "agentColor") == colour {
							return true
						}
					}
				}
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

// placeholderPane is the layout trial's inactive pane: it echoes input with the pane size.
func placeholderPane(role, stateDir string) error {
	enableVT()
	code := "90"
	if role == "Foreman" {
		code = "91"
	} else if role == "Coder" {
		code = "92"
	}
	out(fmt.Sprintf("\x1b[2J\x1b[H\x1b[%smNIGHTSHIFT / %s\x1b[0m\n\n", code, strings.ToUpper(role)))
	out("Layout trial - no agent running.\n\n")
	if role == "Foreman" || role == "Coder" {
		outLine("Foreman + Coder is the first crew.")
	} else {
		outLine("Reserved pane / inactive.")
	}
	out("\nEnter status to show pane dimensions.\nCtrl+C exits this placeholder.\n\n")
	if stateDir != "" {
		_ = os.WriteFile(filepath.Join(stateDir, role+".json"), []byte(renderJSON(object{{"pid", os.Getpid()}, {"role", role}}, false, true)), 0666)
	}
	reader := bufio.NewReader(os.Stdin)
	for {
		out(role + "> ")
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil
		}
		message := strings.TrimRight(line, "\r\n")
		columns, rows := terminalSize()
		out(fmt.Sprintf("%s received: %s | %d columns x %d rows\n", role, message, columns, rows))
		if stateDir != "" {
			received := object{{"pid", os.Getpid()}, {"role", role}, {"message", message}, {"columns", columns}, {"rows", rows}}
			_ = os.WriteFile(filepath.Join(stateDir, role+"-received.json"), []byte(renderJSON(received, false, true)), 0666)
		}
	}
}

// terminalSize is shutil.get_terminal_size: COLUMNS/LINES, else the console window, else 80x24.
func terminalSize() (int, int) {
	columns, _ := strconv.Atoi(os.Getenv("COLUMNS"))
	rows, _ := strconv.Atoi(os.Getenv("LINES"))
	if columns <= 0 || rows <= 0 {
		var info windows.ConsoleScreenBufferInfo
		c, r := 80, 24
		if windows.GetConsoleScreenBufferInfo(windows.Handle(os.Stdout.Fd()), &info) == nil {
			c, r = int(info.Window.Right-info.Window.Left+1), int(info.Window.Bottom-info.Window.Top+1)
		}
		if columns <= 0 {
			columns = c
		}
		if rows <= 0 {
			rows = r
		}
	}
	return columns, rows
}

// topLevelHelpBody is the oracle's -h after the usage line: its module docstring and command list.
const topLevelHelpBody = "Nightshift launcher: one Windows Terminal tab, Foreman, Advisor, Coder and Tester side by side.\n\n`start \"task\"` (via C:/scripts/nightshift.cmd) persists the task to\n<state_dir>/prompt.txt and starts interactive Claude Foreman, Advisor, Coder and Tester (the default crew;\nOpus 5.5, the Advisor Fable 5.1) in the \"Nightshift Crew\" Terminal profile (Cascadia Mono 12, dark). Only Foreman\nreceives the task; Coder and Tester get their role and peer identity, send READY and await work; the Advisor\n(NGH-027) stays dormant until Foreman consults it, and the mailbox lets only Foreman address it. The task\nreaches the panes by file path only, never as argv text. Panes run in the\nshift's isolated git worktree (or the caller's directory with --no-worktree) in Claude's auto permission\nmode (--permission-mode auto). Each `start` is a new shift: mesh names are\n<shift>-foreman / <shift>-advisor / <shift>-coder / <shift>-tester, pane titles are '<ticket or shift> <Role>' (e.g. 'NGH-019 Foreman'), a loop\nwindow is titled 'Nightshift <first ticket>..<last ticket>', and\n<state_dir>/<Role>.json records pid, name, peer and model. There is no restart or\nresume; a closed pane is not relaunched. `start --dry-run` prints the plan as\nJSON without launching or writing anything.\nContainer (NGH-025): a crew with layout 2x4 (8 slots, 2 rows x 4 columns) or 1x4 (4 equal columns) puts the crew\nin the first slots and plain cmd shells in the rest (each records <state_dir>/slots/<N>.json); the launcher exits once every pane and slot has\nreported. `spawn SHIFT --role ROLE [--cli claude|gemini|codex|copilot] [--crew NAME] [--slot N]` starts a session in a free\nslot of that window (Foreman's only way to add a crew member; --crew takes the role from crews/NAME.yaml), and Foreman ends its own shift with `down` (--close, the\ndefault, closes the window; --hold keeps the tab). Git shifts use per-shift worktrees and a short\nrepository lock during `land`; direct and non-git working folders hold one live shift at a time.\n`down` removes landed worktrees or preserves unlanded work on a recovery branch.\nCoders claim files before editing them (`claim`/`release`/`claims`, kept in <state_dir>/claims.json); a file\nheld by another role makes `claim` exit 4 and change nothing.\nCrew messages go through a durable mailbox (<state_dir>/mailbox.sqlite3): `msg send`/`msg sent`, `inbox`, `ack` (exit 5 = protocol violation).\nEvery mailbox row and the start/spawn/result/down events are appended to <state_dir>/bus.jsonl (the shift bus, a\nplain file with no process); `log SHIFT [--follow] [--since N] [--json]` prints it in order.\n`build` runs heavy builds one at a time per folder (exit 6 = waited past --timeout) and records <state_dir>/builds/<n>.json.\n`resume` reruns, through `loop`, every ticket whose latest shift never reached done, continuing from that shift.\nResult ladder: result.json carries a stage (implemented < tested < deployed < validated; none = implemented).\nA ticket line `requires: <rung>` sets the bar (default tested); a done below it is recorded as 'short' and stops the loop.\nThe crew comes from a YAML manifest: crews/default.yaml unless `start --crew NAME` picks\ncrews/NAME.yaml (roles, brief files, colours, counts, model, permission mode; 2-8 panes,\none row up to 4, two rows above). A role with count > 1 gets labels Role-1..N and names <shift>-role-N.\nLegacy modes: no args = placeholder layout; --live = ping/pong communication trial.\nWT separators are native argv, not shell chaining. No software installs.\n\noptions:\n  -h, --help            show this help message and exit\n  --live                diagnostic ping/pong communication trial with live\n                        agents\n  --brief-file BRIEF_FILE\n                        with --live: replace the trial brief with this\n                        assignment file\n\nCommands:\n  start \"task\"|TICKET [--dry-run] [--cwd D] [--crew NAME]\n                                              start a crew (git worktree by default; --no-worktree opts out)\n  land SHIFT [--keep-conflicts]                land a tested commit; exit 2 = retest rebase, 4 = conflict\n  status [--all]                              shifts and whether their crews are alive\n  down [SHIFT|latest] [--hold|--close]        capture panes, write handover.md, stop the crew; --close (default)\n                                              closes its window; --hold keeps the tab open\n  cleaner [--dry-run]                         reap dead or finished shifts\n  warden [SHIFT] [--dry-run]                  patrol live shifts without teardown\n  gaffer [--max-shifts N] [--ticket ID] [--crew NAME] [--repo PATH] [--tick] [--dry-run]\n                                              dispatch below the floor concurrency cap\n  spawn SHIFT --role ROLE [--cli claude|gemini|codex|copilot] [--crew NAME] [--slot N]\n                                              start a session in a free slot of the shift's 1x4 window\n  consult SHIFT QUESTION --asg ID             start the reserved Advisor with a durable first assignment\n  loop TICKET [TICKET...] [--stall-minutes N] optional batch runner: start, wait for result.json, down --close, next\n  loop --project PREFIX [--max N]             same over a project's backlog, lowest number first\n  resume [--project PREFIX] [--dry-run]       rerun tickets whose latest shift never reached done (--continue-from it)\n  claim SHIFT ROLE PATH...                    claim files before editing; exit 4 if another role holds one\n  release SHIFT ROLE [PATH...]                drop those claims, or all of ROLE's when no paths given\n  claims SHIFT                                print the shift's claims as JSON\n  msg send SHIFT FROM TO --kind K --body TEXT [--asg ID]\n                                              store a crew message; prints its header line\n  msg sent SHIFT ID                           mark a stored message as sent with SendMessage\n  inbox SHIFT ROLE                            unacked inbound, unsent outbound, active assignments\n  ack SHIFT ROLE ID                           acknowledge and print the stored body; exit 5 = do not act\n  build SHIFT [--cwd DIR] [--cmd 'COMMAND'] [--timeout MIN]\n                                              run a build one at a time per folder; evidence in builds/<n>.json\n  build --cancel SHIFT N                      kill build N and mark it cancelled\n  log SHIFT [--follow] [--since N] [--json]   print the shift bus (bus.jsonl): messages, acks, start..down\n  floor say FROM TEXT [--to NAME]             append a message to the machine floor bus\n  floor hear ME [--since ID] [--follow]       read floor messages addressed to ME or all\n"
