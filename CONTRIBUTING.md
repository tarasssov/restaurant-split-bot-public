# Contributing

Thanks for considering a contribution.

## Before You Start

- Open an issue first for large changes.
- Keep changes focused and reviewable.
- Do not include secrets, `.env` files, logs, or real receipt photos in commits.
- Prefer synthetic or sanitized fixtures over real-world personal data.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

For most code changes you do not need real production credentials. Unit tests and parser regression can be run locally with synthetic fixtures.

## Recommended Checks

Run before opening a PR:

```bash
make unit-test
make parser-test
bash -n scripts/*.sh ops/deploy/scripts/*.sh
```

If you change OCR integration, you can also run:

```bash
make ocr-test IMAGE=/abs/path/to/receipt.jpg
```

## Scope Guidelines

Good contributions:
- parser quality improvements
- OCR normalization fixes
- UX improvements in the Telegram flow
- quality reporting and observability improvements
- deployment/documentation cleanup

Please avoid:
- committing private infrastructure details
- adding real receipt datasets
- mixing unrelated refactors into one PR

## Pull Requests

Use small PRs with a clear description:
- what changed
- why it changed
- how it was validated
- any risk or follow-up work

If your change affects parser or OCR behavior, include at least one focused test or synthetic fixture.
