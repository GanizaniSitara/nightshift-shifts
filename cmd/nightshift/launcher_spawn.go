package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

var spawnCLIs = map[string]string{
	"claude":   "claude",
	"gemini":   "agy",
	"codex":    "codex",
	"copilot":  "copilot",
	"opencode": "", // unwired
}

var defaultToolModels = map[string]string{
	"agy":     "gemini-3.8-flash-high",
	"codex":   "gpt-6.1-sol",
	"copilot": "gemini-3.8-flash",
}

func init() {
	extraCommands["spawn"] = spawnCommand
	spawnHook = func(argv []string) error {
		return runSpawn(argv, true)
	}
}

type spawnError struct {
	code    int
	message string
}

func (e spawnError) Error() string { return e.message }
func (e spawnError) ExitCode() int { return e.code }

func spawnExit(msg string) error {
	return spawnError{code: 1, message: msg}
}

func spawnUsageError(msg string) error {
	return spawnError{code: 2, message: fmt.Sprintf("%s\nnightshift spawn: error: %s", spawnUsage, msg)}
}

func spawnReadSlot(dir string, slot int) object {
	obj, ok := readObject(filepath.Join(dir, "slots", strconv.Itoa(slot)+".json"))
	if !ok {
		return nil
	}
	return obj
}

func spawnWriteSlot(dir string, slot int, fields object) error {
	record := object{{"slot", slot}}
	for _, p := range fields {
		if p.key != "slot" {
			record = setValue(record, p.key, p.val)
		}
	}
	slotsDir := filepath.Join(dir, "slots")
	_ = os.MkdirAll(slotsDir, 0777)
	return writeJSONFile(filepath.Join(slotsDir, strconv.Itoa(slot)+".json"), record)
}

func containsInt(arr []int, target int) bool {
	for _, v := range arr {
		if v == target {
			return true
		}
	}
	return false
}

func deleteKey(o object, key string) object {
	res := make(object, 0, len(o))
	for _, p := range o {
		if p.key != key {
			res = append(res, p)
		}
	}
	return res
}

func spawnCommand(argv []string) int {
	err := runSpawn(argv, false)
	if err == nil {
		return 0
	}
	var spawnErr spawnError
	if errors.As(err, &spawnErr) {
		errLine(spawnErr.message)
		return spawnErr.code
	}
	errLine(err.Error())
	return 1
}

const spawnUsage = "usage: nightshift spawn [-h] --role ROLE\n                        [--cli {claude,gemini,codex,copilot,opencode}]\n                        [--crew CREW] [--slot SLOT] [--dry-run]\n                        shift"

const spawnHelp = spawnUsage + `

Start a crew session in a free slot of a live shift's own window (2x4/1x4
container, NGH-025): adds the role to crew.json and types the pane wrapper
into that slot's cmd shell. Refused when the window is gone, no slot is free,
or the role is already live. Foreman only.

positional arguments:
  shift                 shift name, unique suffix, or "latest"

options:
  -h, --help            show this help message and exit
  --role ROLE           role label, e.g. Advisor or Coder2 (letters, digits,
                        _)
  --cli {claude,gemini,codex,copilot,opencode}
                        the CLI to run: claude (default), gemini (agy), codex
                        or copilot; opencode is not wired yet. Overrides the
                        tool --crew would give
  --crew CREW           take the role's brief, colour, tool, model, reasoning
                        and account from crews/NAME.yaml instead of the
                        shift's own manifest (e.g. apprentice-codex)
  --slot SLOT           slot number (default: the first free one)
  --dry-run             print the member and the pane command; write, type and
                        start nothing
`

// shiftKnowsRole: the role is already a member of the shift (crew.json) or in the shift's own manifest.
func shiftKnowsRole(saved object, role, repoRoot string) bool {
	members, _ := rowValue(saved, "members").([]any)
	for _, item := range members {
		if m, ok := item.(object); ok && (strings.EqualFold(objectString(m, "label"), role) || strings.EqualFold(objectString(m, "role"), role)) {
			return true
		}
	}
	if manifest, err := loadSavedCrew(saved, repoRoot); err == nil && manifest != nil {
		for _, r := range manifest.roles {
			if strings.EqualFold(r.role, role) {
				return true
			}
		}
	}
	return false
}

func runSpawn(argv []string, quiet bool) error {
	opts := []writeOption{
		{name: "--role"},
		{name: "--cli", choices: []string{"claude", "gemini", "codex", "copilot", "opencode"}},
		{name: "--crew"},
		{name: "--slot", number: "int"},
		{name: "--dry-run", flag: true},
	}
	args, message := parseWriteArgs(argv, opts, nil, []string{"shift"})
	if args.help {
		if !quiet {
			out(spawnHelp)
		}
		return nil
	}
	if message != "" && !strings.HasPrefix(message, "the following arguments are required:") {
		return spawnUsageError(message)
	}
	missing := []string{}
	if len(args.pos) < 1 {
		missing = append(missing, "shift")
	}
	if !args.present["--role"] {
		missing = append(missing, "--role")
	}
	if len(missing) > 0 {
		return spawnUsageError("the following arguments are required: " + strings.Join(missing, ", "))
	}
	if message != "" {
		return spawnUsageError(message)
	}

	cli := "claude"
	if args.present["--cli"] {
		cli = args.values["--cli"]
	}
	tool, ok := spawnCLIs[cli]
	if !ok || tool == "" {
		return spawnExit(fmt.Sprintf("--cli %s is not yet wired (NGH-022)", cli))
	}

	role := args.values["--role"]
	if !roleNamePattern.MatchString(role) {
		return spawnExit(fmt.Sprintf("role name %s must be letters, digits or _ (it names files and panes)", pyRepr(role)))
	}

	stateDir, err := resolveShift(args.pos[0])
	if err != nil {
		return spawnExit(err.Error())
	}
	saved, _ := readObject(filepath.Join(stateDir, "crew.json"))
	cwd := objectString(saved, "cwd")
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	repoRoot := canonicalRepoRoot(cwd, stateDir)

	var specifiedCrewRole *crewRole
	if args.present["--crew"] {
		crewName := args.values["--crew"]
		manifest, err := loadCrew(crewName, repoRoot)
		if err != nil {
			return spawnExit(err.Error())
		}
		for _, r := range manifest.roles {
			if strings.EqualFold(r.role, role) {
				specifiedCrewRole = r
				break
			}
		}
		if specifiedCrewRole == nil {
			return spawnExit(fmt.Sprintf("crew %s has no role %s", crewName, role))
		}
		if !args.present["--cli"] {
			tool = specifiedCrewRole.tool
		}
	}

	// A role the shift's crew does not have (and that was never in this shift) defaults to the specialists
	// crew, e.g. Decompiler on Codex; --crew and --cli override it.
	if specifiedCrewRole == nil && !args.present["--cli"] && !shiftKnowsRole(saved, role, repoRoot) {
		if manifest, err := loadCrew("specialists", repoRoot); err == nil {
			for _, r := range manifest.roles {
				if strings.EqualFold(r.role, role) {
					specifiedCrewRole = r
					tool = r.tool
					break
				}
			}
		}
	}

	slotsVal, hasSlots := objectNumber(saved, "slots")
	slots := int(numberFloat(slotsVal))
	if !hasSlots || slots <= 0 {
		return spawnExit(fmt.Sprintf("shift %s has no slot grid (its crew layout is split); nothing to spawn into", filepath.Base(stateDir)))
	}

	launchObj, _ := readObject(filepath.Join(stateDir, "launch.json"))
	hwndNum, _ := objectNumber(launchObj, "hwnd")
	var hwnd uintptr
	if hwndNum != "" {
		u, _ := strconv.ParseUint(hwndNum.String(), 10, 64)
		hwnd = uintptr(u)
	}
	windowFound := false
	if hwnd != 0 {
		for _, w := range terminalWindows() {
			if w.hwnd == hwnd {
				windowFound = true
				break
			}
		}
	}
	if !windowFound {
		return spawnExit(fmt.Sprintf("window is gone: shift %s has no open Terminal window to spawn into", filepath.Base(stateDir)))
	}

	membersVal, _ := objectGet(saved, "members")
	membersList, _ := membersVal.([]any)
	var existing object
	for _, item := range membersList {
		if m, ok := item.(object); ok {
			if strings.EqualFold(objectString(m, "label"), role) {
				existing = m
				break
			}
		}
	}
	if existing != nil {
		existingLabel := objectString(existing, "label")
		info, _ := readObject(filepath.Join(stateDir, existingLabel+".json"))
		pid := processNumber(info, "pid")
		started, startedOK := objectNumber(info, "started")
		if pid != 0 && pidAlive(pid, started, startedOK) {
			return spawnExit(fmt.Sprintf("role %s is already live in shift %s (pid %d)", existingLabel, filepath.Base(stateDir), pid))
		}
	}

	var slotArg int
	if args.present["--slot"] {
		val, _ := parseInteger(args.values["--slot"])
		slotArg = int(val)
		if slotArg < 1 || slotArg > slots {
			return spawnExit(fmt.Sprintf("no free slot: slot %d is not in 1..%d", slotArg, slots))
		}
	}

	states := make(map[int]string, slots)
	for n := 1; n <= slots; n++ {
		slotRecord := spawnReadSlot(stateDir, n)
		state, _ := slotState(stateDir, slotRecord)
		states[n] = state
	}

	var mine []int
	if existing != nil {
		existingLabel := objectString(existing, "label")
		for n := 1; n <= slots; n++ {
			slotRecord := spawnReadSlot(stateDir, n)
			if slotRecord != nil && objectString(slotRecord, "role") == existingLabel {
				mine = append(mine, n)
			}
		}
		for _, n := range mine {
			if states[n] == "starting" {
				return spawnExit(fmt.Sprintf("role %s is already live in shift %s (starting in slot %d)", existingLabel, filepath.Base(stateDir), n))
			}
		}
	}

	// The last slot is the human's Ops pane (a command line in the repository): spawn only uses it when --slot
	// names it.
	var free []int
	for n := 1; n <= slots; n++ {
		if (states[n] == "empty" || states[n] == "dead") && (n < slots || args.present["--slot"]) {
			free = append(free, n)
		}
	}

	// The manifest Advisor's slot stays reserved until `consult` starts it (acc1f52), and a reserved
	// Coder's until `summon` does (NGH-056): no other label is given either slot.
	ownSlot := 0
	for _, r := range []struct{ label, key, pending, command, until string }{
		{advisorLabel, "advisor_slot", "consult", "consult", "consultation"},
		{coderLabel, "coder_slot", "summon", "summon", "summon"},
	} {
		reserved := intValue(rowValue(saved, r.key))
		if reserved == 0 {
			continue
		}
		if !strings.EqualFold(role, r.label) {
			if args.present["--slot"] && slotArg == reserved {
				return spawnExit(fmt.Sprintf("%s slot %d is reserved until %s", r.label, reserved, r.until))
			}
			kept := []int{}
			for _, n := range free {
				if n != reserved {
					kept = append(kept, n)
				}
			}
			free = kept
			continue
		}
		if args.present["--slot"] && slotArg != reserved {
			return spawnExit(fmt.Sprintf("%s is reserved for slot %d", r.label, reserved))
		}
		if !truthy(rowValue(saved, r.pending)) {
			info, _ := readObject(filepath.Join(stateDir, role+".json"))
			if !truthy(rowValue(info, "pid")) {
				return spawnExit(fmt.Sprintf("use %s to start the %s", r.command, r.label))
			}
		}
		ownSlot = reserved
	}

	var slot int
	if args.present["--slot"] {
		if containsInt(free, slotArg) {
			slot = slotArg
		}
	} else if ownSlot != 0 {
		if containsInt(free, ownSlot) {
			slot = ownSlot
		}
	} else {
		var own []int
		for _, n := range free {
			if containsInt(mine, n) {
				own = append(own, n)
			}
		}
		if len(own) > 0 {
			slot = own[0]
		} else if len(free) > 0 {
			pSlot := patrolSlot(stateDir)
			slot = free[0]
			for _, n := range free {
				if n != pSlot {
					slot = n
					break
				}
			}
		}
	}
	if slot == 0 {
		if args.present["--slot"] {
			return spawnExit(fmt.Sprintf("no free slot: slot %d is taken", slotArg))
		}
		reserved := []string{}
		for _, r := range []struct{ label, key, command string }{{advisorLabel, "advisor_slot", "consult"}, {coderLabel, "coder_slot", "summon"}} {
			if n := intValue(rowValue(saved, r.key)); n != 0 && !strings.EqualFold(role, r.label) {
				reserved = append(reserved, fmt.Sprintf("%d (%s, until %s)", n, r.label, r.command))
			}
		}
		next := "reserved slots: none"
		if len(reserved) > 0 {
			next = "reserved slots: " + strings.Join(reserved, ", ")
		}
		next += fmt.Sprintf("; slot %d is the Ops pane (only with --slot %d); down a member or ask the human for a slot", slots, slots)
		return spawnExit(fmt.Sprintf("no free slot in shift %s (%d slots, all taken)\n%s", filepath.Base(stateDir), slots, next))
	}

	shift := filepath.Base(stateDir)
	var lead object
	if len(membersList) > 0 {
		if m, ok := membersList[0].(object); ok {
			lead = m
		}
	}
	for _, item := range membersList {
		if m, ok := item.(object); ok && objectBool(m, "human_contact") {
			lead = m
			break
		}
	}

	if existing != nil && specifiedCrewRole == nil && !args.present["--cli"] {
		extTool := objectString(existing, "tool")
		if contains(crewTools, extTool) {
			tool = extTool
		}
	}

	var sameRole object
	for _, item := range membersList {
		if m, ok := item.(object); ok && strings.EqualFold(objectString(m, "role"), role) {
			sameRole = m
			break
		}
	}

	var sameTool object
	for _, item := range membersList {
		if m, ok := item.(object); ok && objectString(m, "tool") == tool {
			sameTool = m
			break
		}
	}

	var model string
	if sameTool != nil && objectString(sameTool, "model") != "" {
		model = objectString(sameTool, "model")
	} else if tool == "claude" {
		model = objectString(saved, "model")
		if !strings.HasPrefix(model, "claude") { // a non-Claude crew's model (e.g. gemini-4-argon-high) is not a Claude model
			model = launcherModel
		}
	} else {
		model = defaultToolModels[tool]
	}

	memberRole := role
	if sameRole != nil && objectString(sameRole, "role") != "" {
		memberRole = objectString(sameRole, "role")
	}

	memberBrief := "coder.md"
	if assetIsFile("briefs", strings.ToLower(role)+".md") {
		memberBrief = strings.ToLower(role) + ".md"
	}

	memberPersona := defaultPersona(memberRole)
	if memberPersona == "" {
		memberPersona = defaultPersona("Coder")
	}

	memberColour := "yellow"
	if sameRole != nil && objectString(sameRole, "colour") != "" {
		memberColour = objectString(sameRole, "colour")
	}

	member := object{
		{"role", memberRole},
		{"label", role},
		{"identity", fmt.Sprintf("%s-%s", shift, strings.ToLower(role))},
		{"brief", memberBrief},
		{"persona", memberPersona},
		{"colour", memberColour},
		{"human_contact", false},
		{"tool", tool},
		{"model", model},
		{"peer", objectString(lead, "identity")},
	}

	if existing == nil {
		if manifest, err := loadSavedCrew(saved, repoRoot); err == nil && manifest != nil {
			var manifestRole *crewRole
			for _, r := range manifest.roles {
				if strings.EqualFold(r.role, role) {
					manifestRole = r
					break
				}
			}
			if manifestRole != nil {
				member = setValue(member, "role", manifestRole.role)
				member = setValue(member, "label", manifestRole.role)
				member = setValue(member, "brief", manifestRole.brief)
				member = setValue(member, "persona", manifestRole.persona)
				member = setValue(member, "colour", manifestRole.colour)
				if val, ok := objectGet(manifestRole.opts, "compact_at"); ok {
					member = setValue(member, "compact_at", val)
				}
				if manifestRole.tool == tool {
					member = setValue(member, "model", manifestRole.model)
					for _, opt := range []string{"reasoning", "context", "account", "permissions", "max_credits"} {
						if val, ok := objectGet(manifestRole.opts, opt); ok {
							member = setValue(member, opt, val)
						}
					}
				}
			}
		}
	}

	if existing != nil {
		if val, ok := objectGet(existing, "compact_at"); ok {
			member = setValue(member, "compact_at", val)
		}
		member = setValue(member, "replacement", true)
		roleVal := objectString(existing, "role")
		if roleVal == "" {
			roleVal = objectString(existing, "label")
		}
		member = setValue(member, "role", roleVal)
		member = setValue(member, "label", objectString(existing, "label"))
		idVal := objectString(existing, "identity")
		if idVal == "" {
			idVal = fmt.Sprintf("%s-%s", shift, strings.ToLower(objectString(existing, "label")))
		}
		member = setValue(member, "identity", idVal)
		if b := objectString(existing, "brief"); b != "" {
			member = setValue(member, "brief", b)
		}
		if p, ok := objectGet(existing, "persona"); ok {
			member = setValue(member, "persona", p)
		}
		if c := objectString(existing, "colour"); c != "" {
			member = setValue(member, "colour", c)
		}
		extTool := objectString(existing, "tool")
		if extTool == "" {
			extTool = "claude"
		}
		if tool == extTool && objectString(existing, "model") != "" {
			member = setValue(member, "model", objectString(existing, "model"))
			for _, opt := range []string{"reasoning", "context", "account", "permissions", "max_credits"} {
				if val, ok := objectGet(existing, opt); ok {
					member = setValue(member, opt, val)
				}
			}
		}
	}

	if specifiedCrewRole != nil {
		member = setValue(member, "role", specifiedCrewRole.role)
		member = setValue(member, "brief", specifiedCrewRole.brief)
		member = setValue(member, "persona", specifiedCrewRole.persona)
		member = setValue(member, "colour", specifiedCrewRole.colour)
		if existing == nil {
			member = setValue(member, "label", specifiedCrewRole.role)
		}
		for _, opt := range []string{"reasoning", "context", "account", "permissions", "max_credits", "compact_at"} {
			member = deleteKey(member, opt)
		}
		if val, ok := objectGet(specifiedCrewRole.opts, "compact_at"); ok {
			member = setValue(member, "compact_at", val)
		}
		if specifiedCrewRole.tool == tool {
			member = setValue(member, "model", specifiedCrewRole.model)
			for _, opt := range []string{"reasoning", "context", "account", "permissions", "max_credits"} {
				if val, ok := objectGet(specifiedCrewRole.opts, opt); ok {
					member = setValue(member, opt, val)
				}
			}
		}
	}

	budgetVal, hasBudget := objectNumber(saved, "max_credits")
	if tool != "copilot" {
		member = deleteKey(member, "max_credits")
	} else if hasBudget {
		budget := int(numberFloat(budgetVal))
		var others []object
		for _, item := range membersList {
			if m, ok := item.(object); ok {
				if existing != nil && objectString(m, "label") == objectString(existing, "label") {
					continue
				}
				if objectString(m, "tool") == "copilot" {
					others = append(others, m)
				}
			}
		}
		committed := 0
		for _, m := range others {
			if mc, ok := objectNumber(m, "max_credits"); ok {
				committed += int(numberFloat(mc))
			}
		}
		left := budget - committed
		var share int
		if existing != nil && objectString(existing, "tool") == "copilot" && objectNumberExists(existing, "max_credits") {
			mc, _ := objectNumber(existing, "max_credits")
			share = int(numberFloat(mc))
		} else {
			found := false
			for _, m := range others {
				if mc, ok := objectNumber(m, "max_credits"); ok && int(numberFloat(mc)) != 0 {
					share = int(numberFloat(mc))
					found = true
					break
				}
			}
			if !found {
				share = left
			}
		}
		if share < copilotMinCredits || share > left {
			return spawnExit(fmt.Sprintf("no budget share left: shift %s budget %d, %d committed to other Copilot panes, %d left; a Copilot pane needs %d (minimum %d)", filepath.Base(stateDir), budget, committed, left, share, copilotMinCredits))
		}
		member = setValue(member, "max_credits", share)
	}

	if args.present["--dry-run"] {
		var cmdVal any = nil
		if tool != "claude" {
			perm := objectString(member, "permissions")
			if perm == "" {
				perm = objectString(saved, "permission_mode")
			}
			reasoning := objectString(member, "reasoning")
			contextStr := objectString(member, "context")
			sessName := fmt.Sprintf("%s (%s)", objectString(member, "label"), objectString(member, "identity"))
			maxCred := 0
			if mc, ok := objectNumber(member, "max_credits"); ok {
				maxCred = int(numberFloat(mc))
			}
			rawCmd := toolCommand(tool, objectString(member, "model"), "<brief>", "<opening message>", perm, reasoning, contextStr, &sessName, maxCred)
			cmdSlice := make([]any, len(rawCmd))
			for i, s := range rawCmd {
				cmdSlice[i] = s
			}
			cmdVal = cmdSlice
		}
		report := object{
			{"dry_run", true},
			{"shift", filepath.Base(stateDir)},
			{"slot", slot},
			{"role", objectString(member, "label")},
			{"identity", objectString(member, "identity")},
			{"tool", tool},
			{"member", member},
			{"command", cmdVal},
		}
		if !quiet {
			printResult(report, true, true)
		}
		return nil
	}

	var updated []any
	for _, item := range membersList {
		if m, ok := item.(object); ok {
			if existing != nil && objectString(m, "label") == objectString(existing, "label") {
				continue
			}
			updated = append(updated, m)
		}
	}
	updated = append(updated, member)

	crewCwd := objectString(saved, "cwd")
	if crewCwd == "" {
		leadInfo, _ := readObject(filepath.Join(stateDir, objectString(lead, "label")+".json"))
		crewCwd = objectString(leadInfo, "cwd")
	}
	if crewCwd == "" {
		crewCwd, _ = os.Getwd()
	}

	crewFile := filepath.Join(stateDir, "crew.json")
	beforeData, _ := os.ReadFile(crewFile)
	savedWithUpdated := setValue(saved, "members", updated)
	if err := preflightMember(stateDir, crewCwd, repoRoot, savedWithUpdated, memberFromObject(member)); err != nil {
		return spawnExit(err.Error())
	}
	if err := writeJSONFile(crewFile, savedWithUpdated); err != nil {
		return spawnExit(err.Error())
	}

	line := fmt.Sprintf("\"%s\" --pane %s --state-dir \"%s\" --start --cwd \"%s\" --slot %d",
		launcherExe(),
		objectString(member, "label"),
		stateDir,
		crewCwd,
		slot,
	)
	if crewName := objectString(saved, "crew"); crewName != "" && crewName != "default" {
		line += " --crew " + crewName
	}

	evacuatePatrolSlot(stateDir, slot)
	record := spawnReadSlot(stateDir, slot)
	cmdPid := processNumber(record, "cmd_pid")
	spawned := float64(time.Now().UnixNano()) / 1e9

	var failed string
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "agents-cli", "_attach", "inject", strconv.FormatUint(uint64(cmdPid), 10), "1")
	cmd.Stdin = strings.NewReader(line)
	var stdoutBuf, stderrBuf bytes.Buffer
	cmd.Stdout = &stdoutBuf
	cmd.Stderr = &stderrBuf

	runErr := cmd.Run()
	if runErr != nil {
		var parts []string
		if outStr := strings.TrimSpace(stdoutBuf.String()); outStr != "" {
			parts = append(parts, outStr)
		}
		if errStr := strings.TrimSpace(stderrBuf.String()); errStr != "" {
			parts = append(parts, errStr)
		}
		if len(parts) > 0 {
			failed = strings.Join(parts, "\n")
		} else {
			failed = fmt.Sprintf("exit %d", cmd.ProcessState.ExitCode())
		}
	}
	if failed == "" {
		if val, err := parseJSON(stdoutBuf.Bytes()); err == nil {
			if reply, ok := val.(object); ok {
				if errVal, hasErr := objectGet(reply, "error"); hasErr {
					failed = renderJSON(errVal, false, false)
				}
			}
		}
	}
	if failed != "" {
		_ = os.WriteFile(crewFile, beforeData, 0666)
		return spawnExit(fmt.Sprintf("could not type into slot %d (cmd pid %d): %s", slot, cmdPid, failed))
	}

	kept := spawnReadSlot(stateDir, slot)
	if kept == nil {
		kept = record
	}
	kept = setValue(kept, "role", objectString(member, "label"))
	kept = setValue(kept, "identity", objectString(member, "identity"))
	kept = setValue(kept, "spawned", spawned)
	if err := spawnWriteSlot(stateDir, slot, kept); err != nil {
		return spawnExit(err.Error())
	}

	busAppend(stateDir, "spawn", object{
		{"role", objectString(member, "label")},
		{"slot", slot},
		{"tool", tool},
		{"identity", objectString(member, "identity")},
	})

	if !quiet {
		printResult(object{
			{"shift", shift},
			{"slot", slot},
			{"role", objectString(member, "label")},
			{"identity", objectString(member, "identity")},
			{"tool", tool},
		}, false, true)
	}
	return nil
}
