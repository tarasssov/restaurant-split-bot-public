# Release Checklist

Use this checklist before tagging or announcing a new public release.

## Product

- README still matches current behavior
- `.env.example` matches runtime expectations
- no stale docs point to removed fixtures or private tooling

## Safety

- no `.env`, secrets, logs, or private runbooks are tracked
- no real receipt photos were added
- no personal or payment data appears in fixtures, issues, or docs

## Quality

- `make unit-test`
- `make parser-test`
- `bash -n scripts/*.sh ops/deploy/scripts/*.sh`

## GitHub

- issue templates still match the project scope
- `CONTRIBUTING.md` and `SECURITY.md` are present
- About description and topics are still relevant
- license is present and detected by GitHub
