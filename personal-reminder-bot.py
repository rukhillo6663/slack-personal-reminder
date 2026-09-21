import os
from slack_sdk import WebClient
from datetime import datetime, timedelta

# Bot token (xoxb-...) — used only for sending you DMs
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")

# User token (xoxp-...) — used for searching your mentions
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN")

# Your Slack user ID
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")

if not SLACK_BOT_TOKEN or not SLACK_USER_TOKEN or not YOUR_USER_ID:
    print("ERROR: Missing required secrets! Need SLACK_BOT_TOKEN, SLACK_USER_TOKEN, and YOUR_USER_ID.")
    exit(1)

# Two clients: one for searching (as you), one for sending DMs (as the bot)
user_client = WebClient(token=SLACK_USER_TOKEN)
bot_client = WebClient(token=SLACK_BOT_TOKEN)

# Verify both tokens on startup
try:
    user_auth = user_client.auth_test()
    print(f"User token OK - searching as: {user_auth.get('user')}")
except Exception as e:
    print(f"ERROR: User token check failed: {e}")
    exit(1)

try:
    bot_auth = bot_client.auth_test()
    print(f"Bot token OK - DMs sent by: {bot_auth.get('user')}")
except Exception as e:
    print(f"ERROR: Bot token check failed: {e}")
    exit(1)


def check_mentions():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Searching for unacknowledged mentions in last 24h...")

    since = datetime.now() - timedelta(hours=24)
    date_str = since.strftime("%Y-%m-%d")
    query = f"<@{YOUR_USER_ID}> after:{date_str}"

    mentions_found = 0

    try:
        result = user_client.search_messages(query=query, sort="timestamp", sort_dir="desc", count=100)
        matches = result.get("messages", {}).get("matches", [])
        print(f"Found {len(matches)} mention(s) in search results")

        for msg in matches:
            msg_ts = msg.get("ts")
            channel_id = msg.get("channel", {}).get("id")
            channel_name = msg.get("channel", {}).get("name", channel_id)
            msg_text = msg.get("text", "")
            sender = msg.get("user", "unknown")
            permalink = msg.get("permalink", "")

            if not channel_id or not msg_ts:
                continue

            # Check if the message already has a reaction (acknowledged)
            try:
                reactions_result = user_client.reactions_get(channel=channel_id, timestamp=msg_ts)
                message_data = reactions_result.get("message", {})
                has_reactions = len(message_data.get("reactions", [])) > 0
            except Exception as e:
                print(f"Could not check reactions for message in #{channel_name}: {e}")
                has_reactions = False

            if has_reactions:
                print(f"Skipping #{channel_name} - already has reaction")
                continue

            # No reaction — send DM via the bot
            message_text = (
                f"You were mentioned:\n"
                f"Channel: #{channel_name}\n"
                f"From: <@{sender}>\n"
                f"<{msg_link}|:point_right: View Message>"
            )

            try:
                bot_client.chat_postMessage(channel=YOUR_USER_ID, text=message_text)
                mentions_found += 1
                print(f"DM sent for mention in #{channel_name}")
            except Exception as e:
                print(f"Failed to send DM for #{channel_name}: {e}")

    except Exception as e:
        print(f"ERROR: Search failed: {e}")

    if mentions_found == 0:
        print("No unacknowledged mentions to notify about.")
    else:
        print(f"Done. {mentions_found} DM(s) sent.")


if __name__ == "__main__":
    check_mentions()
