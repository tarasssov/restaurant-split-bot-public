SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install local-run webhook-run ocr-test parser-test unit-test local-check quality-alert-dry-run

help:
	@echo "Targets:"
	@echo "  make venv                  - create virtual environment"
	@echo "  make install               - install/update dependencies"
	@echo "  make local-run             - run Telegram bot locally (polling)"
	@echo "  make webhook-run           - run webhook server locally"
	@echo "  make ocr-test IMAGE=...    - run Yandex OCR integration test on your own image"
	@echo "  make parser-test           - run parser regression test on sanitized fixtures"
	@echo "  make unit-test             - run unittest suite in ./tests"
	@echo "  make local-check           - run quality_report + receipt_auto_check"
	@echo "  make quality-alert-dry-run - evaluate weekly alert without sending Telegram"

venv:
	python3 -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

local-run:
	@test -f .env || (echo "Missing .env. Create it from .env.example first." && exit 1)
	$(PY) -m app.bot

webhook-run:
	@test -f .env || (echo "Missing .env. Create it from .env.example first." && exit 1)
	$(PY) -m app.webhook

ocr-test:
	@test -f .env || (echo "Missing .env. Create it from .env.example first." && exit 1)
	@test -n "$(IMAGE)" || (echo "Usage: make ocr-test IMAGE=/abs/path/to/receipt.jpg" && exit 1)
	$(PY) scripts/test_yandex_ocr.py --image "$(IMAGE)"

parser-test:
	$(PY) test_parser.py

unit-test:
	$(PY) -m unittest discover -s tests -q

local-check:
	$(PY) scripts/quality_report.py --days 7
	$(PY) scripts/receipt_auto_check.py --days 7 --limit 20

quality-alert-dry-run:
	$(PY) scripts/quality_alert_check.py --dry-run
