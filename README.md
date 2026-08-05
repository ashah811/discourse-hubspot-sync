# Discourse → HubSpot Contact Sync

Syncs Discourse users into HubSpot as contacts, setting a "Discourse User"
checkbox property to true on each one. Handles both the initial bulk sync
and ongoing incremental syncs (only users updated since the last run).

## Files

- `sync_discourse_to_hubspot.py` — the sync script
- `discourse-hubspot-sync.yml` — GitHub Actions workflow to run it on a schedule

## One-time setup

### 1. HubSpot

- Settings → Integrations → Service Keys → Create a service key
  - Name: `discourse-contacts-sync`
  - Scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`
  - Copy the key immediately — store it securely, you won't see it again
- Settings → Properties → Contact properties → Create property
  - Label: `Discourse User`
  - Field type: **Single checkbox**
  - Note the internal name shown (should default to `discourse_user`)

### 2. Discourse

- Admin → API → API Keys → New API Key
  - Scope: Global (or narrower "read-only" + "users" if preferred)
  - User Level: All Users
  - Copy the key

### 3. Repo setup

Place `sync_discourse_to_hubspot.py` in your repo. Move
`discourse-hubspot-sync.yml` into `.github/workflows/` in that same repo.

Add these as repo secrets (Settings → Secrets and variables → Actions):

| Secret name              | Value                                      |
|---------------------------|---------------------------------------------|
| `DISCOURSE_URL`           | e.g. `https://your-community.discourse.group` |
| `DISCOURSE_API_KEY`       | Discourse admin API key                    |
| `DISCOURSE_API_USERNAME`  | Username tied to the Discourse key (e.g. `system`) |
| `HUBSPOT_TOKEN`           | HubSpot Service Key                        |

## Running it

**Locally (for testing, or to kick off the first bulk sync manually):**

```bash
pip install requests
export DISCOURSE_URL="https://your-community.discourse.group"
export DISCOURSE_API_KEY="..."
export DISCOURSE_API_USERNAME="system"
export HUBSPOT_TOKEN="..."
python sync_discourse_to_hubspot.py
```

The first run has no `last_sync.json`, so it does a full bulk sync of every
Discourse user. Every run after that only syncs users whose `last_seen_at`
is newer than the last recorded sync time.

**On a schedule:**

Once the workflow file is in `.github/workflows/` and secrets are set, it
runs automatically every day at 6am UTC. You can also trigger it manually
from the repo's Actions tab (workflow_dispatch).

## Verifying it worked

After the first run, spot-check a few contacts in HubSpot:
- "Discourse User" checkbox should be checked
- `discourse_username` and `discourse_trust_level` properties should be populated

## Notes

- Users without an email on file in Discourse are skipped (logged in the run output).
- HubSpot batch upsert matches on email, so re-running never creates duplicates.
- If a key is ever exposed, rotate it in HubSpot (Service Keys support rotation
  without rebuilding the integration) and update the `HUBSPOT_TOKEN` secret.
