import logging
import os
from logging.handlers import RotatingFileHandler


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, "***")
        record.msg, record.args = msg, ()
        return True


def setup_logging(level: str, log_dir: str, secrets: list[str]) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    flt = RedactFilter(secrets)
    for h in (logging.StreamHandler(),
              RotatingFileHandler(os.path.join(log_dir, "vora.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")):
        h.setFormatter(fmt)
        h.addFilter(flt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
