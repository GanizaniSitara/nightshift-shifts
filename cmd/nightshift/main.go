// Command nightshift is the standalone, read-only Go view of a Nightshift shift.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/windows"
	_ "modernc.org/sqlite"
)

const (
	defaultStateRoot  = `C:\nightshift`
	defaultLegacyRoot = `C:\Data\nightshift`
	logPoll           = time.Second
)

var (
	stages        = []string{"implemented", "tested", "deployed", "validated"}
	upliftReasons = []string{"needs-senior", "tester-failed-twice", "timeout"}
)

type pair struct {
	key string
	val any
}

type object []pair

type jsonReader struct {
	dec *json.Decoder
}

func (r *jsonReader) value() (any, error) {
	tok, err := r.dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			var out object
			for r.dec.More() {
				keyTok, err := r.dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := keyTok.(string)
				if !ok {
					return nil, errors.New("JSON object key is not a string")
				}
				v, err := r.value()
				if err != nil {
					return nil, err
				}
				found := false
				for i := range out {
					if out[i].key == key {
						out[i].val = v
						found = true
						break
					}
				}
				if !found {
					out = append(out, pair{key: key, val: v})
				}
			}
			if _, err := r.dec.Token(); err != nil {
				return nil, err
			}
			return out, nil
		case '[':
			var out []any
			for r.dec.More() {
				v, err := r.value()
				if err != nil {
					return nil, err
				}
				out = append(out, v)
			}
			if _, err := r.dec.Token(); err != nil {
				return nil, err
			}
			return out, nil
		}
	case json.Number:
		return t, nil
	}
	return tok, nil
}

func parseJSON(data []byte) (any, error) {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	r := &jsonReader{dec: dec}
	v, err := r.value()
	if err != nil {
		return nil, err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, errors.New("extra JSON value")
		}
		return nil, err
	}
	return v, nil
}

func objectGet(o object, key string) (any, bool) {
	for _, p := range o {
		if p.key == key {
			return p.val, true
		}
	}
	return nil, false
}

func objectString(o object, key string) string {
	v, _ := objectGet(o, key)
	s, _ := v.(string)
	return s
}

func objectNumber(o object, key string) (json.Number, bool) {
	v, ok := objectGet(o, key)
	if !ok {
		return "", false
	}
	switch n := v.(type) {
	case json.Number:
		return n, true
	case float64:
		return json.Number(strconv.FormatFloat(n, 'g', -1, 64)), true
	case int:
		return json.Number(strconv.Itoa(n)), true
	}
	return "", false
}

func objectBool(o object, key string) bool {
	v, _ := objectGet(o, key)
	b, _ := v.(bool)
	return b
}

func readObject(path string) (object, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, false
	}
	v, err := parseJSON(b)
	if err != nil {
		return nil, false
	}
	o, ok := v.(object)
	return o, ok
}

func asciiString(s string) string {
	var b strings.Builder
	for len(s) > 0 {
		r, n := utf8.DecodeRuneInString(s)
		if r == utf8.RuneError && n == 1 {
			s = s[1:]
			b.WriteString(`\ufffd`)
			continue
		}
		s = s[n:]
		switch r {
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else if r <= 0x7f {
				b.WriteRune(r)
			} else if r <= 0xffff {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				r -= 0x10000
				fmt.Fprintf(&b, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
			}
		}
	}
	return b.String()
}

func unicodeString(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	return b.String()
}

func numberText(n json.Number) string {
	text := n.String()
	if !strings.ContainsAny(text, ".eE") {
		if text == "-0" {
			return "0"
		}
		return text
	}
	v, _ := n.Float64()
	return floatText(v)
}

func floatText(v float64) string {
	if math.IsInf(v, 1) {
		return "Infinity"
	}
	if math.IsInf(v, -1) {
		return "-Infinity"
	}
	text := strconv.FormatFloat(v, 'e', -1, 64)
	exponent, _ := strconv.Atoi(text[strings.IndexByte(text, 'e')+1:])
	if exponent >= -4 && exponent < 16 {
		text = strconv.FormatFloat(v, 'f', -1, 64)
		if !strings.Contains(text, ".") {
			text += ".0"
		}
	}
	return text
}

func writeJSON(b *strings.Builder, v any, pretty bool, level int, ascii bool) {
	indentText := func(n int) { b.WriteString(strings.Repeat("  ", n)) }
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		b.WriteByte('"')
		if ascii {
			b.WriteString(asciiString(x))
		} else {
			b.WriteString(unicodeString(x))
		}
		b.WriteByte('"')
	case json.Number:
		b.WriteString(numberText(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint32:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case float64:
		b.WriteString(floatText(x))
	case object:
		if len(x) == 0 {
			b.WriteString("{}")
			return
		}
		if !pretty {
			b.WriteByte('{')
			for i, p := range x {
				if i > 0 {
					b.WriteString(", ")
				}
				b.WriteByte('"')
				if ascii {
					b.WriteString(asciiString(p.key))
				} else {
					b.WriteString(unicodeString(p.key))
				}
				b.WriteByte('"')
				b.WriteString(": ")
				writeJSON(b, p.val, false, level+1, ascii)
			}
			b.WriteByte('}')
			return
		}
		b.WriteString("{\n")
		for i, p := range x {
			indentText(level + 1)
			b.WriteByte('"')
			if ascii {
				b.WriteString(asciiString(p.key))
			} else {
				b.WriteString(unicodeString(p.key))
			}
			b.WriteByte('"')
			b.WriteString(": ")
			writeJSON(b, p.val, true, level+1, ascii)
			if i+1 < len(x) {
				b.WriteByte(',')
			}
			b.WriteByte('\n')
		}
		indentText(level)
		b.WriteByte('}')
	case []any:
		if len(x) == 0 {
			b.WriteString("[]")
			return
		}
		if !pretty {
			b.WriteByte('[')
			for i, item := range x {
				if i > 0 {
					b.WriteString(", ")
				}
				writeJSON(b, item, false, level+1, ascii)
			}
			b.WriteByte(']')
			return
		}
		b.WriteString("[\n")
		for i, item := range x {
			indentText(level + 1)
			writeJSON(b, item, true, level+1, ascii)
			if i+1 < len(x) {
				b.WriteByte(',')
			}
			b.WriteByte('\n')
		}
		indentText(level)
		b.WriteByte(']')
	default:
		b.WriteString("null")
	}
}

func renderJSON(v any, pretty, ascii bool) string {
	var b strings.Builder
	writeJSON(&b, v, pretty, 0, ascii)
	return b.String()
}

func cp1252Bytes(s string) []byte {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r < 0x80:
			b.WriteByte(byte(r))
		case r >= 0xa0 && r <= 0xff:
			b.WriteByte(byte(r))
		default:
			codes := map[rune]byte{0x20ac: 0x80, 0x201a: 0x82, 0x192: 0x83, 0x201e: 0x84, 0x2026: 0x85, 0x2020: 0x86, 0x2021: 0x87, 0x2c6: 0x88, 0x2030: 0x89, 0x160: 0x8a, 0x2039: 0x8b, 0x152: 0x8c, 0x17d: 0x8e, 0x2018: 0x91, 0x2019: 0x92, 0x201c: 0x93, 0x201d: 0x94, 0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97, 0x2dc: 0x98, 0x2122: 0x99, 0x161: 0x9a, 0x203a: 0x9b, 0x153: 0x9c, 0x17e: 0x9e, 0x178: 0x9f}
			if v, ok := codes[r]; ok {
				b.WriteByte(v)
			} else {
				b.WriteByte('?')
			}
		}
	}
	return []byte(b.String())
}

func consoleBytes(s string) []byte {
	encoding := strings.ToLower(os.Getenv("PYTHONIOENCODING"))
	if i := strings.IndexByte(encoding, ':'); i >= 0 {
		encoding = encoding[:i]
	}
	if encoding == "cp1252" || encoding == "windows-1252" {
		return cp1252Bytes(s)
	}
	return []byte(s)
}

func writeConsole(file *os.File, s string) { _, _ = file.Write(consoleBytes(s)) }
func out(s string)                         { writeConsole(os.Stdout, strings.ReplaceAll(s, "\n", "\r\n")) }
func outLine(s string)                     { out(s + "\n") }
func errLine(s string)                     { writeConsole(os.Stderr, strings.ReplaceAll(s+"\n", "\n", "\r\n")) }

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func stateRoot() string  { return envOr("NIGHTSHIFT_STATE_ROOT", defaultStateRoot) }
func legacyRoot() string { return envOr("NIGHTSHIFT_LEGACY_ROOT", defaultLegacyRoot) }

func shiftDirs() []string {
	var dirs []string
	for _, root := range []string{filepath.Join(stateRoot(), "shifts"), filepath.Join(legacyRoot(), "shifts")} {
		entries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if entry.IsDir() {
				dirs = append(dirs, filepath.Join(root, entry.Name()))
			}
		}
	}
	sort.SliceStable(dirs, func(i, j int) bool {
		ai, _ := os.Stat(dirs[i])
		aj, _ := os.Stat(dirs[j])
		if ai != nil && aj != nil {
			return ai.ModTime().Before(aj.ModTime())
		}
		return false
	})
	return dirs
}

func resolveShift(ref string) (string, error) {
	dirs := shiftDirs()
	if len(dirs) == 0 {
		return "", fmt.Errorf("no shifts found under %s", filepath.Join(stateRoot(), "shifts"))
	}
	if ref == "latest" {
		return dirs[len(dirs)-1], nil
	}
	hits := make([]string, 0, 1)
	for _, d := range dirs {
		name := filepath.Base(d)
		if name == ref || strings.HasSuffix(name, ref) {
			hits = append(hits, d)
		}
	}
	if len(hits) != 1 {
		return "", fmt.Errorf("shift %s matches %d shifts; give the full name", pyRepr(ref), len(hits))
	}
	return hits[0], nil
}

func firstLine(path string, limit int) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	text := strings.TrimSpace(string(b))
	if i := strings.IndexByte(text, '\n'); i >= 0 {
		text = strings.TrimSpace(text[:i])
	}
	r := []rune(text)
	if len(r) > limit {
		return string(r[:limit])
	}
	return text
}

func stagesIndex(stage string) int {
	for i, s := range stages {
		if s == stage {
			return i
		}
	}
	return 0
}

func resultStage(v any) string {
	if o, ok := v.(object); ok {
		s := objectString(o, "stage")
		for _, allowed := range stages {
			if s == allowed {
				return s
			}
		}
	}
	return "implemented"
}

func requires(dir string) string {
	s := strings.ToLower(strings.TrimSpace(readText(filepath.Join(dir, "requires.txt"))))
	for _, allowed := range stages {
		if s == allowed {
			return s
		}
	}
	return "tested"
}

func readText(path string) string {
	b, _ := os.ReadFile(path)
	return string(b)
}

func processInfo(pid uint32) (string, float64, bool) {
	if pid == 0 {
		return "", 0, false
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return "", 0, false
	}
	defer windows.CloseHandle(h)
	image := make([]uint16, 1024)
	size := uint32(len(image))
	if windows.QueryFullProcessImageName(h, 0, &image[0], &size) != nil {
		return "", 0, false
	}
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(h, &created, &exited, &kernel, &user) != nil {
		return strings.ToLower(filepath.Base(windows.UTF16ToString(image[:size]))), 0, true
	}
	return strings.ToLower(filepath.Base(windows.UTF16ToString(image[:size]))), float64(created.Nanoseconds()) / 1e9, true
}

func pidAlive(pid uint32, started json.Number, hasStarted bool) bool {
	image, created, exists := processInfo(pid)
	if !exists || !contains([]string{"claude.exe", "node.exe", "agy.exe", "codex.exe", "copilot.exe"}, image) {
		return false
	}
	if !hasStarted || created == 0 {
		return true
	}
	return math.Abs(created-numberFloat(started)) <= 300
}

func processRunning(pid uint32) bool {
	_, _, exists := processInfo(pid)
	return exists
}

func processNumber(o object, key string) uint32 {
	n, ok := objectNumber(o, key)
	if !ok {
		return 0
	}
	v, _ := strconv.ParseUint(n.String(), 10, 32)
	return uint32(v)
}

func crewMembers(dir string) (object, []object) {
	crew, ok := readObject(filepath.Join(dir, "crew.json"))
	if !ok {
		return nil, []object{{pair{"label", "Foreman"}, pair{"identity", filepath.Base(dir) + "-foreman"}, pair{"human_contact", true}}, {pair{"label", "Coder"}, pair{"identity", filepath.Base(dir) + "-coder"}, pair{"human_contact", false}}}
	}
	v, ok := objectGet(crew, "members")
	if !ok {
		return crew, nil
	}
	items, ok := v.([]any)
	if !ok {
		return crew, nil
	}
	members := make([]object, 0, len(items))
	for _, item := range items {
		if o, ok := item.(object); ok {
			members = append(members, o)
		}
	}
	return crew, members
}

func memberByLabel(dir, label string) (object, object, error) {
	_, members := crewMembers(dir)
	wanted := strings.ToLower(label)
	for _, m := range members {
		canonical := objectString(m, "label")
		identity := objectString(m, "identity")
		info, _ := readObject(filepath.Join(dir, canonical+".json"))
		name := objectString(info, "name")
		for _, candidate := range []string{canonical, identity, name} {
			if candidate != "" && strings.ToLower(candidate) == wanted {
				return m, info, nil
			}
		}
	}
	labels := make([]string, 0, len(members))
	for _, m := range members {
		labels = append(labels, objectString(m, "label"))
	}
	return nil, nil, fmt.Errorf("%s is not a member of shift %s; use one of: %s", pyRepr(label), filepath.Base(dir), strings.Join(labels, ", "))
}

func canonicalLabel(dir, label string) (string, object, error) {
	m, info, err := memberByLabel(dir, label)
	if err != nil {
		return "", nil, err
	}
	return objectString(m, "label"), info, nil
}

func memberRole(dir, label string) string {
	crew, members := crewMembers(dir)
	_ = crew
	for _, m := range members {
		if objectString(m, "label") == label {
			if s := objectString(m, "role"); s != "" {
				return s
			}
			return label
		}
	}
	return label
}

func humanLabel(dir string) string {
	_, members := crewMembers(dir)
	for _, m := range members {
		if objectBool(m, "human_contact") {
			return objectString(m, "label")
		}
	}
	return "Foreman"
}

func currentIncarnation(dir, label string) (json.Number, bool) {
	_, info, err := canonicalLabel(dir, label)
	if err != nil {
		return "", false
	}
	return objectNumber(info, "started")
}

func meshName(dir, label string) string {
	_, info, err := canonicalLabel(dir, label)
	if err == nil {
		if name := objectString(info, "name"); name != "" {
			return name
		}
	}
	_, members := crewMembers(dir)
	for _, m := range members {
		if objectString(m, "label") == label {
			return objectString(m, "identity")
		}
	}
	return ""
}

func sameNumber(a json.Number, aok bool, b any) bool {
	if !aok {
		return b == nil
	}
	if b == nil {
		return false
	}
	bnum, ok := numericValue(b)
	if !ok {
		return false
	}
	af, ae := strconv.ParseFloat(a.String(), 64)
	bf, be := strconv.ParseFloat(bnum.String(), 64)
	return ae == nil && be == nil && af == bf
}

func statusRecord(dir string) (object, error) {
	record := object{{"shift", filepath.Base(dir)}, {"state_dir", dir}, {"roles", object{}}, {"task", firstLine(filepath.Join(dir, "prompt.txt"), 120)}}
	_, members := crewMembers(dir)
	roles := record[2].val.(object)
	alive := false
	home := envOr("NIGHTSHIFT_HOME", userHome())
	for _, m := range members {
		label := objectString(m, "label")
		info, exists := readObject(filepath.Join(dir, label+".json"))
		if !exists {
			continue
		}
		pid := processNumber(info, "pid")
		started, hasStarted := objectNumber(info, "started")
		role := object{{"pid", int64(pid)}, {"wrapper_pid", nil}, {"name", nil}, {"cwd", nil}, {"tool", "claude"}, {"alive", pidAlive(pid, started, hasStarted)}, {"registered", fileExists(filepath.Join(home, ".claude", "sessions", strconv.FormatUint(uint64(pid), 10)+".json"))}}
		if n, ok := objectGet(info, "wrapper_pid"); ok {
			role[1].val = n
		}
		if n, ok := objectGet(info, "name"); ok {
			role[2].val = n
		}
		if n, ok := objectGet(info, "cwd"); ok {
			role[3].val = n
		}
		if n := objectString(info, "tool"); n != "" {
			role[4].val = n
		}
		if role[5].val == true {
			alive = true
		}
		roles = append(roles, pair{label, role})
	}
	record[1].val = dir
	record[2].val = roles
	record = append(record, pair{"alive", alive})
	if crew, ok := readObject(filepath.Join(dir, "crew.json")); ok {
		if slots, ok := objectNumber(crew, "slots"); ok {
			arr := make([]any, 0)
			n, _ := strconv.Atoi(slots.String())
			for i := 1; i <= n; i++ {
				slot, _ := readObject(filepath.Join(dir, "slots", strconv.Itoa(i)+".json"))
				state, role := slotState(dir, slot)
				arr = append(arr, object{{"slot", i}, {"role", role}, {"state", state}, {"reusable", state == "empty" || state == "dead"}})
			}
			record = append(record, pair{"slots", arr})
		}
	}
	resultValue, resultOK := readJSONFile(filepath.Join(dir, "result.json"))
	result := "none"
	stage := any(nil)
	if resultOK {
		if o, ok := resultValue.(object); ok {
			result = objectString(o, "status")
			if result == "" {
				result = "invalid"
			}
			stage = resultStage(o)
		} else {
			result = "invalid"
		}
	}
	record = append(record, pair{"stage", stage}, pair{"result", result}, pair{"requires", requires(dir)})
	rows, err := readMessages(dir)
	if err != nil {
		return nil, err
	}
	unsent, unacked := 0, 0
	for _, row := range rows {
		state := objectString(row, "state")
		if state == "stored" {
			unsent++
		}
		if state == "stored" || state == "sent" {
			unacked++
		}
	}
	record = append(record, pair{"messages", object{{"unsent", unsent}, {"unacked", unacked}}})
	record = append(record, pair{"holds", holdForShift(filepath.Base(dir))})
	if result == "done" && fileExists(filepath.Join(dir, "requires.txt")) && stagesIndex(fmt.Sprint(stage)) < stagesIndex(requires(dir)) {
		record[len(record)-4].val = "short"
	}
	return record, nil
}

func readJSONFile(path string) (any, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, false
	}
	v, err := parseJSON(b)
	return v, err == nil
}
func fileExists(path string) bool { _, err := os.Stat(path); return err == nil }
func userHome() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return h
}

func slotState(dir string, slot object) (string, any) {
	if slot == nil {
		return "gone", nil
	}
	roleValue, _ := objectGet(slot, "role")
	role, _ := roleValue.(string)
	shell := processRunning(processNumber(slot, "cmd_pid"))
	if role == "" {
		if shell {
			return "empty", nil
		}
		return "gone", nil
	}
	info, _ := readObject(filepath.Join(dir, role+".json"))
	started, startedOK := objectNumber(info, "started")
	spawned, spawnedOK := objectNumber(slot, "spawned")
	if !objectNumberExists(info, "pid") || (startedOK && spawnedOK && numberFloat(started) < numberFloat(spawned)) {
		return "starting", role
	}
	startedInfo, hasStartedInfo := objectNumber(info, "started")
	if pidAlive(processNumber(info, "pid"), startedInfo, hasStartedInfo) {
		return "live", role
	}
	if shell {
		return "dead", role
	}
	return "gone", role
}

func objectNumberExists(o object, key string) bool { _, ok := objectNumber(o, key); return ok }
func numberFloat(n json.Number) float64            { f, _ := strconv.ParseFloat(n.String(), 64); return f }

func holdForShift(shift string) any {
	var paths []string
	for _, root := range []string{filepath.Join(stateRoot(), "locks"), filepath.Join(legacyRoot(), "locks")} {
		entries, _ := os.ReadDir(root)
		for _, entry := range entries {
			if strings.HasSuffix(entry.Name(), ".json") {
				paths = append(paths, filepath.Join(root, entry.Name()))
			}
		}
	}
	sort.Strings(paths)
	for _, path := range paths {
		o, ok := readObject(path)
		if ok && objectString(o, "shift") == shift {
			if cwd, ok := objectGet(o, "cwd"); ok {
				return cwd
			}
		}
	}
	return nil
}

func readClaims(dir string) object {
	o, ok := readObject(filepath.Join(dir, "claims.json"))
	if !ok {
		return object{}
	}
	return o
}

type mailbox struct {
	rows    []object
	cleanup func()
}

func canonicalMailboxPath(dir string) string { return filepath.Join(dir, "mailbox.sqlite3") }

func finalPathByHandle(path string) (string, error) {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	h, err := windows.CreateFile(name, 0, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(h)
	for size := uint32(256); size <= 32768; size *= 2 {
		buf := make([]uint16, size)
		n, err := windows.GetFinalPathNameByHandle(h, &buf[0], size, 0)
		if err == nil && n < size {
			return windows.UTF16ToString(buf[:n]), nil
		}
		if err != nil && !errors.Is(err, windows.ERROR_INSUFFICIENT_BUFFER) {
			return "", err
		}
		if n >= size {
			size = n
		}
	}
	return "", fmt.Errorf("physical path for %s is too long", path)
}

func normalizePhysicalPath(path string, preserveExtended bool) string {
	if !preserveExtended {
		if strings.HasPrefix(path, `\\?\UNC\`) {
			path = `\\` + path[8:]
		} else if strings.HasPrefix(path, `\\?\`) {
			path = path[4:]
		}
	}
	return strings.ToLower(filepath.Clean(path))
}

func canonicalPath(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	preserveExtended := strings.HasPrefix(path, `\\?\`) || strings.HasPrefix(path, `\\.\`)
	current := filepath.Clean(abs)
	missing := []string{}
	for {
		_, statErr := os.Stat(current)
		if statErr == nil {
			resolved, err := finalPathByHandle(current)
			if err != nil {
				return "", err
			}
			for i := len(missing) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, missing[i])
			}
			return normalizePhysicalPath(resolved, preserveExtended), nil
		}
		if !errors.Is(statErr, os.ErrNotExist) {
			return "", statErr
		}
		parent := filepath.Dir(current)
		if parent == current {
			return strings.ToLower(filepath.Clean(abs)), nil
		}
		missing = append(missing, filepath.Base(current))
		current = parent
	}
}

func mutexName(path string) (string, error) {
	canonical, err := canonicalPath(path)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(canonical))
	return `Global\NightshiftMailbox-` + hex.EncodeToString(digest[:]), nil
}

func withMailboxMutex(path string, fn func() error) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	nameText, err := mutexName(path)
	if err != nil {
		return err
	}
	name, err := windows.UTF16PtrFromString(nameText)
	if err != nil {
		return err
	}
	h, err := windows.CreateMutex(nil, false, name)
	if err != nil && !errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		return err
	}
	if h == 0 {
		return err
	}
	defer windows.CloseHandle(h)
	outcome, err := windows.WaitForSingleObject(h, 10000)
	if err != nil {
		return err
	}
	if outcome != windows.WAIT_OBJECT_0 && outcome != windows.WAIT_ABANDONED {
		if outcome == 258 {
			return fmt.Errorf("mailbox %s is still held after 10s", path)
		}
		return fmt.Errorf("mailbox mutex wait failed: %d", outcome)
	}
	owned := true
	defer func() {
		if owned {
			_ = windows.ReleaseMutex(h)
		}
	}()
	return fn()
}

func copyMailbox(dir string) (string, func(), bool, error) {
	path := canonicalMailboxPath(dir)
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", func() {}, false, nil
		}
		return "", func() {}, false, fmt.Errorf("stat mailbox %s: %w", path, err)
	}
	if info.IsDir() {
		return "", func() {}, false, fmt.Errorf("mailbox path %s is a directory", path)
	}
	tmp, err := os.MkdirTemp("", "nightshift-mailbox-")
	if err != nil {
		return "", func() {}, false, err
	}
	cleanup := func() { _ = os.RemoveAll(tmp) }
	copy := func(src, dst string) error {
		in, err := os.Open(src)
		if err != nil {
			return err
		}
		defer in.Close()
		out, err := os.Create(dst)
		if err != nil {
			return err
		}
		_, cpErr := io.Copy(out, in)
		closeErr := out.Close()
		if cpErr != nil {
			return cpErr
		}
		return closeErr
	}
	if err := copy(path, filepath.Join(tmp, "mailbox.sqlite3")); err != nil {
		cleanup()
		return "", func() {}, false, err
	}
	wal := path + "-wal"
	walInfo, err := os.Stat(wal)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		cleanup()
		return "", func() {}, false, fmt.Errorf("stat mailbox WAL %s: %w", wal, err)
	}
	if err == nil && !walInfo.IsDir() {
		if err := copy(wal, filepath.Join(tmp, "mailbox.sqlite3-wal")); err != nil {
			cleanup()
			return "", func() {}, false, err
		}
	} else if err == nil {
		cleanup()
		return "", func() {}, false, fmt.Errorf("mailbox WAL path %s is a directory", wal)
	}
	return filepath.Join(tmp, "mailbox.sqlite3"), cleanup, true, nil
}

func snapshotMailbox(dir string) (string, func(), bool, error) {
	var scratch string
	cleanup := func() {}
	exists := false
	err := withMailboxMutex(canonicalMailboxPath(dir), func() error {
		var err error
		scratch, cleanup, exists, err = copyMailbox(dir)
		return err
	})
	if err != nil {
		cleanup()
		return "", func() {}, false, err
	}
	return scratch, cleanup, exists, nil
}

func readMessages(dir string) ([]object, error) {
	result := make([]object, 0)
	scratch, cleanup, exists, err := snapshotMailbox(dir)
	if err != nil {
		return nil, err
	}
	if !exists {
		return result, nil
	}
	defer cleanup()
	db, err := sql.Open("sqlite", scratch)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	queryErr := func() (runErr error) {
		defer func() {
			if err := db.Close(); err != nil && runErr == nil {
				runErr = err
			}
		}()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		rows, err := db.QueryContext(ctx, "SELECT * FROM messages ORDER BY id")
		if err != nil {
			return err
		}
		defer func() {
			if err := rows.Close(); err != nil && runErr == nil {
				runErr = err
			}
		}()
		columns, err := rows.Columns()
		if err != nil {
			return err
		}
		for rows.Next() {
			values := make([]any, len(columns))
			ptrs := make([]any, len(values))
			for i := range values {
				ptrs[i] = &values[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				return err
			}
			o := object{}
			for i, key := range columns {
				o = append(o, pair{key, dbValue(values[i], key)})
			}
			result = append(result, o)
		}
		return rows.Err()
	}()
	if queryErr != nil {
		return nil, queryErr
	}
	return result, nil
}

func dbValue(v any, key string) any {
	switch x := v.(type) {
	case nil:
		return nil
	case int64:
		return json.Number(strconv.FormatInt(x, 10))
	case float64:
		text := strconv.FormatFloat(x, 'g', -1, 64)
		if strings.Contains(key, "incarnation") || key == "delivered_epoch" {
			if !strings.ContainsAny(text, ".eE") {
				text += ".0"
			}
		}
		return json.Number(text)
	case []byte:
		return string(x)
	default:
		return x
	}
}

func rowValue(row object, key string) any     { v, _ := objectGet(row, key); return v }
func rowString(row object, key string) string { s, _ := rowValue(row, key).(string); return s }

func numericValue(v any) (json.Number, bool) {
	switch x := v.(type) {
	case json.Number:
		return x, true
	case int:
		return json.Number(strconv.Itoa(x)), true
	case int64:
		return json.Number(strconv.FormatInt(x, 10)), true
	case uint32:
		return json.Number(strconv.FormatUint(uint64(x), 10)), true
	case float64:
		return json.Number(strconv.FormatFloat(x, 'g', -1, 64)), true
	default:
		return "", false
	}
}

func valueText(v any) string {
	if v == nil {
		return "None"
	}
	return fmt.Sprint(v)
}

func rowNumber(row object, key string) (json.Number, bool) {
	return numericValue(rowValue(row, key))
}

func dispatches(dir string, rows []object) []object {
	var out []object
	keys := make([]string, 0)
	for _, row := range rows {
		kind := rowString(row, "kind")
		if kind != "assignment" && kind != "uplift" {
			continue
		}
		recipient := rowString(row, "recipient")
		coding := memberRole(dir, recipient) == "Apprentice" || memberRole(dir, recipient) == "Coder"
		asg := rowValue(row, "asg")
		key := "id:" + valueText(rowValue(row, "id"))
		if asg != nil {
			key = fmt.Sprintf("asg:%v|", asg)
		}
		if coding {
			key += "coding"
		} else {
			key += recipient
		}
		found := -1
		for i, existing := range keys {
			if existing == key {
				found = i
				break
			}
		}
		if found >= 0 {
			out[found] = row
		} else {
			keys = append(keys, key)
			out = append(out, row)
		}
	}
	return out
}

func dispatchDone(dispatch object, rows []object) bool {
	for _, row := range rows {
		if rowString(row, "kind") != "result" || rowString(row, "asg") != rowString(dispatch, "asg") {
			continue
		}
		id, _ := rowNumber(row, "id")
		did, _ := rowNumber(dispatch, "id")
		if numberFloat(id) <= numberFloat(did) || rowString(row, "sender") != rowString(dispatch, "recipient") || rowString(row, "recipient") != rowString(dispatch, "sender") {
			continue
		}
		a, aok := rowNumber(row, "sender_incarnation")
		b, bok := rowNumber(dispatch, "recipient_incarnation")
		if aok && bok && numberFloat(a) == numberFloat(b) {
			return true
		}
	}
	return false
}

func upliftEvidence(dir string, source object, rows []object) map[string][]object {
	evidence := map[string][]object{}
	sourceID, _ := rowNumber(source, "id")
	sourceAsg := rowString(source, "asg")
	sourceSender := rowString(source, "sender")
	sourceRecipient := rowString(source, "recipient")
	sourceInc, _ := rowNumber(source, "recipient_incarnation")
	ownershipStart := numberFloat(sourceID)
	for i := len(rows) - 1; i >= 0; i-- {
		previous := rows[i]
		previousID, _ := rowNumber(previous, "id")
		kind := rowString(previous, "kind")
		role := memberRole(dir, rowString(previous, "recipient"))
		if numberFloat(previousID) >= ownershipStart || rowString(previous, "asg") != sourceAsg || (kind != "assignment" && kind != "uplift") || (role != "Apprentice" && role != "Coder") {
			continue
		}
		previousInc, _ := rowNumber(previous, "recipient_incarnation")
		if rowString(previous, "recipient") != sourceRecipient || numberFloat(previousInc) != numberFloat(sourceInc) || rowString(previous, "sender") != sourceSender {
			break
		}
		ownershipStart = numberFloat(previousID)
	}
	for _, row := range rows {
		id, _ := rowNumber(row, "id")
		inc, _ := rowNumber(row, "sender_incarnation")
		if numberFloat(id) > numberFloat(sourceID) && rowString(row, "asg") == sourceAsg && rowString(row, "sender") == sourceRecipient && rowString(row, "recipient") == sourceSender && numberFloat(inc) == numberFloat(sourceInc) && rowString(row, "kind") == "note" && strings.HasPrefix(rowString(row, "body"), "APPRENTICE-NEEDS-SENIOR") {
			evidence["needs-senior"] = append(evidence["needs-senior"], row)
		}
	}
	var failures []object
	for _, verification := range dispatches(dir, rows) {
		verificationID, _ := rowNumber(verification, "id")
		if numberFloat(verificationID) <= ownershipStart || memberRole(dir, rowString(verification, "recipient")) != "Tester" || rowString(verification, "coding_asg") != sourceAsg || rowString(verification, "sender") != sourceSender {
			continue
		}
		vid, _ := rowNumber(verification, "id")
		vinc, _ := rowNumber(verification, "recipient_incarnation")
		for _, row := range rows {
			rid, _ := rowNumber(row, "id")
			sinc, _ := rowNumber(row, "sender_incarnation")
			if rowString(row, "kind") == "result" && rowString(row, "asg") == rowString(verification, "asg") && numberFloat(rid) > numberFloat(vid) && rowString(row, "sender") == rowString(verification, "recipient") && numberFloat(sinc) == numberFloat(vinc) && rowString(row, "recipient") == sourceSender && strings.HasPrefix(rowString(row, "body"), "TESTED-FAIL") {
				failures = append(failures, row)
				break
			}
		}
	}
	if len(failures) >= 2 {
		evidence["tester-failed-twice"] = failures
	}
	if delivered, ok := rowNumber(source, "delivered_epoch"); ok && time.Since(time.Unix(int64(numberFloat(delivered)), int64((numberFloat(delivered)-math.Trunc(numberFloat(delivered)))*1e9))) >= 15*time.Minute && !dispatchDone(source, rows) {
		evidence["timeout"] = []object{object{{"id", sourceID}, {"delivered_epoch", delivered}, {"body", rowValue(source, "body")}}}
	}
	return evidence
}

func inboxView(dir, label string) (object, error) {
	view := object{{"inbound_unacked", []any{}}, {"outbound_unsent", []any{}}, {"active_assignments", []any{}}}
	human := label == humanLabel(dir)
	if human {
		view = append(view, pair{"uplift_counts", object{{"crew", crewName(dir)}, {"total", 0}, {"by_reason", object{{"needs-senior", 0}, {"tester-failed-twice", 0}, {"timeout", 0}}}}}, pair{"uplift_due", []any{}})
	}
	rows, err := readMessages(dir)
	if err != nil {
		return nil, err
	}
	current, currentOK := currentIncarnation(dir, label)
	inbound := view[0].val.([]any)
	for _, row := range rows {
		state := rowString(row, "state")
		if rowString(row, "recipient") == label && (state == "stored" || state == "sent") && sameNumber(current, currentOK, rowValue(row, "recipient_incarnation")) {
			inbound = append(inbound, row)
		}
	}
	view[0].val = inbound
	outbound := view[1].val.([]any)
	for _, row := range rows {
		if rowString(row, "sender") == label && rowString(row, "state") == "stored" {
			enriched := append(object{}, row...)
			enriched = append(enriched, pair{"header", fmt.Sprintf("msg:%s kind:%s asg:%s", valueText(rowValue(row, "id")), rowString(row, "kind"), asgText(rowValue(row, "asg")))}, pair{"to", meshName(dir, rowString(row, "recipient"))})
			outbound = append(outbound, enriched)
		}
	}
	view[1].val = outbound
	active := view[2].val.([]any)
	for _, row := range dispatches(dir, rows) {
		if rowString(row, "sender") != label && rowString(row, "recipient") != label {
			continue
		}
		if dispatchDone(row, rows) {
			continue
		}
		sourceInc, sourceOK := rowNumber(row, "recipient_incarnation")
		currentInc, currentOK := currentIncarnationFor(dir, rowString(row, "recipient"))
		stale := !sameNumber(sourceInc, sourceOK, numberOrNil(currentInc, currentOK))
		enriched := append(object{}, row...)
		enriched = append(enriched, pair{"stale", stale})
		active = append(active, enriched)
	}
	view[2].val = active
	if human {
		counts := view[3].val.(object)
		transfers := map[string]object{}
		for _, row := range rows {
			if rowString(row, "kind") == "uplift" && rowString(row, "sender") == label && rowValue(row, "source_dispatch_id") != nil && contains(upliftReasons, rowString(row, "reason")) {
				transfers[rowString(row, "source_dispatch_id")] = row
			}
		}
		for i := range counts {
			if counts[i].key == "total" {
				counts[i].val = len(transfers)
			}
			if counts[i].key == "by_reason" {
				br := counts[i].val.(object)
				for i := range br {
					n := 0
					for _, row := range transfers {
						if rowString(row, "reason") == br[i].key {
							n++
						}
					}
					br[i].val = n
				}
			}
		}
		due := view[4].val.([]any)
		for _, source := range dispatches(dir, rows) {
			if memberRole(dir, rowString(source, "recipient")) != "Apprentice" {
				continue
			}
			inc, ok := currentIncarnation(dir, rowString(source, "recipient"))
			srcInc, srcOK := rowNumber(source, "recipient_incarnation")
			if !ok || !srcOK || numberFloat(inc) != numberFloat(srcInc) {
				continue
			}
			ev := upliftEvidence(dir, source, rows)
			if len(ev) == 0 {
				continue
			}
			reasons := []any{}
			for _, reason := range upliftReasons {
				if _, ok := ev[reason]; ok {
					reasons = append(reasons, reason)
				}
			}
			due = append(due, object{{"asg", rowValue(source, "asg")}, {"source_dispatch_id", rowValue(source, "id")}, {"reasons", reasons}})
		}
		view[4].val = due
	}
	return view, nil
}

func currentIncarnationFor(dir, label string) (json.Number, bool) {
	return currentIncarnation(dir, label)
}
func numberOrNil(n json.Number, ok bool) any {
	if !ok {
		return nil
	}
	return n
}
func asgText(v any) string {
	if v == nil {
		return "-"
	}
	if vstr, ok := v.(string); ok && vstr == "" {
		return "-"
	}
	return valueText(v)
}
func contains(items []string, wanted string) bool {
	for _, item := range items {
		if item == wanted {
			return true
		}
	}
	return false
}
func crewName(dir string) string {
	crew, _ := readObject(filepath.Join(dir, "crew.json"))
	if s := objectString(crew, "crew"); s != "" {
		return s
	}
	return filepath.Base(dir)
}

func compactJSON(v any) string { return renderJSON(v, false, false) }
func pyRepr(s string) string {
	var b strings.Builder
	b.WriteByte('\'')
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '\'':
			b.WriteString(`\'`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('\'')
	return b.String()
}

func oneLine(v any) string {
	if v == nil {
		return ""
	}
	return strings.Join(strings.Fields(fmt.Sprint(v)), " ")
}

func formatEvent(v object) string {
	seq := "?"
	if n, ok := objectNumber(v, "seq"); ok {
		seq = n.String()
	} else if value, ok := objectGet(v, "seq"); ok {
		seq = valueText(value)
	}
	ts := valueText(objectValue(v, "ts"))
	if _, ok := objectGet(v, "ts"); !ok {
		ts = ""
	}
	if len(ts) >= 19 {
		ts = ts[11:19]
	}
	event := valueText(objectValue(v, "event"))
	if _, ok := objectGet(v, "event"); !ok {
		event = "?"
	}
	head := fmt.Sprintf("#%s %s %s", seq, ts, event)
	switch event {
	case "msg":
		return fmt.Sprintf("%s %s->%s %s asg:%s msg:%s  %s", head, valueText(objectValue(v, "sender")), valueText(objectValue(v, "recipient")), valueText(objectValue(v, "kind")), asgText(objectValue(v, "asg")), valueText(objectValue(v, "id")), oneLine(objectValue(v, "body")))
	case "sent":
		return fmt.Sprintf("%s msg:%s", head, valueText(objectValue(v, "id")))
	case "ack":
		s := fmt.Sprintf("%s msg:%s by %s: %s", head, valueText(objectValue(v, "id")), valueText(objectValue(v, "by")), valueText(objectValue(v, "outcome")))
		if reason := objectString(v, "reason"); reason != "" {
			s += " (" + reason + ")"
		}
		return s
	case "start":
		return fmt.Sprintf("%s ticket:%s crew:%s members:%s cwd:%s", head, valueOrDash(objectValue(v, "ticket")), valueText(objectValue(v, "crew")), membersText(objectValue(v, "members")), valueText(objectValue(v, "cwd")))
	case "spawn":
		return fmt.Sprintf("%s %s slot:%s tool:%s %s", head, valueText(objectValue(v, "role")), valueText(objectValue(v, "slot")), valueText(objectValue(v, "tool")), valueText(objectValue(v, "identity")))
	case "down":
		return fmt.Sprintf("%s stopped:%s lock_released:%s", head, valueOrDash(stringArrayText(objectValue(v, "stopped"))), valueOrDash(stringArrayText(objectValue(v, "lock_released"))))
	default:
		rest := object{}
		for _, p := range v {
			if p.key != "seq" && p.key != "ts" && p.key != "event" {
				rest = append(rest, p)
			}
		}
		return head + " " + compactJSON(rest)
	}
}

func objectValue(o object, key string) any { return rowValue(o, key) }
func valueOrDash(v any) string {
	if v == nil {
		return "-"
	}
	s := valueText(v)
	if s == "" {
		return "-"
	}
	return s
}
func membersText(v any) string {
	var out []string
	if a, ok := v.([]any); ok {
		for _, x := range a {
			out = append(out, valueText(x))
		}
	}
	return strings.Join(out, ",")
}
func stringArray(v any) []string {
	var out []string
	if a, ok := v.([]any); ok {
		for _, x := range a {
			out = append(out, valueText(x))
		}
	}
	return out
}
func stringArrayText(v any) string { return strings.Join(stringArray(v), ",") }

func readComplete(path string, offset int64) ([]string, int64, bool) {
	f, err := os.Open(path)
	if err != nil {
		return nil, offset, false
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return nil, offset, false
	}
	data, _ := io.ReadAll(f)
	end := bytes.LastIndexByte(data, '\n') + 1
	if end == 0 {
		return nil, offset, true
	}
	parts := bytes.Split(data[:end], []byte{'\n'})
	lines := make([]string, 0, len(parts))
	for _, raw := range parts[:len(parts)-1] {
		line := strings.TrimSuffix(strings.ToValidUTF8(string(raw), "�"), "\r")
		if strings.TrimSpace(line) == "" {
			continue
		}
		lines = append(lines, line)
	}
	return lines, offset + int64(end), true
}

func longOption(arg string, options []string) string {
	if !strings.HasPrefix(arg, "--") || arg == "--" {
		return arg
	}
	for _, option := range options {
		if strings.HasPrefix(option, arg) {
			return option
		}
	}
	return arg
}

func positionalArgs(args, required []string) ([]string, bool, string) {
	var positional, extra []string
	terminated := false
	for _, arg := range args {
		if !terminated && arg == "--" {
			terminated = true
			continue
		}
		if !terminated && (arg == "-h" || longOption(arg, []string{"--help"}) == "--help") {
			return nil, true, ""
		}
		if (!terminated && strings.HasPrefix(arg, "-") && arg != "-") || len(positional) == len(required) {
			extra = append(extra, arg)
		} else {
			positional = append(positional, arg)
		}
	}
	if len(positional) < len(required) {
		return nil, false, "the following arguments are required: " + strings.Join(required[len(positional):], ", ")
	}
	if len(extra) != 0 {
		return nil, false, "unrecognized arguments: " + strings.Join(extra, " ")
	}
	return positional, false, ""
}

func logCommand(args []string) int {
	shift := ""
	follow := false
	raw := false
	since := 0
	terminated := false
	var extra []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if !terminated && a == "--" {
			terminated = true
			continue
		}
		if terminated {
			if shift == "" {
				shift = a
			} else {
				extra = append(extra, a)
			}
			continue
		}
		option, value, hasValue := strings.Cut(a, "=")
		option = longOption(option, []string{"--help", "--follow", "--since", "--json"})
		if hasValue && contains([]string{"-h", "--help", "-f", "--follow", "--json"}, option) {
			return usageLog(fmt.Sprintf("argument %s: ignored explicit argument %s", option, pyRepr(value)))
		}
		switch option {
		case "-h", "--help":
			out(helpLog())
			return 0
		case "-f", "--follow":
			follow = true
		case "--json":
			raw = true
		case "--since":
			if !hasValue {
				if i+1 >= len(args) || strings.HasPrefix(args[i+1], "--") || args[i+1] == "-h" || args[i+1] == "-f" {
					return usageLog("argument --since: expected one argument")
				}
				i++
				value = args[i]
			}
			n, err := strconv.Atoi(value)
			if err != nil {
				return usageLog(fmt.Sprintf("argument --since: invalid int value: %s", pyRepr(value)))
			}
			since = n
		default:
			if strings.HasPrefix(a, "-") && a != "-" {
				extra = append(extra, a)
			} else if shift == "" {
				shift = a
			} else {
				extra = append(extra, a)
			}
		}
	}
	if shift == "" {
		return usageLog("the following arguments are required: shift")
	}
	if len(extra) != 0 {
		return usageLog("unrecognized arguments: " + strings.Join(extra, " "))
	}
	dir, err := resolveShift(shift)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	path := filepath.Join(dir, "bus.jsonl")
	var offset int64
	warned := false
	for {
		if !fileExists(path) {
			if !warned {
				errLine("no bus yet: " + path)
				warned = true
			}
			if !follow {
				return 0
			}
			time.Sleep(logPoll)
			continue
		}
		lines, next, _ := readComplete(path, offset)
		offset = next
		done := false
		for _, line := range lines {
			v, err := parseJSON([]byte(line))
			o, ok := v.(object)
			if err != nil || !ok {
				if raw {
					outLine(line)
				} else {
					outLine("? " + line)
				}
				continue
			}
			if objectString(o, "event") == "down" {
				done = true
			}
			seq, _ := objectGet(o, "seq")
			skip := false
			switch n := seq.(type) {
			case bool:
				value := 0
				if n {
					value = 1
				}
				skip = value <= since
			case json.Number:
				if !strings.ContainsAny(n.String(), ".eE") {
					value, err := n.Int64()
					if err == nil {
						skip = value <= int64(since)
					} else {
						skip = strings.HasPrefix(n.String(), "-")
					}
				}
			}
			if skip {
				continue
			}
			if raw {
				outLine(line)
			} else if len(o) == 0 {
				outLine("? " + line)
			} else {
				outLine(formatEvent(o))
			}
		}
		if done || !follow {
			return 0
		}
		time.Sleep(logPoll)
	}
}

func usageLog(msg string) int {
	errLine("usage: nightshift log [-h] [--follow] [--since N] [--json] shift")
	errLine("nightshift log: error: " + msg)
	return 2
}
func helpLog() string {
	return "usage: nightshift log [-h] [--follow] [--since N] [--json] shift\n\nPrint a shift's bus (<state_dir>/bus.jsonl) in order: every mailbox message,\nsent and ack, and the start, spawn, result and down events. Reads the file\nonly; there is no bus process. --follow keeps watching until Ctrl+C, or until\nthe shift's down event has been printed.\n\npositional arguments:\n  shift         shift name, unique suffix, or \"latest\"\n\noptions:\n  -h, --help    show this help message and exit\n  --follow, -f  keep printing new events as they are appended\n  --since N     only events with seq greater than N\n  --json        print the raw JSON lines\n"
}

func statusCommand(args []string) int {
	if contains(args, "--help") || contains(args, "-h") {
		out("usage: nightshift status [-h] [--all]\n\nList shifts and whether their crews are alive.\n\noptions:\n  -h, --help  show this help message and exit\n  --all       include shifts whose crew is gone\n")
		return 0
	}
	all := false
	for _, a := range args {
		if a == "--all" {
			all = true
		} else {
			return usageStatus("unrecognized arguments: " + a)
		}
	}
	dirs := shiftDirs()
	records := []any{}
	for _, d := range dirs {
		r, err := statusRecord(d)
		if err != nil {
			errLine(err.Error())
			return 1
		}
		if all || objectBool(r, "alive") {
			records = append(records, r)
		}
	}
	outLine(renderJSON(records, true, true))
	return 0
}
func usageStatus(msg string) int {
	errLine("usage: nightshift status [-h] [--all]")
	errLine("nightshift status: error: " + msg)
	return 2
}

func claimsCommand(args []string) int {
	positional, help, parseError := positionalArgs(args, []string{"shift"})
	if help {
		out("usage: nightshift claims [-h] shift\n\nPrint a shift's file claims as JSON.\n\npositional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n\noptions:\n  -h, --help  show this help message and exit\n")
		return 0
	}
	if parseError != "" {
		return usageClaims(parseError)
	}
	dir, err := resolveShift(positional[0])
	if err != nil {
		errLine(err.Error())
		return 1
	}
	outLine(renderJSON(readClaims(dir), true, true))
	return 0
}
func usageClaims(msg string) int {
	errLine("usage: nightshift claims [-h] shift")
	errLine("nightshift claims: error: " + msg)
	return 2
}

func inboxCommand(args []string) int {
	positional, help, parseError := positionalArgs(args, []string{"shift", "role"})
	if help {
		out("usage: nightshift inbox [-h] shift role\n\nA member's mailbox as JSON: inbound_unacked, outbound_unsent (send each, then\n`msg sent`), active_assignments (stale = sent to an earlier incarnation of the\nrecipient).\n\npositional arguments:\n  shift       shift name, unique suffix, or \"latest\"\n  role        your crew label (Foreman, Coder) or session name\n\noptions:\n  -h, --help  show this help message and exit\n")
		return 0
	}
	if parseError != "" {
		return usageInbox(parseError)
	}
	dir, err := resolveShift(positional[0])
	if err != nil {
		errLine(err.Error())
		return 1
	}
	label, _, err := canonicalLabel(dir, positional[1])
	if err != nil {
		errLine(err.Error())
		return 1
	}
	view, err := inboxView(dir, label)
	if err != nil {
		errLine(err.Error())
		return 1
	}
	outLine(renderJSON(view, true, true))
	return 0
}
func usageInbox(msg string) int {
	errLine("usage: nightshift inbox [-h] shift role")
	errLine("nightshift inbox: error: " + msg)
	return 2
}

func main() {
	if len(os.Args) < 2 {
		return
	}
	var code int
	switch os.Args[1] {
	case "status":
		code = statusCommand(os.Args[2:])
	case "claims":
		code = claimsCommand(os.Args[2:])
	case "inbox":
		code = inboxCommand(os.Args[2:])
	case "log":
		code = logCommand(os.Args[2:])
	default:
		errLine("unknown command: " + os.Args[1])
		code = 1
	}
	os.Exit(code)
}
