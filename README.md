# restaurant-split-bot

[![CI](https://github.com/tarasssov/restaurant-split-bot-public/actions/workflows/ci.yml/badge.svg)](https://github.com/tarasssov/restaurant-split-bot-public/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/tarasssov/restaurant-split-bot-public)](./LICENSE)

Telegram bot for splitting restaurant bills from receipt photos.

This bot solves a very common post-dinner problem: one or two people pay the full bill, everyone ordered different things, and then the group has to figure out who owes what.

The project accepts a receipt image, runs OCR, extracts line items, asks users to confirm or fix the parse, tracks who ate what and who already paid, and calculates who owes whom in the end.

![restaurant-split-bot preview](docs/preview-card.svg)

## Why This Exists

Restaurant bill splitting is messy in real life:
- one or two people pay the whole check
- several people ordered different dishes and drinks
- someone shared food
- someone already transferred money
- the group still needs a clear final answer: who owes whom

This bot is built to take that chaos and turn it into a structured settlement flow.

## How It Works

1. A user sends a receipt photo.
2. The bot runs OCR and extracts raw receipt text.
3. The parser builds line items and the total.
4. Users confirm or fix the parsed items.
5. Participants mark who ate what and who already paid.
6. The bot calculates balances and minimal transfers.

```mermaid
flowchart LR
    A[Receipt Photo] --> B[OCR]
    B --> C[Receipt Parser]
    C --> D[User Confirmation]
    D --> E[Participants and Payments]
    E --> F[Settlement Result]
```

## Features

- OCR-based receipt parsing
- Manual confirmation and correction of parsed items
- Split by item or split evenly
- Optional LLM-assisted refinement
- Polling mode for local development
- Webhook mode for production
- Quality and observability scripts for parser/OCR iterations

## Architecture

Main modules:
- `app/bot.py` — aiogram bot flow and Telegram interaction logic
- `app/webhook.py` — webhook runtime built on `aiohttp`
- `app/ocr.py`, `app/ocr_layout.py`, `app/ocr_normalizer.py` — OCR extraction and normalization
- `app/receipt_parser.py`, `app/receipt_layout_parser.py` — rule-based receipt parsing
- `app/llm.py`, `app/llm_refiner.py` — optional LLM parse/refine layer
- `app/storage.py` — in-memory session storage
- `app/split_calc.py` — balances and minimal transfer calculation
- `scripts/` — OCR integration checks, quality reports, replay and export tools
- `tests/` — unit tests for parser and quality-related logic

## Quick Start

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a local env file:

```bash
cp .env.example .env
```

Fill at least:
- `BOT_TOKEN`
- `OCR_PROVIDER`
- `YANDEX_FOLDER_ID` and `YANDEX_VISION_API_KEY` if you use Yandex OCR
- `OPENAI_API_KEY` if you want LLM-assisted refinement

Run locally in polling mode:

```bash
python3 -m app.bot
```

Or with `make`:

```bash
make install
make local-run
```

## Runtime Modes

### Polling

Recommended for local development:

```bash
make local-run
```

### Webhook

Recommended for production behind HTTPS reverse proxy:

```bash
make webhook-run
```

Required webhook variables:
- `WEBHOOK_BASE_URL`
- `WEBHOOK_PATH`
- `WEBHOOK_SECRET`
- `WEBHOOK_HOST`
- `WEBHOOK_PORT`

## Environment Variables

Core:
- `BOT_TOKEN`
- `TIP_PERCENT`

OCR:
- `OCR_PROVIDER`
- `YANDEX_FOLDER_ID`
- `YANDEX_VISION_API_KEY`

LLM:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_PROXY_URL`
- `LLM_MAX_CALLS_PER_RECEIPT`
- `LLM_OCR_EXCERPT_CHARS`
- `LLM_GROUNDING_STRICT`
- `LLM_MAX_UNSUPPORTED_RATIO`
- `LLM_MAX_NOVEL_TOKEN_RATIO`

Quality alerts:
- `QUALITY_ALERT_ENABLED`
- `QUALITY_ALERT_WEEKDAY`
- `QUALITY_ALERT_HOUR`
- `QUALITY_ALERT_TZ`
- `QUALITY_ALERT_MIN_NEW_FAILS`
- `QUALITY_ALERT_CHAT_ID`
- `QUALITY_ALERT_BOT_TOKEN`
- `QUALITY_ALERT_PROJECT_NAME`
- `QUALITY_ALERT_PROXY_URL`

Webhook:
- `WEBHOOK_BASE_URL`
- `WEBHOOK_PATH`
- `WEBHOOK_SECRET`
- `WEBHOOK_HOST`
- `WEBHOOK_PORT`

See the full template in `.env.example`.

## Running Tests

Unit tests:

```bash
make unit-test
```

Parser regression on sanitized synthetic fixtures:

```bash
make parser-test
```

Yandex OCR integration test on your own image:

```bash
make ocr-test IMAGE=/abs/path/to/receipt.jpg
```

Or with multiple files:

```bash
python3 scripts/test_yandex_ocr.py --image /abs/path/receipt1.jpg --image /abs/path/receipt2.jpg
```

The public repository intentionally does not ship real receipt photos.

## User Flow

At confirmation time the bot:
- shows parsed items
- reports quality status: `good`, `warning`, or `low_confidence`
- asks the user to confirm, fix, or rescan if needed

After confirmation, users can:
- split by item
- split evenly
- record payments
- get final balances and minimal transfers

## Observability

The bot writes diagnostic logs to:
- `logs/receipt_sessions.jsonl`
- `logs/receipt_user_edits.jsonl`
- `logs/ocr_sessions/<receipt_session_id>.txt`
- `logs/quality_reports/alert_state.json`

Useful commands:

```bash
python3 scripts/quality_report.py --days 7
python3 scripts/receipt_auto_check.py --days 7 --limit 20
python3 scripts/export_receipt_dataset.py --days 30 --status low_confidence --limit 20
./scripts/run_weekly_quality_report.sh
```

To generate local weekly automation templates for your own machine:

```bash
./scripts/install_weekly_quality_automation.sh
```

## Deployment

The repo includes generic Linux VM deployment scaffolding:
- `ops/deploy/systemd/restaurant-split-bot.service`
- `ops/deploy/nginx/restaurant-split-bot.conf`
- `ops/deploy/scripts/bootstrap_vm.sh`
- `ops/deploy/scripts/deploy_vm.sh`

Example bootstrap flow:

```bash
cd /opt/restaurant-split-bot
chmod +x ops/deploy/scripts/bootstrap_vm.sh ops/deploy/scripts/deploy_vm.sh
DOMAIN=bot.example.com RUN_USER=ubuntu APP_DIR=/opt/restaurant-split-bot ./ops/deploy/scripts/bootstrap_vm.sh
sudo certbot --nginx -d bot.example.com
APP_DIR=/opt/restaurant-split-bot BRANCH=main SERVICE_NAME=restaurant-split-bot ./ops/deploy/scripts/deploy_vm.sh
```

The GitHub Actions workflow `.github/workflows/deploy.yml` can be used as a starting point for SSH-based deployment.

## Repository Layout

```text
app/         bot runtime, OCR, parser, split logic
tests/       unit tests
scripts/     integration and observability tools
ops/deploy/  generic deployment scaffolding
fixtures/    sanitized synthetic parser fixtures
docs/        public notes and roadmap
```

## Community

- Contributing guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Security policy: [SECURITY.md](./SECURITY.md)
- Release checklist: [docs/RELEASE_CHECKLIST.md](./docs/RELEASE_CHECKLIST.md)

## Notes

- No real credentials should be committed. Use `.env`.
- This public repo intentionally excludes private ops/runbooks and real receipt datasets.
- If you want production rollout, adapt `ops/deploy/` to your own infrastructure.

## License

MIT
