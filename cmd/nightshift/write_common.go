package main

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type commandError struct {
	code    int
	message string
}

func (e commandError) Error() string { return e.message }
func (e commandError) ExitCode() int { return e.code }

func commandExit(err error) int {
	if err == nil {
		return 0
	}
	var exit interface{ ExitCode() int }
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	if err.Error() != "" {
		errLine(err.Error())
	}
	return 1
}

func normalizedClaimPath(path string) (string, error) { return canonicalPath(path) }

func takeFileLock(path string, wait, stale time.Duration) error {
	deadline := time.Now().Add(wait)
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0666)
		if err == nil {
			return f.Close()
		}
		if errors.Is(err, os.ErrExist) {
			info, statErr := os.Stat(path)
			if statErr != nil {
				continue
			}
			if time.Since(info.ModTime()) > stale {
				_ = os.Remove(path)
				continue
			}
			if time.Now().After(deadline) {
				return fmt.Errorf("%s is still held after %.0fs", path, wait.Seconds())
			}
			time.Sleep(100 * time.Millisecond)
		} else if errors.Is(err, os.ErrPermission) {
			if time.Now().After(deadline) {
				return err
			}
			time.Sleep(10 * time.Millisecond)
		} else {
			return err
		}
	}
}

func releaseFileLock(path string) {
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := os.Remove(path)
		if err == nil || !errors.Is(err, os.ErrPermission) || time.Now().After(deadline) {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func withClaimsLock(dir string, fn func() error) error {
	path := filepath.Join(dir, "claims.lock")
	if err := takeFileLock(path, 5*time.Second, 30*time.Second); err != nil {
		if err.Error() == fmt.Sprintf("%s is still held after 5s", path) {
			return fmt.Errorf("%s; try again", err)
		}
		return err
	}
	defer releaseFileLock(path)
	return fn()
}

func busAppend(dir, event string, fields object) {
	err := func() error {
		lock := filepath.Join(dir, "bus.lock")
		if err := takeFileLock(lock, 5*time.Second, 10*time.Second); err != nil {
			return err
		}
		defer releaseFileLock(lock)
		path := filepath.Join(dir, "bus.jsonl")
		data, err := os.ReadFile(path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		prefix := ""
		if len(data) > 0 && data[len(data)-1] != '\n' {
			prefix = "\n"
		}
		record := object{{"seq", bytes.Count(data, []byte{'\n'}) + len(prefix) + 1}, {"ts", time.Now().Format("2006-01-02T15:04:05")}, {"event", event}}
		record = append(record, fields...)
		f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0666)
		if err != nil {
			return err
		}
		_, err = f.WriteString(prefix + renderJSON(record, false, false) + "\n")
		closeErr := f.Close()
		if err != nil {
			return err
		}
		return closeErr
	}()
	if err != nil {
		errLine(fmt.Sprintf("warning: shift bus not written (%s): %s", event, err))
	}
}

func writeJSONFile(path string, value any) error {
	tmp := path + "." + strconv.Itoa(os.Getpid()) + ".tmp"
	if err := os.WriteFile(tmp, []byte(strings.ReplaceAll(renderJSON(value, true, true), "\n", "\r\n")), 0666); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func setValue(o object, key string, value any) object {
	for i := range o {
		if o[i].key == key {
			o[i].val = value
			return o
		}
	}
	return append(o, pair{key, value})
}

// The write parsers retain argparse's option abbreviations, -- separator and error codes.
type writeOption struct {
	name    string
	flag    bool
	choices []string
	number  string
	multi   bool // repeatable: every value is kept in lists, in order
}
type writeArgs struct {
	pos     []string
	values  map[string]string
	present map[string]bool
	lists   map[string][]string
	help    bool
}

func parseWriteArgs(args []string, options []writeOption, required, optional []string) (writeArgs, string) {
	r := writeArgs{values: map[string]string{}, present: map[string]bool{}, lists: map[string][]string{}}
	names := []string{"--help"}
	for _, o := range options {
		names = append(names, o.name)
	}
	ended := false
	extras := []string{}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if !ended && a == "--" {
			ended = true
			continue
		}
		name, value, explicit := strings.Cut(a, "=")
		if !ended && strings.HasPrefix(a, "-") && a != "-" && !negativeNumber(a) {
			matches := []string{}
			if strings.HasPrefix(name, "--") {
				for _, n := range names {
					if strings.HasPrefix(n, name) {
						matches = append(matches, n)
					}
				}
			}
			if !contains(names, name) && len(matches) > 1 {
				return r, fmt.Sprintf("ambiguous option: %s could match %s", name, strings.Join(matches, ", "))
			}
			name = longOption(name, names)
			if name == "-h" || name == "--help" {
				if explicit {
					return r, fmt.Sprintf("argument %s: ignored explicit argument %s", name, pyRepr(value))
				}
				r.help = true
				return r, ""
			}
			found := false
			for _, o := range options {
				if o.name != name {
					continue
				}
				found = true
				if o.flag {
					if explicit {
						return r, fmt.Sprintf("argument %s: ignored explicit argument %s", name, pyRepr(value))
					}
					value = "true"
				} else if !explicit {
					if i+1 >= len(args) || (strings.HasPrefix(args[i+1], "-") && args[i+1] != "-" && !negativeNumber(args[i+1])) {
						return r, "argument " + name + ": expected one argument"
					}
					i++
					value = args[i]
				}
				if o.number != "" {
					if _, err := parseNumber(value, o.number); err != nil {
						return r, fmt.Sprintf("argument %s: invalid %s value: %s", name, o.number, pyRepr(value))
					}
				}
				if len(o.choices) > 0 && !contains(o.choices, value) {
					return r, fmt.Sprintf("argument %s: invalid choice: %s (choose from %s)", name, pyRepr(value), strings.Join(o.choices, ", "))
				}
				r.values[name] = value
				r.present[name] = true
				if o.multi {
					r.lists[name] = append(r.lists[name], value)
				}
				break
			}
			if !found {
				extras = append(extras, a)
			}
		} else if len(r.pos) < len(required)+len(optional) {
			position := len(r.pos)
			labels := append(append([]string{}, required...), optional...)
			if labels[position] == "id" || labels[position] == "n" {
				if _, err := parseInteger(a); err != nil {
					return r, fmt.Sprintf("argument %s: invalid int value: %s", labels[position], pyRepr(a))
				}
			}
			r.pos = append(r.pos, a)
		} else {
			extras = append(extras, a)
		}
	}
	missing := []string{}
	if len(r.pos) < len(required) {
		missing = append(missing, required[len(r.pos):]...)
	}
	if len(missing) > 0 {
		return r, "the following arguments are required: " + strings.Join(missing, ", ")
	}
	if len(extras) > 0 {
		return r, "unrecognized arguments: " + strings.Join(extras, " ")
	}
	return r, ""
}
func negativeNumber(s string) bool {
	return negativeArgument.MatchString(s)
}

var negativeArgument = regexp.MustCompile(`^-(?:[0-9]+|[0-9]*\.[0-9]+)$`)
var integerArgument = regexp.MustCompile(`^[+-]?[0-9](?:_?[0-9])*$`)

func parseInteger(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if !integerArgument.MatchString(s) {
		return 0, fmt.Errorf("invalid integer")
	}
	return strconv.ParseInt(strings.ReplaceAll(s, "_", ""), 10, 64)
}
func parseNumber(s, kind string) (float64, error) {
	if kind == "int" {
		n, e := parseInteger(s)
		return float64(n), e
	}
	s = strings.TrimSpace(s)
	for i, c := range s {
		if c == '_' && (i == 0 || i+1 == len(s) || s[i-1] < '0' || s[i-1] > '9' || s[i+1] < '0' || s[i+1] > '9') {
			return 0, fmt.Errorf("invalid float")
		}
	}
	return strconv.ParseFloat(strings.ReplaceAll(strings.TrimSpace(s), "_", ""), 64)
}
func writeUsage(prog, usage, message string) int {
	errLine(usage)
	errLine(prog + ": error: " + message)
	return 2
}
