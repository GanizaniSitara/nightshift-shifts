package main

// The brief renderer (NGH-062, docs/instructions-review.md §4-§7b): a role's brief is generated from shift facts in
// a fixed order — the task (human contact only), the "Your shift" facts, the repository and site prose, the role
// page, the hard rules — in one of two modes. Legacy brief files are not tagged, so the role page drops every
// sentence or list item that names a member or feature this crew lacks (crewFeatureWords); rewritten briefs use
// explicit <!-- if KEY -->...<!-- end --> blocks instead.

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type briefLayer struct {
	Name          string
	Words, Budget int
}

type renderedBrief struct {
	Text     string
	Mode     string
	Layers   []briefLayer
	Warnings []string
}

// Word budgets per layer; the shift facts and the hard rules are free.
const (
	roleBudgetStandard = 250
	roleBudgetCompact  = 150
	repositoryBudget   = 150
	siteBudget         = 50
	compactRoleWords   = 30
)

// crewFeatureWords: a sentence or list item of a legacy role page that matches one of a feature's patterns is
// dropped when the crew lacks that member or feature. Matching ignores case.
var crewFeatureWords = map[string][]*regexp.Regexp{
	"apprentice":     {regexp.MustCompile(`(?i)apprentice`)},
	"reserved_coder": {regexp.MustCompile(`(?i)\bsummon`), regexp.MustCompile(`(?i)reserved coder`), regexp.MustCompile(`(?i)coder is reserved`)},
	"advisor":        {regexp.MustCompile(`(?i)advisor`), regexp.MustCompile(`(?i)\bconsult`)},
	"copilot":        {regexp.MustCompile(`(?i)copilot credit`), regexp.MustCompile(`(?i)\bcredits?\b`), regexp.MustCompile(`(?i)max_credits`)},
	"non_claude":     {regexp.MustCompile(`(?i)agents-cli`), regexp.MustCompile(`(?i)non-claude`)},
	"worktree":       {regexp.MustCompile(`(?i)worktree`), regexp.MustCompile(`(?i)\bland(s|ed|ing)?\b`)},
	"queue":          {regexp.MustCompile(`(?i)\bqueue`)},
}

// Text that only a Claude session can act on, and text that drives other panes through agents-cli.
var (
	claudeOnlyWords = []*regexp.Regexp{regexp.MustCompile(`ListAgents`), regexp.MustCompile(`SendMessage`), regexp.MustCompile(`(?i)does not apply to you`)}
	agentsCLIWords  = []*regexp.Regexp{regexp.MustCompile(`(?i)agents-cli`)}
	ifBlock         = regexp.MustCompile(`(?s)<!-- if (\w+) -->(\n?)(.*?)<!-- end -->(\n?)`)
)

// briefModeFor is the published model table: compact for Gemini Flash, Luna, Terra on low reasoning, a Copilot
// pane on a credit share, Mistral models and small Codex models; standard for everything else (Opus, Sonnet,
// Sol, Astra, Argon...). A crew role's brief_mode overrides it.
func briefModeFor(m *crewMember) string {
	if mode := m.optString("brief_mode"); mode != "" {
		return mode
	}
	model := strings.ToLower(m.model)
	switch {
	case strings.Contains(model, "flash"), strings.Contains(model, "luna"), strings.Contains(model, "mistral"),
		strings.Contains(model, "terra") && m.optString("reasoning") == "low",
		m.tool == "copilot" && intValue(m.opt("max_credits")) > 0,
		m.tool == "codex" && (strings.Contains(model, "-mini") || strings.Contains(model, "-nano")):
		return "compact"
	}
	return "standard"
}

func wordCount(s string) int { return len(strings.Fields(s)) }

// crewFeatures says which optional members and features the crew has.
func crewFeatures(stateDir string, crew object, members []*crewMember) map[string]bool {
	f := map[string]bool{"reserved_coder": intValue(rowValue(crew, "coder_slot")) > 0, "queue": intValue(rowValue(crew, "slots")) > 0,
		"copilot": rowValue(crew, "max_credits") != nil, "worktree": truthy(rowValue(crew, "worktree"))}
	if stateDir != "" && isFile(filepath.Join(stateDir, "worktree.json")) {
		f["worktree"] = true
	}
	for _, m := range members {
		switch m.role {
		case "Apprentice":
			f["apprentice"] = true
		case "Advisor":
			f["advisor"] = true
		}
		if m.tool != "" && m.tool != "claude" {
			f["non_claude"] = true
		}
		if m.tool == "copilot" && m.has("max_credits") {
			f["copilot"] = true
		}
	}
	return f
}

func matchesAny(s string, patterns []*regexp.Regexp) bool {
	for _, p := range patterns {
		if p.MatchString(s) {
			return true
		}
	}
	return false
}

// splitSentences splits a paragraph after . ! or ? followed by whitespace and an upper-case letter, digit,
// backtick or quote, never inside `code`.
func splitSentences(p string) []string {
	out := []string{}
	start, ticks := 0, 0
	for i := 0; i < len(p); i++ {
		switch p[i] {
		case '`':
			ticks++
		case '.', '!', '?':
			if ticks%2 == 1 || i+2 >= len(p) || (p[i+1] != ' ' && p[i+1] != '\n') {
				continue
			}
			j := i + 1
			for j < len(p) && (p[j] == ' ' || p[j] == '\n') {
				j++
			}
			if j < len(p) && (p[j] >= 'A' && p[j] <= 'Z' || p[j] >= '0' && p[j] <= '9' || p[j] == '`' || p[j] == '"' || p[j] == '(') {
				out = append(out, p[start:i+1])
				start = j
				i = j - 1
			}
		}
	}
	return append(out, p[start:])
}

var listItem = regexp.MustCompile(`^\s*(?:[-*]|[0-9]+\.)\s`)

// filterPage drops every sentence and list item that matches drop; a paragraph left empty disappears.
func filterPage(text string, drop []*regexp.Regexp) string {
	if len(drop) == 0 {
		return text
	}
	kept := []string{}
	for _, para := range strings.Split(text, "\n\n") {
		if strings.HasPrefix(strings.TrimSpace(para), "#") || strings.TrimSpace(para) == "---" {
			kept = append(kept, para)
			continue
		}
		lines := strings.Split(para, "\n")
		if listItem.MatchString(lines[0]) {
			items := []string{}
			for _, line := range lines {
				if !listItem.MatchString(line) && len(items) > 0 {
					items[len(items)-1] += "\n" + line
				} else {
					items = append(items, line)
				}
			}
			out := []string{}
			for _, item := range items {
				if !matchesAny(item, drop) {
					out = append(out, item)
				}
			}
			if len(out) > 0 {
				kept = append(kept, strings.Join(out, "\n"))
			}
			continue
		}
		if !matchesAny(para, drop) {
			kept = append(kept, para)
			continue
		}
		out := []string{}
		for _, s := range splitSentences(para) {
			if !matchesAny(s, drop) {
				out = append(out, strings.TrimSpace(s))
			}
		}
		if len(out) > 0 {
			kept = append(kept, strings.Join(out, " "))
		}
	}
	return strings.Join(kept, "\n\n")
}

// applyIfBlocks keeps the body of <!-- if KEY --> blocks whose feature the crew has.
func applyIfBlocks(text string, features map[string]bool) string {
	if !ifBlock.MatchString(text) {
		return text // untagged (legacy) pages render unchanged
	}
	return extraBlankLines.ReplaceAllString(ifBlock.ReplaceAllStringFunc(text, func(block string) string {
		m := ifBlock.FindStringSubmatch(block)
		// A block on lines of its own takes its newlines with it; an inline block keeps the line's end.
		tail := m[4]
		if m[2] != "" {
			tail = ""
		}
		if features[m[1]] {
			return m[3] + tail
		}
		return tail
	}), "\n\n")
}

var extraBlankLines = regexp.MustCompile(`\n{3,}`)

// readOnlyPage: a role page that says its role never edits files (Advisor) or repository files (Decompiler,
// Researcher); the generated edit rule says the same instead of claim-before-edit.
var readOnlyPage = regexp.MustCompile(`(?i)never edit (repository )?files`)

func crewMemberList(crew object) []*crewMember {
	members := []*crewMember{}
	items, _ := rowValue(crew, "members").([]any)
	for _, item := range items {
		if o, ok := item.(object); ok {
			members = append(members, memberFromObject(o))
		}
	}
	return members
}

// memberSlot is the slot a member runs in: the slots/<N>.json naming it (spawned members), else its crew order.
func memberSlot(stateDir string, m *crewMember, index int) int {
	if stateDir != "" {
		entries, _ := os.ReadDir(filepath.Join(stateDir, "slots"))
		for _, e := range entries {
			if n, err := strconv.Atoi(strings.TrimSuffix(e.Name(), ".json")); err == nil {
				if slot, ok := readObject(filepath.Join(stateDir, "slots", e.Name())); ok && objectString(slot, "role") == m.label {
					return n
				}
			}
		}
	}
	return index + 1
}

// shiftFacts is the "Your shift" section: members by slot, the human's Ops pane, the worktree and the repository's
// commands from .nightshift/config.yaml.
func shiftFacts(stateDir string, crew object, members []*crewMember, config object) string {
	type row struct {
		slot int
		line string
	}
	rows := []row{}
	for i, m := range members {
		tool, model := m.tool, m.model
		if tool == "" {
			tool = "claude"
		}
		if model == "" {
			model = objectString(crew, "model")
		}
		state := "live"
		reserved := (m.label == advisorLabel && intValue(rowValue(crew, "advisor_slot")) > 0) || (m.label == coderLabel && intValue(rowValue(crew, "coder_slot")) > 0)
		if reserved && (stateDir == "" || !isFile(filepath.Join(stateDir, m.label+".json"))) {
			state = "asleep"
		}
		slot := memberSlot(stateDir, m, i)
		rows = append(rows, row{slot, fmt.Sprintf("| %d | %s | %s | %s | %s |", slot, m.label, tool, model, state)})
	}
	if slots := intValue(rowValue(crew, "slots")); slots > 0 {
		rows = append(rows, row{slots, fmt.Sprintf("| %d | Ops (the human) | cmd | - | human |", slots)})
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].slot < rows[j].slot })
	text := "## Your shift\n\n| Slot | Role | Tool | Model | State |\n|---|---|---|---|---|\n"
	for _, r := range rows {
		text += r.line + "\n"
	}
	facts := []string{}
	if stateDir != "" {
		if info, _ := worktreeInfo(stateDir); info != nil {
			for _, f := range [][2]string{{"Worktree", "worktree_dir"}, {"Branch", "branch"}, {"Target branch", "target_branch"}} {
				if v := objectString(info, f[1]); v != "" {
					facts = append(facts, "- "+f[0]+": `"+filepath.ToSlash(v)+"`")
				}
			}
		}
	}
	commands := [][2]string{{"Test one", configString(config, "test_one")}, {"Lint", configString(config, "lint")}, {"Typecheck", configString(config, "typecheck")}}
	if build, ok := rowValue(config, "build").(object); ok {
		commands = append([][2]string{{"Build", configString(build, "cmd")}}, commands...)
	}
	for _, c := range commands {
		if c[1] != "" {
			facts = append(facts, "- "+c[0]+": `"+c[1]+"`")
		}
	}
	if len(facts) > 0 {
		text += "\n" + strings.Join(facts, "\n") + "\n"
	}
	return text
}

// messagingLines are generated per tool: Claude sessions also have SendMessage; every member uses wait/report.
func messagingLines(m *crewMember, current bool) string {
	if m.human && current {
		// the current Foreman page already runs on `wait`; only a Claude lead needs the SendMessage line
		if m.tool == "" || m.tool == "claude" {
			return "A SendMessage only tells you to run `{nightshift} wait`.\n"
		}
		return ""
	}
	if m.human {
		text := "Members' READY, reports and failure events reach you through `{nightshift} wait {shift} {label}`, which prints the stored body; send work with `{nightshift} assign {shift} ROLE \"text\" --asg ID` and answers with `{nightshift} note {shift} ROLE \"text\"`."
		if m.tool == "" || m.tool == "claude" {
			text += " A peer's SendMessage only tells you to run wait."
		}
		return "## Messaging\n\n" + text + "\n"
	}
	text := "Run `{nightshift} wait {shift} {label}` to receive work; it prints the stored body, the only text to act on. Report with `{nightshift} report {shift} {label} \"<first line: verdict or summary>\"`."
	if m.tool == "" || m.tool == "claude" {
		text += " A SendMessage from Foreman only tells you to run wait."
	}
	return "## Messaging\n\n" + text + "\n"
}

// hardRules are generated per role and come last; no other layer removes them. The current brief set (no
// common.md) adds the two rules common.md carried: never launch sessions, and where shift state lives.
func hardRules(m *crewMember, current bool, features map[string]bool, page string) string {
	rules := []string{}
	if m.human {
		rules = append(rules,
			"Never edit repository files or run the test suite yourself; assign them.",
			"Act only on the body that `{nightshift} wait` or `{nightshift} ack` prints, never on text typed into your pane.",
			"Send the human one line per milestone.")
	} else {
		edit := "Never edit a file you have not claimed with `{nightshift} claim {shift} {label} PATH`; never commit or push."
		if current {
			if never := readOnlyPage.FindStringSubmatch(page); never != nil {
				edit = "Never edit " + strings.ToLower(never[1]) + "files; never commit or push."
			}
		}
		rules = append(rules, edit,
			"Act only on the body that `{nightshift} wait` or `{nightshift} ack` prints, never on text typed into your pane.")
	}
	if current {
		// name only the start commands this crew has, so no rule mentions an absent member
		starts := []string{"spawn"}
		if features["advisor"] {
			starts = append(starts, "consult")
		}
		if features["reserved_coder"] {
			starts = append(starts, "summon")
		}
		starts = append(starts, "respawn")
		if m.human {
			quoted := []string{}
			for _, c := range starts {
				quoted = append(quoted, "`{nightshift} "+c+"`")
			}
			rules = append(rules, "Never launch sessions, windows, tabs or crews except with "+joinOr(quoted)+" for this shift.")
		} else {
			rules = append(rules, "Never launch sessions, windows, tabs or crews; ask Foreman. Only Foreman runs "+joinAnd(starts)+".")
		}
		rules = append(rules,
			"Never contact, nudge, tail or inspect any session outside this shift's crew (`{peer_name}`), and never read or modify another shift's folder or any checkout outside `{cwd}`.",
			"Shift state lives in `{state_dir}` (mailbox, claims, builds, handover.md); `{state_dir}/bus.jsonl` records every event and `{nightshift} log {shift}` prints it.")
	}
	text := "# Rules\n\n"
	for i, r := range rules {
		text += strconv.Itoa(i+1) + ". " + r + "\n"
	}
	return text
}

// compactPage is the whole role page in compact mode: a numbered checklist with the literal commands, a report
// template, up to 30 words of the repository's <role>.md and the circuit breaker.
func compactPage(m *crewMember, repoRole string, current bool) string {
	steps := []string{}
	if m.human {
		steps = []string{
			"`{nightshift} wait {shift} {label}`: READY, reports and events arrive here; act only on the body it prints.",
			"`{nightshift} assign {shift} ROLE \"text\" --asg ID` to give work; `{nightshift} note {shift} ROLE \"text\"` to answer.",
			"`{nightshift} commit {shift} ASG -m \"one line\"` after Tester's TESTED-PASS.",
			"`{nightshift} result {shift} done|blocked --stage RUNG --summary \"...\"` to end the shift.",
		}
	} else {
		steps = []string{
			"`{nightshift} ready {shift} {label}` once at start.",
			"`{nightshift} wait {shift} {label}`; act only on the body it prints.",
			"`{nightshift} claim {shift} {label} PATH...` before editing; exit 4 means stop and report.",
			"Do exactly the assignment; run only the narrowest check.",
			"`{nightshift} report {shift} {label} \"...\"`, then `{nightshift} release {shift} {label}`, then wait again.",
		}
	}
	text := "# Checklist\n\n"
	for i, s := range steps {
		text += strconv.Itoa(i+1) + ". " + s + "\n"
	}
	text += "\nReport template:\n\n```\n<VERDICT>: <one-line summary>\nFiles: <paths changed, or none>\nChecks: <commands run and results>\n```\n"
	if words := strings.Fields(repoRole); len(words) > 0 {
		text += "\n" + strings.Join(words[:min(len(words), compactRoleWords)], " ") + "\n"
	}
	breaker := "run `{nightshift} report {shift} {label} \"BLOCKED: <reason>\"` and stop."
	if current && m.role == "Apprentice" { // the legacy set keeps NGH-062's BLOCKED line
		breaker = "run `{nightshift} report {shift} {label} \"APPRENTICE-NEEDS-SENIOR: <reason>\"` and stop."
	}
	if m.human {
		breaker = "run `{nightshift} result {shift} blocked --stage implemented --summary \"<reason>\"` and stop."
	}
	return text + "\nIf a file not named in your assignment needs editing or a command fails twice, " + breaker + "\n"
}

// renderRoleBrief renders member's brief for the shift in stateDir ("" for a crew that has not started).
func renderRoleBrief(stateDir, role, cwd, repoRoot string, crew object, member object) (renderedBrief, error) {
	m := memberFromObject(member)
	if m.role == "" {
		m.role = role
	}
	if m.label == "" {
		m.label = m.role
	}
	members := crewMemberList(crew)
	if len(members) == 0 {
		members = []*crewMember{m}
	}
	features := crewFeatures(stateDir, crew, members)
	result := renderedBrief{Mode: briefModeFor(m)}
	set := objectString(crew, "briefs")
	legacy := set == "legacy"

	if cwd == "" && stateDir != "" {
		if info, _ := worktreeInfo(stateDir); info != nil {
			cwd = objectString(info, "worktree_dir")
		}
		if cwd == "" {
			cwd = objectString(crew, "cwd")
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
		return result, err
	}
	readOptional := func(p string) (string, error) {
		if !isFile(p) {
			return "", nil
		}
		body, err := readTextUniversal(p)
		return pyStrip(body), err
	}
	repoDir := filepath.Join(repoRoot, ".nightshift")
	repoCommon, err := readOptional(filepath.Join(repoDir, "instructions.md"))
	if err != nil {
		return result, err
	}
	repoRole, err := readOptional(filepath.Join(repoDir, strings.ToLower(m.role)+".md"))
	if err != nil {
		return result, err
	}
	site, err := loadSite()
	if err != nil {
		return result, err
	}

	// The role page: persona, common.md and the role brief, filtered for this crew and this member's tool.
	page := ""
	if result.Mode == "compact" {
		page = compactPage(m, repoRole, !legacy)
	} else {
		page = "# " + m.role + "\n\n# Protocol\n\n"
		persona, _ := m.persona.(string)
		if persona == "" {
			persona = defaultPersonaIn(set, m.role)
		}
		if persona != "" {
			if page, err = personaText(m.role, briefAsset(set, persona)); err != nil {
				return result, err
			}
		}
		personaShown := persona != ""
		if !legacy {
			// The current set has no protocol section; the brief's own title stands when there is no persona.
			page = strings.TrimSuffix(page, "---\n\n# Protocol\n\n")
			if !personaShown {
				page = ""
			}
		}
		brief := m.brief
		if brief == "" {
			brief = strings.ToLower(m.role) + ".md"
		}
		files := []string{brief}
		if legacy {
			files = []string{"common.md", brief}
		}
		for _, name := range files {
			body, err := assetRead("briefs", briefAsset(set, name))
			if err != nil {
				return result, err
			}
			body = pyStrip(body)
			if !legacy && personaShown {
				// the persona already printed "# <role>"; drop the brief's own title line
				if first, rest, _ := strings.Cut(body, "\n"); strings.HasPrefix(first, "# ") {
					body = pyStrip(rest)
				}
				body = "## Your loop\n\n" + body
			}
			page += body + "\n\n"
		}
		page = strings.ReplaceAll(strings.ReplaceAll(page, "# Task\n", ""), humanTaskMarker, "")
		page = applyIfBlocks(page, features)
		drop := []*regexp.Regexp{}
		for _, key := range []string{"apprentice", "reserved_coder", "advisor", "copilot", "non_claude", "worktree", "queue"} {
			if !features[key] {
				drop = append(drop, crewFeatureWords[key]...)
			}
		}
		claude := m.tool == "" || m.tool == "claude"
		if !claude {
			drop = append(drop, claudeOnlyWords...)
		} else if !m.human {
			drop = append(drop, agentsCLIWords...)
		}
		page = strings.TrimRight(filterPage(page, drop), "\n") + "\n\n" + messagingLines(m, !legacy)
	}

	roleBudget := roleBudgetStandard
	if result.Mode == "compact" {
		roleBudget = roleBudgetCompact
	}
	repoProse := strings.TrimSpace(strings.Join(nonEmpty(repoCommon, repoRole), "\n\n"))
	if result.Mode == "compact" {
		repoProse = strings.TrimSpace(repoCommon) // the role file's first words are in the checklist
	}
	result.Layers = []briefLayer{{"role", wordCount(page), roleBudget}, {"repository", wordCount(repoProse), repositoryBudget}, {"site", wordCount(site.Prose), siteBudget}}
	for _, l := range result.Layers {
		if l.Words > l.Budget {
			result.Warnings = append(result.Warnings, fmt.Sprintf("brief %s: %s layer %d words, budget %d", m.label, l.Name, l.Words, l.Budget))
		}
	}

	text := shiftFacts(stateDir, crew, members, config) + "\n"
	if repoProse != "" {
		text += "# This repository\n\n" + repoProse + "\n\n"
	}
	if prose := strings.TrimSpace(site.Prose); prose != "" {
		text += "# This site\n\n" + prose + "\n\n"
	}
	text += strings.TrimRight(page, "\n") + "\n\n" + hardRules(m, !legacy, features, page)

	shift, statePosix := "", ""
	if stateDir != "" {
		shift, statePosix = filepath.Base(stateDir), filepath.ToSlash(stateDir)
	}
	for _, kv := range [][2]string{{"{role}", m.role}, {"{label}", m.label}, {"{identity}", m.identity}, {"{peer_name}", m.peer},
		{"{state_dir}", statePosix}, {"{shift}", shift}, {"{nightshift}", nightshiftCommand()}, {"{cwd}", filepath.ToSlash(cwd)}} {
		text = strings.ReplaceAll(text, kv[0], kv[1])
	}
	// The task comes first and is never substituted: it is the human's text, verbatim.
	if m.human {
		task := ""
		if stateDir != "" {
			task, _ = readTextUniversal(filepath.Join(stateDir, "prompt.txt"))
		}
		text = "# Task\n\n" + humanTaskMarker + "\n\n" + strings.TrimRight(task, "\n") + "\n\n" + text
	}
	result.Text = text
	return result, nil
}

func nonEmpty(items ...string) []string {
	out := []string{}
	for _, s := range items {
		if strings.TrimSpace(s) != "" {
			out = append(out, s)
		}
	}
	return out
}

// briefPlan is a member's brief summary for the start --dry-run plan: mode, words per layer and warnings.
func briefPlan(stateDir, cwd string, crew object, m *crewMember) object {
	r, err := renderRoleBrief(stateDir, m.role, cwd, "", crew, m.object())
	if err != nil {
		return object{{"brief_mode", nil}, {"brief_layers", nil}, {"brief_warnings", []any{err.Error()}}}
	}
	layers := object{}
	for _, l := range r.Layers {
		layers = append(layers, pair{l.Name, l.Words})
	}
	warnings := []any{}
	for _, w := range r.Warnings {
		warnings = append(warnings, w)
	}
	return object{{"brief_mode", r.Mode}, {"brief_layers", layers}, {"brief_warnings", warnings}}
}

// joinAnd and joinOr list items in prose: "a, b and c", "a, b or c".
func joinAnd(items []string) string { return joinList(items, " and ") }
func joinOr(items []string) string  { return joinList(items, " or ") }
func joinList(items []string, last string) string {
	if len(items) < 2 {
		return strings.Join(items, "")
	}
	return strings.Join(items[:len(items)-1], ", ") + last + items[len(items)-1]
}
