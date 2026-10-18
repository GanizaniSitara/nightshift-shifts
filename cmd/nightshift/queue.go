package main

// The live-shift queue (docs/design-live-shift.md): one warm crew per repository works items one after another.
// `nightshift add` appends an item and wakes the Foreman; `nightshift queue` lists, starts (`next`), finishes
// (`done`) and drops items. Items live in <state_dir>/queue.jsonl, rewritten whole under queue.lock.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func init() {
	extraCommands["add"] = addCommand
	extraCommands["queue"] = queueCommand
}

type queueItem struct {
	ID     int    `json:"id"`
	TS     string `json:"ts"`
	From   string `json:"from"`
	Ticket string `json:"ticket,omitempty"`
	Task   string `json:"task"`
	State  string `json:"state"`
	Ended  string `json:"ended,omitempty"`
}

const addUsage = `usage: nightshift add [--shift SHIFT] [--from NAME] (TASK... | TICKET [TICKET ...])

Queue a ticket ID or a task for the live shift of the repository you are in (or --shift, or NIGHTSHIFT_SHIFT),
and wake its Foreman.`

const queueUsage = `usage: nightshift queue [--shift SHIFT] [list | next | done ID [--state landed|blocked] | drop ID]

list (default) shows the items; next starts the oldest queued item (fresh branch off the target branch, per-item
records archived) and prints it; done marks the running item; drop removes a queued item.`

// liveShiftFor finds the live shift whose repository or worktree contains cwd.
func liveShiftFor(cwd string) string {
	cwd = pyResolve(cwd)
	dirs := shiftDirs()
	for i := len(dirs) - 1; i >= 0; i-- {
		dir := dirs[i]
		info, _ := worktreeInfo(dir)
		if info == nil {
			continue
		}
		repo, tree := pyResolve(objectString(info, "repo_root")), pyResolve(objectString(info, "worktree_dir"))
		if !pathWithin(cwd, repo) && !pathWithin(cwd, tree) {
			continue
		}
		if rec, err := statusRecord(dir); err == nil {
			if alive, _ := objectGet(rec, "alive"); alive == true {
				return dir
			}
		}
	}
	return ""
}

func queueShift(ref string) (string, error) {
	if ref == "" {
		ref = os.Getenv("NIGHTSHIFT_SHIFT")
	}
	if ref != "" {
		if info, err := os.Stat(ref); err == nil && info.IsDir() {
			return ref, nil
		}
		return resolveShift(ref)
	}
	cwd, _ := os.Getwd()
	if dir := liveShiftFor(cwd); dir != "" {
		return dir, nil
	}
	return "", fmt.Errorf("no live shift for %s; start one with: nightshift start \"task\"", cwd)
}

func withQueue(dir string, fn func(items []queueItem) ([]queueItem, error)) error {
	lock := filepath.Join(dir, "queue.lock")
	if err := takeFileLock(lock, 5*time.Second, 30*time.Second); err != nil {
		return err
	}
	defer releaseFileLock(lock)
	items := []queueItem{}
	if f, err := os.Open(filepath.Join(dir, "queue.jsonl")); err == nil {
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 1<<20), 1<<24)
		for scanner.Scan() {
			var item queueItem
			if json.Unmarshal(scanner.Bytes(), &item) == nil && item.ID > 0 {
				items = append(items, item)
			}
		}
		f.Close()
	}
	updated, err := fn(items)
	if err != nil || updated == nil {
		return err
	}
	var b strings.Builder
	for _, item := range updated {
		line, _ := json.Marshal(item)
		b.Write(line)
		b.WriteByte('\n')
	}
	return os.WriteFile(filepath.Join(dir, "queue.jsonl"), []byte(b.String()), 0666)
}

func queueArgs(argv []string) (shift string, rest []string, flags map[string]string, err error) {
	flags = map[string]string{}
	for i := 0; i < len(argv); i++ {
		switch a := argv[i]; a {
		case "--shift", "--from", "--state":
			if i+1 >= len(argv) {
				return "", nil, nil, fmt.Errorf("%s needs a value", a)
			}
			if a == "--shift" {
				shift = argv[i+1]
			} else {
				flags[a] = argv[i+1]
			}
			i++
		default:
			rest = append(rest, a)
		}
	}
	return shift, rest, flags, nil
}

func addCommand(argv []string) int {
	if len(argv) > 0 && (argv[0] == "-h" || argv[0] == "--help") {
		out(addUsage + "\n")
		return 0
	}
	ref, rest, flags, err := queueArgs(argv)
	text := strings.TrimSpace(strings.Join(rest, " "))
	if err == nil && text == "" {
		err = fmt.Errorf("give a ticket ID or a task")
	}
	if err != nil {
		errLine(addUsage + "\nnightshift add: error: " + err.Error())
		return 2
	}
	dir, err := queueShift(ref)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	// Several ticket IDs (nightshift add LAP-050 LAP-045 ...) queue one item each, in order.
	texts := []string{text}
	if len(rest) > 1 {
		all := true
		for _, r := range rest {
			all = all && ticketFull.MatchString(r)
		}
		if all {
			texts = rest
		}
	}
	for _, t := range texts {
		_, added, err := queueAdd(dir, t, flags["--from"])
		if err != nil {
			errLine(err.Error())
			return 1
		}
		if humanOutput() {
			out(fmt.Sprintf("Queued item %d in %s: %s\n", added.ID, filepath.Base(dir), humanLine(added.Task)))
			continue
		}
		line, _ := json.Marshal(added)
		out(string(line) + "\n")
	}
	return 0
}

// queueAdd appends an item and wakes the Foreman (review note a); woken reports whether the pane took the line.
func queueAdd(dir, text, from string) (bool, queueItem, error) {
	if from == "" {
		from = "owner"
	}
	var added queueItem
	err := withQueue(dir, func(items []queueItem) ([]queueItem, error) {
		next := 1
		for _, it := range items {
			if it.ID >= next {
				next = it.ID + 1
			}
		}
		added = queueItem{ID: next, TS: time.Now().Format("2006-01-02T15:04:05"), From: from, Task: text, State: "queued"}
		if ticketFull.MatchString(text) {
			added.Ticket = text
		} else if t := promptTicket(text); t != "" {
			added.Ticket = t
		}
		return append(items, added), nil
	})
	if err != nil {
		return false, added, err
	}
	busAppend(dir, "queue_add", object{{"id", added.ID}, {"from", added.From}, {"task", added.Task}})
	return wakeForeman(dir, fmt.Sprintf("QUEUE item %d added: %s. When you are free, run: nightshift queue next", added.ID, added.Task)), added, nil
}

func wakeForeman(dir, line string) bool {
	record, ok := readObject(filepath.Join(dir, "Foreman.json"))
	if !ok {
		return false
	}
	pid := processNumber(record, "pid")
	if pid == 0 || !processRunning(pid) {
		return false
	}
	tool := objectString(record, "tool")
	if tool == "" {
		tool = "claude"
	}
	return sendHeader(pid, line, tool)
}

func queueCommand(argv []string) int {
	if len(argv) > 0 && (argv[0] == "-h" || argv[0] == "--help") {
		out(queueUsage + "\n")
		return 0
	}
	ref, rest, flags, err := queueArgs(argv)
	if err != nil {
		errLine(queueUsage + "\nnightshift queue: error: " + err.Error())
		return 2
	}
	dir, err := queueShift(ref)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	verb := "list"
	if len(rest) > 0 {
		verb = rest[0]
	}
	id := 0
	if verb == "done" || verb == "drop" {
		if len(rest) < 2 {
			errLine(queueUsage + "\nnightshift queue: error: " + verb + " needs an item ID")
			return 2
		}
		if id, err = strconv.Atoi(rest[1]); err != nil {
			errLine("item ID must be a number: " + rest[1])
			return 2
		}
	}
	switch verb {
	case "list":
		return queuePrint(dir)
	case "next":
		item, err := queueNext(dir)
		if err != nil {
			errLine(err.Error())
			return 1
		}
		if item == nil {
			if humanOutput() {
				out("The queue is empty.\n")
				return 0
			}
			out(`{"next": null}` + "\n")
			return 0
		}
		if humanOutput() {
			out(fmt.Sprintf("Started item %d: %s\n", item.ID, humanLine(item.Task)))
			return 0
		}
		line, _ := json.Marshal(item)
		out(string(line) + "\n")
		return 0
	case "done", "drop":
		state := "dropped"
		if verb == "done" {
			state = flags["--state"]
			if state == "" {
				state = "landed"
			}
			if state != "landed" && state != "blocked" {
				errLine("--state must be landed or blocked")
				return 2
			}
			// queue next archives landed.json, so for a worktree shift it exists only if land ran for this item.
			if info, _ := worktreeInfo(dir); state == "landed" && info != nil && !fileExists(filepath.Join(dir, "landed.json")) {
				errLine("this item has not been landed: run nightshift land " + filepath.Base(dir) + " first, or mark it --state blocked")
				return 1
			}
		}
		err := withQueue(dir, func(items []queueItem) ([]queueItem, error) {
			for i := range items {
				if items[i].ID != id {
					continue
				}
				want := "running"
				if verb == "drop" {
					want = "queued"
				}
				if items[i].State != want {
					return nil, fmt.Errorf("item %d is %s, not %s", id, items[i].State, want)
				}
				items[i].State, items[i].Ended = state, time.Now().Format("2006-01-02T15:04:05")
				return items, nil
			}
			return nil, fmt.Errorf("no item %d", id)
		})
		if err != nil {
			errLine(err.Error())
			return 1
		}
		busAppend(dir, "queue_"+verb, object{{"id", id}, {"state", state}})
		return queuePrint(dir)
	}
	errLine(queueUsage + "\nnightshift queue: error: unknown action " + pyRepr(verb))
	return 2
}

func queuePrint(dir string) int {
	var list []queueItem
	_ = withQueue(dir, func(items []queueItem) ([]queueItem, error) { list = items; return nil, nil })
	if list == nil {
		list = []queueItem{}
	}
	if humanOutput() {
		humanQueue(filepath.Base(dir), list)
		return 0
	}
	text, _ := json.MarshalIndent(struct {
		Shift string      `json:"shift"`
		Items []queueItem `json:"items"`
	}{filepath.Base(dir), list}, "", "  ")
	out(string(text) + "\n")
	return 0
}

// queueNext starts the oldest queued item (review note b): refuses while an item runs or the worktree is dirty,
// archives the last item's records, resets claims, and puts the shift branch on the target branch's tip.
func queueNext(dir string) (*queueItem, error) {
	var started *queueItem
	err := withQueue(dir, func(items []queueItem) ([]queueItem, error) {
		pick := -1
		for i, it := range items {
			if it.State == "running" {
				return nil, fmt.Errorf("item %d is still running; finish it with: nightshift queue done %d [--state blocked]", it.ID, it.ID)
			}
			if it.State == "queued" && pick < 0 {
				pick = i
			}
		}
		if pick < 0 {
			return nil, nil
		}
		if err := queueFreshBranch(dir, items[pick].ID); err != nil {
			return nil, err
		}
		items[pick].State = "running"
		copied := items[pick]
		started = &copied
		return items, nil
	})
	if err == nil && started != nil {
		if started.Ticket != "" {
			_ = os.WriteFile(filepath.Join(dir, "ticket.txt"), []byte(started.Ticket+"\n"), 0666)
		}
		busAppend(dir, "queue_next", object{{"id", started.ID}, {"task", started.Task}})
	}
	return started, err
}

func queueFreshBranch(dir string, id int) error {
	archive := filepath.Join(dir, "items", strconv.Itoa(id-1))
	for _, name := range []string{"result.json", "landed.json", "review.json"} {
		if fileExists(filepath.Join(dir, name)) {
			if err := os.MkdirAll(archive, 0777); err != nil {
				return err
			}
			if err := os.Rename(filepath.Join(dir, name), filepath.Join(archive, name)); err != nil {
				return err
			}
		}
	}
	if fileExists(filepath.Join(dir, "claims.json")) {
		if err := os.WriteFile(filepath.Join(dir, "claims.json"), []byte("{}\n"), 0666); err != nil {
			return err
		}
	}
	info, _ := worktreeInfo(dir)
	if info == nil {
		return nil
	}
	tree, branch := objectString(info, "worktree_dir"), objectString(info, "branch")
	target := objectString(info, "target_branch")
	if target == "" {
		target = "main"
	}
	git := func(args ...string) (string, error) {
		b, err := exec.Command("git", append([]string{"-C", tree}, args...)...).CombinedOutput()
		return strings.TrimSpace(string(b)), err
	}
	if status, err := git("status", "--porcelain"); err != nil || status != "" {
		return fmt.Errorf("worktree %s is not clean; commit, land or discard the last item first:\n%s", tree, status)
	}
	if out, err := git("checkout", "-B", branch, target); err != nil {
		return fmt.Errorf("could not reset %s to %s: %s", branch, target, out)
	}
	head, err := git("rev-parse", "HEAD")
	if err != nil {
		return err
	}
	return writeJSONFile(filepath.Join(dir, "worktree.json"), setValue(info, "base_sha", head))
}

// liveShiftIdle: the shift has a queue, nothing queued or running, its last item finished (result.json present),
// and nothing happened on its bus or queue for idle_minutes (crew.json, default 5).
func liveShiftIdle(dir string) bool {
	if !fileExists(filepath.Join(dir, "result.json")) {
		return false
	}
	busy := false
	_ = withQueue(dir, func(items []queueItem) ([]queueItem, error) {
		for _, it := range items {
			if it.State == "queued" || it.State == "running" {
				busy = true
			}
		}
		return nil, nil
	})
	if busy {
		return false
	}
	minutes := 5.0
	if crew, ok := readObject(filepath.Join(dir, "crew.json")); ok {
		if n, ok := objectNumber(crew, "idle_minutes"); ok {
			if v, err := n.Float64(); err == nil && v > 0 {
				minutes = v
			}
		}
	}
	last := time.Time{}
	for _, name := range []string{"bus.jsonl", "queue.jsonl", "result.json"} {
		if info, err := os.Stat(filepath.Join(dir, name)); err == nil && info.ModTime().After(last) {
			last = info.ModTime()
		}
	}
	return time.Since(last) > time.Duration(minutes*float64(time.Minute))
}

// watchLiveShiftIdle runs in the Foreman's pane wrapper for the life of the Foreman: once a minute it closes the
// shift when liveShiftIdle says so, so no scheduler is needed for the idle timeout.
func watchLiveShiftIdle(done <-chan struct{}, dir string) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return
		case <-ticker.C:
			if liveShiftIdle(dir) {
				busAppend(dir, "idle_close", object{{"reason", "queue empty and quiet for idle_minutes"}})
				_ = stopShift(dir, true, nil, false)
				return
			}
		}
	}
}
