from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

_LEVEL_COLORS = {
    logging.DEBUG: "\033[2;37m",
    logging.INFO: "\033[36m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_RESET = "\033[0m"


@dataclass(frozen=True)
class LogSettings:
    component: str
    level: int
    color: bool


class OrbitFormatter(logging.Formatter):
    def __init__(self, *, component: str, color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.component = component
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        level_text = record.levelname.upper().ljust(7)
        logger_name = _short_logger_name(record.name)
        message = record.getMessage()
        if self.color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            level_text = f"{color}{level_text}{_RESET}"
        line = f"[{timestamp}] {level_text} {self.component:<6} │ {logger_name:<18} │ {message}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


def configure_logging(component: str, *, level_name: str | None = None, color: bool | None = None) -> None:
    settings = _settings(component, level_name=level_name, color=color)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(OrbitFormatter(component=settings.component, color=settings.color))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.level)

    logging.captureWarnings(True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_kv(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    parts = [event]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_quote_value(value)}")
    logger.log(level, " ".join(parts))


def _settings(component: str, *, level_name: str | None, color: bool | None) -> LogSettings:
    raw_level = (level_name or os.getenv("ORBIT_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, raw_level, logging.INFO)
    if color is None:
        color = sys.stderr.isatty() and os.getenv("TERM", "dumb") != "dumb" and os.getenv("NO_COLOR") is None
    return LogSettings(component=component, level=level, color=color)


def _short_logger_name(name: str) -> str:
    prefix = "mvp_orbit."
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


def _quote_value(value: Any) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or any(ch in text for ch in '"='):
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return text
