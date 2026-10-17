package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

func floorPath() string { return filepath.Join(stateRoot(), "floor.json") }

func readFloor() (object, error) {
	if !fileExists(floorPath()) {
		return object{}, nil
	}
	return lifecycleReadObject(floorPath())
}

func processRunningSince(record object, key string) bool {
	_, created, alive := processInfo(processNumber(record, "pid"))
	started, ok := objectNumber(record, key)
	return alive && (!ok || created == 0 || math.Abs(created-numberFloat(started)) <= 2)
}

func withFloorLock(dryRun bool, fn func() error) error {
	if dryRun {
		return fn()
	}
	if err := os.MkdirAll(filepath.Dir(floorPath()), 0777); err != nil {
		return err
	}
	path := filepath.Join(stateRoot(), "floor.lock")
	deadline := time.Now().Add(5 * time.Second)
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0666)
		if err == nil {
			_, writeErr := f.WriteString(renderJSON(object{{"pid", os.Getpid()}}, false, true))
			closeErr := f.Close()
			if writeErr != nil {
				return writeErr
			}
			if closeErr != nil {
				return closeErr
			}
			break
		}
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		stale := false
		if held, ok := readObject(path); ok && objectNumberExists(held, "pid") {
			stale = !processRunningSince(held, "started")
		} else if stat, err := os.Stat(path); err == nil {
			stale = time.Since(stat.ModTime()) > 30*time.Second
		}
		if stale {
			_ = os.Remove(path)
			continue
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("%s is held by another floor command", path)
		}
		time.Sleep(100 * time.Millisecond)
	}
	defer releaseFileLock(path)
	return fn()
}

func repositoryLockPath(repo string) (string, error) {
	path, err := canonicalPath(repo)
	if err != nil {
		return "", err
	}
	return filepath.Join(stateRoot(), "locks", strings.NewReplacer(":", "_", "\\", "_", "/", "_").Replace(path)+".json"), nil
}

func lockShiftDir(lock object) string {
	if dir := objectString(lock, "state_dir"); dir != "" {
		return dir
	}
	for _, dir := range shiftDirs() {
		if filepath.Base(dir) == objectString(lock, "shift") {
			return dir
		}
	}
	return ""
}

func lockHolderAlive(held object) (bool, error) {
	if dir := lockShiftDir(held); dir != "" && fileExists(dir) {
		record, err := statusRecord(dir)
		return objectBool(record, "alive"), err
	}
	return false, nil
}

func takeLandingLock(path, repo, dir string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0777); err != nil {
		return err
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0666)
		if err == nil {
			_, created, _ := processInfo(uint32(os.Getpid()))
			var started any
			if created != 0 {
				started = created
			}
			record := object{{"shift", filepath.Base(dir)}, {"cwd", repo}, {"state_dir", dir}, {"kind", "landing"}, {"pid", os.Getpid()}, {"started_epoch", started}}
			_, writeErr := f.WriteString(renderJSON(record, false, true))
			closeErr := f.Close()
			if writeErr != nil {
				return writeErr
			}
			return closeErr
		}
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		held, ok := readObject(path)
		stale := false
		if ok && len(held) > 0 {
			if objectString(held, "kind") == "landing" {
				stale = !processRunningSince(held, "started_epoch")
			} else {
				alive, err := lockHolderAlive(held)
				if err != nil {
					return err
				}
				stale = !alive
			}
		}
		if stale {
			_ = os.Remove(path)
			continue
		}
		if time.Now().After(deadline) {
			return commandError{3, "repository landing lock is held: " + path}
		}
		time.Sleep(100 * time.Millisecond)
	}
}

type lifecycleLock struct {
	path   string
	record object
}

func lifecycleLocks() []lifecycleLock {
	roots := []string{filepath.Join(stateRoot(), "locks"), filepath.Join(legacyRoot(), "locks")}
	var paths []string
	for _, root := range roots {
		entries, _ := os.ReadDir(root)
		for _, entry := range entries {
			if !entry.IsDir() && filepath.Ext(entry.Name()) == ".json" {
				paths = append(paths, filepath.Join(root, entry.Name()))
			}
		}
	}
	sort.Strings(paths)
	locks := []lifecycleLock{}
	for _, path := range paths {
		if record, ok := readObject(path); ok {
			locks = append(locks, lifecycleLock{path, record})
		}
	}
	return locks
}

func releaseShiftLocks(shift string) ([]any, error) {
	released := []any{}
	for _, lock := range lifecycleLocks() {
		if objectString(lock.record, "shift") != shift {
			continue
		}
		err := withMailboxMutex(lock.path, func() error {
			held, ok := readObject(lock.path)
			if !ok || objectString(held, "shift") != shift {
				return nil
			}
			if objectString(held, "kind") == "landing" && processRunningSince(held, "started_epoch") {
				return nil
			}
			if err := os.Remove(lock.path); err == nil {
				released = append(released, rowValue(held, "cwd"))
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return released, nil
}

func staleLifecycleLock(path string) bool {
	stat, err := os.Stat(path)
	if err != nil {
		return false
	}
	if time.Since(stat.ModTime()) > 30*time.Second {
		return true
	}
	held, ok := readObject(path)
	return ok && processNumber(held, "pid") != 0 && !processRunning(processNumber(held, "pid"))
}

// Toolhelp snapshots include shells and wrappers, not just recognized crew images.
func ownAncestors() map[uint32]bool {
	parents := map[uint32]uint32{}
	h, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err == nil {
		defer windows.CloseHandle(h)
		entry := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
		for err = windows.Process32First(h, &entry); err == nil; err = windows.Process32Next(h, &entry) {
			parents[entry.ProcessID] = entry.ParentProcessID
		}
	}
	seen := map[uint32]bool{}
	for pid := uint32(os.Getpid()); pid != 0 && !seen[pid]; pid = parents[pid] {
		seen[pid] = true
	}
	return seen
}

func killShiftTree(pid uint32) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, err := exec.CommandContext(ctx, "taskkill", "/F", "/T", "/PID", strconv.FormatUint(uint64(pid), 10)).Output()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return nil
	}
	return err
}

var lifecycleUser32 = windows.NewLazySystemDLL("user32.dll")

type terminalWindow struct {
	hwnd  uintptr
	title string
}

var lifecycleEnumWindowsCallback = syscall.NewCallback(func(hwnd, param uintptr) uintptr {
	visible, _, _ := lifecycleUser32.NewProc("IsWindowVisible").Call(hwnd)
	if visible == 0 {
		return 1
	}
	class := make([]uint16, 256)
	lifecycleUser32.NewProc("GetClassNameW").Call(hwnd, uintptr(unsafe.Pointer(&class[0])), uintptr(len(class)))
	if windows.UTF16ToString(class) != "CASCADIA_HOSTING_WINDOW_CLASS" {
		return 1
	}
	title := make([]uint16, 512)
	lifecycleUser32.NewProc("GetWindowTextW").Call(hwnd, uintptr(unsafe.Pointer(&title[0])), uintptr(len(title)))
	found := (*[]terminalWindow)(unsafe.Pointer(param))
	*found = append(*found, terminalWindow{hwnd, windows.UTF16ToString(title)})
	return 1
})

func terminalWindows() []terminalWindow {
	found := []terminalWindow{}
	lifecycleUser32.NewProc("EnumWindows").Call(lifecycleEnumWindowsCallback, uintptr(unsafe.Pointer(&found)))
	runtime.KeepAlive(&found)
	return found
}

func closeTarget(dir string) uintptr {
	launch, _ := readObject(filepath.Join(dir, "launch.json"))
	hwndNumber, _ := objectNumber(launch, "hwnd")
	hwndInt, _ := strconv.ParseInt(hwndNumber.String(), 10, 64)
	hwnd := uintptr(hwndInt)
	windows := terminalWindows()
	if hwnd == 0 {
		label := strings.TrimSpace(readText(filepath.Join(dir, "ticket.txt")))
		if label == "" {
			label = filepath.Base(dir)
		}
		titles := map[string]bool{label + " shift": true, "Foreman": true, "Coder": true}
		_, members := crewMembers(dir)
		for _, m := range members {
			titles[label+" "+objectString(m, "label")] = true
		}
		var candidates []uintptr
		for _, w := range windows {
			if titles[w.title] {
				candidates = append(candidates, w.hwnd)
			}
		}
		if len(candidates) == 1 {
			hwnd = candidates[0]
		}
	}
	for _, w := range windows {
		if hwnd != 0 && hwnd == w.hwnd {
			return hwnd
		}
	}
	return 0
}

func postWindowClose(hwnd uintptr) { lifecycleUser32.NewProc("PostMessageW").Call(hwnd, 0x0010, 0, 0) }

func recoveryRemovalError(err error, path string) error {
	var code syscall.Errno
	if errors.As(err, &code) {
		message := strings.TrimRight(code.Error(), ".\r\n ")
		return fmt.Errorf("[WinError %d] %s: %s", uint32(code), message, pyRepr(path))
	}
	return err
}

const cleanerUsage = "usage: nightshift cleaner [-h] [--dry-run]"
const cleanerHelp = cleanerUsage + "\n\nReap dead or finished shifts only.\n\noptions:\n  -h, --help  show this help message and exit\n  --dry-run\n"

func cleanerCommand(argv []string) int {
	args, message := parseWriteArgs(argv, []writeOption{{name: "--dry-run", flag: true}}, nil, nil)
	if args.help {
		out(cleanerHelp)
		return 0
	}
	if message != "" {
		return writeUsage("nightshift cleaner", cleanerUsage, message)
	}
	dryRun := args.present["--dry-run"]
	reaped, locksReleased, windowsClosed := []any{}, []any{}, []any{}
	err := withFloorLock(dryRun, func() error {
		floor, err := readFloor()
		if err != nil {
			return err
		}
		old := map[string]bool{}
		if rows, ok := rowValue(floor, "reaped").([]any); ok {
			for _, row := range rows {
				if r, ok := row.(object); ok {
					old[objectString(r, "shift")] = true
				}
			}
		}
		for _, dir := range shiftDirs() {
			record, err := statusRecord(dir)
			if err != nil {
				return err
			}
			if objectBool(record, "alive") && !contains([]string{"done", "halted"}, objectString(record, "result")) {
				continue
			}
			locks := []string{}
			for _, name := range []string{"claims.lock", "bus.lock"} {
				path := filepath.Join(dir, name)
				if staleLifecycleLock(path) {
					locks = append(locks, path)
				}
			}
			repoLocks := []string{}
			for _, lock := range lifecycleLocks() {
				if objectString(lock.record, "shift") == filepath.Base(dir) {
					repoLocks = append(repoLocks, lock.path)
				}
			}
			claims := readClaims(dir)
			hwnd := closeTarget(dir)
			worktree, err := worktreeInfo(dir)
			if err != nil {
				return err
			}
			recovery := object{}
			if fileExists(filepath.Join(dir, "recovery.json")) {
				recovery, err = lifecycleReadObject(filepath.Join(dir, "recovery.json"))
				if err != nil {
					return err
				}
			}
			pending := len(worktree) > 0 && (fileExists(objectString(worktree, "worktree_dir")) || objectBool(recovery, "cleanup_pending"))
			if old[filepath.Base(dir)] && len(locks) == 0 && len(repoLocks) == 0 && len(claims) == 0 && hwnd == 0 && !pending {
				continue
			}
			summary := object{{"shift", filepath.Base(dir)}, {"task", rowValue(record, "task")}, {"result", rowValue(record, "result")}, {"reaped_at", time.Now().Format("2006-01-02 15:04:05")}}
			for _, path := range append(locks, repoLocks...) {
				locksReleased = append(locksReleased, path)
			}
			for _, claim := range claims {
				locksReleased = append(locksReleased, claim.key)
			}
			if hwnd != 0 {
				windowsClosed = append(windowsClosed, filepath.Base(dir))
			}
			if !dryRun {
				for _, path := range locks {
					if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
						return err
					}
				}
				if err := stopShift(dir, true, &floor, false); err != nil {
					return err
				}
				worktrees, _ := rowValue(floor, "worktrees").(object)
				if recovery, ok := rowValue(worktrees, filepath.Base(dir)).(object); ok {
					for _, p := range recovery {
						summary = setValue(summary, p.key, p.val)
					}
				}
				if len(claims) > 0 {
					if err := withClaimsLock(dir, func() error { return writeClaims(dir, object{}) }); err != nil {
						return err
					}
				}
				rows, _ := rowValue(floor, "reaped").([]any)
				updated := []any{}
				for _, row := range rows {
					r, _ := row.(object)
					if objectString(r, "shift") != filepath.Base(dir) {
						updated = append(updated, row)
					}
				}
				floor = setValue(floor, "reaped", append(updated, summary))
			}
			reaped = append(reaped, summary)
		}
		if !dryRun && len(reaped) > 0 {
			return writeJSONFile(floorPath(), floor)
		}
		return nil
	})
	if err != nil {
		return commandExit(err)
	}
	printResult(object{{"reaped", reaped}, {"locks_released", locksReleased}, {"windows_closed", windowsClosed}, {"dry_run", dryRun}}, true, true)
	return 0
}
