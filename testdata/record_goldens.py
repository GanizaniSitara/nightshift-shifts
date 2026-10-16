"""Re-record the frozen Python-oracle goldens the test_go_*.py parity tests compare against (NGH-055).

The Python launcher (nightshift_shifts.py) is frozen and Go is the only implementation, so the configured
build never runs the oracle. This recorder is the one place that still does: it deletes the golden files,
runs the six Go parity files serially with NIGHTSHIFT_RECORD_GOLDENS=1 (each oracle call site runs the
oracle and stores what it printed, with fixture roots and live PIDs tokenised), and the native binary is
still compared against every freshly recorded value. Run it only if the oracle itself changes:

    C:/miniconda3/envs/python312/python.exe testdata/record_goldens.py [extra pytest args]

Serial on purpose: each golden file is rewritten per call site.
"""
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
FILES = ['test_go_readonly.py', 'test_go_write_commands.py', 'test_go_lifecycle.py', 'test_go_launcher.py',
         'test_go_crews.py', 'test_go_telemetry.py']


def main(extra):
    for golden in [REPO / 'testdata' / 'go_readonly' / 'golden.json', *(REPO / 'testdata' / 'goldens').glob('*.json')]:
        golden.unlink(missing_ok=True)
    env = {**os.environ, 'NIGHTSHIFT_RECORD_GOLDENS': '1'}
    return subprocess.run([sys.executable, '-m', 'pytest', *FILES, '-q', '-p', 'no:xdist', *extra],
                          cwd=REPO, env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
