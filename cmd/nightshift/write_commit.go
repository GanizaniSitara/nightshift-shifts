package main

// `nightshift commit SHIFT ASG -m MSG` (NGH-061): commit exactly the working tree Tester passed for ASG. The
// pass is the latest Tester TESTED-PASS result for ASG (or for its verification assignment), the build is the
// first builds/<n>.json it cites, and the current tree id must equal that build's recorded tree.

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

func init() { extraCommands["commit"] = commitCommand }

const commitUsage = "usage: nightshift commit [-h] -m MESSAGE shift asg"

const commitHelp = commitUsage + `

Commit the working tree Tester passed for ASG: the latest TESTED-PASS result
for ASG must cite a builds/<n>.json that exited 0, and the current tree must be
the tree that build recorded. Stages everything and commits; never amends.
Exit 4 = refused (nothing committed).

positional arguments:
  shift       shift name, unique suffix, or "latest"
  asg         the coding assignment id

options:
  -h, --help  show this help message and exit
  -m MESSAGE  the one-line commit message
`

var buildReference = regexp.MustCompile(`builds[/\\]([0-9]+)\.json`)

func commitCommand(argv []string) int {
	a, parseErr := parseWriteArgs(argv, []writeOption{{name: "-m"}}, []string{"shift", "asg"}, nil)
	if a.help {
		out(commitHelp)
		return 0
	}
	usage := func(message string) int {
		return busExit(commandError{2, "nightshift commit: error: " + message + `; usage: nightshift commit SHIFT ASG -m "one line"`})
	}
	if parseErr != "" {
		return usage(parseErr)
	}
	message := a.values["-m"]
	if !a.present["-m"] || strings.TrimSpace(message) == "" {
		return usage("give the commit message with -m")
	}
	if strings.ContainsAny(message, "\r\n") {
		return usage("the commit message must be a single line")
	}
	dir, err := resolveShift(a.pos[0])
	if err != nil {
		return busExit(err)
	}
	asg := a.pos[1]
	rows, err := readMessages(dir)
	if err != nil {
		return busExit(err)
	}
	var pass object
	for _, r := range rows {
		if rowString(r, "kind") != "result" || memberRole(dir, rowString(r, "sender")) != "Tester" || !strings.HasPrefix(rowString(r, "body"), "TESTED-PASS") {
			continue
		}
		matches := rowValue(r, "asg") == asg
		for _, v := range rows {
			if rowString(v, "kind") == "assignment" && rowValue(v, "asg") != nil && rowValue(v, "asg") == rowValue(r, "asg") && rowString(v, "recipient") == rowString(r, "sender") && rowValue(v, "coding_asg") == asg {
				matches = true
			}
		}
		if matches {
			pass = r
		}
	}
	refuse := func(format string, args ...any) int { return busExit(commandError{4, fmt.Sprintf(format, args...)}) }
	if pass == nil {
		return refuse("no Tester TESTED-PASS for %s; have Tester verify it, then commit", asg)
	}
	ref := buildReference.FindStringSubmatch(rowString(pass, "body"))
	if ref == nil {
		return refuse("Tester's pass msg:%v cites no builds/<n>.json; ask Tester to report the build it passed", rowValue(pass, "id"))
	}
	build := "builds/" + ref[1] + ".json"
	record, ok := readObject(filepath.Join(dir, "builds", ref[1]+".json"))
	if !ok {
		return refuse("%s cited by msg:%v does not exist; ask Tester to rebuild and pass again", build, rowValue(pass, "id"))
	}
	if code, ok := rowNumber(record, "exit"); !ok || code.String() != "0" {
		return refuse("%s did not exit 0; nothing to commit until Tester passes a green build", build)
	}
	want := objectString(record, "tree")
	cwd := objectString(record, "cwd")
	if want == "" || cwd == "" {
		return refuse("%s recorded no tree; have Tester rerun nightshift build and pass it", build)
	}
	if current := gitTree(cwd); current != want {
		return refuse("the working tree differs from what Tester passed in %s; have Tester verify the current tree", build)
	}
	git := func(args ...string) (string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, args...)...)
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		if err != nil {
			line := strings.TrimSpace(strings.SplitN(strings.TrimSpace(stderr.String()+"\n"+string(out)), "\n", 2)[0])
			return "", fmt.Errorf("git %s failed: %s", args[0], line)
		}
		return strings.TrimSpace(string(out)), nil
	}
	if _, err = git("add", "-A"); err != nil {
		return busExit(err)
	}
	if _, err = git("commit", "-m", message); err != nil {
		return busExit(err)
	}
	sha, err := git("rev-parse", "HEAD")
	if err != nil {
		return busExit(err)
	}
	n, _ := strconv.Atoi(ref[1])
	busAppend(dir, "commit", object{{"asg", asg}, {"sha", sha}, {"build", n}})
	outLine(fmt.Sprintf("committed %s for %s (%s)", sha, asg, build))
	return 0
}
