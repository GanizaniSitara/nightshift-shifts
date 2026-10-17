package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func init() {
	extraCommands["floor"] = floorCommand
	extraCommands["statusline"] = statuslineCommand
	floorAppendHook = floorAppend
}

const floorLockWait = 5 * time.Second
const floorLockStale = 10 * time.Second

const floorUsage = "usage: nightshift floor [-h] {say,hear} ..."

const floorHelp = "usage: nightshift floor [-h] {say,hear} ...\n\nCommunicate through the machine-level floor bus.\n\npositional arguments:\n  {say,hear}\n    say       append FROM TEXT to the floor bus\n    hear      read messages addressed to ME\n\noptions:\n  -h, --help  show this help message and exit\n"

const floorSayUsage = "usage: nightshift floor say [-h] [--to NAME] FROM TEXT [TEXT ...]"

const floorSayHelp = "usage: nightshift floor say [-h] [--to NAME] FROM TEXT [TEXT ...]\n\npositional arguments:\n  FROM\n  TEXT\n\noptions:\n  -h, --help  show this help message and exit\n  --to NAME   recipient, or all (the default)\n"

const floorHearUsage = "usage: nightshift floor hear [-h] [--since ID] [--follow] ME"

const floorHearHelp = "usage: nightshift floor hear [-h] [--since ID] [--follow] ME\n\npositional arguments:\n  ME\n\noptions:\n  -h, --help  show this help message and exit\n  --since ID  read after ID, without regressing the saved cursor\n  --follow    keep watching until Ctrl+C\n"

func floorPaths() (root, bus, cursors string) {
	root = filepath.Join(stateRoot(), "floor")
	bus = filepath.Join(root, "bus.jsonl")
	cursors = filepath.Join(root, "cursors")
	return root, bus, cursors
}

func parseFloorEventID(v any) (int, bool) {
	switch x := v.(type) {
	case int:
		return x, true
	case int64:
		return int(x), true
	case json.Number:
		if strings.ContainsAny(x.String(), ".eE") {
			return 0, false
		}
		n, err := x.Int64()
		if err == nil {
			return int(n), true
		}
	}
	return 0, false
}

func floorNextID(data []byte) int {
	highest := 0
	for _, raw := range bytes.Split(data, []byte{'\n'}) {
		raw = bytes.TrimSuffix(raw, []byte{'\r'})
		if len(bytes.TrimSpace(raw)) == 0 {
			continue
		}
		val, err := parseJSON(raw)
		if err != nil {
			continue
		}
		obj, ok := val.(object)
		if !ok {
			continue
		}
		idVal, ok := objectGet(obj, "id")
		if !ok {
			continue
		}
		if id, ok := parseFloorEventID(idVal); ok {
			if id > highest {
				highest = id
			}
		}
	}
	return highest + 1
}

func floorAppend(sender, text, to string) (object, error) {
	if to == "" {
		to = "all"
	}
	if sender == "" || text == "" || to == "" ||
		strings.ContainsAny(sender, "\r\n") ||
		strings.ContainsAny(text, "\r\n") ||
		strings.ContainsAny(to, "\r\n") {
		return nil, errors.New("floor bus fields must be non-empty and single-line")
	}
	root, busPath, _ := floorPaths()
	if err := os.MkdirAll(root, 0777); err != nil {
		return nil, err
	}
	lockPath := filepath.Join(root, "bus.lock")
	if err := takeFileLock(lockPath, floorLockWait, floorLockStale); err != nil {
		return nil, err
	}
	defer releaseFileLock(lockPath)

	data, err := os.ReadFile(busPath)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	prefix := ""
	if len(data) > 0 && data[len(data)-1] != '\n' {
		prefix = "\n"
	}
	nextID := floorNextID(data)
	event := object{
		{"ts", time.Now().Format("2006-01-02T15:04:05")},
		{"from", sender},
		{"to", to},
		{"text", text},
		{"id", nextID},
	}
	line := prefix + renderJSON(event, false, false) + "\n"
	f, err := os.OpenFile(busPath, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0666)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if _, err := f.WriteString(line); err != nil {
		return nil, err
	}
	return event, nil
}

func floorCursorPath(cursors, reader string) string {
	sum := sha256.Sum256([]byte(reader))
	digest := hex.EncodeToString(sum[:])
	return filepath.Join(cursors, digest+".json")
}

func floorCursor(path string) int {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	val, err := parseJSON(data)
	if err != nil {
		return 0
	}
	if obj, ok := val.(object); ok {
		if idVal, ok := objectGet(obj, "id"); ok {
			if id, ok := parseFloorEventID(idVal); ok && id >= 0 {
				return id
			}
		}
		return 0
	}
	if id, ok := parseFloorEventID(val); ok && id >= 0 {
		return id
	}
	return 0
}

func floorEventLines(path string, offset int64) ([]object, int64) {
	f, err := os.Open(path)
	if err != nil {
		return nil, offset
	}
	defer f.Close()
	if offset > 0 {
		if _, err := f.Seek(offset, io.SeekStart); err != nil {
			return nil, offset
		}
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return nil, offset
	}
	end := bytes.LastIndexByte(data, '\n') + 1
	if end <= 0 {
		return nil, offset
	}
	var events []object
	lines := bytes.Split(data[:end], []byte{'\n'})
	for _, raw := range lines {
		raw = bytes.TrimSuffix(raw, []byte{'\r'})
		if len(bytes.TrimSpace(raw)) == 0 {
			continue
		}
		val, err := parseJSON(raw)
		if err != nil {
			continue
		}
		obj, ok := val.(object)
		if !ok {
			continue
		}
		idVal, ok := objectGet(obj, "id")
		if !ok {
			continue
		}
		if _, ok := parseFloorEventID(idVal); ok {
			events = append(events, obj)
		}
	}
	return events, offset + int64(end)
}

func floorSayCommand(args []string) int {
	to := "all"
	toExplicit := false
	var pos []string
	var unrecognized []string

	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "-h" || a == "--help" {
			out(floorSayHelp)
			return 0
		}
		if a == "--to" {
			if i+1 >= len(args) {
				return writeUsage("nightshift floor say", floorSayUsage, "argument --to: expected one argument")
			}
			i++
			to = args[i]
			toExplicit = true
		} else if strings.HasPrefix(a, "--to=") {
			to = strings.TrimPrefix(a, "--to=")
			toExplicit = true
		} else if a == "--" {
			pos = append(pos, args[i+1:]...)
			break
		} else if strings.HasPrefix(a, "-") && a != "-" {
			unrecognized = append(unrecognized, a)
		} else {
			pos = append(pos, a)
		}
	}

	if len(unrecognized) > 0 {
		return writeUsage("nightshift floor", floorUsage, "unrecognized arguments: "+strings.Join(unrecognized, " "))
	}

	if len(pos) == 0 {
		return writeUsage("nightshift floor say", floorSayUsage, "the following arguments are required: FROM, TEXT")
	}
	if len(pos) == 1 {
		return writeUsage("nightshift floor say", floorSayUsage, "the following arguments are required: TEXT")
	}

	sender := pos[0]
	text := pos[1:]
	if toExplicit && to == "" {
		to = "all"
	}

	event, err := floorAppend(sender, strings.Join(text, " "), to)
	if err != nil {
		if err.Error() == "floor bus fields must be non-empty and single-line" {
			errLine(err.Error())
		} else {
			errLine(fmt.Sprintf("floor bus append failed: %v", err))
		}
		return 1
	}
	outLine(compactJSON(event))
	return 0
}

func floorHearCommand(args []string) int {
	var sinceVal *int
	follow := false
	var pos []string
	var unrecognized []string

	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "-h" || a == "--help" {
			out(floorHearHelp)
			return 0
		}
		if a == "--follow" {
			follow = true
		} else if a == "--since" {
			if i+1 >= len(args) {
				return writeUsage("nightshift floor hear", floorHearUsage, "argument --since: expected one argument")
			}
			i++
			valStr := args[i]
			n, err := strconv.Atoi(valStr)
			if err != nil {
				return writeUsage("nightshift floor hear", floorHearUsage, fmt.Sprintf("argument --since: invalid int value: %s", pyRepr(valStr)))
			}
			sinceVal = &n
		} else if strings.HasPrefix(a, "--since=") {
			valStr := strings.TrimPrefix(a, "--since=")
			n, err := strconv.Atoi(valStr)
			if err != nil {
				return writeUsage("nightshift floor hear", floorHearUsage, fmt.Sprintf("argument --since: invalid int value: %s", pyRepr(valStr)))
			}
			sinceVal = &n
		} else if a == "--" {
			pos = append(pos, args[i+1:]...)
			break
		} else if strings.HasPrefix(a, "-") && a != "-" && !negativeNumber(a) {
			unrecognized = append(unrecognized, a)
		} else {
			pos = append(pos, a)
		}
	}

	if len(unrecognized) > 0 {
		return writeUsage("nightshift floor", floorUsage, "unrecognized arguments: "+strings.Join(unrecognized, " "))
	}

	if len(pos) == 0 {
		return writeUsage("nightshift floor hear", floorHearUsage, "the following arguments are required: ME")
	}
	if len(pos) > 1 {
		return writeUsage("nightshift floor", floorUsage, "unrecognized arguments: "+strings.Join(pos[1:], " "))
	}

	if sinceVal != nil && *sinceVal < 0 {
		return writeUsage("nightshift floor hear", floorHearUsage, "--since must be non-negative")
	}

	reader := pos[0]
	return runFloorHear(reader, sinceVal, follow)
}

func runFloorHear(reader string, since *int, follow bool) int {
	root, busPath, cursorsDir := floorPaths()
	_ = root
	cursorPath := floorCursorPath(cursorsDir, reader)
	var offset int64 = 0

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt)
	defer signal.Stop(sigChan)

	for {
		err := func() error {
			lockPath := strings.TrimSuffix(cursorPath, filepath.Ext(cursorPath)) + ".lock"
			if err := os.MkdirAll(filepath.Dir(cursorPath), 0777); err != nil {
				return err
			}
			if err := takeFileLock(lockPath, floorLockWait, floorLockStale); err != nil {
				return err
			}
			defer releaseFileLock(lockPath)

			saved := floorCursor(cursorPath)
			baseline := saved
			if since != nil {
				baseline = *since
			}
			var events []object
			if fileExists(busPath) {
				events, offset = floorEventLines(busPath, offset)
			}
			highest := saved
			var output []object
			for _, event := range events {
				idVal, _ := objectGet(event, "id")
				id, _ := parseFloorEventID(idVal)
				if id > highest {
					highest = id
				}
				toVal, _ := objectGet(event, "to")
				toStr, _ := toVal.(string)
				if id > baseline && (toStr == reader || toStr == "all") {
					output = append(output, event)
				}
			}
			for _, event := range output {
				if humanOutput() {
					from, _ := objectGet(event, "from")
					to, _ := objectGet(event, "to")
					ts, _ := objectGet(event, "ts")
					text, _ := objectGet(event, "text")
					id, _ := objectGet(event, "id")
					outLine(fmt.Sprintf("#%v %v  %v -> %v: %v", id, ts, from, to, text))
					continue
				}
				outLine(compactJSON(event))
			}
			if since == nil && highest > saved {
				_ = writeJSONFile(cursorPath, object{{"id", highest}})
			}
			return nil
		}()
		if err != nil {
			errLine(err.Error())
			return 1
		}
		if !follow {
			return 0
		}
		select {
		case <-sigChan:
			return 0
		case <-time.After(1 * time.Second):
		}
	}
}

func floorCommand(argv []string) int {
	if len(argv) == 0 {
		return writeUsage("nightshift floor", floorUsage, "the following arguments are required: command")
	}
	if argv[0] == "-h" || argv[0] == "--help" {
		out(floorHelp)
		return 0
	}
	if argv[0] != "say" && argv[0] != "hear" {
		if strings.HasPrefix(argv[0], "-") {
			return writeUsage("nightshift floor", floorUsage, "the following arguments are required: command")
		}
		return writeUsage("nightshift floor", floorUsage, fmt.Sprintf("argument command: invalid choice: %s (choose from say, hear)", pyRepr(argv[0])))
	}
	if argv[0] == "say" {
		return floorSayCommand(argv[1:])
	}
	return floorHearCommand(argv[1:])
}

func statuslineCommand(argv []string) int {
	_, _ = io.ReadAll(os.Stdin)
	role := os.Getenv("NIGHTSHIFT_ROLE")
	identity := os.Getenv("NIGHTSHIFT_IDENTITY")
	if role != "" && identity != "" {
		colour := "37"
		switch os.Getenv("NIGHTSHIFT_ROLE_COLOUR") {
		case "red":
			colour = "31"
		case "purple":
			colour = "35"
		case "green":
			colour = "32"
		case "blue":
			colour = "34"
		case "yellow":
			colour = "33"
		}
		outLine(fmt.Sprintf("\033[%sm%s · %s\033[0m", colour, role, identity))
	}
	return 0
}
