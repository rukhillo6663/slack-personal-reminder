import os
import re
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

LOOKBACK_HOURS = 24        # how far back to look for mentions
MAX_ITEMS_IN_DM = 40       # safety cap on items listed in one reminder
MAX_SEARCH_PAGES = 3       # safety cap on search pagination (100 results/page)

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


def display_name(user_id):
    """Plain-text name for a user ID (never a real <@mention>, so the reminder
    itself can never be picked up as a mention on the next run)."""
    if not user_id:
        return "unknown"
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = user_id
    try:
        u = bot_client.users_info(user=user_id).get("user", {}) or {}
        name = u.get("real_name") or (u.get("profile") or {}).get("display_name") or u.get("name") or user_id
    except Exception:
        pass
    _name_cache[user_id] = name
    return name


MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")


def clean_text(text, limit=150):
    text = MENTION_RE.sub(
        lambda m: "@you" if m.group(1) == YOUR_USER_ID else "@" + display_name(m.group(1)), text or ""
    )
    text = CHANNEL_RE.sub(lambda m: "#" + m.group(2), text)
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def is_bots_own_message(msg):
    ch = msg.get("channel") or {}
    return (
        msg.get("user") == BOT_USER_ID
        or msg.get("username") == BOT_USERNAME
        or ch.get("name") == BOT_USER_ID  # the DM channel between you and the bot
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
        # results are newest-first: stop once we're past the window
        if float(page_matches[-1].get("ts", "0")) < since_ts:
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
    # Same search, restricted to messages that already have ANY emoji reaction.
    # Needs only search:read - no reactions:read scope required.
    reacted = run_search(base + " has:reaction", since_ts)
    reacted_keys = {msg_key(m) for m in reacted}
    return all_mentions, reacted_keys, since_ts


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
        ch = msg.get("channel") or {}
        if not ts or not channel_id:
            continue
        if (channel_id, ts) in seen:
            continue
        seen.add((channel_id, ts))

        if float(ts) < since_ts:
            skipped_old += 1
            continue
        if is_bots_own_message(msg):
            skipped_bot += 1
            continue
        if (channel_id, ts) in reacted_keys:
            skipped_reacted += 1
            continue

        if msg.get("type") == "im" or ch.get("is_im"):
            where = "DM with " + display_name(ch.get("name"))
        else:
            where = "#" + (ch.get("name") or channel_id)

        pending.append({
            "where": where,
            "from": display_name(msg.get("user")),
            "text": clean_text(msg.get("text")),
            "link": msg.get("permalink", ""),
        })

    print(f"Skipped: {skipped_reacted} already reacted, {skipped_bot} bot's own reminders, {skipped_old} outside window")

    if not pending:
        print("No unacknowledged mentions. Nothing sent.")
        return

    n = len(pending)
    lines = [f"*You have {n} unacknowledged mention{'s' if n != 1 else ''}* (no emoji reaction yet). React to the original message to clear it."]
    for it in pending[:MAX_ITEMS_IN_DM]:
        lines.append(f"• *{it['where']}* — {it['from']}: \"{it['text']}\"  <{it['link']}|Open>")
    if n > MAX_ITEMS_IN_DM:
        lines.append(f"...and {n - MAX_ITEMS_IN_DM} more.")

    try:
        bot_client.chat_postMessage(channel=YOUR_USER_ID, text="\n".join(lines), unfurl_links=False, unfurl_media=False)
        print(f"Done. 1 reminder DM sent listing {n} mention(s).")
    except Exception as e:
        print(f"ERROR: Failed to send reminder DM: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    check_mentions()
