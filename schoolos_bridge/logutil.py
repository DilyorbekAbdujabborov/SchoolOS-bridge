"""Logging setup. Logs go to stdout/stderr only: journald (systemd), WinSW's
log files (Windows), launchd's log file (macOS) or `docker logs` collect them.

Defense in depth: no code path logs the bridge token or a terminal password on
purpose, but an exception text could echo one (a URL, a server reply), so a
filter masks every configured secret before a record is written."""

import logging
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
    redact = RedactSecrets(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
