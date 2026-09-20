import os
import json
from slack_sdk import WebClient
from datetime import datetime, timedelta

# Read from GitHub Secrets
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")
CHANNELS_STR = os.environ.get("CHANNELS_TO_MONITOR", "")
CHANNELS_TO_MONITOR = [ch.strip() for ch in CHANNELS_STR.split(",") if ch.strip()]

# Validate secrets
if not SLACK_BOT_TOKEN or not YOUR_USER_ID or not CHANNELS_TO_MONITOR:
    print("❌ ERROR: Missing required secrets!")
    print(f"   SLACK_BOT_TOKEN: {'set' if SLACK_BOT_TOKEN else 'MISSING'}")
    print(f"   YOUR_USER_ID: {'set' if YOUR_USER_ID else 'MISSING'}")
    print(f"   CHANNELS_TO_MONITOR: {'set' if CHANNELS_TO_MONITOR else 'MISSING'}")
    exit(1)

client = WebClient(token=SLACK_BOT_TOKEN)
try:
    auth = client.auth_test()
    print(f"🔑 Token OK — bot user: {auth.get('user')} | scopes: {auth.headers.get('x-oauth-scopes')}")
except Exception as e:
    print(f"❌ Token check failed: {e}")

# File to track already-notified messages
SENT_LOG_FILE = "sent_messages.json"

def load_sent_messages():
    if os.path.exists(SENT_LOG_FILE):
        with open(SENT_LOG_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_sent_messages(sent):
    with open(SENT_LOG_FILE, "w") as f:
        json.dump(list(sent), f)

def check_mentions():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔍 Scanning last 24h for unacknowledged mentions...")

    sent_messages = load_sent_messages()
    since = int((datetime.now() - timedelta(hours=24)).timestamp())
    mentions_found = 0

    for channel_id in CHANNELS_TO_MONITOR:
        try:
            result = client.conversations_history(
                channel=channel_id,
                oldest=since
            )

            for msg in result.get("messages", []):
                msg_text = msg.get("text", "")

                if f"<@{YOUR_USER_ID}>" in msg_text:
                    msg_id = msg.get("ts")

                    # Skip if already notified
                    if msg_id in sent_messages:
                        print(f"⏭ Already notified for message {msg_id}, skipping.")
                        continue

                    # Check if message has reactions
                    has_reactions = "reactions" in msg and len(msg.get("reactions", [])) > 0

                    if not has_reactions:
                        msg_link = f"https://slack.com/archives/{channel_id}/p{msg_id.replace('.', '')}"

                        try:
                            channel_info = client.conversations_info(channel=channel_id)
                            channel_name = channel_info["channel"]["name"]
                        except:
                            channel_name = channel_id

                        user_id = msg.get("user", "unknown")

                        message_text = (
    f"🔔 *Unacknowledged Mention*\n"
    f"Channel: #{channel_name}\n"
    f"From: <@{user_id}>\n"
    f"Message: {msg_text[:150]}\n"
    f"<{msg_link}|👉 View Message>"
)

client.chat_postMessage(
    channel=YOUR_USER_ID,   # DM the user directly — no conversations.open needed
    text=message_text
)

                        sent_messages.add(msg_id)
                        mentions_found += 1
                        print(f"✅ DM sent for mention in #{channel_name}")

                    else:
                        # Mark as seen so we don't check it again
                        sent_messages.add(msg_id)
                        print(f"⏭ Mention in {channel_id} already has reactions, skipping.")

        except Exception as e:
            print(f"❌ Error checking {channel_id}: {str(e)}")

    save_sent_messages(sent_messages)

    if mentions_found == 0:
        print("✓ No unacknowledged mentions found.")
    else:
        print(f"✓ Done. {mentions_found} DM(s) sent.")

if __name__ == "__main__":
    check_mentions()
