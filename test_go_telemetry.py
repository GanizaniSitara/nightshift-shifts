"""Black-box Go telemetry verification for NGH-048.

Every PID and every CLI-looking executable below is a disposable copy of the
Nightshift test binary.  These tests never start or inspect a real agent
session; ``down --hold`` is exercised because it is the observable pre-kill
contract that must preserve offline role telemetry.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

from test_go_lifecycle import DEAD_PID, cli, exe_path, owned_fixture_process, world, write_json
from test_go_launcher import fake_bin, go_toolchain, pane_world, run, sleeper
from test_go_readonly import REPO, oracle


COMPLETION_REGRESSION = r'''package main

import (
    "os"
    "path/filepath"
    "sync"
    "testing"
    "time"
)

var refreshEntered = make(chan struct{})
var refreshRelease = make(chan struct{})
var refreshOnce sync.Once

func refreshRoleTelemetry(stateDir, role string, pid uint32, started float64, harvest bool) error {
    refreshOnce.Do(func() { close(refreshEntered); <-refreshRelease })
    return originalRefreshRoleTelemetry(stateDir, role, pid, started, harvest)
}

func TestWatcherShortLivedChild(t *testing.T) {
    if os.Getenv("NIGHTSHIFT_COMPLETION_CHILD") == "1" {
        _ = os.WriteFile(os.Getenv("NIGHTSHIFT_COMPLETION_EXIT"), []byte("exiting"), 0600)
        os.Exit(0)
    }
    // This test exercises telemetry only; never let a Go test executable act
    // as the real console helper while its disposable child is starting.
    delete(trustPrompts,"codex")
    base:=t.TempDir()
    home:=filepath.Join(base,"home")
    state:=filepath.Join(base,"state")
    if err:=os.MkdirAll(state,0700);err!=nil {t.Fatal(err)}
    t.Setenv("NIGHTSHIFT_HOME",home)
    codexHome:=filepath.Join(home,".codex")
    t.Setenv("CODEX_HOME",codexHome)
    transcript:=filepath.Join(codexHome,"sessions","rollout-completion.jsonl")
    if err:=os.MkdirAll(filepath.Dir(transcript),0700);err!=nil {t.Fatal(err)}
    stamp:=time.Now().UTC().Format(time.RFC3339)
    meta:=object{{"type","session_meta"},{"payload",object{{"id","completion-session"},{"cwd",base},{"timestamp",stamp}}}}
    if err:=os.WriteFile(transcript,[]byte(renderJSON(meta,false,true)+"\n"),0600);err!=nil {t.Fatal(err)}
    exe,err:=os.Executable();if err!=nil {t.Fatal(err)}
    childExit:=filepath.Join(base,"child-exiting")
    env:=append(os.Environ(),"NIGHTSHIFT_COMPLETION_CHILD=1","NIGHTSHIFT_COMPLETION_EXIT="+childExit)
    returned:=make(chan error,1)
    go func(){ _,err:=runPaneChild([]string{exe,"-test.run=^TestWatcherShortLivedChild$"},base,env,"Coder","fixture-coder","","","codex","","",state,true,0,nil);returned<-err }()
    released:=false
    defer func(){if !released {close(refreshRelease)}}()
    select {
    case <-refreshEntered:
    case err:=<-returned:t.Fatalf("wrapper returned before watcher started: %v",err)
    case <-time.After(10*time.Second):t.Fatal("watcher did not start")
    }
    deadline:=time.Now().Add(10*time.Second)
    for !fileExists(childExit) && time.Now().Before(deadline) {time.Sleep(time.Millisecond)}
    if !fileExists(childExit) {t.Fatal("short-lived child did not exit")}
    select {
    case err:=<-returned:t.Fatalf("wrapper returned with refresh blocked: %v",err)
    case <-time.After(200*time.Millisecond):
    }
    close(refreshRelease);released=true
    select {
    case err:=<-returned:if err!=nil {t.Fatal(err)}
    case <-time.After(10*time.Second):t.Fatal("wrapper failed to finish after refresh")
    }
    record,ok:=readObject(filepath.Join(state,"Coder.json"))
    if !ok || objectString(record,"session_id")!="completion-session" || objectString(record,"transcript")!=transcript {t.Fatal(record)}
    if rowValue(record,"usage")!=nil {t.Fatal("watcher harvested usage")}
}
'''


def test_go_short_lived_child_waits_for_blocked_telemetry_refresh(tmp_path):
    """Block discovery in a disposable build; child exit cannot finish its wrapper early."""
    cached = sorted(Path.home().glob('go/pkg/mod/golang.org/toolchain@v0.0.1-go1.2*.windows-amd64/bin/go.exe'))
    go = os.environ.get('GOEXE') or (str(cached[-1]) if cached else go_toolchain())
    if not go:
        pytest.skip('no provisioned Go toolchain')
    source = tmp_path / 'completion source'
    source.mkdir()
    for name in ('go.mod', 'go.sum', 'assets.go'):
        shutil.copyfile(REPO / name, source / name)
    for name in ('cmd', 'internal', 'crews', 'briefs', 'personas'):
        shutil.copytree(REPO / name, source / name)
    telemetry = source / 'cmd' / 'nightshift' / 'telemetry.go'
    original = telemetry.read_text(encoding='utf-8')
    signature = 'func refreshRoleTelemetry('
    assert original.count(signature) == 1
    telemetry.write_text(original.replace(signature, 'func originalRefreshRoleTelemetry(', 1), encoding='utf-8')
    (telemetry.parent / 'completion_regression_test.go').write_text(COMPLETION_REGRESSION, encoding='utf-8')
    result = subprocess.run([go, 'test', './cmd/nightshift', '-run', '^TestWatcherShortLivedChild$', '-count=1'],
                            cwd=source, capture_output=True, timeout=90,
                            env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOSUMDB': 'off'},
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, (result.stdout, result.stderr)
    # Prove this barrier detects the regression, using only the disposable source.
    launcher = telemetry.parent / 'launcher_pane.go'
    original = launcher.read_text(encoding='utf-8')
    join = '\tif telemetryDone != nil {\n\t\t<-telemetryDone\n\t}'
    assert original.count(join) == 1
    launcher.write_text(original.replace(join, '', 1), encoding='utf-8')
    broken = subprocess.run([go, 'test', './cmd/nightshift', '-run', '^TestWatcherShortLivedChild$', '-count=1'],
                            cwd=source, capture_output=True, timeout=90,
                            env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOSUMDB': 'off'},
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert broken.returncode != 0 and b'wrapper returned' in broken.stdout, (broken.stdout, broken.stderr)


def native_down(env, shift='fixture'):
    assert exe_path().is_file(), 'compile native binary before telemetry verification'
    result = cli(['down', shift, '--hold'], env, native=True)
    assert result.returncode == 0 and not result.stderr, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def crew_with_roles(d, *labels):
    crew = json.loads((d / 'crew.json').read_bytes())
    known = {member['label'] for member in crew['members']}
    for label in labels:
        if label not in known:
            crew['members'].append({'label': label, 'role': label,
                                    'identity': f'fixture-{label.lower()}', 'human_contact': False})
    write_json(d / 'crew.json', crew)


def role(d, label, *, tool, cwd, pid=DEAD_PID, started=None, **extra):
    record = {'pid': pid, 'wrapper_pid': pid, 'role': label, 'name': f'fixture-{label.lower()}',
              'tool': tool, 'cwd': str(cwd), 'started': time.time() - 30 if started is None else started}
    record.update(extra)
    write_json(d / f'{label}.json', record)
    return record


def claude_slug(cwd):
    """The observed contract: every non-ASCII-alphanumeric rune is a dash."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(cwd))


def claude_path(home, cwd, session_id, project=None):
    return home / '.claude' / 'projects' / (project or claude_slug(cwd)) / f'{session_id}.jsonl'


def assistant(message_id, input_tokens, cache_read, cache_create, output, reasoning=0):
    return {'type': 'assistant', 'message': {'id': message_id, 'usage': {
        'input_tokens': input_tokens, 'cache_read_input_tokens': cache_read,
        'cache_creation_input_tokens': cache_create, 'output_tokens': output,
        'reasoning_output_tokens': reasoning}}}


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(row if isinstance(row, str) else json.dumps(row) for row in rows) + '\n',
                    encoding='utf-8')


def rfc3339(epoch):
    """UTC metadata stamped relative to the fixture process, never wall-clock literals."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def test_go_down_harvests_claude_direct_slug_over_decoy_and_last_message_id(tmp_path):
    """A session cwd override chooses its exact slug; duplicate stream ids are final-only."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    crew_with_roles(d, 'Coder')
    session_id = 'claude-session-fixture'
    session_cwd = tmp_path / 'session cwd café Ω'
    record = role(d, 'Coder', tool='claude', cwd=work, history=[{
        'pid': 101, 'session_id': 'earlier-session', 'transcript': 'C:/saved/earlier.jsonl',
        'usage': {'total_tokens': 9}}])
    write_json(home / '.claude' / 'sessions' / f'{DEAD_PID}.json',
               {'sessionId': session_id, 'cwd': str(session_cwd)})
    direct = claude_path(home, session_cwd, session_id)
    write_jsonl(direct, [assistant('msg-a', 1, 2, 3, 4, 99),
                         assistant('msg-b', 20, 21, 22, 23),
                         assistant('msg-a', 10, 11, 12, 13, 77),
                         {'type': 'assistant', 'message': {'id': 'no-usage'}},
                         '{truncated final writer record'])
    decoy = claude_path(home, session_cwd, session_id, project='fallback-decoy')
    write_jsonl(decoy, [assistant('decoy', 999, 0, 0, 0)])

    native_down(env)
    saved = json.loads((d / 'Coder.json').read_bytes())
    assert saved['session_id'] == session_id and Path(saved['transcript']) == direct
    assert saved['history'] == record['history']
    # Ticket total is input + cache read + cache creation + output.  Claude's
    # specified record shape does not make reasoning a separately billable sum.
    assert saved['usage'] == {'input_tokens': 30, 'cached_input_tokens': 66,
                              'output_tokens': 36, 'reasoning_output_tokens': 0,
                              'total_tokens': 132, 'turns': 2}
    assert '## Token Usage' in (d / 'handover.md').read_text(encoding='utf-8')


def test_go_down_harvests_claude_fallback_when_direct_slug_is_absent(tmp_path):
    """Fallback remains available for old/changed Claude project directory names."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    crew_with_roles(d, 'Tester')
    session_id = 'claude-fallback-fixture'
    role(d, 'Tester', tool='claude', cwd=work)
    write_json(home / '.claude' / 'sessions' / f'{DEAD_PID}.json', {'sessionId': session_id})
    fallback = claude_path(home, work, session_id, project='old-project-slug')
    write_jsonl(fallback, [assistant('msg-c', 7, 0, 0, 5)])

    native_down(env)
    saved = json.loads((d / 'Tester.json').read_bytes())
    assert saved['session_id'] == session_id and Path(saved['transcript']) == fallback
    assert saved['usage']['total_tokens'] == 12


def rollout(path, session_id, cwd, stamp, usage=None, turns=0):
    rows = [{'timestamp': stamp, 'type': 'session_meta',
             'payload': {'id': session_id, 'cwd': str(cwd), 'timestamp': stamp}}]
    rows.extend({'type': 'turn_context', 'payload': {}} for _ in range(turns))
    if usage is not None:
        rows.extend([
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                **usage, 'input_tokens': usage['input_tokens'] + 1,
                'total_tokens': usage['total_tokens'] + 1}}}},
        ])
    write_jsonl(path, rows)


def test_go_down_refreshes_live_codex_to_nearest_rollout_latest_cumulative_usage(tmp_path, monkeypatch):
    """Final down harvest beats an old watcher sample before its safe fixture PID dies."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    crew_with_roles(d, 'Coder')
    # Parent machines can carry a real CODEX_HOME.  Both fake homes are local,
    # and the configured second home is deliberately the selected source.
    configured = home / '.codex-second'
    monkeypatch.setenv('CODEX_HOME', str(configured))
    base = configured / 'sessions' / '2026' / '10' / '16'
    nearest = base / 'rollout-nearest.jsonl'

    with owned_fixture_process(tmp_path / 'live codex fixture') as holder:
        started = oracle.process_start_epoch(holder.pid)
        assert started is not None, 'fixture PID must have a creation epoch'
        rollout(nearest, 'codex-nearest', work, rfc3339(started + 1),
                {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 30,
                 'reasoning_output_tokens': 4, 'total_tokens': 154}, turns=2)
        rollout(base / 'rollout-wrong-cwd.jsonl', 'codex-wrong', repo, rfc3339(started + 1),
                {'input_tokens': 999, 'cached_input_tokens': 0, 'output_tokens': 0,
                 'reasoning_output_tokens': 0, 'total_tokens': 999})
        rollout(base / 'rollout-stale.jsonl', 'codex-stale', work, rfc3339(started - 61),
                {'input_tokens': 888, 'cached_input_tokens': 0, 'output_tokens': 0,
                 'reasoning_output_tokens': 0, 'total_tokens': 888})
        # Same cwd and valid window, but farther from start: selection is based
        # on a bounded nearest timestamp, never merely newest-on-disk.
        rollout(base / 'rollout-later-same-cwd.jsonl', 'codex-later', work, rfc3339(started + 15),
                {'input_tokens': 777, 'cached_input_tokens': 0, 'output_tokens': 0,
                 'reasoning_output_tokens': 0, 'total_tokens': 777})
        # The default home is a decoy: configured CODEX_HOME isolation must stop
        # accidental attribution to it.
        rollout(home / '.codex' / 'sessions' / '2026' / '10' / '16' / 'rollout-decoy.jsonl',
                'codex-decoy', work, rfc3339(started + 1))
        role(d, 'Coder', tool='codex', cwd=work, pid=holder.pid, started=started,
             usage={'input_tokens': 1, 'cached_input_tokens': 1, 'output_tokens': 1,
                    'reasoning_output_tokens': 1, 'total_tokens': 4, 'turns': 1})
        native_down(env)
        holder.wait(timeout=15)

    saved = json.loads((d / 'Coder.json').read_bytes())
    assert saved['session_id'] == 'codex-nearest' and Path(saved['transcript']) == nearest
    assert saved['usage'] == {'input_tokens': 101, 'cached_input_tokens': 20,
                              'output_tokens': 30, 'reasoning_output_tokens': 4,
                              'total_tokens': 155, 'turns': 2}


def test_go_down_omits_unavailable_copilot_agy_and_exact_usage_heading(tmp_path):
    """No discovery source means no guessed fields and byte-compatible handover shape."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    crew_with_roles(d, 'Coder', 'Apprentice')
    role(d, 'Coder', tool='copilot', cwd=work)
    role(d, 'Apprentice', tool='agy', cwd=work)
    native_down(env)

    for label in ('Coder', 'Apprentice'):
        saved = json.loads((d / f'{label}.json').read_bytes())
        assert not ({'session_id', 'transcript', 'usage'} & saved.keys())
    assert '## Token Usage' not in (d / 'handover.md').read_text(encoding='utf-8')


def test_go_down_keeps_session_link_but_omits_usage_for_empty_or_oversized_claude_transcript(tmp_path):
    """Torn/oversized lines cannot erase the offline link or invent a zero summary."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    crew_with_roles(d, 'Coder', 'Tester')
    for label, pid, session_id in [('Coder', DEAD_PID - 1, 'empty-readable'),
                                   ('Tester', DEAD_PID, 'large-line')]:
        role(d, label, tool='claude', cwd=work, pid=pid)
        write_json(home / '.claude' / 'sessions' / f'{pid}.json', {'sessionId': session_id})
        path = claude_path(home, work, session_id)
        if label == 'Coder':
            write_jsonl(path, [])
        else:
            # A line over Scanner's historical 1 MiB ceiling precedes valid
            # data; robust harvesting skips it and continues to the next line.
            write_jsonl(path, ['x' * (1024 * 1024 + 1), assistant('after-large', 2, 3, 4, 5)])

    native_down(env)
    empty = json.loads((d / 'Coder.json').read_bytes())
    large = json.loads((d / 'Tester.json').read_bytes())
    assert empty['session_id'] == 'empty-readable' and 'usage' not in empty
    assert large['session_id'] == 'large-line'
    assert large['usage']['total_tokens'] == 14


def test_go_down_discovers_codex_home_glob_and_refuses_ambiguous_ties(tmp_path, monkeypatch):
    """Unconfigured .codex-* homes work, but tied same-cwd candidates do not cross-link roles."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    monkeypatch.delenv('CODEX_HOME', raising=False)
    crew_with_roles(d, 'Coder', 'Tester')
    started = time.time() - 5
    unique = home / '.codex-second' / 'sessions' / '2026' / '10' / '16' / 'rollout-only.jsonl'
    rollout(unique, 'globbed-home', work, rfc3339(started + 1),
            {'input_tokens': 1, 'cached_input_tokens': 2, 'output_tokens': 3,
             'reasoning_output_tokens': 4, 'total_tokens': 10})
    role(d, 'Coder', tool='codex', cwd=work, started=started)
    tie_root = home / '.codex-tied' / 'sessions' / '2026' / '10' / '16'
    tie_a, tie_b = tie_root / 'rollout-a.jsonl', tie_root / 'rollout-b.jsonl'
    stamp = rfc3339(started + 2)
    rollout(tie_a, 'tie-a', repo, stamp)
    rollout(tie_b, 'tie-b', repo, stamp)
    role(d, 'Tester', tool='codex', cwd=repo, started=started)

    native_down(env)
    coder = json.loads((d / 'Coder.json').read_bytes())
    tester = json.loads((d / 'Tester.json').read_bytes())
    assert coder['session_id'] == 'globbed-home' and Path(coder['transcript']) == unique
    assert not ({'session_id', 'transcript', 'usage'} & tester.keys())


def test_go_down_discovers_pretty_json_copilot_session_when_present(tmp_path):
    """Copilot remains conservative when absent, but accepts its readable JSON state file."""
    env, d, repo, work, info = world(tmp_path, worktree=False)
    home = Path(env['NIGHTSHIFT_HOME'])
    crew_with_roles(d, 'Coder')
    state = home / '.copilot' / 'session-state' / 'fixture-session.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'sessionId': 'copilot-fixture', 'cwd': str(work)}, indent=2), encoding='utf-8')
    role(d, 'Coder', tool='copilot', cwd=work)

    native_down(env)
    saved = json.loads((d / 'Coder.json').read_bytes())
    assert saved['session_id'] == 'copilot-fixture' and Path(saved['transcript']) == state
    assert 'usage' not in saved


def test_go_pane_live_discovery_survives_actual_stub_respawn_history(tmp_path, fake_bin, sleeper):
    """The existing launcher stub exercises watcher discovery without an agent session."""
    env, d, repo, record = pane_world(tmp_path / 'pane telemetry', fake_bin, sleeper)
    home = Path(env['NIGHTSHIFT_HOME'])
    transcript = home / '.codex-second' / 'sessions' / '2026' / '10' / '16' / 'rollout-pane.jsonl'
    rollout(transcript, 'pane-session', repo, rfc3339(time.time()),
            {'input_tokens': 3, 'cached_input_tokens': 4, 'output_tokens': 5,
             'reasoning_output_tokens': 6, 'total_tokens': 18}, turns=1)
    args = ['--pane', 'Coder', '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', '3']
    first = run(args, env, native=True, timeout=120)
    assert first.returncode == 0, first.stderr
    first_record = json.loads((d / 'Coder.json').read_bytes())
    assert first_record['session_id'] == 'pane-session'
    assert Path(first_record['transcript']) == transcript
    assert 'usage' not in first_record, 'live watcher is discovery-only; final counts belong to down'
    native_down(env, d.name)
    harvested = json.loads((d / 'Coder.json').read_bytes())
    # rollout() appends a later cumulative token_count snapshot (+1), which
    # down must retain rather than summing with the earlier snapshot.
    assert harvested['usage']['total_tokens'] == 19

    # A real respawn through the launcher moves the complete down-harvested
    # incarnation into history.  The live watcher must never erase it.
    second = run(args, env, native=True, timeout=120)
    assert second.returncode == 0, second.stderr
    current = json.loads((d / 'Coder.json').read_bytes())
    preserved = [row for row in current['history'] if row.get('session_id') == 'pane-session']
    assert len(preserved) == 1 and preserved[0]['usage']['total_tokens'] == 19
