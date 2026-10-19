"""T23: the web limits card formatter (web/lib/model-limits.mjs) via node's built-in test runner."""
import shutil
import subprocess
from pathlib import Path

WEB_LIB = Path(__file__).resolve().parent.parent / "web" / "lib"


def test_web_limits_formatter_node_tests():
    node = shutil.which("node")
    assert node, "node is required for the web limits formatter tests (CI runners ship it)"
    proc = subprocess.run([node, "--test", str(WEB_LIB / "model-limits.test.mjs")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "# fail 0" in proc.stdout and "# pass 3" in proc.stdout, proc.stdout
