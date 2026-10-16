# Supervisor floor-bus convention

Supervisors communicate directly through the machine-level floor bus; the owner is not a relay.
Their stable names are `claude-supervisor` and `agy-supervisor`.

At each patrol and immediately before starting a crew, a supervisor runs:

```
nightshift floor hear <its-name>
```

It directs a note to the other supervisor with:

```
nightshift floor say <its-name> "message" --to <other-name>
```

It announces a start or landing with `floor say`. The launcher additionally publishes a broadcast
start event only after all expected crew panes and slots have reported; dry runs and failed launches
produce no automatic event. `floor hear --since ID` is a replay and does not alter the durable reader
cursor. The Go CLI does not yet implement this command pair.
