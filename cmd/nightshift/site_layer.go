package main

// The site layer (NGH-062): machine settings the tool enforces, from <state root>/site.yaml, plus the site prose in
// <state root>/instructions.md. docs/run-at-work.md "Site layer".

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v2"
)

type siteConfig struct {
	AllowedTools, AllowedModels []string
	Env                         map[string]string
	WebSearch                   bool
	Prose                       string
	Path                        string
}

// loadSite reads site.yaml and instructions.md from the state root; absent files give the zero value (no limits)
// with web search on.
func loadSite() (siteConfig, error) {
	root := stateRoot()
	site := siteConfig{WebSearch: true}
	if prose, err := os.ReadFile(filepath.Join(root, "instructions.md")); err == nil {
		site.Prose = strings.TrimSpace(string(prose))
	} else if !errors.Is(err, os.ErrNotExist) {
		return site, err
	}
	p := filepath.Join(root, "site.yaml")
	data, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return site, nil
	}
	if err != nil {
		return site, err
	}
	var raw struct {
		AllowedTools  []string          `yaml:"allowed_tools"`
		AllowedModels []string          `yaml:"allowed_models"`
		Env           map[string]string `yaml:"env"`
		WebSearch     *bool             `yaml:"web_search"`
	}
	var keys yaml.MapSlice
	if err := yaml.Unmarshal(data, &keys); err != nil {
		return site, fmt.Errorf("%s: site.yaml must be a mapping", p)
	}
	if unknown := unknownYAMLKeys(keys, []string{"allowed_tools", "allowed_models", "env", "web_search"}); len(unknown) > 0 {
		return site, fmt.Errorf("%s: unknown site keys: %s", p, strings.Join(unknown, ", "))
	}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return site, fmt.Errorf("%s: %s", p, strings.Join(strings.Fields(err.Error()), " "))
	}
	site.AllowedTools, site.AllowedModels, site.Env, site.Path = raw.AllowedTools, raw.AllowedModels, raw.Env, p
	if raw.WebSearch != nil {
		site.WebSearch = *raw.WebSearch
	}
	return site, nil
}

// siteEnv is the site environment crewEnv applies to every member (nil when there is none or site.yaml is invalid;
// start and doctor refuse an invalid one).
func siteEnv() map[string]string {
	site, _ := loadSite()
	return site.Env
}

// siteWebFlags are the CLI flags that turn web search off when site.yaml says web_search: false. Codex searches only
// with --search, which Nightshift never passes, and agy has no such flag, so both get none.
func siteWebFlags(tool string) []string {
	site, err := loadSite()
	if err != nil || site.WebSearch {
		return nil
	}
	switch tool {
	case "", "claude":
		return []string{"--disallowedTools", "WebSearch,WebFetch"}
	case "copilot":
		return []string{"--deny-tool", "url"}
	}
	return nil
}

// siteAllows reports whether value is in the allow-list: empty means no limit; entries may be path.Match globs
// (claude-opus-*). Tool names also match their spawn --cli alias (gemini = agy).
func siteAllows(list []string, value string) bool {
	if len(list) == 0 {
		return true
	}
	for _, allowed := range list {
		if ok, _ := path.Match(allowed, value); ok || allowed == value || spawnCLIs[allowed] == value {
			return true
		}
	}
	return false
}

// siteCheckMembers refuses (exit 4, one line) the first member whose tool or model site.yaml does not allow.
// Each member is {label, tool, model}; an empty tool is claude.
func siteCheckMembers(site siteConfig, members [][3]string) error {
	for _, m := range members {
		label, tool, model := m[0], m[1], m[2]
		if tool == "" {
			tool = "claude"
		}
		if !siteAllows(site.AllowedTools, tool) {
			return commandError{4, fmt.Sprintf("%s: %s uses tool %s, not in allowed_tools (%s)", site.Path, label, tool, strings.Join(site.AllowedTools, ", "))}
		}
		if model != "" && !siteAllows(site.AllowedModels, model) {
			return commandError{4, fmt.Sprintf("%s: %s uses model %s, not in allowed_models (%s)", site.Path, label, model, strings.Join(site.AllowedModels, ", "))}
		}
	}
	return nil
}
