"""Logging setup. Logs go to stdout/stderr only: journald (systemd), WinSW's
log files (Windows), launchd's log file (macOS) or `docker logs` collect them.

Defense in depth: no code path logs the bridge token or a terminal password on
purpose, but an exception text could echo one (a URL, a server reply), so a
filter masks every configured secret before a record is written."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

MASK = "***"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactSecrets(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        # Longest first, so a secret containing another is masked whole.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def _clean(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        cleaned = self._clean(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class LogBuffer(logging.Handler):
    """Keeps recent WARNING+ records until the service ships them to the cloud.

    Bounded: if the cloud is unreachable the oldest lines fall off instead of
    growing memory. Sits behind the same secret filter as every other handler."""

    def __init__(self, maxlen=500):
        super().__init__(level=logging.WARNING)
        self._lines = deque(maxlen=maxlen)
        self._guard = threading.Lock()

    def emit(self, record):
        text = record.getMessage()
        if record.exc_text:
            text += "\n" + record.exc_text.strip().splitlines()[-1]
        elif record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info).splitlines()[-1]
        item = {
            "level": record.levelname,
            "message": f"{record.name}: {text}"[:4000],
            "logged_at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        }
        with self._guard:
            self._lines.append(item)

    def take(self, limit=200):
        """Remove and return up to `limit` oldest pending lines."""
        with self._guard:
            return [self._lines.popleft() for _ in range(min(limit, len(self._lines)))]

    def restore(self, items):
        """Put lines back (oldest first) after a failed upload."""
        with self._guard:
            self._lines.extendleft(reversed(items))


LOG_BUFFER = LogBuffer()


def setup_logging(level=logging.INFO, secrets=(), log_file=None) -> None:
    """Send logs to stderr and, when given, to a rotating file.

    A service manager collects stderr on its own (journald, launchd, WinSW); the
    file is for the Windows scheduled-task and by-hand installs that keep no
    history. Either way the secret filter is applied to every handler."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        )
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    root = logging.getLogger()
    LOG_BUFFER.filters.clear()
    root.addHandler(LOG_BUFFER)
    redact = RedactSecrets(secrets)
    for handler in root.handlers:
        handler.addFilter(redact)


def set_secrets(secrets) -> None:
    """Replace the masked values on every handler (the config was reloaded)."""
    fresh = RedactSecrets(secrets).secrets
    for handler in logging.getLogger().handlers:
        for item in handler.filters:
            if isinstance(item, RedactSecrets):
                item.secrets = fresh
