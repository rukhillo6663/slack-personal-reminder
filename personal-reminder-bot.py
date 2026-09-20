import os
import time
import schedule
from slack_sdk import WebClient
from datetime import datetime, timedelta

# Get credentials from GitHub secrets (we'll set these next)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
YOUR_USER_ID = os.environ.get("YOUR_USER_ID")
CHANNELS_STR = os.environ.get("CHANNELS_TO_MONITOR", "")
CHANNELS_TO_MONITOR = [ch.strip() for ch in CHANNELS_STR.split(",") if ch.strip()]

# Track sent messages to avoid duplicates
SENT_MESSAGES = set()

client = WebClient(token=SLACK_BOT_TOKEN)

def check_mentions():
    """Check for mentions without reactions in the last hour"""
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔍 Scanning for unacknowledged mentions...")
    
    one_hour_ago = int((datetime.now() - timedelta(hours=1)).timestamp())
    mentions_found = 0
    
    for channel_id in CHANNELS_TO_MONITOR:
        if not channel_id:
            continue
            
        try:
            # Get messages from the channel in the last hour
            result = client.conversations_history(
                channel=channel_id,
                oldest=one_hour_ago
            )
            
            for msg in result.get("messages", []):
                msg_text = msg.get("text", "")
                
                # Check if you're mentioned
                if f"<@{YOUR_USER_ID}>" in msg_text:
                    msg_id = msg.get("ts")
                    
                    # Skip if already processed
                    if msg_id in SENT_MESSAGES:
                        continue
                    
                    # Check if message has reactions
                    has_reactions = "reactions" in msg and len(msg.get("reactions", [])) > 0
                    
                    if not has_reactions:
                        # Build message link
                        msg_link = f"https://slack.com/archives/{channel_id}/p{msg_id.replace('.', '')}"
                        
                        # Get channel name
                        channel_info = client.conversations_info(channel=channel_id)
                        channel_name = channel_info["channel"]["name"]
                        
                        # Get sender info
                        user_id = msg.get("user", "unknown")
                        
                        # Send DM
                        dm_result = client.conversations_open(users=YOUR_USER_ID)
                        
                        message_text = (
                            f"🔔 *Unacknowledged Mention*\n"
                            f"Channel: #{channel_name}\n"
                            f"From: <@{user_id}>\n"
                            f"Message: {msg_text[:150]}...\n"
                            f"<{msg_link}|View Message>"
                        )
                        
                        client.chat_postMessage(
                            channel=dm_result["channel"]["id"],
                            text=message_text
                        )
                        
                        SENT_MESSAGES.add(msg_id)
                        mentions_found += 1
                        print(f"✅ DM sent for mention in #{channel_name}")
        
        except Exception as e:
            print(f"❌ Error checking {channel_id}: {str(e)}")
    
    if mentions_found == 0:
        print("✓ No unacknowledged mentions found.")
    else:
        print(f"✓ Scan complete. {mentions_found} DM(s) sent.")

# Run the check once when executed
if __name__ == "__main__":
    check_mentions()
