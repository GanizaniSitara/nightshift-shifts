package main

// Crew panes are started by Windows Terminal, so they inherit Terminal's environment as it was when Terminal
// itself started. A variable saved to the user or system environment later (an API key, say) is then missing
// from every pane even though new console windows have it. withSavedEnv adds such variables back from the
// registry before a crew member's CLI starts. It only adds names the process does not have: anything already
// set (PATH included) is left exactly as inherited.

import (
	"strings"

	"golang.org/x/sys/windows/registry"
)

func withSavedEnv(environ []string) []string {
	have := map[string]bool{}
	for _, kv := range environ {
		if k, _, ok := strings.Cut(kv, "="); ok && k != "" {
			have[strings.ToUpper(k)] = true
		}
	}
	out := append([]string{}, environ...)
	// User variables take precedence over system ones of the same name, as in a new console.
	for _, src := range []struct {
		root registry.Key
		path string
	}{
		{registry.CURRENT_USER, `Environment`},
		{registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Control\Session Manager\Environment`},
	} {
		key, err := registry.OpenKey(src.root, src.path, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		names, _ := key.ReadValueNames(0)
		for _, name := range names {
			if name == "" || have[strings.ToUpper(name)] {
				continue
			}
			value, kind, err := key.GetStringValue(name)
			if err != nil {
				continue
			}
			if kind == registry.EXPAND_SZ {
				if expanded, err := registry.ExpandString(value); err == nil {
					value = expanded
				}
			}
			out = append(out, name+"="+value)
			have[strings.ToUpper(name)] = true
		}
		key.Close()
	}
	return out
}
