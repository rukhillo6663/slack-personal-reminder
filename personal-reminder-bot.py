import os
import time
from datetime import datetime, timedelta

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# ---------------------------------------------------------------------------
# Secrets (GitHub → Settings → Secrets → Actions)
#   SLACK_BOT_TOKEN   xoxb-...  used ONLY to send you the reminder DM
#   SLACK_USER_TOKEN  xoxp-...  used to search your mentions (needs search:read)
#   YOUR_USER_ID      your Slack user ID
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")

LOOKBACK_HOURS = 1           # bot runs hourly — only look at last 1 hour
MAX_DMS_PER_RUN = 30         # safety cap
MAX_SEARCH_PAGES = 3         # safety cap on search pagination (100 results/page)

if not SLACK_BOT_TOKEN or not SLACK_USER_TOKEN or not YOUR_USER_ID:
    print("ERROR: Missing required secrets! Need SLACK_BOT_TOKEN, SLACK_USER_TOKEN, and YOUR_USER_ID.")
    raise SystemExit(1)

user_client = WebClient(token=SLACK_USER_TOKEN)
bot_client = WebClient(token=SLACK_BOT_TOKEN)
for _c in (user_client, bot_client):
    _c.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))

# ---- verify tokens --------------------------------------------------------
try:
    user_auth = user_client.auth_test()
    user_scopes = user_auth.headers.get("x-oauth-scopes", "") or ""
    print(f"User token OK - searching as: {user_auth.get('user')} | scopes: {user_scopes}")
except Exception as e:
    print(f"ERROR: User token check failed: {e}")
    raise SystemExit(1)

if user_scopes and "search:read" not in {s.strip() for s in user_scopes.split(",")}:
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


def msg_key(msg):
    return ((msg.get("channel") or {}).get("id"), msg.get("ts"))


def run_search(query, since_ts):
    matches, page = [], 1
    while page <= MAX_SEARCH_PAGES:
        res = user_client.search_messages(query=query, sort="timestamp", sort_dir="desc", count=100, page=page)
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
    now = datetime.now()
    since_ts = (now - timedelta(hours=LOOKBACK_HOURS)).timestamp()
    # Slack's after: filter is day-granular, so widen to yesterday and trim by timestamp below
    after_date = (now - timedelta(hours=LOOKBACK_HOURS + 24)).strftime("%Y-%m-%d")
    base = f"<@{YOUR_USER_ID}> after:{after_date}"

    all_mentions = run_search(base, since_ts)
    reacted = run_search(base + " has:reaction", since_ts)
    return all_mentions, {msg_key(m) for m in reacted}, since_ts


def build_dm(msg):
    sender = display_name(msg.get("user"))
    return (
        "You were mentioned :\n\n"
        f"Sender: @{sender}\n\n"
        f"Open the post: {msg.get('permalink', '')}"
    )


# ---- main ------------------------------------------------------------------
def check_mentions():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Searching for unacknowledged mentions in last {LOOKBACK_HOURS}h...")

    try:
        matches, reacted_keys, since_ts = search_recent_mentions()
    except Exception as e:
        print(f"ERROR: Search failed: {e}")
        raise SystemExit(1)
    print(f"Search returned {len(matches)} mention(s), {len(reacted_keys)} of them with a reaction")

    seen = set()
    pending = []
    skipped_reacted = skipped_bot = skipped_old = 0

    for msg in matches:
        channel_id, ts = msg_key(msg)
        if not ts or not channel_id or (channel_id, ts) in seen:
            continue
        seen.add((channel_id, ts))

        if float(ts) < since_ts:
            skipped_old += 1
            continue
        if is_bots_own_message(msg) or sender_is_bot(msg):
            skipped_bot += 1
            continue
        if (channel_id, ts) in reacted_keys:
            skipped_reacted += 1
            continue

        pending.append(msg)

    print(f"Skipped: {skipped_reacted} already reacted, {skipped_bot} bot/workflow posts, {skipped_old} outside window")

    if not pending:
        print("No new unacknowledged mentions. Nothing sent.")
        return

    # oldest first so DMs arrive in chronological order
    pending.sort(key=lambda m: float(m.get("ts", "0")))
    delivered = 0
    for msg in pending[:MAX_DMS_PER_RUN]:
        try:
            bot_client.chat_postMessage(channel=YOUR_USER_ID, text=build_dm(msg))
            delivered += 1
            print(f"DM sent for mention in #{(msg.get('channel') or {}).get('name', '?')}")
        except Exception as e:
            print(f"WARN: failed to send DM: {e}")

    print(f"Done. {delivered} new DM(s) sent.")


if __name__ == "__main__":
    check_mentions()
