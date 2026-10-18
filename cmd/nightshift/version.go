package main

// `nightshift -v`: the git commit this binary was built from (Go records it at build time in a checkout),
// and where the binary is, so an old copy earlier on PATH is easy to spot.

import (
	"runtime/debug"
	"strings"
)

// buildCommit is stamped by scripts/build.cmd from .gitpull.version when building outside a git checkout.
var buildCommit = ""

func versionText() string {
	revision, when, dirty := "unknown", "", false
	if buildCommit != "" {
		revision = buildCommit
		if len(revision) > 7 {
			revision = revision[:7]
		}
	}
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, s := range info.Settings {
			switch s.Key {
			case "vcs.revision":
				revision = s.Value
				if len(revision) > 7 {
					revision = revision[:7]
				}
			case "vcs.time":
				when = strings.Replace(strings.TrimSuffix(s.Value, "Z"), "T", " ", 1)
			case "vcs.modified":
				dirty = s.Value == "true"
			}
		}
	}
	text := "nightshift " + revision
	if dirty {
		text += " (with uncommitted changes)"
	}
	if when != "" {
		text += ", committed " + when + " UTC"
	}
	return text + "\n" + launcherExe()
}
