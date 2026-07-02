"""
core/log_quiet.py — silence "service isn't running" tracebacks
==============================================================

Some third-party libraries dump a full traceback at ERROR level when a
``connect()`` is refused simply because the target app isn't running:

  * ``obsws_python`` (OBS WebSocket) — when OBS is closed.
  * the underlying ``websocket`` client it sits on.

Our own code already catches these and prints a friendly one-line
warning (e.g. "[OBS] Connection failed: ... Make sure OBS is running"),
so the library's traceback is pure noise that scares people into
thinking something crashed.

``quiet_known_connection_errors()`` installs a logging filter that drops
ONLY connection-refused tracebacks from those libraries, while letting
every other (genuinely unexpected) error through with its traceback
intact. It does not touch our own loggers or print statements.
"""

from __future__ import annotations

import logging

# Windows: WinError 10061 (WSAECONNREFUSED). POSIX: errno 111 (ECONNREFUSED).
_REFUSED_CODES = {10061, 111}

# Libraries whose connection-refused tracebacks we want to mute.
_NOISY_LOGGERS = ("obsws_python", "websocket")


def _is_connection_refused(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in _REFUSED_CODES:
            return True
        if exc.errno in _REFUSED_CODES:
            return True
    return False


class _DropConnectionRefused(logging.Filter):
    """Drop log records whose attached exception is a connection refusal."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        # Returning False suppresses the record (and its traceback).
        return not _is_connection_refused(exc)


def quiet_known_connection_errors() -> None:
    """Mute connection-refused tracebacks from OBS/websocket libraries.

    Idempotent — safe to call more than once. Other errors from these
    libraries still surface normally.
    """
    handler = logging.StreamHandler()  # -> stderr, like the default
    handler.addFilter(_DropConnectionRefused())

    for name in _NOISY_LOGGERS:
        lg = logging.getLogger(name)
        # Replace any handlers so our filtered handler is the only sink,
        # and stop propagation so the root "last resort" handler doesn't
        # re-print the record we just filtered out.
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.addHandler(handler)
        lg.propagate = False
        lg.setLevel(logging.WARNING)
