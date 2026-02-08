"""
Logging configuration for Alert Dashboard V2.
Uses rotating file handlers to prevent log files from growing too large.
"""

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

import structlog


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: Optional[str] = None,
    log_dir: Optional[Path] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB per file
    backup_count: int = 5,  # Keep 5 backup files
    console_output: bool = True,
) -> None:
    """
    Configure logging for the application with rotating file support.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_output: If True, output JSON logs (for production)
        log_file: Specific log file path (overrides log_dir)
        log_dir: Directory to store log files (creates alert_dashboard.log)
        max_bytes: Maximum size per log file before rotation (default 10MB)
        backup_count: Number of backup files to keep (default 5)
        console_output: Whether to output to console (default True)
    """
    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Silence noisy third-party loggers
    noisy_loggers = [
        "watchfiles",        # File watcher (spams "change detected" on every log write)
        "watchfiles.main",
        "httpcore",          # HTTP connection details
        "httpx",             # HTTP client details
        "aiohttp",           # Async HTTP client
        "urllib3",           # HTTP library
        "asyncio",           # Async internals
        "websockets",        # WebSocket connection details (PING/PONG, connection state)
        "websockets.server",
        "websockets.protocol",
        "uvicorn.error",     # Uvicorn connection errors (often just client disconnects)
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Create formatter for file output (more detailed)
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Create formatter for console (simpler)
    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    )

    # Add console handler if enabled
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    # Determine log file path
    log_path = None
    if log_file:
        log_path = Path(log_file)
    elif log_dir:
        log_path = Path(log_dir) / "alert_dashboard.log"

    # Add rotating file handler if log path is specified
    if log_path:
        # Ensure directory exists
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Use RotatingFileHandler for size-based rotation
        # Files: alert_dashboard.log, alert_dashboard.log.1, .log.2, etc.
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        # Log that file logging is enabled
        root_logger.info(f"File logging enabled: {log_path} (max {max_bytes / 1024 / 1024:.1f}MB, {backup_count} backups)")

    # Configure structlog
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        # JSON output for production/log aggregation
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable console output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=console_output),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def setup_daily_rotating_log(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    backup_count: int = 30,  # Keep 30 days of logs
    console_output: bool = True,
) -> None:
    """
    Configure logging with daily rotating files.

    Creates log files like: alert_dashboard.log, alert_dashboard.log.2024-01-15, etc.

    Args:
        level: Log level
        log_dir: Directory to store log files
        backup_count: Number of days of logs to keep (default 30)
        console_output: Whether to output to console
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()

    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    )

    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)

    if log_dir:
        log_path = Path(log_dir) / "alert_dashboard.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # TimedRotatingFileHandler rotates at midnight
        file_handler = TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(file_formatter)
        file_handler.suffix = "%Y-%m-%d"  # Date suffix for rotated files
        root_logger.addHandler(file_handler)

        root_logger.info(f"Daily rotating log enabled: {log_path} (keeping {backup_count} days)")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Bound logger instance
    """
    return structlog.get_logger(name)


# Create a default logger for quick imports
logger = get_logger("alert_dashboard")
