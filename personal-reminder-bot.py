import json
import os
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# ---------------------------------------------------------------------------
# Secrets (GitHub -> Settings -> Secrets and variables -> Actions)
#   SLACK_BOT_TOKEN   xoxb-...  used ONLY to send you the reminder DM
#   SLACK_USER_TOKEN  xoxp-...  used to search your mentions (needs: search:read)
#   YOUR_USER_ID      your Slack user ID (U...)
#
# NOTE: This version does NOT need reactions:write. Dedup is handled by a small
# state file committed back to the repo (see STATE_FILE), so no message is ever
# DMed twice even though the bot never touches reactions.
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")

# Dubai is UTC+4, with no daylight saving.
DUBAI = timezone(timedelta(hours=4))

# Where the dedup memory lives. The workflow commits this file back to the repo
# after each run, so the next run remembers what was already sent.
STATE_FILE = os.environ.get("STATE_FILE", ".reminder-state/sent.json")
STATE_RETENTION_DAYS = 7   # forget entries older than this so the file stays small

# Default rolling window for the hourly daytime runs. Generous overlap so a
# skipped or delayed GitHub run is always caught by the following one.
DEFAULT_LOOKBACK_HOURS = 6

# Two flows, nothing more:
#   1. The 2 PM Dubai run is the daily SWEEP - one run that covers everything
#      from 11 PM last night to 2 PM today.
#   2. Every run from 3 PM to 11 PM is HOURLY, with the short 6h window.
#
# ACTIVE_START_HOUR / ACTIVE_END_HOUR describe your Dubai active window and MUST
# match the cron hours in the workflow (cron 10-19 UTC = 14-23 Dubai).
ACTIVE_START_HOUR = 14      # 2 PM Dubai - the single daily sweep run
ACTIVE_END_HOUR = 23        # 11 PM Dubai - last scheduled run of the day
SWEEP_MARGIN_HOURS = 2      # padding added to the computed overnight gap

MAX_DMS_PER_RUN = 40         # safety cap
MAX_SEARCH_PAGES = 4         # safety cap on search pagination (100/page)

if not SLACK_BOT_TOKEN or not SLACK_USER_TOKEN or not YOUR_USER_ID:
    print("ERROR: Missing required secrets! Need SLACK_BOT_TOKEN, SLACK_USER_TOKEN, and YOUR_USER_ID.")
    raise SystemExit(1)

user_client = WebClient(token=SLACK_USER_TOKEN)
bot_client = WebClient(token=SLACK_BOT_TOKEN)
for _c in (user_client, bot_client):
    _c.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))


# ---- decide the lookback window for THIS run ------------------------------
def resolve_lookback_hours():
    override = os.environ.get("LOOKBACK_HOURS_OVERRIDE", "").strip()
    if override:
        try:
            print(f"Lookback overridden to {int(override)}h via LOOKBACK_HOURS_OVERRIDE.")
            return int(override)
        except ValueError:
            print(f"WARN: LOOKBACK_HOURS_OVERRIDE='{override}' is not an integer; ignoring.")

    now_dubai = datetime.now(DUBAI)

    # Flow 1: the 2 PM run is the single daily sweep. Its window is sized to
    # reach back to last night's ACTIVE_END_HOUR (11 PM), covering the overnight
    # gap in one run, whatever minute it actually fires at.
    if now_dubai.hour == ACTIVE_START_HOUR:
        prev_end = (now_dubai - timedelta(days=1)).replace(
            hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0)
        gap_hours = (now_dubai - prev_end).total_seconds() / 3600.0
        window = max(int(gap_hours) + SWEEP_MARGIN_HOURS, DEFAULT_LOOKBACK_HOURS)
        print(f"Daily sweep run ({now_dubai:%H:%M} Dubai): using {window}h window "
              f"(back to ~{prev_end:%Y-%m-%d %H:%M}).")
        return window

    # Flow 2: every other run (3 PM - 11 PM) is hourly with the short window.
    print(f"Hourly run ({now_dubai:%H:%M} Dubai): using {DEFAULT_LOOKBACK_HOURS}h window.")
    return DEFAULT_LOOKBACK_HOURS


LOOKBACK_HOURS = resolve_lookback_hours()


# ---- dedup state (committed back to the repo by the workflow) --------------
def load_state():
    """Return {key: iso_timestamp} of mentions already DMed, pruned to recent."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"WARN: could not read state file ({STATE_FILE}): {e}. Starting fresh.")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    pruned = {}
    for key, iso in data.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                pruned[key] = iso
        except Exception:
            pruned[key] = iso  # keep anything unparseable rather than lose it
    return pruned


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        print(f"State saved: {len(state)} remembered mention(s) in {STATE_FILE}")
    except Exception as e:
        print(f"WARN: could not write state file ({STATE_FILE}): {e}")


# ---- verify tokens --------------------------------------------------------
try:
    user_auth = user_client.auth_test()
    user_scopes = user_auth.headers.get("x-oauth-scopes", "") or ""
    scope_set = {s.strip() for s in user_scopes.split(",")}
    print(f"User token OK - acting as: {user_auth.get('user')} | scopes: {user_scopes}")
except Exception as e:
    print(f"ERROR: User token check failed: {e}")
    raise SystemExit(1)

if user_scopes and "search:read" not in scope_set:
    print("ERROR: User token is missing the 'search:read' scope.")
    raise SystemExit(1)

try:
    bot_auth = bot_client.auth_test()
    BOT_USER_ID = bot_auth.get("user_id")
    BOT_USERNAME = bot_auth.get("user")
    print(f"Bot token OK - DMs sent by: {BOT_USERNAME} ({BOT_USER_ID})")
except Exception as e:
    print(f"ERROR: Bot token check failed: {e}")
    raise SystemExit(1)


# ---- helpers ---------------------------------------------------------------
_name_cache = {}
_is_bot_cache = {}


def _lookup_user(user_id):
    if user_id in _name_cache:
        return
    name, is_bot = user_id, False
    try:
        u = bot_client.users_info(user=user_id).get("user", {}) or {}
        name = u.get("real_name") or (u.get("profile") or {}).get("display_name") or u.get("name") or user_id
        is_bot = bool(u.get("is_bot")) or u.get("id") == "USLACKBOT"
    except Exception:
        pass
    _name_cache[user_id] = name
    _is_bot_cache[user_id] = is_bot


def sender_is_bot(msg):
    """True for posts by apps, bots and Workflow Builder workflows."""
    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
        return True
    uid = msg.get("user")
    if not uid:
        return True
    _lookup_user(uid)
    return _is_bot_cache.get(uid, False)


def display_name(user_id):
    """Plain-text name (never a real <@mention>, so the reminder DM itself
    can never be picked up as a mention on the next run)."""
    if not user_id:
        return "unknown"
    _lookup_user(user_id)
    return _name_cache.get(user_id, user_id)


def is_bots_own_message(msg):
    ch = msg.get("channel") or {}
    return (
        msg.get("user") == BOT_USER_ID
        or msg.get("username") == BOT_USERNAME
        or ch.get("name") == BOT_USER_ID
    )


def has_any_reaction(msg):
    """Any reaction at all -> you've acknowledged it yourself, skip.
    This still works with only search:read (via the has:reaction search and the
    reactions array Slack returns on matched messages)."""
    return bool(msg.get("reactions"))


def msg_key(msg):
    ch, ts = (msg.get("channel") or {}).get("id"), msg.get("ts")
    return f"{ch}_{ts}"  # string key so it serializes cleanly into JSON state


def run_search(query, since_ts):
    matches, page = [], 1
    while page <= MAX_SEARCH_PAGES:
        res = user_client.search_messages(
            query=query, sort="timestamp", sort_dir="desc", count=100, page=page
        )
        block = res.get("messages", {}) or {}
        page_matches = block.get("matches", []) or []
        matches.extend(page_matches)
        total_pages = (block.get("paging") or {}).get("pages") or 1
        if not page_matches or page >= total_pages:
            break
        if float(page_matches[-1].get("ts", "0")) < since_ts:
            break
        page += 1
    return matches


def search_recent_mentions():
    now = datetime.now(timezone.utc)
    since_ts = (now - timedelta(hours=LOOKBACK_HOURS)).timestamp()
    # Slack's after: filter is day-granular, so widen by a day and trim by ts below.
    after_date = (now - timedelta(hours=LOOKBACK_HOURS + 24)).strftime("%Y-%m-%d")
    base = f"<@{YOUR_USER_ID}> after:{after_date}"

    all_mentions = run_search(base, since_ts)
    reacted = run_search(base + " has:reaction", since_ts)
    return all_mentions, {msg_key(m) for m in reacted}, since_ts


def is_direct_or_group(msg):
    """True for 1:1 DMs and group DMs. Slack already sends you a native
    notification for those, so the bot skips them - it only covers channel
    mentions you might scroll past. DM channel IDs start with 'D', group DMs
    with 'G' (or an 'mpdm' name); a DM's channel 'name' is the other user's
    ID (starts with U/W, all caps) rather than a real channel name."""
    ch = msg.get("channel") or {}
    cid = ch.get("id", "") or ""
    cname = ch.get("name", "") or ""
    if cid.startswith("D") or cid.startswith("G"):
        return True
    if cname.startswith("mpdm"):
        return True
    if cname[:1] in ("U", "W") and cname.isupper():
        return True
    return False


def build_dm(msg):
    sender = display_name(msg.get("user"))
    ch = msg.get("channel") or {}
    where = f"#{ch.get('name')}" if ch.get("name") else "a channel"
    return (
        f"You were mentioned in {where}:\n\n"
        f"From: @{sender}\n\n"
        f"Open the post: {msg.get('permalink', '')}"
    )


# ---- main ------------------------------------------------------------------
def check_mentions():
    now_dubai = datetime.now(DUBAI)
    print(f"\n[{now_dubai:%Y-%m-%d %H:%M:%S} Dubai] "
          f"Searching unacknowledged mentions in last {LOOKBACK_HOURS}h...")

    state = load_state()          # {key: iso} already DMed on a previous run
    already_sent = set(state)
    print(f"Loaded {len(already_sent)} previously-sent mention(s) from state.")

    try:
        matches, reacted_keys, since_ts = search_recent_mentions()
    except Exception as e:
        print(f"ERROR: Search failed: {e}")
        raise SystemExit(1)
    print(f"Search returned {len(matches)} mention(s); {len(reacted_keys)} already have a reaction")

    seen = set()
    pending = []
    skipped_reacted = skipped_bot = skipped_old = skipped_sent = skipped_dm = 0

    for msg in matches:
        key = msg_key(msg)
        ch = (msg.get("channel") or {}).get("id")
        ts = msg.get("ts")
        if not ts or not ch or key in seen:
            continue
        seen.add(key)  # in-run dedup: overlapping searches can't double-send

        if float(ts) < since_ts:
            skipped_old += 1
            continue
        # DMs and group DMs -> Slack already notifies you natively, skip.
        if is_direct_or_group(msg):
            skipped_dm += 1
            continue
        if is_bots_own_message(msg) or sender_is_bot(msg):
            skipped_bot += 1
            continue
        # You reacted to it yourself -> you've seen it, skip.
        if key in reacted_keys or has_any_reaction(msg):
            skipped_reacted += 1
            continue
        # Already DMed on an earlier run -> never send twice.
        if key in already_sent:
            skipped_sent += 1
            continue

        pending.append(msg)

    print(f"Skipped: {skipped_sent} already sent, {skipped_reacted} you reacted, "
          f"{skipped_dm} DM/group-DM, {skipped_bot} bot/workflow, {skipped_old} outside window")

    if not pending:
        print("No new unacknowledged mentions. Nothing sent.")
        save_state(state)  # still rewrite (prunes old entries)
        return

    # oldest first so DMs arrive in chronological order
    pending.sort(key=lambda m: float(m.get("ts", "0")))
    delivered = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for msg in pending[:MAX_DMS_PER_RUN]:
        key = msg_key(msg)
        # Record BEFORE sending so a crash mid-loop can't re-DM an item that
        # already went out. Worst case if the DM then fails: one missed reminder,
        # not a duplicate. (The WARN below makes that visible in the run log.)
        state[key] = now_iso
        try:
            bot_client.chat_postMessage(channel=YOUR_USER_ID, text=build_dm(msg))
            delivered += 1
            print(f"DM sent for mention in #{(msg.get('channel') or {}).get('name', '?')}")
        except Exception as e:
            print(f"WARN: failed to send DM (will not retry this one): {e}")

    save_state(state)
    print(f"Done. {delivered} new DM(s) sent.")


if __name__ == "__main__":
    check_mentions()
