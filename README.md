# Marketplace Telegram Parser

Parses new listings from Goofish and Grailed by brand and posts them into Telegram forum topics.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

Edit `.env`, then run:

```bash
python -m app test-telegram
python -m app worker
```

The app supports both SQLite and Postgres/Supabase:

- set `DATABASE_URL` to use Postgres/Supabase
- leave `DATABASE_URL` empty to stay on SQLite
- keep `DATABASE_PATH` even with Postgres if you want a local SQLite file as a migration source

To migrate the current SQLite state into Postgres:

```bash
python -m app migrate-to-postgres
```

For Goofish login:

```bash
python -m app goofish-login
```

If Grailed shows Cloudflare/security verification in headless mode:

```bash
python -m app grailed-warmup
```

If you have Grailed credentials and want to create a reusable signed-in session:

```bash
python -m app grailed-login
```

The Telegram bot supports:

```text
/whoami
/add_brand Nike
/remove_brand Nike
/list_brands
/status
```

Telegram topics require a forum supergroup. A normal channel cannot create Bot API topics.

## CapRover

The app is a background worker. It does not expose an HTTP port.

Use persistent directories in CapRover:

```text
/app/data
/app/state
/app/logs
```

Optional cookie storage can be supplied via `GOOFISH_STORAGE_STATE_JSON` and
`GRAILED_STORAGE_STATE_JSON`.

`MAX_SENDS_PER_RUN` limits how many unsent listings are posted per scheduler tick,
which helps avoid Telegram flood limits during the initial backlog drain.
