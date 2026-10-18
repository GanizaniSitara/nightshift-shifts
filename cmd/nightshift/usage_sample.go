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

// paneScreen is the visible text of a pane (agents-cli tail); tests stub it.
var paneScreen = func(pid uint32) string {
	lines, _ := rowValue(agentTail(pid), "lines").([]any)
	text := make([]string, 0, len(lines))
	for _, l := range lines {
		text = append(text, valueText(l))
	}
	return strings.Join(text, "\n")
}

var footerPatterns = map[string]struct {
	unit string
	re   *regexp.Regexp
}{
	"copilot": {"credits", regexp.MustCompile(`(?i)(\d[\d,]*(?:\.\d+)?)\s*AIC\b`)},
	"claude":  {"usd", regexp.MustCompile(`\$\s?(\d[\d,]*(?:\.\d+)?)`)},
	"agy":     {"context_left", regexp.MustCompile(`(?i)(\d+(?:\.\d+)?)\s*%\s*context left`)},
	"codex":   {"context_left", regexp.MustCompile(`(?i)(\d+(?:\.\d+)?)\s*%\s*context left`)},
}

// parseFooter reads one figure from a pane's visible text: Copilot "Session: N AIC used" (credits), the Claude
// status line's "$1.23" (usd), agy and Codex "N% context left" (context_left). The last match on screen wins, as
// footers sit at the bottom. An empty tool is claude.
func parseFooter(tool, screen string) (usageSample, bool) {
	if tool == "" {
		tool = "claude"
	}
	p, ok := footerPatterns[tool]
	if !ok {
		return usageSample{}, false
	}
	matches := p.re.FindAllStringSubmatch(screen, -1)
	if len(matches) == 0 {
		return usageSample{}, false
	}
	value, err := strconv.ParseFloat(strings.ReplaceAll(matches[len(matches)-1][1], ",", ""), 64)
	if err != nil {
		return usageSample{}, false
	}
	return usageSample{Unit: p.unit, Value: value, Source: "screen"}, true
}

// sampleMember takes one figure for a crew member: Claude and Codex token totals from the transcript recorded in
// <Role>.json (NGH-048), else the footer on its screen. Copilot and agy write no usage file Nightshift can read
// (the bundled Copilot rejects --usage-output-file), so they are read from the screen. A Claude pane's dollars come
// from its status line, which records them itself (statusline --state-dir).
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
	if transcript := objectString(info, "transcript"); transcript != "" && (tool == "claude" || tool == "codex") {
		if usage := harvestTranscript(transcript, tool); usage != nil {
			if total, ok := objectNumber(usage, "total_tokens"); ok {
				return usageSample{Role: label, Unit: "tokens", Source: "transcript", Value: numberFloat(total), At: now}, true
			}
		}
	}
	pid := processNumber(info, "pid")
	if pid == 0 {
		return usageSample{}, false
	}
	s, ok := parseFooter(tool, paneScreen(pid))
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
