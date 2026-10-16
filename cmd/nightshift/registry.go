package main

// extraCommands lets each command file register itself from init(), so new
// commands never need an edit to main.go.
var extraCommands = map[string]func([]string) int{}

// startHook launches a shift quietly and returns its state directory; set by
// the start command. gaffer uses it to dispatch without parsing stdout.
var startHook func(argv []string) (stateDir string, err error)

// spawnHook (re)launches one crew pane quietly, with no stdout; set by the
// spawn command. gaffer and warden use it for recover_pane.
var spawnHook func(argv []string) error

// floorAppendHook appends one event to the machine floor bus; set by the floor
// command's file. Callers treat nil as "no floor bus in this build".
var floorAppendHook func(sender, text, to string) (object, error)
