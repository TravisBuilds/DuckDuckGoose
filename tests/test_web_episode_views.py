"""The episode page's pure views (web/lib/*.mjs) under node's test runner, so red-green mutations of
those files are caught by the Python harness too: what the Next step, the stepper, the finish card and
the status chips say (next-step.mjs), how an ended or archived run reads and when the page stops
polling (run-outcome.mjs), who gets the Cast panel's add form and what a character with a photo
needs and sends (cast.mjs), what a picture redo sends and says it costs (redo.mjs), and what a prop or
place with a photo needs and what a shot plan save sends (props.mjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def _node_tests(name: str) -> str:
    node = shutil.which("node")
    assert node, "node is required for the web view tests (CI runners ship it)"
    proc = subprocess.run([node, "--test", "--test-reporter=tap", f"lib/{name}"], cwd=WEB,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "# fail 0" in proc.stdout, proc.stdout
    return proc.stdout


@pytest.mark.parametrize("name", ["next-step.test.mjs", "run-outcome.test.mjs", "cast.test.mjs", "redo.test.mjs",
                                  "props.test.mjs", "consistency.test.mjs", "world.test.mjs"])
def test_episode_view_node_tests(name):
    _node_tests(name)
