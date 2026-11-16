"""The alert webhook URL never reaches the logs of the api or the worker (hfvg/log_safety.py).

httpx logs each request at INFO with its full URL, and the webhook URL is a credential. The process
tests start the real entry point in a fresh interpreter (its logging set-up runs at import), send a
test alert to a local fake webhook whose URL carries made-up secrets in path and query, and read
everything the process wrote.
"""
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hfvg.log_safety import HTTP_CLIENT_LOGGERS, RedactUrlsFilter, quiet_http_client_logs, redact_urls

REPO = Path(__file__).resolve().parents[1]
PATH_SECRET = "not-a-real-path-token-7f3a"
QUERY_SECRET = "not-a-real-query-token-91c2"
HOOK_PATH = f"/services/T000/B000/{PATH_SECRET}?token={QUERY_SECRET}"


@pytest.fixture
def fake_webhook():
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append((self.path, json.loads(body)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # the fake's own request log is not the process under test
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{HOOK_PATH}", received
    finally:
        server.shutdown()
        server.server_close()


def _run(script: str, webhook_url: str, tmp_path) -> tuple[subprocess.CompletedProcess, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BALANCE_ALERT_")}
    env.update({"BALANCE_ALERT_WEBHOOK_URL": webhook_url, "DATABASE_PATH": str(tmp_path / "alerts.db")})
    proc = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)
    return proc, proc.stdout + proc.stderr


WORKER = """
import asyncio, os
import hfvg.worker  # noqa: F401 - the worker's logging set-up runs at import
from hfvg.balance_notifier import BalanceNotifier
print("RESULT", asyncio.run(BalanceNotifier(os.environ["DATABASE_PATH"]).send_test_alert("log check"))["results"])
"""

API = """
import logging
logging.basicConfig(level=logging.DEBUG)  # a deploy that prints everything the process logs
import os
from fastapi.testclient import TestClient
import api.main as api_main
client = TestClient(api_main.app)
client.headers["Authorization"] = "Bearer " + os.environ["ADMIN_SECRET"]
print("RESULT", client.post("/api/alerts/test").json()["results"])
"""


@pytest.mark.timeout(150)
@pytest.mark.parametrize("entry_point", ["worker", "api"])
def test_sending_an_alert_never_logs_the_webhook_url(fake_webhook, tmp_path, entry_point):
    url, received = fake_webhook
    proc, logs = _run(WORKER if entry_point == "worker" else API, url, tmp_path)
    assert proc.returncode == 0, logs[-3000:]
    assert "RESULT {'webhook': 'sent'}" in proc.stdout, logs[-3000:]
    assert [path for path, _ in received] == [HOOK_PATH]
    # the process logs at INFO or below, so the URL's absence below is not just silence
    assert "[test] DuckDuckGoose test alert" in proc.stderr, logs[-3000:]
    assert PATH_SECRET not in logs and QUERY_SECRET not in logs, logs[-3000:]


def test_redact_urls_keeps_only_scheme_and_host():
    line = f'HTTP Request: POST https://user:pw@hooks.example.test{HOOK_PATH} "HTTP/1.1 200 OK"'
    assert redact_urls(line) == 'HTTP Request: POST https://hooks.example.test/[redacted] "HTTP/1.1 200 OK"'
    assert redact_urls("see http://example.test and HTTPS://example.test/a?b=c, then stop") == (
        "see http://example.test and HTTPS://example.test/[redacted] then stop")
    assert redact_urls("connect_tcp.started host='127.0.0.1' port=8080") == "connect_tcp.started host='127.0.0.1' port=8080"


@pytest.fixture
def http_client_loggers():
    """The HTTP client loggers restored afterwards, with the root logger at DEBUG meanwhile (a
    process that logs everything, so only the client loggers' own level can keep them quiet)."""
    names = [n for n in logging.Logger.manager.loggerDict if n.split(".")[0] in HTTP_CLIENT_LOGGERS]
    names += list(HTTP_CLIENT_LOGGERS)
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).filters)) for n in names}
    root_level = logging.getLogger().level
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    logging.getLogger().setLevel(logging.DEBUG)
    yield
    logging.getLogger().setLevel(root_level)
    for name, (level, filters) in saved.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.filters[:] = filters


def test_http_client_loggers_drop_requests_and_redact_what_they_still_log(http_client_loggers):
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO)
    quiet_http_client_logs()
    quiet_http_client_logs()
    for name in ("httpx", "httpx2"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    for name in ("httpcore.http11", "httpcore.connection", "httpcore2.http11"):
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
    for name in ("httpx", "httpcore.http11", "httpcore2.connection"):
        assert sum(isinstance(f, RedactUrlsFilter) for f in logging.getLogger(name).filters) == 1

    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    keep = Keep()
    httpx_logger, httpcore_logger = logging.getLogger("httpx"), logging.getLogger("httpcore")
    httpx_logger.addHandler(keep)
    httpcore_logger.addHandler(keep)
    try:
        httpx_logger.warning("HTTP Request: %s %s", "POST", f"https://hooks.example.test{HOOK_PATH}")
        try:
            raise RuntimeError(f"failed for url 'https://hooks.example.test{HOOK_PATH}'")
        except RuntimeError:
            logging.getLogger("httpcore.http11").error("send failed", exc_info=True)
    finally:
        httpx_logger.removeHandler(keep)
        httpcore_logger.removeHandler(keep)
    assert lines and lines[0] == "HTTP Request: POST https://hooks.example.test/[redacted]"
    assert "failed for url 'https://hooks.example.test/[redacted]'" in lines[1]
    assert not any(PATH_SECRET in line or QUERY_SECRET in line for line in lines)
