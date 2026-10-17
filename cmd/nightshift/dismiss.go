package main

// `nightshift dismiss SHIFT ROLE`: the Foreman stops one crew member and frees its slot, without ending the shift.
// Only the member's agent process is stopped (by pid, never by image name); its slot's shell stays, so
// `spawn` can start another member there. The member's file claims are released and the bus records it.

import (
	"fmt"
	"path/filepath"
	"strings"
)

const dismissUsage = `usage: nightshift dismiss [-h] [--dry-run] shift role`

const dismissHelp = dismissUsage + `

Stop one member of a live shift and free its slot for nightshift spawn. The
shift, its window and every other member keep running. The member's file
claims are released and a dismiss event goes on the shift bus. The
human-contact role (the Foreman) cannot be dismissed; end the shift with
nightshift down instead. Foreman only.

positional arguments:
  shift       shift name, unique suffix, or "latest"
  role        the member's label, e.g. Coder2 or Decompiler

options:
  -h, --help  show this help message and exit
  --dry-run   say what would be stopped; stop nothing
`

func init() { extraCommands["dismiss"] = dismissCommand }

func dismissCommand(argv []string) int {
	args, parseError := parseWriteArgs(argv, []writeOption{{name: "--dry-run", flag: true}}, []string{"shift", "role"}, nil)
	if args.help {
		out(dismissHelp)
		return 0
	}
	if parseError != "" {
		return writeUsage("nightshift dismiss", dismissUsage, parseError)
	}
	dir, err := resolveShift(args.pos[0])
	if err != nil {
		return commandExit(err)
	}
	shift := filepath.Base(dir)
	saved, _ := readObject(filepath.Join(dir, "crew.json"))
	members, _ := rowValue(saved, "members").([]any)
	var member object
	for _, m := range members {
		if o, ok := m.(object); ok && strings.EqualFold(objectString(o, "label"), args.pos[1]) {
			member = o
		}
	}
	if member == nil {
		return commandExit(fmt.Errorf("shift %s has no member %s", shift, args.pos[1]))
	}
	label, identity := objectString(member, "label"), objectString(member, "identity")
	if objectBool(member, "human_contact") {
		return commandExit(fmt.Errorf("%s is the shift's human contact; end the shift with nightshift down %s", label, shift))
	}
	info, _ := readObject(filepath.Join(dir, label+".json"))
	pid := processNumber(info, "pid")
	started, startedOK := objectNumber(info, "started")
	if pid == 0 || !pidAlive(pid, started, startedOK) {
		return commandExit(fmt.Errorf("%s is not running in shift %s", label, shift))
	}
	if args.present["--dry-run"] {
		printResult(object{{"dry_run", true}, {"shift", shift}, {"role", label}, {"pid", pid}}, false, true)
		return 0
	}
	if err := killShiftTree(pid); err != nil {
		return commandExit(fmt.Errorf("could not stop %s (pid %d): %v", label, pid, err))
	}
	released := []string{}
	err = withClaimsLock(dir, func() error {
		claims := readClaims(dir)
		for _, claim := range claims {
			if sameClaimRole(claim.val, identity) || sameClaimRole(claim.val, label) {
				released = append(released, claim.key)
			}
		}
		for _, path := range released {
			claims = claimsWithout(claims, path)
		}
		if len(released) != 0 {
			return writeClaims(dir, claims)
		}
		return nil
	})
	if err != nil {
		return commandExit(err)
	}
	busAppend(dir, "dismiss", object{{"role", label}, {"pid", pid}, {"released", stringsToAny(released)}})
	printResult(object{{"shift", shift}, {"dismissed", label}, {"pid", pid}, {"released", stringsToAny(released)}}, false, true)
	return 0
}
