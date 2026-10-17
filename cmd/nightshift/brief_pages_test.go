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
		setValue(tbCrewMember("Decompiler", "Decompiler", "claude", "claude-opus-5-5"), "persona", ""),
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
