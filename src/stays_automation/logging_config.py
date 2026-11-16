"""Configuração centralizada de logs com filtro automático de ocultação de segredos."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

from stays_automation.security import SecretScrubber


class SecretRedactingFilter(logging.Filter):
    """Filtro de logging que garante que nenhum valor secreto seja gravado em arquivo ou console."""

    def __init__(self, scrubber: SecretScrubber) -> None:
        super().__init__()
        self.scrubber = scrubber

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.scrubber.redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self.scrubber.redact_secrets(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self.scrubber.redact_secrets(str(arg)) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def configure_logging(
    scrubber: SecretScrubber,
    log_file: Optional[Path] = None,
    level: int = logging.INFO,
    console: Optional[Console] = None,
) -> logging.Logger:
    """Configura o logger 'stays_automation' com saída Rich no terminal e arquivo execution.log."""
    logger = logging.getLogger("stays_automation")
    logger.setLevel(level)
    logger.propagate = False

    # Limpa handlers anteriores para evitar duplicação em múltiplas execuções/testes
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redacting_filter = SecretRedactingFilter(scrubber)

    rich_console = console or Console(stderr=True)
    console_handler = RichHandler(
        console=rich_console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,
    )
    console_handler.setLevel(level)
    console_handler.addFilter(redacting_filter)
    logger.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(redacting_filter)
        logger.addHandler(file_handler)

    return logger
