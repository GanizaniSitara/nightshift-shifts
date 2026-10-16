package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"gopkg.in/yaml.v2"
)

const landUsage = "usage: nightshift land [-h] [--target-branch TARGET_BRANCH] [--keep-conflicts]\n                       shift"
const landHelp = landUsage + "\n\npositional arguments:\n  shift\n\noptions:\n  -h, --help            show this help message and exit\n  --target-branch TARGET_BRANCH\n                        override the repository default landing branch\n  --keep-conflicts\n"
const downUsage = "usage: nightshift down [-h] [--hold | --close] [shift]"
const downHelp = downUsage + "\n\nStop a crew: capture each pane, write handover.md, end the sessions and\nrelease the folder lock. --close (default) closes the window; --hold leaves\nthe tab open. When run from inside the crew (Foreman ending its own shift),\nits own process tree is killed last, after the output.\n\npositional arguments:\n  shift       shift name, unique suffix, or \"latest\" (default)\n\noptions:\n  -h, --help  show this help message and exit\n  --hold      leave the Terminal tab open\n  --close     close the shift's Terminal window (default)\n"

// Keep Git's exit status separate: ancestry, rebase and symbolic-ref use it as data.
func lifecycleGit(cwd string, args ...string) (string, int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, args...)...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	data, err := cmd.Output()
	text := strings.ReplaceAll(strings.ToValidUTF8(string(data), "\ufffd"), "\r\n", "\n")
	if ctx.Err() != nil {
		return text, -1, fmt.Errorf("git command timed out after 120 seconds")
	}
	if err == nil {
		return text, 0, nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		message := strings.TrimSpace(strings.ToValidUTF8(stderr.String(), "\ufffd"))
		if message == "" {
			message = "git command failed"
		}
		return text, exit.ExitCode(), errors.New(message)
	}
	return text, -1, err
}

func lifecycleGitText(cwd string, args ...string) (string, error) {
	text, _, err := lifecycleGit(cwd, args...)
	return strings.TrimSpace(text), err
}

func worktreeInfo(dir string) (object, error) {
	path := filepath.Join(dir, "worktree.json")
	if !fileExists(path) {
		return nil, nil
	}
	return lifecycleReadObject(path)
}

func lifecycleReadObject(path string) (object, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	v, err := parseJSON(data)
	if err != nil {
		return nil, err
	}
	o, ok := v.(object)
	if !ok {
		return nil, fmt.Errorf("%s must contain a JSON object", path)
	}
	return o, nil
}

func sameLifecyclePath(a, b string) bool {
	aa, errA := canonicalPath(a)
	bb, errB := canonicalPath(b)
	return errA == nil && errB == nil && aa == bb
}

// result.json errors are persisted on the bus; retain the oracle's diagnostic.
func resultJSONError(data []byte, original error) error {
	var value any
	err := json.Unmarshal(data, &value)
	var syntax *json.SyntaxError
	if !errors.As(err, &syntax) {
		return original
	}
	pos := int(syntax.Offset) - 1
	message := ""
	switch {
	case strings.Contains(err.Error(), "object key string"):
		message = "Expecting property name enclosed in double quotes"
	case strings.Contains(err.Error(), "after object key:value pair"), strings.Contains(err.Error(), "after array element"):
		message = "Expecting ',' delimiter"
	case strings.Contains(err.Error(), "after object key"):
		message = "Expecting ':' delimiter"
	case strings.Contains(err.Error(), "after top-level value"):
		message = "Extra data"
	case strings.Contains(err.Error(), "looking for beginning of value"):
		message = "Expecting value"
	default:
		return original
	}
	if pos < 0 || pos > len(data) {
		return original
	}
	prefix := string(data[:pos])
	line := strings.Count(prefix, "\n") + 1
	column := utf8.RuneCountInString(prefix[strings.LastIndex(prefix, "\n")+1:]) + 1
	return fmt.Errorf("%s: line %d column %d (char %d)", message, line, column, utf8.RuneCountInString(prefix))
}

func landCommand(argv []string) int {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--target-branch"}, {name: "--keep-conflicts", flag: true}}, []string{"shift"}, nil)
	usage := func(s string) int { return writeUsage("nightshift land", landUsage, s) }
	if args.help {
		out(landHelp)
		return 0
	}
	if message != "" {
		return usage(message)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return commandExit(err)
	}
	info, err := worktreeInfo(dir)
	if err != nil {
		return commandExit(err)
	}
	if len(info) == 0 {
		return usage("shift has no isolated worktree")
	}
	cwd, repo := objectString(info, "worktree_dir"), objectString(info, "repo_root")
	// Reuse the build validator before reading the one landing-specific setting.
	if _, _, err = loadBuildConfig(repo); err != nil {
		return commandExit(err)
	}
	target := args.values["--target-branch"]
	if target == "" {
		target = objectString(info, "target_branch")
	}
	if target == "" {
		data, _ := os.ReadFile(filepath.Join(repo, ".nightshift", "config.yaml"))
		var config struct {
			Target string `yaml:"target_branch"`
		}
		_ = yaml.Unmarshal(data, &config)
		target = config.Target
	}
	if target == "" {
		target = "main"
	}
	path, err := repositoryLockPath(repo)
	if err != nil {
		return commandExit(err)
	}
	var result object
	code := 0
	err = withMailboxMutexTimeout(path, 5000, func() error {
		if err := takeLandingLock(path, repo, dir); err != nil {
			return err
		}
		defer releaseFileLock(path)
		fail := func(s string) error { code = usage(s); return commandError{code, ""} }
		headValue, dirty, _ := gitBuildState(cwd)
		head, _ := headValue.(string)
		if head == "" || dirty != false {
			return fail("landing requires a clean worktree commit")
		}
		branch, err := lifecycleGitText(cwd, "symbolic-ref", "--short", "HEAD")
		if err != nil {
			return err
		}
		if branch != objectString(info, "branch") {
			return fail("worktree is not on its recorded shift branch")
		}
		var latest object
		for _, b := range handoverBuilds(dir) {
			if objectString(b, "head") == head && objectString(b, "cwd") != "" && sameLifecyclePath(objectString(b, "cwd"), cwd) {
				latest = b
			}
		}
		exit, hasExit := objectNumber(latest, "exit")
		if len(latest) == 0 || objectString(latest, "status") != "passed" || !hasExit || numberFloat(exit) != 0 || rowValue(latest, "dirty") != false {
			return fail("landing requires a passed nightshift build on this clean commit")
		}
		targetHead, err := lifecycleGitText(repo, "rev-parse", "refs/heads/"+target)
		if err != nil {
			return err
		}
		checkTargetBranch := func() error {
			current, err := lifecycleGitText(repo, "symbolic-ref", "--short", "HEAD")
			if err != nil {
				return err
			}
			if current != target {
				return fail("canonical repository must have the target branch checked out")
			}
			return nil
		}
		checkLocalOverlap := func(changedPaths []string) error {
			dirtyPaths := map[string]bool{}
			for _, diffArgs := range [][]string{{"diff", "--name-only", "-z"}, {"diff", "--cached", "--name-only", "-z"}, {"ls-files", "--others", "-z"}} {
				paths, _, err := lifecycleGit(repo, diffArgs...)
				if err != nil {
					return err
				}
				for _, p := range strings.Split(paths, "\x00") {
					if p != "" {
						dirtyPaths[p] = true
					}
				}
			}
			overlap := []string{}
			for _, p := range changedPaths {
				if p == "" {
					continue
				}
				for d := range dirtyPaths {
					if p == d || strings.HasPrefix(p, d+"/") || strings.HasPrefix(d, p+"/") {
						overlap = append(overlap, p)
						break
					}
				}
			}
			if len(overlap) > 0 {
				return fail("landing overlaps local changes: " + strings.Join(overlap, ", "))
			}
			return nil
		}
		_, ancestry, ancestryErr := lifecycleGit(cwd, "merge-base", "--is-ancestor", targetHead, head)
		if ancestry == 1 {
			mergeBase, err := lifecycleGitText(cwd, "merge-base", targetHead, head)
			if err != nil {
				return err
			}
			shiftDiff, _, err := lifecycleGit(cwd, "diff", "--name-only", "--no-renames", "-z", mergeBase, head)
			if err != nil {
				return err
			}
			mainDiff, _, err := lifecycleGit(cwd, "diff", "--name-only", "--no-renames", "-z", mergeBase, targetHead)
			if err != nil {
				return err
			}
			parseFileList := func(s string) []string {
				list := []string{}
				for _, p := range strings.Split(s, "\x00") {
					if p != "" {
						list = append(list, p)
					}
				}
				sort.Strings(list)
				return list
			}
			shiftFiles := parseFileList(shiftDiff)
			mainFiles := parseFileList(mainDiff)
			shiftSet := make(map[string]bool, len(shiftFiles))
			for _, f := range shiftFiles {
				shiftSet[f] = true
			}
			rebaseOverlap := []string{}
			for _, f := range mainFiles {
				if shiftSet[f] {
					rebaseOverlap = append(rebaseOverlap, f)
				}
			}
			sort.Strings(rebaseOverlap)
			if rebaseOverlap == nil {
				rebaseOverlap = []string{}
			}

			if len(rebaseOverlap) == 0 {
				if err := checkTargetBranch(); err != nil {
					return err
				}
				if err := checkLocalOverlap(shiftFiles); err != nil {
					return err
				}
			}

			_, rebaseCode, rebaseErr := lifecycleGit(cwd, "rebase", targetHead)
			if rebaseCode < 0 {
				return rebaseErr
			}
			if rebaseCode != 0 {
				files, err := lifecycleGitText(cwd, "diff", "--name-only", "--diff-filter=U")
				if err != nil {
					return err
				}
				if !args.present["--keep-conflicts"] {
					if _, err := lifecycleGitText(cwd, "rebase", "--abort"); err != nil {
						return err
					}
				}
				paths := []string{}
				if files != "" {
					paths = strings.Split(files, "\n")
				}
				result = object{{"status", "conflict"}, {"target_head", targetHead}, {"files", stringsToAny(paths)}}
				code = 4
				return nil
			}

			info = setValue(info, "base_sha", targetHead)
			if err := writeJSONFile(filepath.Join(dir, "worktree.json"), info); err != nil {
				return err
			}
			newHead, err := lifecycleGitText(cwd, "rev-parse", "HEAD")
			if err != nil {
				return err
			}
			decision := "build_carried"
			if len(rebaseOverlap) > 0 {
				decision = "build_required"
			}
			busAppend(dir, "land_rebase", object{
				{"decision", decision},
				{"old_head", head},
				{"new_head", newHead},
				{"target_head", targetHead},
				{"merge_base", mergeBase},
				{"shift_files", stringsToAny(shiftFiles)},
				{"main_files", stringsToAny(mainFiles)},
				{"overlap", stringsToAny(rebaseOverlap)},
			})
			if len(rebaseOverlap) > 0 {
				result = object{{"status", "rebased_needs_test"}, {"new_base", targetHead}, {"head", newHead}}
				code = 2
				return nil
			}
			head = newHead
		} else if ancestryErr != nil {
			return errors.New("cannot determine landing ancestry")
		}
		if err := checkTargetBranch(); err != nil {
			return err
		}
		changed, _, err := lifecycleGit(repo, "diff", "--name-only", "--no-renames", "-z", targetHead, head)
		if err != nil {
			return err
		}
		if err := checkLocalOverlap(strings.Split(changed, "\x00")); err != nil {
			return err
		}
		if _, err = lifecycleGitText(repo, "read-tree", "-m", "-u", targetHead, head); err != nil {
			return err
		}
		if _, err = lifecycleGitText(repo, "update-ref", "refs/heads/"+target, head, targetHead); err != nil {
			return err
		}
		landed := object{{"repo_root", repo}, {"target_branch", target}, {"base_sha", rowValue(info, "base_sha")}, {"landed_sha", head}, {"landed_at", time.Now().Format("2006-01-02 15:04:05")}}
		if err := writeJSONFile(filepath.Join(dir, "landed.json"), landed); err != nil {
			return err
		}
		busAppend(dir, "land", landed)
		result = object{{"status", "landed"}, {"sha", head}}
		return nil
	})
	if err != nil {
		if strings.Contains(err.Error(), "is still held after 5s") {
			return 3
		}
		return commandExit(err)
	}
	outLine(renderJSON(result, false, true))
	return code
}

func recordRecovery(dir string, recovery object, floor *object) error {
	if err := writeJSONFile(filepath.Join(dir, "recovery.json"), recovery); err != nil {
		return err
	}
	path := filepath.Join(dir, "handover.md")
	text := "# Handover: " + filepath.Base(dir) + "\n"
	if fileExists(path) {
		text = strings.ReplaceAll(readText(path), "\r\n", "\n")
	}
	line := "\n## Worktree recovery\n\n" + renderJSON(recovery, false, true) + "\n"
	if !strings.Contains(text, line) {
		if err := os.WriteFile(path, []byte(strings.ReplaceAll(text+line, "\n", "\r\n")), 0666); err != nil {
			return err
		}
	}
	update := func(current *object) error {
		worktrees, _ := rowValue(*current, "worktrees").(object)
		worktrees = setValue(worktrees, filepath.Base(dir), append(object{}, recovery...))
		*current = setValue(*current, "worktrees", worktrees)
		return writeJSONFile(floorPath(), *current)
	}
	if floor != nil {
		return update(floor)
	}
	return withFloorLock(false, func() error {
		current, err := readFloor()
		if err != nil {
			return err
		}
		return update(&current)
	})
}

func teardownWorktree(dir string, hold bool, floor *object) (object, error) {
	info, err := worktreeInfo(dir)
	if err != nil || len(info) == 0 {
		return nil, err
	}
	cwd, repo, branch := objectString(info, "worktree_dir"), objectString(info, "repo_root"), objectString(info, "branch")
	previous := object{}
	if fileExists(filepath.Join(dir, "recovery.json")) {
		previous, err = lifecycleReadObject(filepath.Join(dir, "recovery.json"))
		if err != nil {
			return nil, err
		}
	}
	recovery := append(object{}, previous...)
	for _, p := range (object{{"branch", branch}, {"worktree_dir", cwd}, {"cleanup_pending", true}, {"error", nil}}) {
		recovery = setValue(recovery, p.key, p.val)
	}
	landedPath := filepath.Join(dir, "landed.json")
	landed := fileExists(landedPath) && objectString(previous, "preserved_branch") == ""
	disposition := "landed"
	if !landed {
		disposition = "preserved"
	}
	recovery = setValue(recovery, "disposition", disposition)
	if !landed {
		recovery = setValue(recovery, "preserved_branch", branch)
	}
	if err := recordRecovery(dir, recovery, floor); err != nil {
		return nil, err
	}
	err = func() error {
		registrations, _, err := lifecycleGit(repo, "worktree", "list", "--porcelain", "-z")
		if err != nil {
			return err
		}
		registered := false
		for _, field := range strings.Split(registrations, "\x00") {
			if strings.HasPrefix(field, "worktree ") && sameLifecyclePath(strings.TrimPrefix(field, "worktree "), cwd) {
				registered = true
				break
			}
		}
		recovery = setValue(recovery, "registered", registered)
		if fileExists(cwd) && registered && fileExists(filepath.Join(cwd, ".git")) {
			root, err := lifecycleGitText(cwd, "rev-parse", "--show-toplevel")
			if err != nil {
				return err
			}
			if !sameLifecyclePath(root, cwd) {
				return fmt.Errorf("worktree root does not match recovery path: %s", cwd)
			}
			head, dirty, _ := gitBuildState(cwd)
			if head == nil || dirty == nil {
				return fmt.Errorf("cannot inspect recovery worktree: %s; left intact", cwd)
			}
			if landed {
				saved, err := lifecycleReadObject(landedPath)
				if err != nil {
					return err
				}
				landed = dirty == false && head == rowValue(saved, "landed_sha")
			}
			if !landed {
				recovery = setValue(recovery, "preserved_branch", branch)
				disposition := "preserved"
				if hold {
					disposition = "held"
				}
				recovery = setValue(recovery, "disposition", disposition)
				if err := recordRecovery(dir, recovery, floor); err != nil {
					return err
				}
				if hold {
					recovery = setValue(recovery, "cleanup_pending", false)
					return nil
				}
			}
			current, code, _ := lifecycleGit(cwd, "symbolic-ref", "--short", "HEAD")
			if code != 0 || strings.TrimSpace(current) != branch {
				return fmt.Errorf("recovery worktree is detached or on another branch: %s; left intact", cwd)
			}
			if !landed {
				if dirty == true {
					if _, err := lifecycleGitText(cwd, "add", "--update"); err != nil {
						return err
					}
					untracked, _, err := lifecycleGit(cwd, "ls-files", "--others", "--exclude-standard", "-z")
					if err != nil {
						return err
					}
					paths := []string{"add", "--"}
					for _, p := range strings.Split(untracked, "\x00") {
						if p != "" && p != ".nightshift" && !strings.HasPrefix(p, ".nightshift/") {
							paths = append(paths, p)
						}
					}
					if len(paths) > 2 {
						if _, err := lifecycleGitText(cwd, paths...); err != nil {
							return err
						}
					}
					if _, err := lifecycleGitText(cwd, "commit", "-m", "Nightshift recovery: "+filepath.Base(dir)); err != nil {
						return err
					}
				}
				sha, err := lifecycleGitText(cwd, "rev-parse", "HEAD")
				if err != nil {
					return err
				}
				recovery = setValue(recovery, "recovery_sha", sha)
			}
			recovery = setValue(recovery, "removal_started", true)
			if err := recordRecovery(dir, recovery, floor); err != nil {
				return err
			}
			if _, err := lifecycleGitText(repo, "worktree", "remove", "--force", cwd); err != nil {
				return err
			}
		} else if fileExists(cwd) {
			if !objectBool(previous, "removal_started") {
				return fmt.Errorf("unrecognised recovery directory: %s; left intact", cwd)
			}
			// os.Remove, like rmdir, refuses a remnant containing any unexpected files.
			if err := os.Remove(cwd); err != nil {
				return recoveryRemovalError(err, cwd)
			}
		}
		if _, err := lifecycleGitText(repo, "worktree", "prune"); err != nil {
			return err
		}
		if objectString(recovery, "preserved_branch") == "" {
			if _, code, _ := lifecycleGit(repo, "show-ref", "--verify", "--quiet", "refs/heads/"+branch); code == 0 {
				if _, err := lifecycleGitText(repo, "branch", "-d", branch); err != nil {
					return err
				}
			}
		}
		recovery = setValue(recovery, "cleanup_pending", false)
		recovery = setValue(recovery, "registered", false)
		return nil
	}()
	if err != nil {
		recovery = setValue(recovery, "error", err.Error())
	}
	if err := recordRecovery(dir, recovery, floor); err != nil {
		return recovery, err
	}
	return recovery, nil
}

func downCommand(argv []string) int {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--hold", flag: true}, {name: "--close", flag: true}}, nil, []string{"shift"})
	if args.help {
		out(downHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift down", downUsage, message)
	}
	if args.present["--hold"] && args.present["--close"] {
		previous := "--hold"
		second := "--close"
		for _, a := range argv {
			if longOption(strings.SplitN(a, "=", 2)[0], []string{"--hold", "--close"}) == "--close" {
				previous, second = "--close", "--hold"
				break
			}
			if longOption(a, []string{"--hold", "--close"}) == "--hold" {
				break
			}
		}
		return writeUsage("nightshift down", downUsage, "argument "+second+": not allowed with argument "+previous)
	}
	ref := "latest"
	if len(args.pos) > 0 {
		ref = args.pos[0]
	}
	dir, err := resolveShift(ref)
	if err != nil {
		return commandExit(err)
	}
	return commandExit(stopShift(dir, !args.present["--hold"], nil, true))
}

func stopShift(dir string, closeWindow bool, floor *object, printOutput bool) error {
	record, err := statusRecord(dir)
	if err != nil {
		return err
	}
	roles, _ := rowValue(record, "roles").(object)
	tails := object{}
	for _, role := range roles {
		info, _ := role.val.(object)
		if objectBool(info, "alive") {
			tails = append(tails, pair{role.key, stringsToAny(paneTail(processNumber(info, "pid"), 40))})
		}
	}
	if _, err := recordDenials(dir, tails); err != nil {
		return err
	}
	// Claude removes its PID session registry on exit, so take the final
	// transcript/usage sample while every recorded pane is still alive.
	harvestShiftTelemetry(dir, record)
	handoverPath := filepath.Join(dir, "handover.md")
	if objectBool(record, "alive") || !fileExists(handoverPath) {
		report, err := handoverReport(dir, record)
		if err != nil {
			return err
		}
		lines := []string{"# Handover: " + objectString(record, "shift"), "", "Task: " + objectString(record, "task"), "Stopped: " + time.Now().Format("2006-01-02 15:04"), ""}
		if report != "" {
			lines = append(lines, strings.Split(report, "\n")...)
		}
		for _, role := range roles {
			info, _ := role.val.(object)
			pid := processNumber(info, "pid")
			if pid == 0 {
				status := objectString(info, "status")
				if status == "" {
					status = "not_started"
				}
				lines = append(lines, fmt.Sprintf("## %s (%s)", role.key, status), "")
				continue
			}
			state := "gone"
			if objectBool(info, "alive") {
				state = "alive"
			}
			lines = append(lines, fmt.Sprintf("## %s (%s, pid %s, %s)", role.key, pythonValueString(rowValue(info, "name")), valueText(rowValue(info, "pid")), state), "", "```")
			tail := stringArray(rowValue(tails, role.key))
			if len(tail) == 0 {
				tail = []string{"(pane not readable)"}
			}
			lines = append(lines, tail...)
			lines = append(lines, "```", "")
		}
		if err := os.WriteFile(handoverPath, []byte(strings.ReplaceAll(strings.Join(lines, "\n"), "\n", "\r\n")), 0666); err != nil {
			return err
		}
	}
	mine := ownAncestors()
	var kills, last []uint32
	stopped := []any{}
	for _, role := range roles {
		info, _ := role.val.(object)
		if !objectBool(info, "alive") {
			continue
		}
		stopped = append(stopped, role.key)
		pids := []uint32{}
		isMine := false
		for _, key := range []string{"pid", "wrapper_pid"} {
			if pid := processNumber(info, key); pid != 0 {
				pids = append(pids, pid)
				isMine = isMine || mine[pid]
			}
		}
		if isMine {
			last = append(last, pids...)
		} else {
			kills = append(kills, pids...)
		}
	}
	for _, pid := range kills {
		if err := killShiftTree(pid); err != nil {
			return err
		}
	}
	recovery, err := teardownWorktree(dir, !closeWindow, floor)
	if err != nil {
		return err
	}
	released, err := releaseShiftLocks(objectString(record, "shift"))
	if err != nil {
		return err
	}
	resultPath := filepath.Join(dir, "result.json")
	if fileExists(resultPath) {
		data, readErr := os.ReadFile(resultPath)
		var result any
		if readErr == nil {
			result, readErr = parseJSON(data)
			if readErr != nil {
				readErr = resultJSONError(data, readErr)
			}
		}
		fields := object{{"result", result}}
		if readErr != nil {
			fields = object{{"result", nil}, {"error", "result.json unreadable: " + readErr.Error()}}
		}
		busAppend(dir, "result", fields)
	}
	var recoveryValue any
	if recovery != nil {
		recoveryValue = recovery
	}
	busAppend(dir, "down", object{{"stopped", stopped}, {"lock_released", released}, {"worktree", recoveryValue}})
	var hwnd uintptr
	if closeWindow {
		hwnd = closeTarget(dir)
	}
	result := object{{"shift", rowValue(record, "shift")}, {"handover", handoverPath}, {"stopped", stopped}, {"window_closed", hwnd != 0}, {"held", !closeWindow}, {"lock_released", released}}
	if recovery != nil {
		result = append(result, pair{"worktree", recovery})
	}
	if printOutput {
		outLine(renderJSON(result, false, true))
		_ = os.Stdout.Sync()
	}
	if hwnd != 0 {
		postWindowClose(hwnd)
	}
	for _, pid := range last {
		if err := killShiftTree(pid); err != nil {
			return err
		}
	}
	return nil
}
