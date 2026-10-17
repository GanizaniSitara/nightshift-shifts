package main

// `nightshift crews`: the crew manifests visible from a folder, in lookup
// order (machine > repo > built-in), and which one --crew NAME would pick.

const crewsUsage = `usage: nightshift crews [-h] [--cwd CWD]`

const crewsHelp = crewsUsage + `

List the crew manifests start --crew NAME can use, as JSON: one entry per
manifest file with its name, source (machine = <state root>/crews, repo =
<repo>/.nightshift/crews, built-in = crews/), path, and active = true for the
file --crew NAME picks (the first in that order) and false for those it shadows.

options:
  -h, --help  show this help message and exit
  --cwd CWD   folder whose repository supplies the repo layer (default: the
              current directory)
`

func init() { extraCommands["crews"] = crewsCommand }

func crewsCommand(argv []string) int {
	args, parseError := parseWriteArgs(argv, []writeOption{{name: "--cwd"}}, nil, nil)
	if args.help {
		out(crewsHelp)
		return 0
	}
	if parseError != "" {
		return writeUsage("nightshift crews", crewsUsage, parseError)
	}
	entries := []any{}
	seen := map[string]bool{}
	for _, src := range crewSources(canonicalRepoRoot(args.values["--cwd"], "")) {
		entries = append(entries, object{{"name", src.name}, {"source", src.source}, {"path", src.path}, {"active", !seen[src.name]}})
		seen[src.name] = true
	}
	if humanOutput() {
		humanCrews(entries)
		return 0
	}
	printResult(entries, true, false)
	return 0
}
