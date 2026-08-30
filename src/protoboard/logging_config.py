# SPDX-License-Identifier: MIT
"""Central structlog configuration for Protoboard Designer."""

from __future__ import annotations

import logging
import sys

import structlog
import structlog.dev
import structlog.processors
import structlog.stdlib


def configure_logging(
    verbose: bool,
    module_overrides: dict[str, int] | None = None,
) -> None:
    """Configure structlog with stdlib backend.

    Args:
        verbose: If True, set root level to DEBUG; otherwise INFO.
        module_overrides: Optional mapping of logger name → logging level constant.
            Example: {"protoboard.domain": logging.DEBUG,
                      "protoboard.services.drc": logging.WARNING}
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty(), sort_keys=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    if module_overrides:
        for name, level in module_overrides.items():
            logging.getLogger(name).setLevel(level)
