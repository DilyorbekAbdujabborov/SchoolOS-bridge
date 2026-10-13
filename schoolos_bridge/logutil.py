"""Logging setup. Logs go to stdout/stderr only: journald (systemd), WinSW's
log files (Windows), launchd's log file (macOS) or `docker logs` collect them.

Defense in depth: no code path logs the bridge token or a terminal password on
purpose, but an exception text could echo one (a URL, a server reply), so a
filter masks every configured secret before a record is written."""

import logging

MASK = "***"


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


def setup_logging(level=logging.INFO, secrets=()) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactSecrets(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
