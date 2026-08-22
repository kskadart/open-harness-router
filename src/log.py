"""Structured logging via structlog with JSON output to stdout.

The file is named ``log.py`` (not ``logging.py``) so that, with a flat
``src``, it doesn't shadow the stdlib ``logging`` module. Handlers and
levels are set by the external ``logging_conf.json`` via
``logging.config.dictConfig``; the JSON formatter is built by the
:func:`json_formatter_factory` factory, which the config references.
"""

from __future__ import annotations

import json
import logging
import logging.config

import structlog

from settings import LoggingSettings

# Processors shared by structlog and stdlib logs (uvicorn etc.).
_SHARED: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
]


def json_formatter_factory() -> logging.Formatter:
    """Create a stdlib formatter that renders records as JSON via structlog.

    Referenced from ``logging_conf.json`` (``"()": "log.json_formatter_factory"``).

    Returns:
        A ProcessorFormatter with JSON rendering.
    """
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )


def setup_logging(settings: LoggingSettings) -> None:
    """Configure structlog and stdlib logging from the external config.

    Args:
        settings: logging parameters (config path and level).
    """
    conf = json.loads(settings.conf_path.read_text(encoding="utf-8"))
    level = settings.level.split()[0].upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    conf.setdefault("root", {})["level"] = level
    logging.config.dictConfig(conf)

    structlog.configure(
        processors=[
            *_SHARED,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named structlog logger.

    Args:
        name: logger name (usually ``__name__``).

    Returns:
        A bound structlog logger.
    """
    return structlog.stdlib.get_logger(name)
