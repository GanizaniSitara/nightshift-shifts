package main

// Cost sampling (NGH-066): one figure per live member per patrol tick, from a file source when the member's CLI
// writes one, else from the footer on its screen. Samples go to <state_dir>/<Role>-usage.json; the patrol sums the
// latest figure per member per unit against the shift's budgets. docs/ARCHITECTURE.md "Cost sampling (NGH-066)".

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type usageSample struct {
	Role, Unit, Source string // Unit: "usd" | "credits" | "tokens" | "context_left"
	Value, At          float64
}

// usageKeep is how many samples a <Role>-usage.json keeps.
const usageKeep = 200

// paneScreen is the visible text of a pane (the last 40 lines, console.Tail); tests stub it. Only the lines are
// used, so the tool is left to agentTail's default.
var paneScreen = func(pid uint32) string {
	return strings.Join(agentTail(pid, "").Lines, "\n")
}

type footerPattern struct {
	unit string
	re   *regexp.Regexp
	tail int // match only in the last tail non-empty lines (the status line); 0 = anywhere on screen
}

// contextLeft is the agy and Codex default footer "83% context left"; Codex's configured status line renders
// context-remaining as "Context 83% left".
var contextLeft = regexp.MustCompile(`(?i)(?:(\d+(?:\.\d+)?)\s*%\s*context left|context\s+(\d+(?:\.\d+)?)\s*%\s*left)`)

// footerPatterns are tried in order, so a budgeted unit wins over context_left. Codex 0.163 renders the
// tui.status_line items estimated-thread-cost as "~$1.82" and thread-credits as "5.2 credits" (Enterprise
// workspaces only; omitted when unavailable).
var footerPatterns = map[string][]footerPattern{
	"copilot": {{"credits", regexp.MustCompile(`(?i)(\d[\d,]*(?:\.\d+)?)\s*AIC\b`), 0}},
	"claude":  {{"usd", regexp.MustCompile(`\$\s?(\d[\d,]*(?:\.\d+)?)`), 0}},
	"agy":     {{"context_left", contextLeft, 0}},
	"codex": {
		// cost and credits only on the status line, so "$1" or "5 credits" in the conversation never counts
		{"usd", regexp.MustCompile(`~\$\s?(\d[\d,]*(?:\.\d+)?)`), 3},
		{"credits", regexp.MustCompile(`(?i)(\d[\d,]*(?:\.\d+)?)\s*credits\b`), 3},
		{"context_left", contextLeft, 0},
	},
}

// parseFooter reads one figure from a pane's visible text: Copilot "Session: N AIC used" (credits), the Claude
// status line's "$1.23" (usd), Codex "~$1.82" (usd), else "5.2 credits" (credits), else its context left, and agy
// "N% context left" (context_left). The last match on screen wins, as footers sit at the bottom. An empty tool is
// claude.
func parseFooter(tool, screen string) (usageSample, bool) {
	if tool == "" {
		tool = "claude"
	}
	for _, p := range footerPatterns[tool] {
		text := screen
		if p.tail > 0 {
			text = lastLines(screen, p.tail)
		}
		matches := p.re.FindAllStringSubmatch(text, -1)
		if len(matches) == 0 {
			continue
		}
		last := matches[len(matches)-1]
		figure := last[1]
		if figure == "" && len(last) > 2 {
			figure = last[2]
		}
		if value, err := strconv.ParseFloat(strings.ReplaceAll(figure, ",", ""), 64); err == nil {
			return usageSample{Unit: p.unit, Value: value, Source: "screen"}, true
		}
	}
	return usageSample{}, false
}

// lastLines is the last n non-empty lines of a screen.
func lastLines(screen string, n int) string {
	lines := []string{}
	for _, l := range strings.Split(screen, "\n") {
		if strings.TrimSpace(l) != "" {
			lines = append(lines, l)
		}
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// sampleMember takes one figure for a crew member: a Codex footer's cost or credits first (NGH-067), then Claude and
// Codex token totals from the transcript recorded in <Role>.json (NGH-048), else the footer on its screen. Copilot
// and agy write no usage file Nightshift can read (the bundled Copilot rejects --usage-output-file), so they are read
// from the screen. A Claude pane's dollars come from its status line, which records them itself (statusline
// --state-dir).
func sampleMember(dir string, member object) (usageSample, bool) {
	label := objectString(member, "label")
	info, _ := readObject(filepath.Join(dir, label+".json"))
	tool := objectString(member, "tool")
	if tool == "" {
		tool = objectString(info, "tool")
	}
	if tool == "" {
		tool = "claude"
	}
	now := epochNow()
	pid := processNumber(info, "pid")
	screen := ""
	if pid != 0 && tool == "codex" {
		// a Codex footer with a budgeted figure (cost or credits) wins over the transcript's tokens
		screen = paneScreen(pid)
		if s, ok := parseFooter(tool, screen); ok && s.Unit != "context_left" {
			s.Role, s.At = label, now
			return s, true
		}
	}
	if transcript := objectString(info, "transcript"); transcript != "" && (tool == "claude" || tool == "codex") {
		if usage := harvestTranscript(transcript, tool); usage != nil {
			if total, ok := objectNumber(usage, "total_tokens"); ok {
				return usageSample{Role: label, Unit: "tokens", Source: "transcript", Value: numberFloat(total), At: now}, true
			}
		}
	}
	if pid == 0 {
		return usageSample{}, false
	}
	if screen == "" {
		screen = paneScreen(pid)
	}
	s, ok := parseFooter(tool, screen)
	if !ok {
		return usageSample{}, false
	}
	s.Role, s.At = label, now
	return s, true
}

func usagePath(dir, role string) string { return filepath.Join(dir, role+"-usage.json") }

// recordUsage appends a sample to <dir>/<Role>-usage.json {"role", "samples": [{"at", "unit", "value", "source"}],
// "latest": {...}}, keeping the last usageKeep samples. A file in another shape is started again.
func recordUsage(dir string, s usageSample) error {
	path := usagePath(dir, s.Role)
	lock := path + ".lock"
	if err := takeFileLock(lock, 5*time.Second, 30*time.Second); err != nil {
		return err
	}
	defer releaseFileLock(lock)
	existing, _ := readObject(path)
	samples, _ := rowValue(existing, "samples").([]any)
	sample := object{{"at", s.At}, {"unit", s.Unit}, {"value", s.Value}, {"source", s.Source}}
	samples = append(samples, sample)
	if len(samples) > usageKeep {
		samples = samples[len(samples)-usageKeep:]
	}
	return writeJSONFile(path, object{{"role", s.Role}, {"samples", samples}, {"latest", sample}})
}

// latestUsage is a usage file's most recent sample per unit.
func latestUsage(file object) map[string]object {
	latest := map[string]object{}
	samples, _ := rowValue(file, "samples").([]any)
	for _, item := range samples {
		sample, ok := item.(object)
		if !ok {
			continue
		}
		unit := objectString(sample, "unit")
		at, _ := objectNumber(sample, "at")
		if prior, seen := latest[unit]; seen {
			if priorAt, _ := objectNumber(prior, "at"); numberFloat(priorAt) > numberFloat(at) {
				continue
			}
		}
		latest[unit] = sample
	}
	return latest
}

// shiftUsageTotals is the latest figure of every member, summed per unit.
func shiftUsageTotals(dir string) map[string]float64 {
	totals := map[string]float64{}
	paths, _ := filepath.Glob(filepath.Join(dir, "*-usage.json"))
	for _, path := range paths {
		file, ok := readObject(path)
		if !ok {
			continue
		}
		for unit, sample := range latestUsage(file) {
			if v, ok := objectNumber(sample, "value"); ok {
				totals[unit] += numberFloat(v)
			}
		}
	}
	return totals
}

// usageText is a figure as the status line shows it.
func usageText(unit string, value float64) string {
	switch unit {
	case "usd":
		return fmt.Sprintf("$%.2f", value)
	case "context_left":
		return strconv.FormatFloat(value, 'f', -1, 64) + "% context left"
	}
	return strconv.FormatFloat(value, 'f', -1, 64) + " " + unit
}
