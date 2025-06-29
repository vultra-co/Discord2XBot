# /home/vulture/DiscordXBot/VultraMessageCourier/main.py

# --- Imports ---
import discord
import tweepy
import requests # For downloading media AND for internal API calls
import os
import json
import asyncio
import time
import traceback
from io import BytesIO
from threading import Thread, Lock
from collections import deque
from flask import Flask, render_template, jsonify # render_template for dashboard
from functools import partial # For run_in_executor
import logging
from dotenv import load_dotenv

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [D2X] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logging.getLogger('discord.gateway').setLevel(logging.WARNING)
logging.getLogger('discord.client').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# --- Configuration Loading ---
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
X_API_KEY = os.getenv('X_API_KEY')
X_API_SECRET = os.getenv('X_API_SECRET')
X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
X_ACCESS_TOKEN_SECRET = os.getenv('X_ACCESS_TOKEN_SECRET')
X_BEARER_TOKEN = os.getenv('X_BEARER_TOKEN')
TARGET_DISCORD_CHANNEL_ID_STR = os.getenv('TARGET_DISCORD_CHANNEL_ID')
THREADS_BOT_STATUS_URL = os.getenv('THREADS_BOT_STATUS_URL', 'http://localhost:8081/api/status') # Default URL

if not all([DISCORD_BOT_TOKEN, X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_BEARER_TOKEN, TARGET_DISCORD_CHANNEL_ID_STR]):
    logging.critical("[D2X] FATAL ERROR: Missing critical environment variables. Check .env file.")
    exit(1)
try:
    TARGET_DISCORD_CHANNEL_ID = int(TARGET_DISCORD_CHANNEL_ID_STR)
except ValueError:
    logging.critical(f"[D2X] FATAL ERROR: TARGET_DISCORD_CHANNEL_ID ('{TARGET_DISCORD_CHANNEL_ID_STR}') is not valid.")
    exit(1)

STATE_FILE_X = 'discord_to_x_state.json'

# --- Global State Variables & Lock (for X Bot) ---
state_lock_x = Lock() # Specific lock
discord_message_to_x_post_map = {}
posts_attempted_x = 0
posts_succeeded_x = 0
posts_failed_x = 0
discord_status_x = "Initializing"
last_x_api_status = "Unknown"
last_x_api_timestamp = None
recent_activity_x = deque(maxlen=10)

# --- State Persistence Functions (for X Bot) ---
def load_state_x():
    global discord_message_to_x_post_map, posts_attempted_x, posts_succeeded_x, posts_failed_x
    with state_lock_x: # Use specific lock
        try:
            with open(STATE_FILE_X, 'r') as f:
                data = json.load(f)
                discord_message_to_x_post_map = data.get("discord_message_to_x_post_map", {})
                posts_attempted_x = data.get("posts_attempted_x", 0) # Use specific var names
                posts_succeeded_x = data.get("posts_succeeded_x", 0)
                posts_failed_x = data.get("posts_failed_x", 0)
                logging.info(f"[D2X] Loaded X Bot state from {STATE_FILE_X}.")
        except FileNotFoundError:
            logging.warning(f"[D2X] {STATE_FILE_X} not found, starting X Bot with empty state.")
            discord_message_to_x_post_map = {}
            posts_attempted_x = posts_succeeded_x = posts_failed_x = 0
        except Exception as e: # Catch other errors like JSONDecodeError
            logging.error(f"[D2X] Error loading {STATE_FILE_X}: {e}", exc_info=True)
            discord_message_to_x_post_map = {}
            posts_attempted_x = posts_succeeded_x = posts_failed_x = 0
        if not isinstance(discord_message_to_x_post_map, dict):
            discord_message_to_x_post_map = {}


def save_state_x():
    with state_lock_x: # Use specific lock
        try:
            data_to_save = {
                "discord_message_to_x_post_map": dict(discord_message_to_x_post_map),
                "posts_attempted_x": posts_attempted_x,
                "posts_succeeded_x": posts_succeeded_x,
                "posts_failed_x": posts_failed_x
            }
            with open(STATE_FILE_X, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            logging.debug(f"[D2X] Saved X Bot state to {STATE_FILE_X}")
        except Exception as e:
            logging.error(f"[D2X] Error saving {STATE_FILE_X}: {e}", exc_info=True)

def save_mapping_entry_x(discord_msg_id, x_post_id):
    with state_lock_x: # Use specific lock
        discord_message_to_x_post_map[str(discord_msg_id)] = str(x_post_id)
    save_state_x()

def get_x_post_id(discord_msg_id):
    with state_lock_x: # Use specific lock
        return discord_message_to_x_post_map.get(str(discord_msg_id))

def add_activity_x(activity_type, status, details=""):
    with state_lock_x: # Use specific lock
        try:
            log_entry = {
                "timestamp": time.time(), "type": str(activity_type),
                "status": str(status), "details": str(details)
            }
            recent_activity_x.appendleft(log_entry)
        except Exception as e:
            logging.error(f"[D2X] Error adding X Bot activity: {e}", exc_info=True)

# --- X (Twitter) API Setup ---
# (Keep this section as it was in discord_to_x_bot_main_py_with_executor_fix)
api_v1 = None
client_v2 = None
try:
    if X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET and X_BEARER_TOKEN:
        auth_v1 = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth_v1)
        client_v2 = tweepy.Client(
            bearer_token=X_BEARER_TOKEN, consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET, access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET, wait_on_rate_limit=True
        )
        logging.info("[D2X] X API clients initialized successfully.")
    else:
        logging.error("[D2X] One or more X API credentials missing, skipping initialization.")
except Exception as e:
     logging.critical(f"[D2X] FATAL ERROR during X API client initialization: {e}", exc_info=True)
     add_activity_x("System", "❌ Error", f"X Init Failed: {e}")


# --- Helper Function to Post to X (from discord_to_x_bot_main_py_with_executor_fix) ---
# (This is the full post_to_x function that uses run_in_executor)
# (Ensure it updates posts_attempted_x, posts_succeeded_x, posts_failed_x, last_x_api_status, last_x_api_timestamp)
async def post_to_x(text_content, discord_media_attachments=None, reply_to_x_id=None, discord_message_id=None):
    global posts_attempted_x, posts_succeeded_x, posts_failed_x, last_x_api_status, last_x_api_timestamp

    if client_v2 is None or api_v1 is None:
        logging.error("[D2X] X API client(s) not initialized. Cannot post.")
        add_activity_x("X Post", "❌ Failed", f"D ID: {discord_message_id} | Error: X Client Not Initialized")
        return None

    loop = asyncio.get_event_loop()
    current_timestamp = time.time()
    status_detail_info = f"Discord ID: {discord_message_id}" if discord_message_id else "Unknown Discord ID"

    with state_lock_x: posts_attempted_x += 1
    media_ids_v1_to_attach = []

    try:
        media_count, has_video, has_gif = 0, False, False
        if discord_media_attachments:
            logging.info(f"[D2X] Processing {len(discord_media_attachments)} media for Msg {discord_message_id}...")
            for attachment in discord_media_attachments:
                if media_count >= 4:
                    logging.warning(f"[D2X] Max 4 media for X. Skipping for Msg {discord_message_id}.")
                    add_activity_x("Warning", "⚠️ X Media Skipped", f"D ID: {discord_message_id} - Max 4")
                    break
                ct, filename, cat, is_vid, is_gif = attachment.content_type, attachment.filename, None, False, False
                if ct and ct.startswith('video/'):
                    if has_gif: logging.warning(f"[D2X] Mix Video/GIF. Skip video '{filename}'."); continue
                    cat, is_vid = 'tweet_video', True
                elif ct and ct.startswith('image/gif'):
                    if has_video: logging.warning(f"[D2X] Mix Video/GIF. Skip GIF '{filename}'."); continue
                    cat, is_gif = 'tweet_gif', True
                elif ct and ct.startswith('image/'): cat = 'tweet_image'
                else: logging.warning(f"[D2X] Unsupported type '{ct}' for '{filename}'. Skip."); continue
                
                logging.info(f"[D2X] Downloading: {filename}")
                media_data = None
                try:
                    response = await loop.run_in_executor(None, partial(requests.get, attachment.url, stream=True, timeout=90))
                    response.raise_for_status(); media_data = BytesIO()
                    for chunk in response.iter_content(8192): media_data.write(chunk)
                    media_data.seek(0); logging.info(f"[D2X] Downloaded {filename}.")
                except Exception as e: logging.error(f"[D2X] Download error {filename}: {e}"); add_activity_x("Warning", "⚠️ X Media Failed", f"D ID: {discord_message_id} - Download fail '{filename}'"); continue 
                
                if media_data:
                    logging.info(f"[D2X] Uploading {filename} to X as '{cat}'...")
                    try:
                        up_media = await loop.run_in_executor(None,partial(api_v1.media_upload,filename=filename,file=media_data,media_category=cat,wait_for_async_finalize=True))
                        media_ids_v1_to_attach.append(up_media.media_id_string)
                        logging.info(f"[D2X] Uploaded {filename}, X Media ID: {up_media.media_id_string}")
                        media_count += 1; has_video = has_video or is_vid; has_gif = has_gif or is_gif
                    except Exception as e: logging.error(f"[D2X] Upload error for {filename}: {e}"); add_activity_x("Warning", "⚠️ X Media Failed", f"D ID: {discord_message_id} - Upload fail '{filename}'")
        
        if not text_content and not media_ids_v1_to_attach:
            logging.warning(f"[D2X] No text/media for Msg {discord_message_id}. No post.")
            with state_lock_x: posts_attempted_x -=1 
            return None

        post_params = {"text": text_content}
        if media_ids_v1_to_attach: post_params["media_ids"] = media_ids_v1_to_attach
        if reply_to_x_id: post_params["in_reply_to_tweet_id"] = reply_to_x_id
        
        logging.info(f"[D2X] Sending create_tweet request for Msg {discord_message_id}...")
        resp_data_v2 = await loop.run_in_executor(None, partial(client_v2.create_tweet, **post_params))
        
        new_id = resp_data_v2.data['id']; status_detail_info += f" -> X ID: {new_id}"
        with state_lock_x: posts_succeeded_x += 1; last_x_api_status = "✅ Success"; last_x_api_timestamp = current_timestamp
        add_activity_x("X Post", "✅ Success", status_detail_info); save_state_x()
        logging.info(f"[D2X] Posted to X. ID: {new_id}")
        return new_id
    except Exception as e:
        err_type, err_msg = type(e).__name__, repr(e).replace('\n', ' ')
        logging.error(f"[D2X] Error in post_to_x for Msg {discord_message_id}: {err_type}: {err_msg}", exc_info=True)
        short_err = f"{err_type}: {err_msg[:70]}..."
        if "Error:" not in status_detail_info: status_detail_info += f" | Error: {short_err}"
        with state_lock_x: posts_failed_x += 1; last_x_api_status = f"❌ Failed ({short_err})"; last_x_api_timestamp = current_timestamp
        add_activity_x("X Post", "❌ Failed", status_detail_info); save_state_x()
        return None

# --- Flask Setup (for Combined Dashboard) ---
app_x = Flask(__name__) # Use a distinct app name if running multiple Flask apps in one process (not recommended here)
                        # For separate processes, 'app' is fine. Let's use app_x for clarity.
X_BOT_API_PORT = int(os.getenv('X_BOT_API_PORT', '8080')) # Port for this bot's dashboard

@app_x.route('/')
def route_home_combined():
    return "Combined Bot Dashboard Server is alive!"

@app_x.route('/dashboard') # Main dashboard URL
def route_dashboard_combined():
    return render_template('dashboard.html') # Assumes dashboard.html is in ./templates/

@app_x.route('/api/status/combined') # Main API status URL
def route_api_status_combined():
    combined_status = {}
    # --- Get X Bot Status (This Bot's Status) ---
    with state_lock_x:
        x_bot_data = {
            "bot_name": "Discord-to-X Bot",
            "discord_status": discord_status_x, # Ensure this is the X bot's Discord status
            "last_target_api_status": last_x_api_status,
            "last_target_api_timestamp": last_x_api_timestamp,
            "posts_attempted": posts_attempted_x,
            "posts_succeeded": posts_succeeded_x,
            "posts_failed": posts_failed_x,
            "recent_activity": list(recent_activity_x),
            "monitoring_channel": TARGET_DISCORD_CHANNEL_ID
        }
        current_x_bot_status = "Running ✅"
        if client_v2 is None or api_v1 is None: current_x_bot_status = "X Client Init Failed ❌"
        elif "❌ Failed" in last_x_api_status: current_x_bot_status = "Running with X API Errors ⚠️"
        if "Disconnected" in discord_status_x: current_x_bot_status = "Discord Disconnected ❌"
        elif "Connecting" in discord_status_x: current_x_bot_status = "Discord Connecting ⚠️"
        x_bot_data["bot_status"] = current_x_bot_status
    combined_status['x_bot'] = x_bot_data

    # --- Get Threads Bot Status (via HTTP request to its API) ---
    threads_bot_data = {"bot_name": "Discord-to-Threads Bot", "fetch_status": "Not Attempted", "bot_status": "Unknown ❓"}
    try:
        logging.info(f"[D2X] Fetching status from Threads Bot API: {THREADS_BOT_STATUS_URL}")
        response = requests.get(THREADS_BOT_STATUS_URL, timeout=3) 
        response.raise_for_status()
        fetched_tb_data = response.json()
        # Ensure all expected keys are present or provide defaults
        threads_bot_data.update({
            "discord_status": fetched_tb_data.get("discord_status", "Unknown"),
            "last_target_api_status": fetched_tb_data.get("last_target_api_status", "Unknown"),
            "last_target_api_timestamp": fetched_tb_data.get("last_target_api_timestamp"),
            "posts_attempted": fetched_tb_data.get("posts_attempted", 0),
            "posts_succeeded": fetched_tb_data.get("posts_succeeded", 0),
            "posts_failed": fetched_tb_data.get("posts_failed", 0),
            "recent_activity": fetched_tb_data.get("recent_activity", []),
            "monitoring_channel": fetched_tb_data.get("monitoring_channel", "N/A"),
            "bot_status": fetched_tb_data.get("bot_status", "Unknown ❓") # Get bot_status from Threads bot API
        })
        threads_bot_data["fetch_status"] = "Success ✅"
        logging.info(f"[D2X] Successfully fetched Threads Bot status.")
    except requests.exceptions.Timeout:
        logging.warning(f"[D2X] Timeout fetching Threads bot status from {THREADS_BOT_STATUS_URL}")
        threads_bot_data["fetch_status"] = "Error: Timeout ❌"
    except requests.exceptions.ConnectionError:
        logging.warning(f"[D2X] Connection error fetching Threads bot status from {THREADS_BOT_STATUS_URL}")
        threads_bot_data["fetch_status"] = "Error: Connection Failed ❌"
    except requests.exceptions.RequestException as e:
        logging.error(f"[D2X] Error fetching Threads bot status: {e}")
        threads_bot_data["fetch_status"] = f"Error: Request Failed ❌"
    except Exception as e:
        logging.error(f"[D2X] Unexpected error parsing Threads bot status: {e}", exc_info=True)
        threads_bot_data["fetch_status"] = "Error: Parsing Failed ❌"
    combined_status['threads_bot'] = threads_bot_data
    
    combined_status["current_time_unix"] = time.time()
    return jsonify(combined_status)

def run_combined_flask_server():
    try:
        logging.info(f"[D2X] Starting Combined Dashboard Flask server thread on 0.0.0.0:{X_BOT_API_PORT}")
        app_x.run(host='0.0.0.0', port=X_BOT_API_PORT, debug=False, use_reloader=False)
    except Exception as e:
        logging.critical(f"[D2X] FATAL ERROR: Combined Dashboard Flask server thread failed: {e}", exc_info=True)

# --- Discord Bot Setup (for X Bot) ---
intents_x = discord.Intents.default()
intents_x.message_content = True
client_x = discord.Client(intents=intents_x) # Distinct client

# --- Discord Event Handlers (for X Bot) ---
@client_x.event
async def on_ready():
    global discord_status_x
    load_state_x()
    with state_lock_x: discord_status_x = "Connected"
    add_activity_x("Discord", "✅ Connected", f"Logged in as {client_x.user.name}")
    logging.info(f'[D2X] Logged in as {client_x.user.name} ({client_x.user.id})')
    logging.info(f'[D2X] Monitoring Channel ID: {TARGET_DISCORD_CHANNEL_ID}')
    logging.info('[D2X] ------ X Bot Ready ------')

@client_x.event
async def on_disconnect():
    global discord_status_x
    with state_lock_x: discord_status_x = "Disconnected"
    add_activity_x("Discord", "❌ Disconnected", "Lost gateway connection")
    logging.warning('[D2X] X Bot disconnected from Discord.')

@client_x.event
async def on_connect():
    global discord_status_x
    with state_lock_x: discord_status_x = "Connecting"
    logging.info('[D2X] X Bot connecting to Discord gateway...')

@client_x.event
async def on_resumed():
    global discord_status_x
    with state_lock_x: discord_status_x = "Connected (Resumed)"
    add_activity_x("Discord", "✅ Resumed", "Session resumed")
    logging.info('[D2X] X Bot session resumed.')

@client_x.event
async def on_message(message: discord.Message):
    if message.author == client_x.user: return
    if message.type == discord.MessageType.thread_created:
        logging.info(f"[D2X] Ignoring message {message.id}: Type is thread_created.")
        return

    is_in_target_channel = message.channel.id == TARGET_DISCORD_CHANNEL_ID
    is_in_target_thread = isinstance(message.channel, discord.Thread) and message.channel.parent_id == TARGET_DISCORD_CHANNEL_ID
    if not (is_in_target_channel or is_in_target_thread): return

    logging.info(f"[D2X] Relevant message: ID {message.id} | Author: {message.author.name} | In Thread: {is_in_target_thread}")
    post_as_reply_to_x, is_initial_message = None, False
    if is_in_target_thread:
        original_discord_message_id = message.channel.id
        original_x_post_id = get_x_post_id(original_discord_message_id)
        if original_x_post_id:
            post_as_reply_to_x = original_x_post_id
        else:
            logging.warning(f"[D2X] Original X Post ID for thread {original_discord_message_id} not found. Ignoring reply {message.id}.")
            add_activity_x("Warning", "⚠️ X Ignored Reply", f"D ID: {message.id} - Original X map missing")
            return
    elif is_in_target_channel: is_initial_message = True

    text_content = message.content if message.content else ""
    attachments = message.attachments
    has_text = bool(text_content)
    if not has_text and message.embeds:
        if message.embeds[0].description: text_content, has_text = message.embeds[0].description, True
        elif not attachments: logging.info(f"[D2X] Msg {message.id} no actionable content, skip."); return
    if not has_text and not attachments: logging.info(f"[D2X] Msg {message.id} empty, skip."); return

    new_x_post_id = await post_to_x(
        text_content=text_content, discord_media_attachments=attachments,
        reply_to_x_id=post_as_reply_to_x, discord_message_id=message.id
    )
    if is_initial_message and new_x_post_id:
        save_mapping_entry_x(message.id, new_x_post_id) # Use _x suffixed function
        logging.info(f"[D2X] Saved mapping Discord {message.id} -> X {new_x_post_id}")
    elif not new_x_post_id:
        logging.error(f"[D2X] Failed to send Discord message {message.id} to X.")
    logging.info(f"[D2X] --- Message Handling Complete for Msg ID: {message.id} ---")

# --- Main Execution Block ---
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_BEARER_TOKEN]) or not TARGET_DISCORD_CHANNEL_ID:
        logging.critical("[D2X] FATAL ERROR: Missing critical X Bot configuration variables.")
        exit(1)

    load_state_x() # Load state for X bot

    try:
        logging.info("[D2X] Starting Combined Dashboard Flask server in background thread...")
        flask_thread_combined = Thread(target=run_combined_flask_server, name="CombinedDashboardFlaskThread")
        flask_thread_combined.daemon = True
        flask_thread_combined.start()
    except Exception as e:
        logging.critical(f"[D2X] Failed to start Combined Dashboard Flask thread: {e}", exc_info=True)
        # exit(1) 

    time.sleep(1)

    try:
        logging.info("[D2X] Starting Discord client for X Bot...")
        if client_x:
            client_x.run(DISCORD_BOT_TOKEN, log_handler=None)
        else:
            logging.critical("[D2X] Discord client_x object not created. Cannot run.")
    except discord.errors.LoginFailure:
        logging.critical("[D2X] FATAL ERROR: Invalid Discord Bot Token for X Bot.")
    except discord.errors.PrivilegedIntentsRequired:
        logging.critical("[D2X] FATAL ERROR: Privileged Intents not enabled for X Bot.")
    except Exception as e:
        logging.critical(f"[D2X] FATAL ERROR during X Bot Discord client execution: {e}", exc_info=True)

    logging.info("[D2X] X Bot Discord client has stopped. Shutting down.")
    add_activity_x("System", "⏹️ Stopped", "X Bot: Discord client has stopped.")


