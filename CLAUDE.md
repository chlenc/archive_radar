# Project notes for Claude

Marketplace Telegram parser. Polls Grailed/Goofish per active brand, plus
hardcoded Shopify stores (`app/stores.py`), and posts new listings into
Telegram forum topics.

## Deploy target

Production runs on CapRover at `captain.tinyoyster.ventolabs.com`.

- App: **archive-radar** (renamed from old `china-bot`)
- URL: https://captain.tinyoyster.ventolabs.com/#/apps/details/archive-radar
- App token, Telegram bot token, Supabase DSN, etc. live in `.env` —
  the same `.env` is the source of truth for both local and deploy.

To deploy from a clean checkout:

```bash
set -a; source .env; set +a
caprover deploy \
  --caproverUrl "$CAPROVER_URL" \
  --caproverApp "$CAPROVER_APP" \
  --appToken "$CAPROVER_APP_TOKEN" \
  --branch main
```

CapRover pulls `main` from `origin` (`git@github.com:chlenc/archive_radar.git`),
builds via `Dockerfile`, runs `python -m app worker` as a background worker.

Persistent CapRover volumes that must stay mounted on `archive-radar`:
`/app/data`, `/app/state`, `/app/logs`. Without these the SQLite fallback
and Playwright storage states get wiped on every redeploy.

## Stores (hardcoded, not editable from the bot)

Edit `app/stores.py` to add/remove. Topics are created automatically on
worker startup; backlog is suppressed on the first ingest per store
(`mark_existing_listings_sent_for_store` + `mark_store_seeded`). The
`stores` table only stores the auto-created `thread_id` — there are no
admin commands for stores. archivethreads.ca was excluded because it is
a Wix shop, not Shopify.

## Bot commands worth remembering

- `/list_brands`, `/add_brand`, `/bind_brand`, `/remove_brand` — manage brands.
- `/list_stores` — read-only list of hardcoded stores with `seeded` state.
- `/status` — counts plus recent parser-status rows.
