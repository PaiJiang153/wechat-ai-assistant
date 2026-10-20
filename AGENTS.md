# Instructions for coding agents

Read `README.md`, then `docs/AI_QUICKSTART.md` and `docs/CONTRACT.md` before changing code.

## Reproduce first

- Python 3.13 (64-bit) is the tested version. Install `requirements-dev.txt` into an isolated virtual environment.
- Run `python -m pytest -q`. Tests use fake model/WeChat dependencies and temporary runtime directories.
- Copy `config.minimal.yaml` to `config.yaml` only when the destination does not exist.
- `python -m src.main manage --port 8010` starts the maintenance UI without a model key or WeChat. Check `http://127.0.0.1:8010/manage/health`.
- Ask for the user's model credentials and target account/conversations only when needed for their requested integration. Do not invent credentials or put them in source files.

## Boundaries

- Never commit `config.yaml`, its backups, `.env`, `data/`, `logs/`, databases, account identifiers, or user backup archives. Never print secrets.
- Do not overwrite an existing user's configuration or runtime data during installation, testing, or upgrades.
- Keep the maintenance service local to loopback. Preserve token/origin checks, configuration conflict detection, and cleanup previews.
- Real WeChat requires an interactive Windows desktop. Do not launch `run`, send real messages, call paid models, or install automatic startup unless the user requested that action.
- Optional integrations must stay optional. Core installation and the maintenance UI must not import the Windows bridge or load ASR models.

## Architecture and verification

- `src/config.py`: dataclass defaults and YAML loading. `src/settings_ui.py`: allowlisted advanced settings and atomic configuration edits.
- `src/engine.py`: generation. `src/bot.py`: scheduling and delivery. `src/runtime.py`: receipts, metrics and atomic files.
- `src/wechatauto_adapter.py`: current WeChat integration. Other adapters are legacy compatibility paths.
- `web/manage.py` and `web/static/{js,css}/manage.*`: maintenance service and UI. `web/app.py`: chat sandbox.
- Confirm a reply before committing it to conversation history. Keep per-room ordering, bounded queues, and account-scoped receiver cursors.
- Run the affected tests, then the full suite for delivery/configuration/concurrency changes. If frontend JavaScript changed, also run `node --check web/static/js/manage.js` (or `app.js`).
- Keep Windows PowerShell scripts with a UTF-8 BOM; verify syntax with Windows PowerShell 5.1. Preserve `.gitattributes` line endings.
- Update configuration examples and docs when behavior changes. Run `python scripts/verify_public.py` before publishing.
