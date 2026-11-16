"""HTTP client logging without URLs.

An alert webhook URL is a credential: Slack-style URLs carry the secret in the path, others in the
query (BALANCE_ALERT_WEBHOOK_URL). httpx logs every request at INFO with its full URL and httpcore
traces each connection at DEBUG, so a process logging at INFO printed the webhook URL whenever an alert
went out. The api and the worker call quiet_http_client_logs() at start-up: the HTTP client loggers
drop to WARNING, and anything they still emit (or emit after someone lowers their level to debug)
carries URLs cut down to scheme and host.
"""

from __future__ import annotations

import logging
import re

HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")
# httpcore logs from these children; a filter on a parent logger does not see their records
_HTTPCORE_CHILDREN = ("connection", "http11", "http2", "proxy", "socks")
_URL = re.compile(r"(?i)\bhttps?://[^\s'\"<>]+")


def redact_urls(text: str) -> str:
    """Every URL in text reduced to scheme and host (no credentials, path or query)."""
    def host_only(match: re.Match[str]) -> str:
        scheme, _, rest = match.group(0).partition("://")
        authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
        host = authority.rpartition("@")[2]
        return f"{scheme}://{host}" if authority == rest == host else f"{scheme}://{host}/[redacted]"
    return _URL.sub(host_only, text)


class RedactUrlsFilter(logging.Filter):
    """Rewrites a record's message and traceback with redact_urls."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is reported by the handler, not here
            return True
        redacted = redact_urls(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_urls(logging.Formatter().formatException(record.exc_info))
        return True


def _http_client_logger_names() -> list[str]:
    names = []
    for family in HTTP_CLIENT_LOGGERS:
        names.append(family)
        if family.startswith("httpcore"):
            names.extend(f"{family}.{child}" for child in _HTTPCORE_CHILDREN)
    return names


def quiet_http_client_logs() -> None:
    """Idempotent: safe to call from every entry point that may share a process."""
    for family in HTTP_CLIENT_LOGGERS:
        logging.getLogger(family).setLevel(logging.WARNING)
    for name in _http_client_logger_names():
        client_logger = logging.getLogger(name)
        if not any(isinstance(f, RedactUrlsFilter) for f in client_logger.filters):
            client_logger.addFilter(RedactUrlsFilter())
