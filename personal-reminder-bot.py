import os
import json
from slack_sdk import WebClient
from datetime import datetime, timedelta

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")
CHANNELS_STR = os.environ.get("CHANNELS_TO_MONITOR", "")
CHANNELS_TO_MONITOR = [ch.strip() for ch in CHANNELS_STR.split(",") if ch.strip()]

if not SLACK_BOT_TOKEN or not YOUR_USER_ID or not CHANNELS_TO_MONITOR:
    print("❌ ERROR: Missing required secrets!")
    exit(1)

client = WebClient(token=SLACK_BOT_TOKEN)

try:
    auth = client.auth_test()
    print(f"🔑 Token OK — bot user: {auth.get('user')} | scopes: {auth.headers.get('x-oauth-scopes')}")
except Exception as e:
    print(f"❌ Token check failed: {e}")
    exit(1)

SENT_LOG_FILE = "sent_messages.json"

def load_sent_messages():
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_sent_messages(sent):
    with open(SENT_LOG_FILE, "w") as f:
        json.dump(list(sent), f)

def check_mentions():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔍 Scanning last 24h for unacknowledged mentions...")

    sent_messages = load_sent_messages()
    since = int((datetime.now() - timedelta(hours=2)).timestamp())
    mentions_found = 0

    for channel_id in CHANNELS_TO_MONITOR:
        try:
            result = client.conversations_history(channel=channel_id, oldest=since)

            for msg in result.get("messages", []):
                msg_text = msg.get("text", "")

                if f"<@{YOUR_USER_ID}>" not in msg_text:
                    continue

                msg_id = msg.get("ts")

                if msg_id in sent_messages:
                    continue

                has_reactions = len(msg.get("reactions", [])) > 0

                if has_reactions:
                    sent_messages.add(msg_id)
                    continue

                msg_link = f"https://slack.com/archives/{channel_id}/p{msg_id.replace('.', '')}"

                try:
                    channel_info = client.conversations_info(channel=channel_id)
                    channel_name = channel_info["channel"]["name"]
                except Exception:
                    channel_name = channel_id

                sender = msg.get("user", "unknown")

                message_text = (

                    f"You were mentioned:\n"
                    f"Channel: #{channel_name}\n"
                    f"From: <@{sender}>\n"
                    
                    f"<{msg_link}|👉 View Message>"
                )

                # DM the user directly — needs only chat:write, no conversations.open
                client.chat_postMessage(channel=YOUR_USER_ID, text=message_text)

                sent_messages.add(msg_id)
                mentions_found += 1
                print(f"✅ DM sent for mention in #{channel_name}")

        except Exception as e:
            print(f"❌ Error checking {channel_id}: {str(e)}")

    save_sent_messages(sent_messages)

    if mentions_found == 0:
        print("✓ No unacknowledged mentions found.")
    else:
        print(f"✓ Done. {mentions_found} DM(s) sent.")

if __name__ == "__main__":
    check_mentions()
