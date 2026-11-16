.PHONY: help setup setup-venv install-browsers test inspect dry-run-url dry-run-file save-file clean

PYTHON ?= python3
UV ?= uv
TARGET_URL ?= https://ssmt.stays.com.br/i/apartment/AB25I/setup
SOURCE_URL ?= https://www.multitemporada.com/pt/apartment/SR08H
SOURCE_FILE ?= ./examples/entrada_exemplo.txt

help:
	@echo "Comandos disponíveis para stays-content-automation:"
	@echo "  make setup            - Instala dependências via uv e navegador Chromium do Playwright"
	@echo "  make setup-venv       - Alternativa sem uv usando python -m venv e pip"
	@echo "  make install-browsers - Instala apenas o navegador Chromium do Playwright"
	@echo "  make test             - Executa a suíte de testes automatizados (pytest)"
	@echo "  make inspect          - Executa o modo de inspeção de seletores no Stays"
	@echo "  make dry-run-url      - Executa em modo dry-run usando SOURCE_URL"
	@echo "  make dry-run-file     - Executa em modo dry-run usando SOURCE_FILE"
	@echo "  make save-file        - Executa com permissão de salvamento (--save) usando SOURCE_FILE"
	@echo "  make clean            - Remove caches temporários de teste e bytecode"

setup:
	@test -f .env || cp .env.example .env
	$(UV) sync --extra dev
	$(UV) run playwright install chromium

setup-venv:
	@test -f .env || cp .env.example .env
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	.venv/bin/playwright install chromium

install-browsers:
	$(UV) run playwright install chromium

test:
	$(UV) run pytest

inspect:
	$(UV) run python -m stays_automation inspect --target-url "$(TARGET_URL)"

dry-run-url:
	$(UV) run python -m stays_automation --target-url "$(TARGET_URL)" --source-url "$(SOURCE_URL)" --dry-run

dry-run-file:
	$(UV) run python -m stays_automation --target-url "$(TARGET_URL)" --source-file "$(SOURCE_FILE)" --dry-run

save-file:
	$(UV) run python -m stays_automation --target-url "$(TARGET_URL)" --source-file "$(SOURCE_FILE)" --save

clean:
	rm -rf .pytest_cache build dist src/*.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +
