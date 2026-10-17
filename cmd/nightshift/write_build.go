package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"gopkg.in/yaml.v2"
)

func resolvedBuildPath(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	// Path.resolve preserves case for ordinary paths, and resolves junctions/symlinks.
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved, nil
	}
	return abs, nil
}

func buildLockPath(cwd string) (string, error) {
	path, err := normalizedClaimPath(cwd)
	if err != nil {
		return "", err
	}
	key := strings.NewReplacer(":", "_", "\\", "_", "/", "_").Replace(path)
	return filepath.Join(stateRoot(), "locks", "builds", key+".json"), nil
}
func buildProcessRunning(record object) bool {
	pid := processNumber(record, "pid")
	_, created, alive := processInfo(pid)
	if !alive {
		return false
	}
	started, ok := objectNumber(record, "started_epoch")
	return !ok || created == 0 || math.Abs(created-numberFloat(started)) <= 2
}

func takeBuildLock(cwd, shift string, n int, timeout float64) (string, error) {
	path, err := buildLockPath(cwd)
	if err != nil {
		return "", err
	}
	if err = os.MkdirAll(filepath.Dir(path), 0777); err != nil {
		return "", err
	}
	_, created, alive := processInfo(uint32(os.Getpid()))
	var started any
	if alive && created != 0 {
		started = created
	}
	mine := object{{"shift", shift}, {"n", n}, {"cwd", cwd}, {"pid", os.Getpid()}, {"started_epoch", started}, {"started", time.Now().Format("2006-01-02 15:04:05")}}
	deadline := float64(time.Now().UnixNano())/1e9 + timeout*60
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0666)
		if err == nil {
			_, writeErr := f.WriteString(renderJSON(mine, false, true))
			closeErr := f.Close()
			if writeErr != nil {
				return "", writeErr
			}
			if closeErr != nil {
				return "", closeErr
			}
			return path, nil
		}
		if !errors.Is(err, os.ErrExist) {
			return "", err
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		stat, err := os.Stat(path)
		if err != nil {
			continue
		}
		v, _ := parseJSON(raw)
		held, ok := v.(object)
		stale := time.Since(stat.ModTime()) > 5*time.Second
		if ok {
			stale = !buildProcessRunning(held)
		}
		if stale {
			if current, e := os.ReadFile(path); e == nil && string(current) == string(raw) {
				_ = os.Remove(path)
			}
			continue
		}
		if float64(time.Now().UnixNano())/1e9 >= deadline {
			holder := "a build still writing its lock"
			if ok {
				holder = fmt.Sprintf("build #%s of shift %s (pid %s, since %s)", valueText(rowValue(held, "n")), valueText(rowValue(held, "shift")), valueText(rowValue(held, "pid")), valueText(rowValue(held, "started")))
			}
			message := fmt.Sprintf("folder %s is still held by %s after %g minutes; lock %s", cwd, holder, timeout, path)
			errLine(message)
			return "", commandError{6, message}
		}
		time.Sleep(5 * time.Second)
	}
}
func releaseBuildLock(path string) {
	if held, ok := readObject(path); ok && processNumber(held, "pid") == uint32(os.Getpid()) {
		_ = os.Remove(path)
	}
}

func reserveBuild(dir string) (int, error) {
	if err := os.MkdirAll(dir, 0777); err != nil {
		return 0, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	n := 1
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") {
			stem := strings.TrimSuffix(e.Name(), ".json")
			if i, err := strconv.Atoi(stem); err == nil && i >= 0 && i >= n {
				n = i + 1
			}
		}
	}
	for {
		f, err := os.OpenFile(filepath.Join(dir, strconv.Itoa(n)+".json"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0666)
		if err == nil {
			return n, f.Close()
		}
		if !errors.Is(err, os.ErrExist) {
			return 0, err
		}
		n++
	}
}

func gitBuildOutput(cwd string, timeout time.Duration, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, args...)...)
	return cmd.Output()
}
func gitBuildState(cwd string) (any, any, []any) {
	head, err := gitBuildOutput(cwd, 30*time.Second, "rev-parse", "HEAD")
	if err != nil {
		return nil, nil, []any{}
	}
	var sha any
	if s := strings.TrimSpace(strings.ToValidUTF8(string(head), "\ufffd")); s != "" {
		sha = s
	}
	status, err := gitBuildOutput(cwd, 60*time.Second, "status", "--porcelain", "--untracked-files=all")
	if err != nil {
		return sha, nil, []any{}
	}
	lines := []any{}
	for _, line := range strings.Split(strings.ReplaceAll(strings.ToValidUTF8(string(status), "\ufffd"), "\r\n", "\n"), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if strings.HasPrefix(line, "?? ") {
			file := strings.TrimLeft(line[3:], "\"")
			if strings.HasPrefix(file, ".nightshift/") || line[3:] == ".nightshift" {
				continue
			}
		}
		lines = append(lines, line)
	}
	return sha, len(lines) > 0, lines
}

func cancelBuild(dir string, n int) error {
	path := filepath.Join(dir, "builds", strconv.Itoa(n)+".json")
	record, ok := readObject(path)
	if !ok {
		return fmt.Errorf("no build #%d in shift %s", n, filepath.Base(dir))
	}
	status := objectString(record, "status")
	if status != "waiting" && status != "running" {
		printResult(object{{"build", n}, {"cancelled", false}, {"message", fmt.Sprintf("build #%d already finished with status %s", n, valueText(rowValue(record, "status")))}}, false, true)
		return nil
	}
	if err := os.WriteFile(filepath.Join(dir, "builds", strconv.Itoa(n)+".cancel"), []byte(time.Now().Format("2006-01-02 15:04:05")), 0666); err != nil {
		return err
	}
	var killed any
	if status == "running" && processNumber(record, "pid") != 0 {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_ = exec.CommandContext(ctx, "taskkill", "/F", "/T", "/PID", valueText(rowValue(record, "pid"))).Run()
		killed = rowValue(record, "pid")
	}
	printResult(object{{"build", n}, {"cancelled", true}, {"killed_pid", killed}}, false, true)
	return nil
}

func buildRepoRoot(cwd, dir string) string {
	if info, ok := readObject(filepath.Join(dir, "worktree.json")); ok {
		if repo := objectString(info, "repo_root"); repo != "" {
			p, _ := resolvedBuildPath(repo)
			return p
		}
	}
	for _, d := range shiftDirs() {
		if info, ok := readObject(filepath.Join(d, "worktree.json")); ok {
			work := objectString(info, "worktree_dir")
			if work == "" {
				continue
			}
			p, _ := resolvedBuildPath(work)
			rel, err := filepath.Rel(p, cwd)
			if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
				p, _ = resolvedBuildPath(objectString(info, "repo_root"))
				return p
			}
		}
	}
	return cwd
}
func yamlMapping(v any) (yaml.MapSlice, bool) { m, ok := v.(yaml.MapSlice); return m, ok }
func yamlValue(m yaml.MapSlice, key string) (any, bool) {
	for i := len(m) - 1; i >= 0; i-- {
		p := m[i]
		if p.Key == key {
			return p.Value, true
		}
	}
	return nil, false
}
func unknownYAMLKeys(m yaml.MapSlice, allowed []string) []string {
	out := []string{}
	for _, p := range m {
		s, ok := p.Key.(string)
		if !ok || !contains(allowed, s) {
			out = append(out, fmt.Sprint(p.Key))
		}
	}
	return out
}

// yaml.v2 flattens parser marks. Restore the oracle's diagnostic for a flow
// sequence ending immediately after its opening bracket; other errors stay as-is.
func buildConfigYAMLError(path string, data []byte, err error) error {
	text := strings.ReplaceAll(strings.ReplaceAll(string(data), "\r\n", "\n"), "\r", "\n")
	if !strings.HasSuffix(err.Error(), "did not find expected node content") || !strings.HasSuffix(strings.TrimSpace(text), "[") {
		return fmt.Errorf("%s: %s", path, err)
	}
	lines := strings.Split(text, "\n")
	last := []rune(lines[len(lines)-1])
	column := len(last)
	head := ""
	start := 0
	if column > 36 {
		start = column - 32
		head = " ... "
	}
	return fmt.Errorf("%s: while parsing a flow node\nexpected the node content, but found '<stream end>'\n  in \"<unicode string>\", line %d, column %d:\n    %s%s\n%s^", path, len(lines), column+1, head, string(last[start:]), strings.Repeat(" ", 4+column-start+len(head)))
}

func loadBuildConfig(repo string) (string, float64, error) {
	path := filepath.Join(repo, ".nightshift", "config.yaml")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", 60, nil
	}
	if err != nil {
		return "", 0, err
	}
	var decoded any
	if err = yaml.Unmarshal(data, &decoded); err != nil {
		return "", 0, buildConfigYAMLError(path, data, err)
	}
	// Decode as MapSlice to preserve the Python mapping order in validation errors.
	if _, ok := decoded.(map[interface{}]interface{}); !ok {
		return "", 0, fmt.Errorf("%s: config must be a mapping", path)
	}
	var m yaml.MapSlice
	if err = yaml.Unmarshal(data, &m); err != nil {
		return "", 0, fmt.Errorf("%s: %s", path, err)
	}
	if unknown := unknownYAMLKeys(m, []string{"target_branch", "default_crew", "build"}); len(unknown) > 0 {
		return "", 0, fmt.Errorf("%s: unknown config keys: %s", path, strings.Join(unknown, ", "))
	}
	for _, key := range []string{"target_branch", "default_crew"} {
		if v, ok := yamlValue(m, key); ok {
			s, ok := v.(string)
			if !ok || strings.TrimSpace(s) == "" {
				return "", 0, fmt.Errorf("%s: %s must be a non-empty string", path, key)
			}
		}
	}
	command := ""
	timeout := 60.0
	if value, ok := yamlValue(m, "build"); ok {
		build, ok := yamlMapping(value)
		if !ok {
			return "", 0, fmt.Errorf("%s: build must be a mapping", path)
		}
		if unknown := unknownYAMLKeys(build, []string{"cmd", "timeout"}); len(unknown) > 0 {
			return "", 0, fmt.Errorf("%s: unknown build keys: %s", path, strings.Join(unknown, ", "))
		}
		if value, ok := yamlValue(build, "cmd"); ok {
			s, ok := value.(string)
			if !ok || strings.TrimSpace(s) == "" {
				return "", 0, fmt.Errorf("%s: build.cmd must be a non-empty string", path)
			}
			command = s
		}
		if value, ok := yamlValue(build, "timeout"); ok {
			seconds := math.NaN()
			switch n := value.(type) {
			case int:
				seconds = float64(n)
			case int64:
				seconds = float64(n)
			case uint64:
				seconds = float64(n)
			case float64:
				seconds = n
			}
			if math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds < 0 {
				return "", 0, fmt.Errorf("%s: build.timeout must be a finite non-negative number of seconds", path)
			}
			timeout = seconds / 60
		}
	}
	return command, timeout, nil
}

const buildUsage = "usage: nightshift build [-h] [--cwd CWD] [--cmd CMD] [--timeout TIMEOUT]\n                        [--cancel]\n                        shift [n]"
const buildHelp = buildUsage + "\n\nRun a heavy build or test suite in a folder, one at a time across shifts:\nwaits while another live build holds the folder (exit 6 after --timeout), then\nruns COMMAND and records <state_dir>/builds/<n>.json (head, dirty tree, exit,\nlog) as evidence; exit code = COMMAND's. --cancel SHIFT N kills build N's\nprocess tree and marks it cancelled.\n\npositional arguments:\n  shift              shift name, unique suffix, or \"latest\"\n  n                  with --cancel: the build number\n\noptions:\n  -h, --help         show this help message and exit\n  --cwd CWD          folder to build in (its build lock serialises builds\n                     there)\n  --cmd CMD          the command line to run (through the shell)\n  --timeout TIMEOUT  minutes to wait for the folder before giving up with exit\n                     6 (default 60)\n  --cancel           cancel build N of SHIFT\n  --asg ASG          the assignment this build verifies; a fourth build after\n                     three failures with the same error is refused (exit 4)\n"

func buildCommand(args []string) int {
	a, pe := parseWriteArgs(args, []writeOption{{name: "--cwd"}, {name: "--cmd"}, {name: "--timeout", number: "float"}, {name: "--cancel", flag: true}, {name: "--asg"}}, []string{"shift"}, []string{"n"})
	if a.help {
		out(buildHelp)
		return 0
	}
	if pe != "" {
		return writeUsage("nightshift build", buildUsage, pe)
	}
	n := 0
	if len(a.pos) > 1 {
		value, err := parseInteger(a.pos[1])
		if err != nil {
			return writeUsage("nightshift build", buildUsage, "argument n: invalid int value: "+pyRepr(a.pos[1]))
		}
		n = int(value)
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return commandExit(err)
	}
	if a.present["--cancel"] {
		if len(a.pos) < 2 {
			return writeUsage("nightshift build", buildUsage, "--cancel needs the build number: nightshift build --cancel SHIFT N")
		}
		return commandExit(cancelBuild(dir, n))
	}
	cwd := a.values["--cwd"]
	if cwd == "" {
		if info, ok := readObject(filepath.Join(dir, "worktree.json")); ok {
			cwd = objectString(info, "worktree_dir")
		}
		if cwd == "" {
			if crew, ok := readObject(filepath.Join(dir, "crew.json")); ok {
				if _, ok := rowValue(crew, "members").([]any); ok {
					cwd = objectString(crew, "cwd")
				}
			}
		}
	}
	if cwd == "" {
		return writeUsage("nightshift build", buildUsage, "give --cwd DIR; the shift has no recorded working folder")
	}
	if stat, e := os.Stat(cwd); e != nil || !stat.IsDir() {
		return writeUsage("nightshift build", buildUsage, "build folder does not exist: "+cwd)
	}
	cwd, err = resolvedBuildPath(cwd)
	if err != nil {
		return commandExit(err)
	}
	command, timeout, err := loadBuildConfig(buildRepoRoot(cwd, dir))
	if err != nil {
		return commandExit(err)
	}
	if a.present["--cmd"] {
		command = a.values["--cmd"]
	}
	if command == "" {
		return writeUsage("nightshift build", buildUsage, "give --cmd COMMAND or set build.cmd in .nightshift/config.yaml")
	}
	if a.present["--timeout"] {
		timeout, _ = parseNumber(a.values["--timeout"], "float")
	}
	var asg any
	if a.present["--asg"] {
		asg = a.values["--asg"]
		if loop := loopBuilds(dir, a.values["--asg"]); loop != nil {
			ns := []any{}
			for _, b := range loop {
				ns = append(ns, rowValue(b, "n"))
			}
			fields := object{{"asg", asg}, {"builds", ns}, {"signature", rowValue(loop[0], "signature")}}
			if _, err := raiseEvent(dir, "loop-detected", a.values["--asg"]+"@"+renderJSON(ns, false, true), "loop-detected "+a.values["--asg"], asg, fields); err != nil {
				return commandExit(err)
			}
			errLine("loop detected: escalate or report BLOCKED")
			return 4
		}
	}
	return runBuild(dir, cwd, command, timeout, asg)
}

// loopBuilds returns the last three finished builds for asg when all three failed with one error signature.
func loopBuilds(dir, asg string) []object {
	entries, _ := os.ReadDir(filepath.Join(dir, "builds"))
	finished := []object{}
	for _, e := range entries {
		if _, err := strconv.Atoi(strings.TrimSuffix(e.Name(), ".json")); err != nil || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		record, ok := readObject(filepath.Join(dir, "builds", e.Name()))
		if ok && rowValue(record, "asg") == asg && !contains([]string{"waiting", "running"}, objectString(record, "status")) {
			finished = append(finished, record)
		}
	}
	if len(finished) < 3 {
		return nil
	}
	sort.Slice(finished, func(i, j int) bool {
		return intValue(rowValue(finished[i], "n")) < intValue(rowValue(finished[j], "n"))
	})
	last := finished[len(finished)-3:]
	for _, b := range last {
		if objectString(b, "status") != "failed" || objectString(b, "signature") == "" || objectString(b, "signature") != objectString(last[0], "signature") {
			return nil
		}
	}
	return last
}

var (
	failureLine = regexp.MustCompile(`^(FAILED |ERROR )|--- FAIL:`)
	digitRun    = regexp.MustCompile(`[0-9]+`)
)

// buildSignature hashes the exit code and the log lines naming failures (pytest FAILED/ERROR, go --- FAIL),
// else the last five non-empty lines, with digits (and so timestamps and durations) normalised.
func buildSignature(exit int, logText string) string {
	lines := []string{}
	for _, line := range strings.Split(strings.ReplaceAll(strings.ToValidUTF8(logText, "\ufffd"), "\r\n", "\n"), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			lines = append(lines, line)
		}
	}
	picked := []string{}
	for _, line := range lines {
		if failureLine.MatchString(line) {
			picked = append(picked, line)
		}
	}
	if len(picked) == 0 {
		picked = lines[max(0, len(lines)-5):]
	}
	for i := range picked {
		picked[i] = digitRun.ReplaceAllString(picked[i], "N")
	}
	sum := sha256.Sum256([]byte(strconv.Itoa(exit) + "\n" + strings.Join(picked, "\n")))
	return hex.EncodeToString(sum[:])
}

// gitTree is the tree id of cwd's working tree including untracked, non-ignored files, built in a temporary
// index so the real one is untouched; nil outside git.
func gitTree(cwd string) any {
	f, err := os.CreateTemp("", "nightshift-index-*")
	if err != nil {
		return nil
	}
	index := f.Name()
	f.Close()
	os.Remove(index)
	defer os.Remove(index)
	git := func(args ...string) (string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, args...)...)
		cmd.Env = append(os.Environ(), "GIT_INDEX_FILE="+index)
		out, err := cmd.Output()
		return strings.TrimSpace(string(out)), err
	}
	if _, err := git("read-tree", "HEAD"); err != nil {
		if _, err := git("read-tree", "--empty"); err != nil {
			return nil
		}
	}
	if _, err := git("add", "-A"); err != nil {
		return nil
	}
	tree, err := git("write-tree")
	if err != nil || tree == "" {
		return nil
	}
	return tree
}

// buildOutput is the record as build has always printed it, without the NGH-061 fields.
func buildOutput(record object) object {
	shown := object{}
	for _, p := range record {
		if !contains([]string{"asg", "tree", "signature"}, p.key) {
			shown = append(shown, p)
		}
	}
	return shown
}

func runBuild(dir, cwd, command string, timeout float64, asg any) int {
	builds := filepath.Join(dir, "builds")
	n, err := reserveBuild(builds)
	if err != nil {
		return commandExit(err)
	}
	path := filepath.Join(builds, strconv.Itoa(n)+".json")
	marker := filepath.Join(builds, strconv.Itoa(n)+".cancel")
	log := filepath.Join(builds, strconv.Itoa(n)+".log")
	record := object{{"n", n}, {"cmd", command}, {"cwd", cwd}, {"head", nil}, {"dirty", nil}, {"porcelain", []any{}}, {"started", nil}, {"ended", nil}, {"exit", nil}, {"log", log}, {"pid", nil}, {"status", "waiting"}, {"asg", asg}, {"tree", nil}, {"signature", nil}}
	if err = writeJSONFile(path, record); err != nil {
		return commandExit(err)
	}
	err = func() (runErr error) {
		lock := ""
		defer func() {
			if status := rowString(record, "status"); status == "waiting" || status == "running" {
				record = setValue(record, "status", "failed")
				record = setValue(record, "ended", time.Now().Format("2006-01-02 15:04:05"))
			}
			if e := writeJSONFile(path, record); e != nil {
				runErr = e
			}
			if lock != "" {
				releaseBuildLock(lock)
			}
		}()
		var err error
		lock, err = takeBuildLock(cwd, filepath.Base(dir), n, timeout)
		if err != nil {
			var exit interface{ ExitCode() int }
			if errors.As(err, &exit) && exit.ExitCode() == 6 {
				record = setValue(record, "status", "timeout")
				record = setValue(record, "ended", time.Now().Format("2006-01-02 15:04:05"))
			}
			return err
		}
		head, dirty, porcelain := gitBuildState(cwd)
		record = setValue(record, "head", head)
		record = setValue(record, "dirty", dirty)
		record = setValue(record, "porcelain", porcelain)
		record = setValue(record, "tree", gitTree(cwd))
		started := time.Now().Format("2006-01-02 15:04:05")
		record = setValue(record, "started", started)
		if fileExists(marker) {
			record = setValue(record, "status", "cancelled")
			record = setValue(record, "ended", started)
			return nil
		}
		f, err := os.Create(log)
		if err != nil {
			return err
		}
		// Pass the command verbatim to cmd.exe, as subprocess.Popen(shell=True) does.
		shell := envOr("COMSPEC", `C:\Windows\System32\cmd.exe`)
		cmd := exec.Command(shell)
		cmd.SysProcAttr = &syscall.SysProcAttr{CmdLine: `"` + shell + `" /c "` + command + `"`}
		cmd.Dir = cwd
		cmd.Stdout = f
		cmd.Stderr = f
		if err = cmd.Start(); err != nil {
			f.Close()
			return err
		}
		record = setValue(record, "status", "running")
		record = setValue(record, "pid", cmd.Process.Pid)
		if err = writeJSONFile(path, record); err != nil {
			f.Close()
			return err
		}
		waitErr := cmd.Wait()
		_ = f.Close()
		code := 0
		if waitErr != nil {
			var exit *exec.ExitError
			if !errors.As(waitErr, &exit) {
				return waitErr
			}
			code = exit.ExitCode()
		}
		record = setValue(record, "exit", code)
		record = setValue(record, "ended", time.Now().Format("2006-01-02 15:04:05"))
		status := "failed"
		if fileExists(marker) {
			status = "cancelled"
		} else if code == 0 {
			status = "passed"
		}
		record = setValue(record, "status", status)
		if status == "failed" {
			logText, _ := os.ReadFile(log)
			record = setValue(record, "signature", buildSignature(code, string(logText)))
		}
		return nil
	}()
	if err != nil {
		return commandExit(err)
	}
	printResult(buildOutput(record), true, true)
	if code, ok := rowValue(record, "exit").(int); ok {
		return code
	}
	return 0
}
