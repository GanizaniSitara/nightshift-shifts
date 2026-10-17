package main

// Crew manifests, briefs, personas and per-tool argv: the launcher core shared
// by start, spawn, pane and the warden/gaffer recovery paths. Ported from
// nightshift_shifts.py (MODEL .. tool_command), which stays the oracle.

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v2"
	nightshiftassets "nightshift-readonly"
)

const (
	launcherModel       = "claude-opus-5-5"
	launcherProfile     = "Nightshift Crew"
	campbellBackground  = "#0C0C0C"
	startPermissionMode = "auto"
	copilotMinCredits   = 30
	defaultRequires     = "tested"
	terminalProfileGUID = "{41cc0613-92f0-4c15-967d-9490faec2f94}"
	foremanStartMessage = "Begin the assignment: the task text is at the end of your role instructions, after \"The human task for this shift follows, verbatim:\"."
	humanTaskMarker     = "The human task for this shift follows, verbatim:"
	advisorLabel        = "Advisor"
	coderLabel          = "Coder"
)

var (
	roleBackgrounds = []pair{{"red", "#291719"}, {"purple", "#241A30"}, {"green", "#15291E"}, {"blue", "#172339"}, {"yellow", "#2B2614"}}
	campbellScheme  = object{
		{"background", campbellBackground}, {"foreground", "#CCCCCC"}, {"cursorColor", "#FFFFFF"},
		{"selectionBackground", "#FFFFFF"}, {"black", "#0C0C0C"}, {"red", "#C50F1F"}, {"green", "#13A10E"},
		{"yellow", "#C19C00"}, {"blue", "#0037DA"}, {"purple", "#881798"}, {"cyan", "#3A96DD"}, {"white", "#CCCCCC"},
		{"brightBlack", "#767676"}, {"brightRed", "#E74856"}, {"brightGreen", "#16C60C"}, {"brightYellow", "#F9F1A5"},
		{"brightBlue", "#3B78FF"}, {"brightPurple", "#B4009E"}, {"brightCyan", "#61D6D6"}, {"brightWhite", "#F2F2F2"},
	}
	startMessages = map[string]string{
		"Foreman":    foremanStartMessage,
		"Coder":      "Begin your Coder role: send READY to Foreman and await assignments.",
		"Tester":     "Begin your Tester role: send READY to Foreman and await verification assignments.",
		"Apprentice": "Begin your primary coding role: send READY to Foreman and await assignments.",
		"Advisor":    "You are the Advisor and dormant: do not send READY, do not poll, read or run anything; wait silently until Foreman messages you.",
	}
	crewTools             = []string{"claude", "agy", "codex", "copilot"}
	layoutNames           = []string{"split", "1x4", "2x4"}
	layoutSlots           = map[string]int{"split": 0, "1x4": 4, "2x4": 8}
	reasoningLevels       = []string{"low", "medium", "high", "xhigh"}
	toolReasoningLevels   = map[string][]string{"copilot": {"none", "minimal", "low", "medium", "high", "xhigh", "max"}}
	contextTiers          = []string{"default", "long_context"}
	contextTools          = []string{"copilot"}
	permissionNames       = []string{"skip", "auto", "default"}
	approveAllFlags       = map[string][]string{"agy": {"--dangerously-skip-permissions"}, "codex": {"--dangerously-bypass-approvals-and-sandbox"}, "copilot": {"--allow-all"}}
	personaLists          = [][2]string{{"good_looks_like", "What good looks like"}, {"how_you_work", "How you work"}, {"never", "What you never do"}}
	roleNamePattern       = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]*$`)
	compactionUnsupported = map[string]string{
		"codex":   "model_auto_compact_token_limit is a threshold; no verified disable equivalent.",
		"copilot": "Compaction thresholds do not prevent forced compaction after a context-limit error.",
		"agy":     "No verified disable switch in installed Antigravity help or official CLI documentation.",
	}
)

// The launcher's assets (crews/, briefs/, personas/), per kind: the
// NIGHTSHIFT_<KIND>_DIR folder, else <exe dir>/<kind>, else <exe dir>/../<kind>
// (build/nightshift.exe in a checkout), else the copy embedded in the binary.
// Repository .nightshift/ layers keep their precedence over all of these.
func assetDir(kind string) (string, fs.FS) {
	if dir := os.Getenv("NIGHTSHIFT_" + strings.ToUpper(kind) + "_DIR"); dir != "" {
		return dir, nil
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		for _, d := range []string{dir, filepath.Dir(dir)} {
			if info, err := os.Stat(filepath.Join(d, kind)); err == nil && info.IsDir() {
				return pyResolve(filepath.Join(d, kind)), nil
			}
		}
	}
	sub, _ := fs.Sub(nightshiftassets.Files, kind)
	return "<embedded>/" + kind, sub
}

func assetPath(kind, name string) string {
	dir, embedded := assetDir(kind)
	if embedded != nil {
		return dir + "/" + name
	}
	return filepath.Join(dir, name)
}

func assetIsFile(kind, name string) bool {
	dir, embedded := assetDir(kind)
	if embedded == nil {
		return isFile(filepath.Join(dir, name))
	}
	info, err := fs.Stat(embedded, name)
	return fs.ValidPath(name) && err == nil && info.Mode().IsRegular()
}

func assetRead(kind, name string) (string, error) {
	dir, embedded := assetDir(kind)
	if embedded == nil {
		return readTextUniversal(filepath.Join(dir, name))
	}
	data, err := fs.ReadFile(embedded, name)
	if err != nil {
		return "", err
	}
	return universalNewlines(string(data)), nil
}

// assetStems lists the names of the *.yaml files of a kind, without extension.
func assetStems(kind string) []string {
	dir, embedded := assetDir(kind)
	var names []string
	if embedded == nil {
		matches, _ := filepath.Glob(filepath.Join(dir, "*.yaml"))
		for _, m := range matches {
			names = append(names, filepath.Base(m))
		}
	} else {
		names, _ = fs.Glob(embedded, "*.yaml")
	}
	stems := []string{}
	for _, n := range names {
		stems = append(stems, strings.TrimSuffix(n, filepath.Ext(n)))
	}
	sort.Strings(stems)
	return stems
}

// launcherExe is what the panes and slots run: this binary, never Python
// (Foreman decision, msg 9).
func launcherExe() string {
	exe, err := os.Executable()
	if err != nil {
		return os.Args[0]
	}
	return exe
}

// nightshiftCommand is the {nightshift} brief placeholder: the exact command,
// with forward slashes because Git Bash strips unquoted backslashes.
func nightshiftCommand() string { return filepath.ToSlash(launcherExe()) }

func statuslineCmd() string    { return nightshiftCommand() + " statusline" }
func agyStatuslineCmd() string { return statuslineCmd() }

func toolExe(tool string) string {
	if p, err := exec.LookPath(tool); err == nil {
		return p
	}
	switch tool {
	case "agy":
		return filepath.Join(os.Getenv("LOCALAPPDATA"), "Microsoft", "WinGet", "Links", "agy.exe")
	case "codex":
		return filepath.Join(os.Getenv("APPDATA"), "npm", "node_modules", "@openai", "codex", "node_modules", "@openai", "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin", "codex.exe")
	case "copilot":
		return filepath.Join(os.Getenv("APPDATA"), "npm", "node_modules", "@github", "copilot", "node_modules", "@github", "copilot-win32-x64", "copilot.exe")
	}
	return ""
}

// pyResolve mirrors pathlib's non-strict Path.resolve() on Windows: the real
// path of the longest existing prefix, with the rest appended, case kept.
func pyResolve(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	current := filepath.Clean(abs)
	missing := []string{}
	for {
		if _, err := os.Stat(current); err == nil {
			resolved, err := finalPathByHandle(current)
			if err != nil {
				return abs
			}
			if strings.HasPrefix(resolved, `\\?\UNC\`) {
				resolved = `\\` + resolved[8:]
			} else if strings.HasPrefix(resolved, `\\?\`) {
				resolved = resolved[4:]
			}
			for i := len(missing) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, missing[i])
			}
			return resolved
		}
		parent := filepath.Dir(current)
		if parent == current {
			return filepath.Clean(abs)
		}
		missing = append(missing, filepath.Base(current))
		current = parent
	}
}

// pathWithin is pathlib's is_relative_to under Windows case folding.
func pathWithin(path, root string) bool {
	rel, err := filepath.Rel(strings.ToLower(root), strings.ToLower(path))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// readTextUniversal is Path.read_text: UTF-8 with universal newlines.
func readTextUniversal(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return universalNewlines(string(data)), nil
}

func universalNewlines(text string) string {
	text = strings.ToValidUTF8(text, "\ufffd")
	return strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", "\n"), "\r", "\n")
}

// writeTextFile is Path.write_text: UTF-8 with \n written as \r\n.
func writeTextFile(path, text string) error {
	return os.WriteFile(path, []byte(strings.ReplaceAll(text, "\n", "\r\n")), 0666)
}

func canonicalRepoRoot(cwd, stateDir string) string {
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	cwd = pyResolve(cwd)
	if stateDir != "" {
		if info, _ := worktreeInfo(stateDir); info != nil {
			return pyResolve(objectString(info, "repo_root"))
		}
	}
	for _, dir := range shiftDirs() {
		info, _ := worktreeInfo(dir)
		if info != nil && pathWithin(cwd, pyResolve(objectString(info, "worktree_dir"))) {
			return pyResolve(objectString(info, "repo_root"))
		}
	}
	return cwd
}

// yamlJSON turns a decoded YAML value into the JSON value json.dumps prints:
// mappings keep their first-seen key order with the last value, as a dict does.
func yamlJSON(v any) any {
	switch x := v.(type) {
	case yaml.MapSlice:
		out := object{}
		for _, p := range x {
			out = setValue(out, yamlKeyText(p.Key), yamlJSON(p.Value))
		}
		return out
	case []any:
		items := make([]any, len(x))
		for i, item := range x {
			items[i] = yamlJSON(item)
		}
		return items
	case uint64:
		return json.Number(strconv.FormatUint(x, 10))
	}
	return v
}

func yamlKeyText(k any) string {
	switch x := k.(type) {
	case string:
		return x
	case bool:
		if x {
			return "true"
		}
		return "false"
	case nil:
		return "null"
	case float64:
		return floatText(x)
	}
	return fmt.Sprint(k)
}

// pyStr is str() of a YAML scalar.
func pyStr(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case nil:
		return "None"
	case float64:
		return floatText(x)
	}
	return fmt.Sprint(v)
}

// pyReprValue is repr() of a YAML scalar.
func pyReprValue(v any) string {
	if s, ok := v.(string); ok {
		return pyRepr(s)
	}
	return pyStr(v)
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case json.Number:
		f, err := x.Float64()
		return err != nil || f != 0
	case uint64:
		return x != 0
	case float64:
		return x != 0
	case yaml.MapSlice:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}

func yamlInt(v any) (int, bool) {
	switch x := v.(type) {
	case int:
		return x, true
	case int64:
		return int(x), true
	}
	return 0, false
}

func loadRepoConfig(repoRoot string) (object, error) {
	path := filepath.Join(repoRoot, ".nightshift", "config.yaml")
	if !fileExists(path) {
		return nil, nil
	}
	if _, _, err := loadBuildConfig(repoRoot); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("%s: %s", path, err)
	}
	var m yaml.MapSlice
	if err := yaml.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("%s: %s", path, err)
	}
	return yamlJSON(m).(object), nil
}

func configString(config object, key string) string {
	s, _ := rowValue(config, key).(string)
	return s
}

type crewRole struct {
	role, brief, colour, tool, model string
	persona                          any
	count                            int
	human                            bool
	opts                             object // reasoning, context, account, permissions, max_credits, in that order
}

type crewManifest struct {
	name, model, permissionMode, contract, layout string
	slots                                         int
	roles                                         []*crewRole
	maxCredits                                    any
	overrides                                     object // start's overrides as recorded; nil when none
}

type crewMember struct {
	role, label, identity, brief, colour, tool, model, peer string
	persona                                                 any
	human, replacement                                      bool
	opts                                                    object
}

func (m crewMember) opt(key string) any { return rowValue(m.opts, key) }
func (m crewMember) has(key string) bool {
	_, ok := objectGet(m.opts, key)
	return ok
}
func (m crewMember) optString(key string) string { s, _ := m.opt(key).(string); return s }

func (m crewMember) object() object {
	o := object{{"role", m.role}, {"label", m.label}, {"identity", m.identity}, {"brief", m.brief}, {"persona", m.persona},
		{"colour", m.colour}, {"human_contact", m.human}, {"tool", m.tool}, {"model", m.model}}
	o = append(o, m.opts...)
	return append(o, pair{"peer", m.peer})
}

func yamlScalar(m yaml.MapSlice, key string) any { v, _ := yamlValue(m, key); return v }

// crewSource is one visible manifest file: machine (<state root>/crews), repo
// (<repo>/.nightshift/crews) or built-in (crews/, possibly embedded).
type crewSource struct {
	name, source, path string
	read               func() (string, error)
}

// crewSources lists every visible manifest, by name, each name's sources in
// lookup order (machine > repo > built-in): the first one is what --crew picks.
func crewSources(repoRoot string) []crewSource {
	byName := map[string][]crewSource{}
	add := func(dir, source string) {
		matches, _ := filepath.Glob(filepath.Join(dir, "*.yaml"))
		for _, m := range matches {
			if path := m; isFile(path) {
				name := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
				byName[name] = append(byName[name], crewSource{name, source, path, func() (string, error) { return readTextUniversal(path) }})
			}
		}
	}
	add(filepath.Join(stateRoot(), "crews"), "machine")
	if repoRoot != "" {
		add(filepath.Join(repoRoot, ".nightshift", "crews"), "repo")
	}
	for _, stem := range assetStems("crews") {
		file := stem + ".yaml"
		byName[stem] = append(byName[stem], crewSource{stem, "built-in", assetPath("crews", file), func() (string, error) { return assetRead("crews", file) }})
	}
	names := []string{}
	for name := range byName {
		names = append(names, name)
	}
	sort.Strings(names)
	sources := []crewSource{}
	for _, name := range names {
		sources = append(sources, byName[name]...)
	}
	return sources
}

// crewOverrides are start's --crew-file/--without/--role/--max-credits,
// applied to the manifest's YAML before validation.
type crewOverrides struct {
	crewFile   string
	without    []string
	roles      []string
	maxCredits string // the raw --max-credits value; "" = not given
}

// overridesFromObject reads the "overrides" a started shift recorded in crew.json.
func overridesFromObject(o object) *crewOverrides {
	if o == nil {
		return nil
	}
	ov := &crewOverrides{crewFile: objectString(o, "crew_file")}
	for _, item := range anyList(rowValue(o, "without")) {
		ov.without = append(ov.without, valueText(item))
	}
	for _, item := range anyList(rowValue(o, "role")) {
		ov.roles = append(ov.roles, valueText(item))
	}
	if v := rowValue(o, "max_credits"); v != nil {
		ov.maxCredits = valueText(v)
	}
	return ov
}

func anyList(v any) []any { items, _ := v.([]any); return items }

// record is the "overrides" object for crew.json, the bus start event and the
// dry-run plan; machineFile is the manifest path when --crew found it in the
// machine folder. nil when nothing was overridden.
func (ov *crewOverrides) record(machineFile string) object {
	o := object{}
	if ov == nil {
		ov = &crewOverrides{}
	}
	if ov.crewFile != "" {
		o = append(o, pair{"crew_file", ov.crewFile})
	} else if machineFile != "" {
		o = append(o, pair{"crew_file", machineFile})
	}
	if len(ov.without) > 0 {
		o = append(o, pair{"without", stringsToAny(ov.without)})
	}
	if len(ov.roles) > 0 {
		o = append(o, pair{"role", stringsToAny(ov.roles)})
	}
	if ov.maxCredits != "" {
		n, _ := parseInteger(ov.maxCredits)
		o = append(o, pair{"max_credits", int(n)})
	}
	if len(o) == 0 {
		return nil
	}
	return o
}

func yamlSet(m yaml.MapSlice, key string, value any) yaml.MapSlice {
	out := append(yaml.MapSlice{}, m...)
	for i, item := range out {
		if yamlKeyText(item.Key) == key {
			out[i].Value = value
			return out
		}
	}
	return append(out, yaml.MapItem{Key: key, Value: value})
}

func yamlDelete(m yaml.MapSlice, key string) yaml.MapSlice {
	out := yaml.MapSlice{}
	for _, item := range m {
		if yamlKeyText(item.Key) != key {
			out = append(out, item)
		}
	}
	return out
}

func entryTool(entry yaml.MapSlice) string {
	if t := yamlScalar(entry, "tool"); truthy(t) {
		return pyStr(t)
	}
	return "claude"
}

// applyCrewOverrides drops --without roles, rewrites --role roles and sets or
// drops max_credits, leaving every other check to loadCrew's validation.
func applyCrewOverrides(name string, data yaml.MapSlice, roles []any, ov *crewOverrides) (yaml.MapSlice, []any, error) {
	roleIndex := func(entries []any, role string) int {
		for i, raw := range entries {
			if entry, ok := raw.(yaml.MapSlice); ok && truthy(yamlScalar(entry, "role")) && strings.EqualFold(pyStr(yamlScalar(entry, "role")), role) {
				return i
			}
		}
		return -1
	}
	roleNames := func(entries []any) string {
		names := []string{}
		for _, raw := range entries {
			if entry, ok := raw.(yaml.MapSlice); ok && truthy(yamlScalar(entry, "role")) {
				names = append(names, pyStr(yamlScalar(entry, "role")))
			}
		}
		return strings.Join(names, ", ")
	}
	dropped := map[int]bool{}
	for _, w := range ov.without {
		i := roleIndex(roles, w)
		if i < 0 {
			return nil, nil, fmt.Errorf("--without %s: crew %s has no role %s; roles: %s", w, name, w, roleNames(roles))
		}
		if yamlScalar(roles[i].(yaml.MapSlice), "human_contact") == true {
			return nil, nil, fmt.Errorf("--without %s: the human_contact role cannot be dropped", w)
		}
		dropped[i] = true
	}
	kept := []any{}
	for i, raw := range roles {
		if !dropped[i] {
			kept = append(kept, raw)
		}
	}
	given := map[string]bool{}
	for _, spec := range ov.roles {
		role, rest, ok := strings.Cut(spec, "=")
		parts := strings.Split(rest, ":")
		valid := ok && role != "" && len(parts) >= 2 && len(parts) <= 3
		for _, p := range parts {
			valid = valid && p != ""
		}
		if !valid {
			return nil, nil, fmt.Errorf("--role %s: use ROLE=TOOL:MODEL[:REASONING]", spec)
		}
		i := roleIndex(kept, role)
		if i < 0 {
			return nil, nil, fmt.Errorf("--role %s: crew %s has no role %s; roles: %s", spec, name, role, roleNames(kept))
		}
		entry := kept[i].(yaml.MapSlice)
		if key := strings.ToLower(pyStr(yamlScalar(entry, "role"))); given[key] {
			return nil, nil, fmt.Errorf("--role %s: role %s is given twice", spec, pyStr(yamlScalar(entry, "role")))
		} else {
			given[key] = true
		}
		oldTool, tool := entryTool(entry), parts[0]
		entry = yamlSet(yamlSet(entry, "tool", tool), "model", parts[1])
		if len(parts) == 3 {
			entry = yamlSet(entry, "reasoning", parts[2])
		} else {
			entry = yamlDelete(entry, "reasoning")
		}
		if tool != oldTool {
			if !contains(contextTools, tool) {
				entry = yamlDelete(entry, "context")
			}
			brief := pyStr(yamlScalar(entry, "brief"))
			if oldTool == "claude" && !strings.HasSuffix(brief, "-cli.md") {
				if cli := strings.TrimSuffix(brief, ".md") + "-cli.md"; assetIsFile("briefs", cli) {
					entry = yamlSet(entry, "brief", cli)
				}
			} else if tool == "claude" && strings.HasSuffix(brief, "-cli.md") {
				if plain := strings.TrimSuffix(brief, "-cli.md") + ".md"; assetIsFile("briefs", plain) {
					entry = yamlSet(entry, "brief", plain)
				}
			}
		}
		kept[i] = entry
	}
	if ov.maxCredits != "" {
		if n, err := parseInteger(ov.maxCredits); err == nil {
			data = yamlSet(data, "max_credits", int(n))
		} else {
			data = yamlSet(data, "max_credits", ov.maxCredits)
		}
	} else if len(ov.without)+len(ov.roles) > 0 && yamlScalar(data, "max_credits") != nil {
		copilot := false
		for _, raw := range kept {
			if entry, ok := raw.(yaml.MapSlice); ok && entryTool(entry) == "copilot" {
				copilot = true
			}
		}
		if !copilot {
			data = yamlDelete(data, "max_credits")
		}
	}
	return data, kept, nil
}

// loadCrew reads crew NAME by the lookup order machine > repo > built-in.
func loadCrew(name, repoRoot string) (*crewManifest, error) { return loadCrewWith(name, repoRoot, nil) }

// loadSavedCrew reloads a started shift's effective crew from its crew.json:
// the recorded crew_file when there is one, with the recorded overrides.
func loadSavedCrew(saved object, repoRoot string) (*crewManifest, error) {
	name := objectString(saved, "crew")
	if name == "" {
		name = "default"
	}
	ov, _ := rowValue(saved, "overrides").(object)
	return loadCrewWith(name, repoRoot, overridesFromObject(ov))
}

// loadCrewWith is loadCrew with start's overrides; with ov.crewFile the
// manifest is that file and name is its stem.
func loadCrewWith(name, repoRoot string, ov *crewOverrides) (*crewManifest, error) {
	fail := func(format string, args ...any) (*crewManifest, error) {
		return nil, fmt.Errorf("crew %s: "+format, append([]any{name}, args...)...)
	}
	var path, machineFile string
	var read func() (string, error)
	if ov != nil && ov.crewFile != "" {
		path = ov.crewFile
		if !isFile(path) {
			return nil, fmt.Errorf("no crew manifest %s", path)
		}
		read = func() (string, error) { return readTextUniversal(path) }
	} else {
		names := []string{}
		for _, src := range crewSources(repoRoot) {
			if src.name == name && read == nil {
				path, read = src.path, src.read
				if src.source == "machine" {
					machineFile = src.path
				}
			}
			if !contains(names, src.name) {
				names = append(names, src.name)
			}
		}
		if read == nil {
			list := strings.Join(names, ", ")
			if list == "" {
				list = "(none)"
			}
			return nil, fmt.Errorf("no crew manifest %s; available crews: %s", assetPath("crews", name+".yaml"), list)
		}
	}
	text, err := read()
	if err != nil {
		return nil, err
	}
	var decoded any
	if err := yaml.Unmarshal([]byte(text), &decoded); err != nil {
		return fail("%s is not valid YAML: %s", path, err)
	}
	if _, ok := decoded.(map[interface{}]interface{}); !ok {
		return fail("%s must be a mapping with a roles: list", path)
	}
	var data yaml.MapSlice
	if err := yaml.Unmarshal([]byte(text), &data); err != nil {
		return fail("%s is not valid YAML: %s", path, err)
	}
	roles, ok := yamlScalar(data, "roles").([]any)
	if !ok || len(roles) == 0 {
		return fail("roles must be a non-empty list")
	}
	if ov != nil {
		if data, roles, err = applyCrewOverrides(name, data, roles, ov); err != nil {
			return nil, err
		}
	}
	checked := []*crewRole{}
	for i, raw := range roles {
		entry, ok := raw.(yaml.MapSlice)
		if !ok || !truthy(yamlScalar(entry, "role")) || !truthy(yamlScalar(entry, "brief")) {
			return fail("role %d needs both role: and brief:", i+1)
		}
		role, brief := pyStr(yamlScalar(entry, "role")), pyStr(yamlScalar(entry, "brief"))
		if !roleNamePattern.MatchString(role) {
			return fail("role name %s must be letters, digits or _ (it names files and panes)", pyRepr(role))
		}
		for _, r := range checked {
			if r.role == role {
				return fail("role %s is listed twice; use count: instead", role)
			}
		}
		if !assetIsFile("briefs", brief) {
			dir, _ := assetDir("briefs")
			return fail("brief %s for %s is not in %s", brief, role, dir)
		}
		var persona any
		if p := yamlScalar(entry, "persona"); truthy(p) {
			persona = pyStr(p)
		} else if d := defaultPersona(role); d != "" {
			persona = d
		}
		if persona != nil {
			if _, err := loadPersona(role, persona.(string)); err != nil {
				return fail("%s", err)
			}
		}
		countValue, present := yamlValue(entry, "count")
		if !present {
			countValue = 1
		}
		count, isInt := yamlInt(countValue)
		if !isInt || count < 1 {
			return fail("count for %s must be a whole number >= 1, not %s", role, pyReprValue(countValue))
		}
		human := yamlScalar(entry, "human_contact") == true
		tool := "claude"
		if t := yamlScalar(entry, "tool"); truthy(t) {
			tool = pyStr(t)
		}
		if !contains(crewTools, tool) {
			return fail("tool %s for %s is not supported; use one of: %s", pyRepr(tool), role, strings.Join(crewTools, ", "))
		}
		colour := "green"
		if human {
			colour = "red"
		}
		if c := yamlScalar(entry, "colour"); truthy(c) {
			colour = pyStr(c)
		}
		model := ""
		if m := yamlScalar(entry, "model"); truthy(m) {
			model = pyStr(m)
		}
		r := &crewRole{role: role, brief: brief, persona: persona, count: count, human: human, colour: colour, tool: tool, model: model}
		if v := yamlScalar(entry, "reasoning"); v != nil {
			reasoning := pyStr(v)
			levels, ok := toolReasoningLevels[tool]
			if !ok {
				levels = reasoningLevels
			}
			if !contains(levels, reasoning) {
				return fail("reasoning %s for %s (tool %s) must be one of: %s", pyRepr(reasoning), role, tool, strings.Join(levels, ", "))
			}
			r.opts = append(r.opts, pair{"reasoning", reasoning})
		}
		if v := yamlScalar(entry, "context"); v != nil {
			context := pyStr(v)
			if !contains(contextTiers, context) {
				return fail("context %s for %s must be one of: %s", pyRepr(context), role, strings.Join(contextTiers, ", "))
			}
			if !contains(contextTools, tool) {
				return fail("context %s for %s: tool %s has no context tier flag", pyRepr(context), role, tool)
			}
			r.opts = append(r.opts, pair{"context", context})
		}
		if v := yamlScalar(entry, "account"); v != nil {
			if !accountAllowed(v) {
				return fail("account for %s must be 1 or 2, not %s", role, pyReprValue(v))
			}
			r.opts = append(r.opts, pair{"account", yamlJSON(v)})
		}
		if v := yamlScalar(entry, "permissions"); v != nil {
			permissions := pyStr(v)
			if !contains(permissionNames, permissions) {
				return fail("permissions %s for %s must be one of: %s", pyRepr(permissions), role, strings.Join(permissionNames, ", "))
			}
			r.opts = append(r.opts, pair{"permissions", permissions})
		}
		if _, ok := yamlValue(entry, "max_credits"); ok {
			return fail("max_credits for %s: the Copilot credit cap is the shift budget, set once as a top-level max_credits: key and split across the Copilot panes", role)
		}
		checked = append(checked, r)
	}
	humans := []*crewRole{}
	for _, r := range checked {
		if r.human {
			humans = append(humans, r)
		}
	}
	if len(humans) != 1 {
		return fail("exactly one role needs human_contact: true, found %d", len(humans))
	}
	if humans[0].count != 1 {
		return fail("the human_contact role %s must have count 1", humans[0].role)
	}
	panes := 0
	for _, r := range checked {
		panes += r.count
	}
	if panes < 2 || panes > 8 {
		return fail("%d panes; a crew has 2 to 8", panes)
	}
	layoutValue, present := yamlValue(data, "layout")
	if !present {
		layoutValue = "split"
	}
	layout, _ := layoutValue.(string)
	if _, ok := layoutValue.(string); !ok || !contains(layoutNames, layout) {
		return fail("layout %s is not supported; use one of: %s", pyReprValue(layoutValue), strings.Join(layoutNames, ", "))
	}
	slots := layoutSlots[layout]
	if slots != 0 && panes > slots {
		return fail("%d panes do not fit layout %s (%d slots)", panes, layout, slots)
	}
	crew := &crewManifest{name: name, model: launcherModel, permissionMode: startPermissionMode, contract: "result.json", layout: layout, slots: slots, roles: checked,
		overrides: ov.record(machineFile)}
	if v := yamlScalar(data, "model"); truthy(v) {
		crew.model = pyStr(v)
	}
	for _, r := range checked {
		if r.model == "" {
			r.model = crew.model
		}
	}
	if v := yamlScalar(data, "permission_mode"); truthy(v) {
		crew.permissionMode = pyStr(v)
	}
	if v := yamlScalar(data, "contract"); truthy(v) {
		crew.contract = pyStr(v)
	}
	if budgetValue := yamlScalar(data, "max_credits"); budgetValue != nil {
		budget, ok := yamlInt(budgetValue)
		if !ok {
			return fail("max_credits (the shift budget) must be a whole number, not %s", pyReprValue(budgetValue))
		}
		copilotPanes := 0
		for _, r := range checked {
			if r.tool == "copilot" {
				copilotPanes += r.count
			}
		}
		if copilotPanes == 0 {
			return fail("max_credits is a Copilot credit budget, but the crew has no copilot panes")
		}
		share := int(math.Floor(float64(budget) / float64(copilotPanes)))
		if share < copilotMinCredits {
			return fail("max_credits %d over %d Copilot panes is %d each; Copilot's minimum --max-ai-credits is %d per pane", budget, copilotPanes, share, copilotMinCredits)
		}
		for _, r := range checked {
			if r.tool == "copilot" {
				r.opts = append(r.opts, pair{"max_credits", share})
			}
		}
		crew.maxCredits = budget
	}
	return crew, nil
}

// accountAllowed is `account in (1, 2)`, where True == 1 and 2.0 == 2.
func accountAllowed(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case int:
		return x == 1 || x == 2
	case int64:
		return x == 1 || x == 2
	case float64:
		return x == 1 || x == 2
	}
	return false
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

// crewMembers lists the panes in launch order; count N gives Role-1..N.
func crewMembersOf(crew *crewManifest, shift string) []*crewMember {
	members := []*crewMember{}
	for _, r := range crew.roles {
		for n := 1; n <= r.count; n++ {
			suffix := ""
			if r.count > 1 {
				suffix = "-" + strconv.Itoa(n)
			}
			members = append(members, &crewMember{role: r.role, label: r.role + suffix, identity: shift + "-" + strings.ToLower(r.role) + suffix,
				brief: r.brief, persona: r.persona, colour: r.colour, human: r.human, tool: r.tool, model: r.model, opts: append(object{}, r.opts...)})
		}
	}
	var lead *crewMember
	for _, m := range members {
		if m.human {
			lead = m
			break
		}
	}
	for _, m := range members {
		if m == lead {
			others := []string{}
			for _, o := range members {
				if o != m {
					others = append(others, o.identity)
				}
			}
			m.peer = strings.Join(others, ", ")
		} else {
			m.peer = lead.identity
		}
	}
	return members
}

func openingMessage(m *crewMember, stateDir string) string {
	message := foremanStartMessage
	if !m.human {
		message = startMessages[m.role]
		if message == "" {
			message = "Begin your " + m.role + " role: send READY to Foreman and await assignments."
		}
	}
	if m.replacement && stateDir != "" {
		recovery := "Report the unfinished assignment IDs to Foreman with READY."
		if strings.ToLower(m.role) == "foreman" {
			recovery = "Re-dispatch stale unfinished assignments to the current members."
		}
		message = "You are a replacement incarnation. First read \"" + filepath.ToSlash(pyResolve(stateDir)) + "/handover.md\" " +
			"if present and run `" + nightshiftCommand() + " inbox " + filepath.Base(stateDir) + " " + m.label + "`. " +
			"Recover acked but unfinished assignments from active_assignments; stale assignments must be " +
			"re-dispatched by Foreman to this incarnation before acting. " + recovery + "\n\n" + message
	}
	return message
}

func defaultPersona(role string) string {
	name := strings.ToLower(role) + ".yaml"
	if assetIsFile("personas", name) {
		return name
	}
	return ""
}

func loadPersona(role, persona string) (yaml.MapSlice, error) {
	var data yaml.MapSlice
	dir, _ := assetDir("personas")
	text, err := assetRead("personas", persona)
	var decoded any
	if err == nil {
		err = yaml.Unmarshal([]byte(text), &decoded)
	}
	if _, ok := decoded.(map[interface{}]interface{}); err != nil || !ok {
		return nil, fmt.Errorf("persona %s for %s is not a YAML mapping in %s", persona, role, dir)
	}
	if err := yaml.Unmarshal([]byte(text), &data); err != nil {
		return nil, fmt.Errorf("persona %s for %s is not a YAML mapping in %s", persona, role, dir)
	}
	if v := yamlScalar(data, "identity"); v != nil {
		if _, ok := v.(string); !ok {
			return nil, fmt.Errorf("persona %s for %s: identity must be text", persona, role)
		}
	}
	for _, list := range personaLists {
		items := yamlScalar(data, list[0])
		if items == nil {
			continue
		}
		valid := false
		if xs, ok := items.([]any); ok {
			valid = true
			for _, x := range xs {
				if _, ok := x.(string); !ok {
					valid = false
				}
			}
		}
		if !valid {
			return nil, fmt.Errorf("persona %s for %s: %s must be a list of text, e.g. %s: [one thing]", persona, role, list[0], list[0])
		}
	}
	return data, nil
}

func personaText(role, persona string) (string, error) {
	data, err := loadPersona(role, persona)
	if err != nil {
		return "", err
	}
	identity := ""
	if v := yamlScalar(data, "identity"); truthy(v) {
		identity = pyStr(v)
	}
	text := "# " + role + "\n\n" + pyStrip(identity) + "\n\n"
	for _, list := range personaLists {
		items, _ := yamlScalar(data, list[0]).([]any)
		if len(items) > 0 {
			text += "## " + list[1] + "\n\n"
			for _, item := range items {
				text += "- " + pyStrip(pyStr(item)) + "\n"
			}
			text += "\n"
		}
	}
	return text + "---\n\n# Protocol\n\n", nil
}

// pyStrip is str.strip() for the whitespace PyYAML and the briefs carry.
func pyStrip(s string) string { return strings.TrimSpace(s) }

// roleBrief renders a role's instructions as the oracle's role_brief does.
func roleBrief(role, identity, peerName, stateDir, brief, persona, cwd, repoRoot string) (string, error) {
	if persona == "" {
		persona = defaultPersona(role)
	}
	text := "# " + role + "\n\n# Protocol\n\n"
	if persona != "" {
		var err error
		if text, err = personaText(role, persona); err != nil {
			return "", err
		}
	}
	if brief == "" {
		brief = strings.ToLower(role) + ".md"
	}
	for _, name := range []string{"common.md", brief} {
		body, err := assetRead("briefs", name)
		if err != nil {
			return "", err
		}
		text += pyStrip(body) + "\n\n"
	}
	text = strings.ReplaceAll(strings.ReplaceAll(text, "# Task\n", ""), humanTaskMarker, "")
	if cwd == "" && stateDir != "" {
		if info, _ := worktreeInfo(stateDir); info != nil {
			cwd = objectString(info, "worktree_dir")
		}
		if cwd == "" {
			if saved, ok := readObject(filepath.Join(stateDir, "crew.json")); ok {
				if _, isList := rowValue(saved, "members").([]any); isList {
					cwd = objectString(saved, "cwd")
				}
			}
		}
	}
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	cwd = pyResolve(cwd)
	if repoRoot == "" {
		repoRoot = cwd
	}
	repoRoot = canonicalRepoRoot(repoRoot, stateDir)
	config, err := loadRepoConfig(repoRoot)
	if err != nil {
		return "", err
	}
	repoDir := filepath.Join(repoRoot, ".nightshift")
	instructions := []string{}
	for _, p := range []string{filepath.Join(repoDir, "instructions.md"), filepath.Join(repoDir, strings.ToLower(role)+".md")} {
		if isFile(p) {
			body, err := readTextUniversal(p)
			if err != nil {
				return "", err
			}
			instructions = append(instructions, pyStrip(body))
		}
	}
	if config != nil || len(instructions) > 0 {
		text += "\n# Repository\n\n"
		if build, ok := rowValue(config, "build").(object); ok {
			if cmd := configString(build, "cmd"); cmd != "" {
				text += "- Default build/test command (`{nightshift} build {shift}`): `" + cmd + "`\n\n"
			}
		}
		text += strings.Join(instructions, "\n\n") + "\n\n"
	}
	if strings.ToLower(role) == "foreman" {
		text += "# Task\n\n" + humanTaskMarker + "\n\n"
	}
	shift, statePosix := "", ""
	if stateDir != "" {
		shift, statePosix = filepath.Base(stateDir), filepath.ToSlash(stateDir)
	}
	for _, kv := range [][2]string{{"{role}", role}, {"{identity}", identity}, {"{peer_name}", peerName}, {"{state_dir}", statePosix},
		{"{shift}", shift}, {"{nightshift}", nightshiftCommand()}, {"{cwd}", filepath.ToSlash(cwd)}} {
		text = strings.ReplaceAll(text, kv[0], kv[1])
	}
	return text, nil
}

func compactionControl(tool, role string) object {
	if strings.ToLower(role) == "foreman" {
		env := object{}
		if tool == "claude" {
			env = object{{"DISABLE_AUTO_COMPACT", "0"}, {"DISABLE_COMPACT", "0"}}
		}
		return object{{"automatic", "permitted"}, {"environment", env}}
	}
	if tool == "claude" {
		return object{{"automatic", "disabled"}, {"environment", object{{"DISABLE_AUTO_COMPACT", "1"}}}}
	}
	return object{{"automatic", "unsupported"}, {"environment", object{}}, {"reason", compactionUnsupported[tool]}}
}

// crewEnv is the environment for a crew member's CLI: inherited Claude Code
// markers dropped (CLAUDE_AFK_* kept), colour forced on, per-tool settings.
func crewEnv(environ []string, tool string, account any, role string) []string {
	keys := []string{}
	values := map[string]string{}
	set := func(k, v string) {
		up := strings.ToUpper(k)
		if _, ok := values[up]; !ok {
			keys = append(keys, up)
		}
		values[up] = k + "=" + v
	}
	unset := func(k string) {
		up := strings.ToUpper(k)
		if _, ok := values[up]; ok {
			delete(values, up)
		}
	}
	for _, kv := range environ {
		k, v, _ := strings.Cut(kv, "=")
		up := strings.ToUpper(k)
		if k == "" || (strings.HasPrefix(up, "CLAUDE") && !strings.HasPrefix(up, "CLAUDE_AFK_")) {
			continue
		}
		set(k, v)
	}
	unset("NO_COLOR")
	set("TERM", "xterm-256color")
	set("COLORTERM", "truecolor")
	set("FORCE_COLOR", "3")
	if tool == "codex" {
		unset("TERM")
		if numberIs(account, 2) { // a YAML int from a manifest or a JSON number from crew.json
			set("CODEX_HOME", filepath.Join(userHome(), ".codex-second"))
		}
	}
	if tool == "claude" {
		set("CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION", "false")
	}
	if tool == "copilot" {
		set("COPILOT_DISABLE_TERMINAL_TITLE", "1")
		set("COPILOT_SETUP_TERMINAL", "false")
	}
	if role != "" {
		for _, p := range compactionControl(tool, role)[1].val.(object) {
			set(p.key, p.val.(string))
		}
	}
	env := []string{}
	for _, k := range keys {
		if v, ok := values[k]; ok {
			env = append(env, v)
		}
	}
	return env
}

// isAdvisor: the single manifest Advisor, whose pane is held until Foreman consults it.
func isAdvisor(m *crewMember) bool { return m.role == advisorLabel && m.label == advisorLabel }

// isCoder: the single manifest Coder, held until Foreman summons it when the crew has an Apprentice (NGH-056).
func isCoder(m *crewMember) bool { return m.role == coderLabel && m.label == coderLabel }

func crewHasRole(crew *crewManifest, role string) bool {
	for _, r := range crew.roles {
		if r.role == role {
			return true
		}
	}
	return false
}

// installAgyStatusline points agy's status line at the generic Nightshift command, keeping a user's own.
func installAgyStatusline() bool {
	path := filepath.Join(userHome(), ".gemini", "antigravity-cli", "settings.json")
	data := object{}
	if fileExists(path) {
		text, err := readTextUniversal(path)
		if err != nil {
			return false
		}
		value, err := parseJSON([]byte(text))
		o, ok := value.(object)
		if err != nil || !ok {
			return false
		}
		data = o
	}
	if current := rowValue(data, "statusLine"); truthy(current) {
		o, ok := current.(object)
		if !ok {
			return false
		}
		if rowValue(o, "command") != agyStatuslineCmd() {
			errLine("Nightshift agy role status line unavailable: preserving the existing custom statusLine command.")
			return false
		}
	}
	data = setValue(data, "statusLine", object{{"type", "command"}, {"command", agyStatuslineCmd()}, {"enabled", true}, {"stack_with_default", true}})
	if err := os.MkdirAll(filepath.Dir(path), 0777); err != nil {
		return false
	}
	return writeTextFile(path, renderJSON(data, true, true)+"\n") == nil
}

// installCopilotStatusline points copilot's status line at the generic Nightshift command, keeping a user's own.
func installCopilotStatusline() bool {
	home := os.Getenv("COPILOT_HOME")
	if home == "" {
		home = filepath.Join(userHome(), ".copilot")
	}
	path := filepath.Join(home, "settings.json")
	data := object{}
	if fileExists(path) {
		text, err := readTextUniversal(path)
		if err != nil {
			return false
		}
		value, err := parseJSON([]byte(text))
		o, ok := value.(object)
		if err != nil || !ok {
			return false
		}
		data = o
	}
	if current := rowValue(data, "statusLine"); truthy(current) {
		o, ok := current.(object)
		if !ok {
			return false
		}
		if rowValue(o, "command") != statuslineCmd() {
			errLine("Nightshift copilot role status line unavailable: preserving the existing custom statusLine command.")
			return false
		}
	}
	data = setValue(data, "statusLine", object{{"type", "command"}, {"command", statuslineCmd()}})
	if err := os.MkdirAll(filepath.Dir(path), 0777); err != nil {
		return false
	}
	return writeTextFile(path, renderJSON(data, true, true)+"\n") == nil
}

func permissionFlags(tool, permissions string) []string {
	if permissions == "" {
		permissions = startPermissionMode
	}
	if tool == "claude" {
		if permissions == "skip" {
			return []string{"--dangerously-skip-permissions"}
		}
		if permissions == "default" {
			return []string{}
		}
		return []string{"--permission-mode", permissions}
	}
	if permissions == "skip" || permissions == "auto" {
		return append([]string{}, approveAllFlags[tool]...)
	}
	return []string{}
}

func agyCommand(model, brief, firstMessage, permissions string) []string {
	cmd := append([]string{toolExe("agy"), "--model", model}, permissionFlags("agy", permissions)...)
	return append(cmd, "-i", brief+"\n\n"+firstMessage)
}

func codexCommand(model, brief, firstMessage, reasoning, permissions string) []string {
	cmd := append([]string{toolExe("codex")}, permissionFlags("codex", permissions)...)
	cmd = append(cmd, "--no-daemon", "-c", "tui.terminal_title=[]",
		"-c", `tui.status_line=["thread-title","model-with-reasoning","context-remaining"]`, "--model", model)
	if reasoning != "" {
		cmd = append(cmd, "-c", "model_reasoning_effort="+reasoning)
	}
	return append(cmd, brief+"\n\n"+firstMessage)
}

func copilotCommand(model, brief, firstMessage, permissions, reasoning, context string, sessionName *string, maxCredits int) []string {
	cmd := append([]string{toolExe("copilot")}, permissionFlags("copilot", permissions)...)
	cmd = append(cmd, "--no-auto-update", "--model", model)
	// Crew members talk through nightshift and agents-cli, not MCP. Without the built-in GitHub MCP and remote
	// export a pane no longer stalls minutes on start in a repo with no GitHub remote, and without the user's
	// MCP servers each turn carries about a third fewer tokens (NGH-057).
	cmd = append(cmd, "--disable-builtin-mcps", "--no-remote-export")
	for _, server := range copilotUserMCPServers() {
		cmd = append(cmd, "--disable-mcp-server", server)
	}
	if reasoning != "" {
		cmd = append(cmd, "--reasoning-effort", reasoning)
	}
	if context != "" {
		cmd = append(cmd, "--context", context)
	}
	if maxCredits != 0 {
		cmd = append(cmd, "--max-ai-credits", strconv.Itoa(maxCredits))
	}
	if sessionName != nil {
		cmd = append(cmd, "--name", *sessionName)
	}
	return append(cmd, "-i", brief+"\n\n"+firstMessage)
}

// copilotUserMCPServers lists, sorted, the server names in the user's Copilot MCP config
// (COPILOT_HOME/mcp-config.json, else ~/.copilot/mcp-config.json); nothing else is read.
func copilotUserMCPServers() []string {
	home := os.Getenv("COPILOT_HOME")
	if home == "" {
		home = filepath.Join(userHome(), ".copilot")
	}
	data, err := os.ReadFile(filepath.Join(home, "mcp-config.json"))
	if err != nil {
		return nil
	}
	var config struct {
		MCPServers map[string]json.RawMessage `json:"mcpServers"`
	}
	if json.Unmarshal(data, &config) != nil {
		return nil
	}
	names := make([]string, 0, len(config.MCPServers))
	for name := range config.MCPServers {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// toolCommand is the argv for a non-Claude crew member (agy, codex or copilot).
func toolCommand(tool, model, brief, firstMessage, permissions, reasoning, context string, sessionName *string, maxCredits int) []string {
	switch tool {
	case "agy":
		return agyCommand(model, brief, firstMessage, permissions)
	case "codex":
		return codexCommand(model, brief, firstMessage, reasoning, permissions)
	}
	return copilotCommand(model, brief, firstMessage, permissions, reasoning, context, sessionName, maxCredits)
}
