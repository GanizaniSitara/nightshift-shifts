package main

import (
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// NGH-063: the new one-page set is the default, common.md is retired into generated Rules, and a crew with
// `briefs: legacy` renders briefs/legacy + personas/legacy (+ legacy common.md) as NGH-062 did. Uses the
// brief_render_test.go fixture helpers and the built-in assets.

const legacyCommonSentence = "records the whole shift" // legacy common.md "Shift bus:" sentence; survives filtering

func TestNewBriefsDoNotReadCommonMd(t *testing.T) {
	f := tbNewRenderFixture(t)
	for label, text := range tbRenderAll(f, tbDefaultCrew()) {
		if strings.Contains(text, legacyCommonSentence) || strings.Contains(text, "Startup gate") {
			t.Fatalf("%s: the new set must not include legacy common.md text", label)
		}
	}
}

var neverLaunch = regexp.MustCompile(`(?i)never (launch|start)[^\n]*(session|window|crew)`)

func TestGeneratedRulesCarryCommonMdHardRules(t *testing.T) {
	f := tbNewRenderFixture(t)
	for label, text := range tbRenderAll(f, tbDefaultCrew()) {
		rules := tbRulesSection(t, text)
		if !neverLaunch.MatchString(rules) {
			t.Fatalf("%s rules lack the never-launch rule:\n%s", label, rules)
		}
		if !strings.Contains(rules, filepath.ToSlash(f.stateDir)) && !strings.Contains(rules, "bus.jsonl") {
			t.Fatalf("%s rules lack where state lives (<state_dir>, the bus):\n%s", label, rules)
		}
		if !strings.Contains(rules, "outside this shift's crew") || !strings.Contains(rules, filepath.ToSlash(f.repo)) {
			t.Fatalf("%s rules lack shift/session/checkout isolation rule:\n%s", label, rules)
		}
	}
	foreman := tbRulesSection(t, tbRenderAll(f, tbDefaultCrew())["Foreman"])
	if !regexp.MustCompile(`(?i)spawn`).MatchString(foreman) {
		t.Fatalf("Foreman's never-launch rule names the controlled start paths (spawn/consult/summon/respawn):\n%s", foreman)
	}
}

func TestLegacySwitchRendersLegacySet(t *testing.T) {
	f := tbNewRenderFixture(t)
	legacy := tbWithKey(tbDefaultCrew(), "briefs", "legacy")
	newText := f.render(tbDefaultCrew(), "Coder").Text
	oldText := f.render(legacy, "Coder").Text
	if !strings.Contains(oldText, legacyCommonSentence) {
		t.Fatal("briefs: legacy must include the legacy common.md text")
	}
	if strings.Contains(newText, legacyCommonSentence) {
		t.Fatal("the default set must not")
	}
	if len(strings.Fields(oldText)) <= len(strings.Fields(newText)) {
		t.Fatalf("the legacy page should be the longer one: legacy %d words, new %d", len(strings.Fields(oldText)),
			len(strings.Fields(newText)))
	}
}

func TestNewRolePagesWithinBudget(t *testing.T) {
	f := tbNewRenderFixture(t)
	crew := tbCrewOf(tbCrewMember("Foreman", "Foreman", "claude", "claude-opus-5-5"),
		tbCrewMember("Apprentice", "Apprentice", "agy", "gemini-3.8-flash-high"),
		tbCrewMember("Coder", "Coder", "claude", "claude-opus-5-5"),
		tbCrewMember("Tester", "Tester", "claude", "claude-opus-5-5"),
		tbCrewMember("Advisor", "Advisor", "claude", "claude-fable-5-1"),
		// no persona file of their own: the default persona applies, as in crews/specialists.yaml
		setValue(tbCrewMember("Researcher", "Researcher", "claude", "claude-opus-5-5"), "persona", ""),
		tbCrewMember("UITester", "UITester", "claude", "claude-opus-5-5"))
	crew = tbWithKey(tbWithKey(crew, "advisor_slot", 5), "coder_slot", 3)
	members, _ := rowValue(crew, "members").([]any)
	for _, item := range members {
		m := item.(object)
		out, err := renderRoleBrief(f.stateDir, objectString(m, "role"), f.repo, f.repo, crew, m)
		if err != nil {
			t.Fatalf("%s: %v", objectString(m, "label"), err)
		}
		for _, w := range out.Warnings {
			if strings.Contains(w, " role layer ") {
				t.Errorf("%s over budget: %s", objectString(m, "label"), w)
			}
		}
		for _, bad := range []string{"does not apply", "{nightshift}", "{shift}", "{role}", "{label}"} {
			if strings.Contains(out.Text, bad) {
				t.Errorf("%s rendered text contains %q", objectString(m, "label"), bad)
			}
		}
	}
}

// ---------------------------------------------------------------- c9 fix-forward

func TestCompactApprenticeBreakerIsTheNeedsSeniorNote(t *testing.T) {
	f := tbNewRenderFixture(t)
	crew := tbCrewOf(tbCrewMember("Foreman", "Foreman", "claude", "claude-opus-5-5"),
		tbCrewMember("Apprentice", "Apprentice", "agy", "gemini-3.8-flash-high"),
		tbCrewMember("Coder", "Coder", "claude", "claude-opus-5-5", pair{"brief_mode", "compact"}))
	appr := f.render(crew, "Apprentice")
	if appr.Mode != "compact" {
		t.Fatalf("Flash Apprentice renders %s", appr.Mode)
	}
	if tbLineWith(appr.Text, " report Nightshift-shift-render Apprentice \"APPRENTICE-NEEDS-SENIOR: <reason>\"") == "" {
		t.Fatalf("compact Apprentice breaker must be the APPRENTICE-NEEDS-SENIOR report:\n%s", appr.Text)
	}
	if strings.Contains(appr.Text, "BLOCKED: <reason>") {
		t.Fatal("the Apprentice's breaker replaces BLOCKED")
	}
	coder := f.render(crew, "Coder").Text
	if tbLineWith(coder, " report Nightshift-shift-render Coder \"BLOCKED: <reason>\"") == "" || strings.Contains(coder, "APPRENTICE-NEEDS-SENIOR") {
		t.Fatalf("other compact roles keep BLOCKED:\n%s", coder)
	}
}

func TestCurrentPagesPutTheRoleBriefUnderYourLoop(t *testing.T) {
	f := tbNewRenderFixture(t)
	for _, label := range []string{"Coder", "Tester"} {
		text := f.render(tbDefaultCrew(), label).Text
		loop := tbIndexOrFail(t, text, "## Your loop")
		good := tbIndexOrFail(t, text, "## What good looks like")
		first := regexp.MustCompile(`(?m)^1\. `).FindStringIndex(text)
		if first == nil || !(good < loop && loop < first[0]) {
			t.Fatalf("%s: '## Your loop' must sit between the persona and the numbered loop", label)
		}
		if strings.Count(text, "## Your loop") != 1 {
			t.Fatalf("%s: one loop heading", label)
		}
	}
	legacy := f.render(tbWithKey(tbDefaultCrew(), "briefs", "legacy"), "Coder").Text
	if strings.Contains(legacy, "## Your loop") {
		t.Fatal("legacy renders unchanged: no loop heading")
	}
}

func TestReadOnlyRolesGetTheirOwnEditRule(t *testing.T) {
	f := tbNewRenderFixture(t)
	crew := tbCrewOf(tbCrewMember("Foreman", "Foreman", "claude", "claude-opus-5-5"),
		tbCrewMember("Advisor", "Advisor", "claude", "claude-fable-5-1"),
		setValue(tbCrewMember("Researcher", "Researcher", "claude", "claude-opus-5-5"), "persona", ""),
		tbCrewMember("Coder", "Coder", "claude", "claude-opus-5-5"),
		tbCrewMember("Tester", "Tester", "claude", "claude-opus-5-5"),
		tbCrewMember("UITester", "UITester", "claude", "claude-opus-5-5"))
	crew = tbWithKey(crew, "advisor_slot", 2)
	want := map[string]string{
		"Advisor":    "1. Never edit files; never commit or push.",
		"Researcher": "1. Never edit repository files; never commit or push.",
	}
	for label, rule := range want {
		rules := tbRulesSection(t, f.render(crew, label).Text)
		if !strings.Contains(rules, rule) {
			t.Fatalf("%s rules lack %q:\n%s", label, rule, rules)
		}
		if strings.Contains(rules, "have not claimed") {
			t.Fatalf("%s never edits: no claim rule:\n%s", label, rules)
		}
	}
	for _, label := range []string{"Coder", "Tester", "UITester"} {
		if !strings.Contains(tbRulesSection(t, f.render(crew, label).Text), "Never edit a file you have not claimed") {
			t.Fatalf("%s keeps the claim rule", label)
		}
	}
	legacy := tbRulesSection(t, f.render(tbWithKey(crew, "briefs", "legacy"), "Advisor").Text)
	if !strings.Contains(legacy, "Never edit a file you have not claimed") {
		t.Fatalf("legacy renders unchanged (claim rule for the legacy Advisor):\n%s", legacy)
	}
}
