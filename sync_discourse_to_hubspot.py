"""
Discourse -> HubSpot Contact Sync

Pulls users from Discourse and upserts them as contacts in HubSpot,
setting a "Discourse User" checkbox property to true on each one.

On first run, syncs ALL users (bulk historical sync).
On subsequent runs, only syncs users updated since the last run
(via last_sync.json), so it can be run repeatedly / on a schedule.

Required environment variables:
  DISCOURSE_URL          e.g. "https://your-community.discourse.group"
  DISCOURSE_API_KEY      Admin API key from Discourse (Admin > API > API Keys)
  DISCOURSE_API_USERNAME Username tied to that key (often "system")
  HUBSPOT_TOKEN          HubSpot Service Key (Settings > Integrations > Service Keys)

Optional:
  HUBSPOT_DISCOURSE_PROPERTY  Internal name of the checkbox property.
                               Defaults to "discourse_user".
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config (from environment variables — never hardcode secrets)
# ---------------------------------------------------------------------------
DISCOURSE_URL = os.environ["DISCOURSE_URL"].rstrip("/")
DISCOURSE_API_KEY = os.environ["DISCOURSE_API_KEY"]
DISCOURSE_API_USERNAME = os.environ["DISCOURSE_API_USERNAME"]

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_DISCOURSE_PROPERTY = os.environ.get("HUBSPOT_DISCOURSE_PROPERTY", "discourse_user")

STATE_FILE = "last_sync.json"

DISCOURSE_HEADERS = {
    "Api-Key": DISCOURSE_API_KEY,
    "Api-Username": DISCOURSE_API_USERNAME,
}

HUBSPOT_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# State tracking (for incremental syncs)
# ---------------------------------------------------------------------------
def get_last_sync():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f).get("last_sync")
    return None


def set_last_sync(ts):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_sync": ts}, f)


# ---------------------------------------------------------------------------
# Discourse
# ---------------------------------------------------------------------------
def get_all_discourse_users():
    """Paginate through all active Discourse users.

    The bulk list endpoint doesn't reliably return the email field for
    every user (a known Discourse API quirk), even with an admin key and
    show_emails=true. So after the bulk pull, any user missing an email
    gets a follow-up per-user call that does reliably include it.
    """
    users = []
    page = 0
    while True:
        resp = requests.get(
            f"{DISCOURSE_URL}/admin/users/list/active.json",
            headers=DISCOURSE_HEADERS,
            params={"page": page, "show_emails": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        users.extend(batch)
        page += 1
        time.sleep(0.5)  # be polite to Discourse's rate limits

    missing_email = [u for u in users if not u.get("email")]
    if missing_email:
        print(
            f"{len(missing_email)} users missing email from bulk list — "
            f"fetching individually..."
        )
        for i, user in enumerate(missing_email):
            email = get_user_email(user["id"])
            if email:
                user["email"] = email
            if (i + 1) % 50 == 0:
                print(f"  ...fetched {i + 1}/{len(missing_email)}")
            time.sleep(0.3)  # per-user calls add up, stay polite to rate limits

    return users


def get_user_email(user_id):
    """Fetch a single user's record, which reliably includes email."""
    resp = requests.get(
        f"{DISCOURSE_URL}/admin/users/{user_id}.json",
        headers=DISCOURSE_HEADERS,
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("email")


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
ANON_SHADOW_USERNAME = re.compile(r"^anon(ymous)?\d+$", re.IGNORECASE)


def is_real_email(email):
    """Filter out Discourse's placeholder emails for deleted/anonymized/system accounts."""
    if not email or email in ("no_email", "discobot_email"):
        return False
    if email.endswith("@anonymized.invalid"):
        return False
    return True


def is_shadow_anon_account(user):
    """Filter out Discourse's anonymous-posting-mode shadow accounts.

    These are auto-generated stand-in accounts (usernames like 'anonymous10'
    or 'anon111105') tied to a real user who posted anonymously — not
    distinct people, so they shouldn't be synced as separate contacts.
    """
    username = user.get("username", "")
    return bool(ANON_SHADOW_USERNAME.match(username))


def to_hubspot_contact(user):
    email = user.get("email")
    if not is_real_email(email):
        return None
    if is_shadow_anon_account(user):
        return None

    name = (user.get("name") or "").strip()
    parts = name.split(" ", 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    return {
        "idProperty": "email",
        "id": email,
        "properties": {
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "discourse_username": user.get("username", ""),
            "discourse_trust_level": str(user.get("trust_level", "")),
            HUBSPOT_DISCOURSE_PROPERTY: "true",
        },
    }


def chunk(lst, size=100):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------
def batch_upsert(contacts_batch):
    resp = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert",
        headers=HUBSPOT_HEADERS,
        json={"inputs": contacts_batch},
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  Batch error ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    last_sync = get_last_sync()
    run_started_at = datetime.now(timezone.utc).isoformat()

    if last_sync:
        print(f"Incremental sync since {last_sync}")
    else:
        print("No previous sync found — running full bulk sync")

    print("Fetching Discourse users...")
    users = get_all_discourse_users()
    print(f"Found {len(users)} total Discourse users")

    if last_sync:
        users = [u for u in users if u.get("last_seen_at", "") > last_sync]
        print(f"{len(users)} updated since last sync")

    contacts = [c for c in (to_hubspot_contact(u) for u in users) if c]
    skipped = len(users) - len(contacts)
    if skipped:
        print(f"Skipping {skipped} users with no email on file")

    print(f"Syncing {len(contacts)} contacts to HubSpot...")

    total_errors = 0
    for i, batch in enumerate(chunk(contacts)):
        result = batch_upsert(batch)
        errors = result.get("errors", [])
        total_errors += len(errors)
        print(f"  Batch {i + 1}: {len(batch)} contacts synced, {len(errors)} errors")
        time.sleep(0.2)  # HubSpot rate limit courtesy

    set_last_sync(run_started_at)
    print(f"Done. {len(contacts)} synced, {total_errors} errors. State saved to {STATE_FILE}.")


if __name__ == "__main__":
    main()
