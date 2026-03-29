# Local Development

## Polling mode

Use polling for local development:

```bash
make local-run
```

Minimal `.env`:
- `BOT_TOKEN`
- `OCR_PROVIDER`
- `YANDEX_FOLDER_ID` and `YANDEX_VISION_API_KEY` if you use Yandex OCR

Optional:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_PROXY_URL`

## Webhook mode

Use webhook mode only if you have a reachable HTTPS endpoint:

```bash
make webhook-run
```

Required variables:
- `WEBHOOK_BASE_URL`
- `WEBHOOK_PATH`
- `WEBHOOK_SECRET`
- `WEBHOOK_HOST`
- `WEBHOOK_PORT`

## Quality tooling

Run unit tests:

```bash
make unit-test
```

Run parser regression:

```bash
make parser-test
```

Run OCR integration on your own image:

```bash
make ocr-test IMAGE=/abs/path/to/receipt.jpg
```
