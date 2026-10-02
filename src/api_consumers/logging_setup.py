"""Logging JSON con redacción: la key nunca debe llegar a un log (ni por accidente)."""

from __future__ import annotations

import json
import logging
import re

# 32+ hex (formato 3scale / generado) y pares clave=valor sensibles.
_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_KV = re.compile(r"(?i)(app_key(_prev)?|api_key|token|secret_id|role_id)(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)")


def redact(text: str) -> str:
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(3)}***", text)
    return _HEX.sub("***", text)


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("op", "kind", "target", "event"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # hvac/urllib3 pueden loguear URLs y headers en DEBUG
    for noisy in ("urllib3", "hvac", "kubernetes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
