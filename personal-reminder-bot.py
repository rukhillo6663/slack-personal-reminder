import json
import os
import re
import time
from datetime import datetime, timedelta

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# ---------------------------------------------------------------------------
# Secrets (GitHub → Settings → Secrets → Actions)
#   SLACK_BOT_TOKEN   xoxb-...  used ONLY to send you the reminder DM
#   SLACK_USER_TOKEN  xoxp-...  used to search your mentions (needs search:read only)
#   YOUR_USER_ID      your Slack user ID
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")

LOOKBACK_HOURS = 24          # how far back to look for mentions
MAX_DMS_PER_RUN = 30         # safety cap; anything beyond carries over to the next hour
MAX_SEARCH_PAGES = 3         # safety cap on search pagination (100 results/page)
STATE_FILE = "sent_messages.json"   # memory of what was already sent (kept between runs by the workflow cache)
STATE_KEEP_DAYS = 7

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

# ---- "already notified" memory -------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        sent = data.get("sent", {}) if isinstance(data, dict) else {}
    except FileNotFoundError:
        print("No memory file found - first run (or cache expired).")
        return {}
    except Exception as e:
        print(f"WARN: could not read {STATE_FILE}, starting fresh: {e}")
        return {}
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    sent = {k: v for k, v in sent.items() if isinstance(v, (int, float)) and v >= cutoff}
    print(f"Memory loaded: {len(sent)} mention(s) already notified")
    return sent


def save_state(sent):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"sent": sent}, f, indent=0)
    except Exception as e:
        print(f"WARN: could not write {STATE_FILE}: {e}")


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
    """Plain-text name (never a real <@mention>, so the reminder itself can
    never be picked up as a mention on a later run)."""
    if not user_id:
        return "unknown"
    _lookup_user(user_id)
    return _name_cache.get(user_id, user_id)


def is_bots_own_message(msg):
    ch = msg.get("channel") or {}
    return (
        msg.get("user") == BOT_USER_ID
        or msg.get("username") == BOT_USERNAME
        or ch.get("name") == BOT_USER_ID  # the DM channel between you and the bot
    )


_reaction_check_unavailable = set()


def exact_has_reaction(channel_id, ts):
    """Ask Slack directly whether the message has reactions. Returns True/False,
    or None if Slack won't let the bot look at that channel."""
    if channel_id in _reaction_check_unavailable:
        return None
    try:
        r = bot_client.reactions_get(channel=channel_id, timestamp=ts)
        return len((r.get("message") or {}).get("reactions", [])) > 0
    except Exception:
        _reaction_check_unavailable.add(channel_id)
        return None


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
        if float(page_matches[-1].get("ts", "0")) < since_ts:  # newest-first: past the window
            break
        page += 1
    return matches


def search_recent_mentions():
    now = datetime.now()
    since_ts = (now - timedelta(hours=LOOKBACK_HOURS)).timestamp()
    # Slack's after: filter is day-granular and exclusive, so ask for a wider
    # window and trim precisely by timestamp below.
    after_date = (now - timedelta(hours=LOOKBACK_HOURS + 24)).strftime("%Y-%m-%d")
    base = f"<@{YOUR_USER_ID}> after:{after_date}"

    all_mentions = run_search(base, since_ts)
    # Same search, restricted to messages that already have ANY emoji reaction
    # (needs only search:read).
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
    sent = load_state()

    try:
        matches, reacted_keys, since_ts = search_recent_mentions()
    except Exception as e:
        print(f"ERROR: Search failed: {e}")
        raise SystemExit(1)
    print(f"Search returned {len(matches)} mention(s), {len(reacted_keys)} of them with a reaction")

    seen = set()
    pending = []
    skipped_reacted = skipped_bot = skipped_old = skipped_sent = 0

    for msg in matches:
        channel_id, ts = msg_key(msg)
        if not ts or not channel_id or (channel_id, ts) in seen:
            continue
        seen.add((channel_id, ts))
        key = f"{channel_id}:{ts}"

        if float(ts) < since_ts:
            skipped_old += 1
            continue
        if is_bots_own_message(msg) or sender_is_bot(msg):
            skipped_bot += 1
            continue
        if key in sent:
            skipped_sent += 1
            continue

        reacted = (channel_id, ts) in reacted_keys
        exact = exact_has_reaction(channel_id, ts)
        if exact is not None:
            reacted = exact
        if reacted:
            skipped_reacted += 1
            continue

        pending.append((key, msg))

    print(f"Skipped: {skipped_sent} already notified, {skipped_reacted} already reacted, "
          f"{skipped_bot} bot/workflow posts, {skipped_old} outside window")

    if not pending:
        print("No new unacknowledged mentions. Nothing sent.")
        save_state(sent)
        return

    # oldest first so DMs arrive in the order the mentions happened
    pending.sort(key=lambda kv: float(kv[1].get("ts", "0")))
    delivered = 0
    for key, msg in pending[:MAX_DMS_PER_RUN]:
        try:
            bot_client.chat_postMessage(channel=YOUR_USER_ID, text=build_dm(msg))
            sent[key] = time.time()
            delivered += 1
            print(f"DM sent for mention in #{(msg.get('channel') or {}).get('name', '?')}")
        except Exception as e:
            print(f"WARN: failed to send DM for {key}, will retry next run: {e}")

    save_state(sent)
    left = len(pending) - min(len(pending), MAX_DMS_PER_RUN)
    print(f"Done. {delivered} new DM(s) sent." + (f" {left} more will go out next run." if left else ""))


if __name__ == "__main__":
    check_mentions()
