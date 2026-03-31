"""structlog configuration for file and console logging."""

import sys
from pathlib import Path

import structlog


def configure_logging(
    state_dir: Path,
    log_level: str = "WARNING",
    debug: bool = False,
) -> None:
    """Configure structlog for file logging.

    In normal mode: JSON logs to ~/.meho/logs/meho.log (WARNING+)
    In debug mode: Pretty console logs to stderr (DEBUG+)
    """
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]

    if debug:
        # Dev mode: pretty output to stderr
        processors.append(structlog.dev.ConsoleRenderer())
        effective_level = "DEBUG"
    else:
        # Production: JSON to log file
        processors.append(structlog.processors.JSONRenderer())
        effective_level = log_level.upper()

    # Resolve log level name to int
    level_int = structlog.stdlib.NAME_TO_LEVEL.get(effective_level, 30)

    # Determine log output target
    if debug:
        log_file = None  # Console output via stderr
    else:
        log_path = state_dir / "logs" / "meho.log"
        log_file = open(log_path, "a")  # noqa: SIM115

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=log_file),
        cache_logger_on_first_use=True,
    )
