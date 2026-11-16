"""Ponto de entrada para execução como módulo: python -m stays_automation."""

from __future__ import annotations

import sys

from stays_automation.cli import main

if __name__ == "__main__":
    sys.exit(main())
