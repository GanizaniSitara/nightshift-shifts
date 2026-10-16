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
                        [--continue-from SHIFT_DIR] [--crew CREW] [--dry-run]
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
  --crew CREW           crew manifest crews/CREW.yaml: roles, briefs, colours,
                        counts, model (default: default = one Foreman, one
                        Advisor, one Coder, one Tester)
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
		{name: "--continue-from"}, {name: "--crew"}, {name: "--dry-run", flag: true}}
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
	if prompt != nil && args.present["--prompt-file"] {
		return "", startUsageError("give the task either as an argument or with --prompt-file, not both")
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
	name := "Nightshift-shift-" + randomHex(8)
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
	crew, err := loadCrew(crewName, repoRoot)
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
	// The manifest Advisor is not started with the crew: its cell is an empty, reserved slot (acc1f52).
	labels, cells := []string{}, []string{}
	advisorSlot := 0
	colours := map[string]string{}
	for i, m := range members {
		if isAdvisor(m) && advisorSlot == 0 {
			advisorSlot = i + 1
			cells = append(cells, "")
		} else {
			labels = append(labels, m.label)
			cells = append(cells, m.label)
		}
		if m.tool == "agy" || m.tool == "codex" || m.tool == "copilot" {
			colours[m.label] = m.colour
		}
	}
	slots := crew.slots
	if slots == 0 && advisorSlot != 0 {
		slots = len(members)
	}
	for len(cells) < slots {
		cells = append(cells, "")
	}
	title := paneTitle(name)
	if ticket != "" {
		title = paneTitle(ticket)
	}
	command := wtCommand(wtSpec{name: name, stateDir: stateDir, agents: true, modeFlag: "--start", cwd: cwd, members: cells,
		crew: crew.name, ticket: ticket, slots: slots, colours: colours})
	if args.present["--dry-run"] {
		if print {
			outLine(renderJSON(startPlan(name, stateDir, cwd, ticket, title, task, required, crew, members, cells, slots, advisorSlot,
				command, worktree, config), true, false))
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
	memberObjects := []any{}
	for _, m := range members {
		memberObjects = append(memberObjects, m.object())
	}
	crewJSON := object{{"crew", crew.name}, {"members", memberObjects}, {"slots", slotsValue(slots)},
		{"advisor_slot", slotsValue(advisorSlot)}, {"permission_mode", crew.permissionMode}, {"model", crew.model}, {"cwd", cwd}}
	if crew.maxCredits != nil {
		crewJSON = append(crewJSON, pair{"max_credits", crew.maxCredits})
	}
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
	labelValues := stringsToAny(labels)
	busAppend(stateDir, "start", object{{"shift", name}, {"ticket", nullable(ticket)}, {"cwd", cwd}, {"crew", crew.name}, {"members", labelValues}})
	if err := installProfile(stateDir); err != nil {
		return "", startExit(err.Error())
	}
	if err := launchCrew(command, name, stateDir, object{{"mode", "start"}, {"prompt_file", promptFile}}, labels, slots, print); err != nil {
		return "", err
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
	slotCount, advisorSlot int, command []string, worktree, config object) object {
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
		if advisorSlot != 0 && advisorSlot <= len(cells) && cells[advisorSlot-1] == "" && members[advisorSlot-1] == m {
			role = append(role, pair{"status", "not_started"}, pair{"reserved_slot", advisorSlot})
		}
		role = append(role, pair{"compaction", compactionControl(m.tool, m.role)})
		if m.tool != "claude" {
			role = append(role, pair{"tool", m.tool}, pair{"model", m.model}, pair{"exe", toolExe(m.tool)})
		} else if m.model != crew.model {
			role = append(role, pair{"model", m.model})
		}
		for _, key := range []string{"reasoning", "context", "account", "max_credits"} {
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
			cwd, shift, dir, strings.TrimPrefix(shift, "Nightshift-shift-"))
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
	agents                                                 bool
	members                                                []string
	slots                                                  int
	colours                                                map[string]string
}

// wtCommand is the wt.exe argv: members fill cells 1..k; with slots the rest
// are empty cmd shells `spawn` can fill. 2 panes side by side, 3-4 one row,
// 5-8 two rows (top ceil(n/2)).
func wtCommand(w wtSpec) []string {
	exe := launcherExe()
	start := w.modeFlag == "--start"
	dir := "%USERPROFILE%"
	if start && w.cwd != "" {
		dir = w.cwd
	}
	label := w.name
	if w.ticket != "" {
		label = w.ticket
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
			title = paneTitle(label)
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
		}
		command = append(command, "--title", paneTitle(label), "--suppressApplicationTitle", "-d", dir,
			"cmd.exe", "/k", exe, "--slot", strconv.Itoa(i+1), "--state-dir", w.stateDir)
	}
	columns := func(from, to int) {
		cells := to - from
		for i := 1; i <= cells; i++ {
			left := cells + 1 - i
			place([]string{"split-pane", "-V", "-s", strconv.FormatFloat(float64(left)/float64(left+1), 'g', 4, 64)}, from+i-1)
		}
	}
	n := w.slots
	if n == 0 {
		n = len(w.members)
	}
	place([]string{"new-tab"}, 0)
	if n == 2 {
		place([]string{"split-pane", "-V", "-s", "0.5"}, 1)
		add([]string{"move-focus", "left"}, "")
		return command
	}
	if n <= 4 {
		columns(1, n)
	} else {
		top := (n + 1) / 2
		place([]string{"split-pane", "-H", "-s", "0.5"}, top)
		add([]string{"move-focus", "up"}, "")
		columns(1, top)
		add([]string{"move-focus", "down"}, "")
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
	for _, m := range members {
		if objectString(m, "tool") == "agy" {
			installAgyStatusline()
			break
		}
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
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return startExit(fmt.Sprintf("Command %s returned non-zero exit status: %s", pyRepr(command[0]), err))
	}
	for i := 0; i < 20; i++ {
		var created uintptr
		for _, w := range terminalWindows() {
			if !before[w.hwnd] {
				created = w.hwnd
				break
			}
		}
		if created != 0 {
			record = append(record, pair{"hwnd", int64(created)})
			if err := os.WriteFile(launchFile, []byte(renderJSON(record, false, true)), 0666); err != nil {
				return startExit(err.Error())
			}
			time.Sleep(time.Second)
			restoreForeground(human)
			break
		}
		time.Sleep(250 * time.Millisecond)
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
		outLine(renderJSON(report, false, true))
	}
	if len(pending) > 0 {
		return startExit("WT activation returned success, but these panes never reported: " + strings.Join(pending, ", ") + ".")
	}
	return nil
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
