#!/usr/bin/env python3
import os
import logging
import random
import string
import time
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import UserNotParticipant, RPCError, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient, errors as pymongo_errors
from flask import Flask
from threading import Thread
from typing import Tuple, List, Optional

# --- Flask Web Server (keeps host alive) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)
# --- Web Server end ---

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL_RAW = os.environ.get("LOG_CHANNEL", "")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "")  # legacy single channel support

# FORCE_CHANNELS: comma separated list of channel usernames or IDs (max 4).
FORCE_CHANNELS_RAW = os.environ.get("FORCE_CHANNELS", "")
FORCE_CHANNELS = [ch.strip().lstrip('@') for ch in FORCE_CHANNELS_RAW.split(",") if ch.strip()][:4]

# If UPDATE_CHANNEL is set but FORCE_CHANNELS is empty, include it for backward compatibility
if UPDATE_CHANNEL and not FORCE_CHANNELS:
    FORCE_CHANNELS = [UPDATE_CHANNEL.lstrip('@')]

# convert LOG_CHANNEL to int if it's a number, else as @username
def parse_chat_identifier(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return f"@{raw.lstrip('@')}"

LOG_CHANNEL = parse_chat_identifier(LOG_CHANNEL_RAW)

# Admin configuration
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI) if MONGO_URI else None
    db = client['file_link_bot'] if client else None
    files_collection = db['files'] if db else None
    settings_collection = db['settings'] if db else None
    uploads_collection = db['uploads'] if db else None  # track uploader info
    if files_collection:
        # ensure unique index on file_unique_id to prevent duplicates
        try:
            files_collection.create_index("file_unique_id", unique=True)
            files_collection.create_index("_id", unique=True)
        except Exception as e:
            logger.warning(f"Could not create indexes: {e}")
    logger.info("MongoDB Connected Successfully!" if client else "MongoDB not configured, running without DB.")
except Exception as e:
    logger.error(f"Error connecting to MongoDB: {e}")
    # don't exit outright: bot can run in no-db mode, but warn
    client = None
    db = None
    files_collection = None
    settings_collection = None
    uploads_collection = None

# --- Pyrogram Client ---
app = Client("FileLinkBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Helper Functions ---

def generate_random_string(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

# membership cache: (user_id, channel_identifier) -> expiry_timestamp
MEMBERSHIP_CACHE = {}
MEMBERSHIP_TTL = int(os.environ.get("MEMBERSHIP_TTL_SECONDS", "300"))  # default 5 minutes

def _cache_get(user_id: int, ch: str) -> Optional[bool]:
    key = (user_id, ch)
    entry = MEMBERSHIP_CACHE.get(key)
    if not entry:
        return None
    value, expiry = entry
    if time.time() > expiry:
        try:
            del MEMBERSHIP_CACHE[key]
        except KeyError:
            pass
        return None
    return value

def _cache_set(user_id: int, ch: str, value: bool):
    key = (user_id, ch)
    MEMBERSHIP_CACHE[key] = (value, time.time() + MEMBERSHIP_TTL)

async def _safe_get_chat_member(client: Client, chat_id, user_id: int) -> Tuple[bool, Optional[Exception]]:
    """
    Tries to check membership and returns (is_member, exception_if_any)
    """
    try:
        await client.get_chat_member(chat_id=chat_id, user_id=user_id)
        return True, None
    except UserNotParticipant as e:
        return False, e
    except PeerIdInvalid as e:
        # channel identifier invalid
        return False, e
    except RPCError as e:
        return False, e
    except Exception as e:
        return False, e

async def is_user_member(client: Client, user_id: int) -> Tuple[bool, List[str]]:
    """
    Check whether a user is a member of all FORCE_CHANNELS.
    Returns: (is_member_all: bool, missing_channels: list[str])
    Uses local TTL cache to reduce API calls.
    """
    missing = []
    if not FORCE_CHANNELS:
        # No force-subscribe configured
        return True, []

    for ch in FORCE_CHANNELS:
        # prepare chat identifier
        try:
            chat_id = int(ch) if ch.lstrip('-').isdigit() else f"@{ch}"
        except Exception:
            chat_id = f"@{ch}"

        # check cache first
        cached = _cache_get(user_id, str(chat_id))
        if cached is not None:
            if not cached:
                missing.append(ch)
            continue

        is_member, exc = await _safe_get_chat_member(client, chat_id, user_id)
        # set cache accordingly (True or False)
        _cache_set(user_id, str(chat_id), bool(is_member))
        if not is_member:
            # treat RPC errors as "missing" to force re-join attempt, but log them
            missing.append(ch)
            if exc and not isinstance(exc, UserNotParticipant):
                logger.warning(f"Membership check warning for user {user_id} in {chat_id}: {exc}")

    return (len(missing) == 0), missing

async def get_bot_mode() -> str:
    if not settings_collection:
        return "public"
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

def parse_media_file_unique_id(message: Message) -> Optional[str]:
    """
    Extract a reliable file_unique_id for media types (document, photo, audio, video, etc).
    For photos, choose the largest size's file_unique_id (pyrogram handles .photo[-1]).
    """
    try:
        if message.document:
            return message.document.file_unique_id
        if message.video:
            return message.video.file_unique_id
        if message.audio:
            return message.audio.file_unique_id
        if message.photo:
            # list of PhotoSize; take the last/largest
            return message.photo[-1].file_unique_id
        if message.voice:
            return message.voice.file_unique_id
        if message.sticker:
            return message.sticker.file_unique_id
    except Exception:
        return None
    return None

def _make_share_link(bot_username: str, file_id_str: str) -> str:
    if bot_username:
        return f"https://t.me/{bot_username}?start={file_id_str}"
    return f"/start {file_id_str}"

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    # If user clicked a start payload with a file id
    if len(message.command) > 1:
        file_id_str = message.command[1]

        is_member_all, missing = await is_user_member(client, user.id)
        if not is_member_all:
            # build keyboard with join links for missing channels
            buttons = []
            for ch in missing:
                btn_url = f"https://t.me/{ch}" if not ch.lstrip('-').isdigit() else None
                if btn_url:
                    buttons.append([InlineKeyboardButton(f"🔗 Join {ch}", url=btn_url)])
                else:
                    join_url = f"https://t.me/{UPDATE_CHANNEL}" if UPDATE_CHANNEL else "https://t.me/"
                    buttons.append([InlineKeyboardButton("🔗 Join Channel", url=join_url)])
            buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])
            keyboard = InlineKeyboardMarkup(buttons)

            await message.reply(
                f"👋 Hello, {user.first_name}!\n\nIs file ko access karne ke liye aapko in channels ko join karna hoga:\n\n" +
                ("\n".join(f"- {m}" for m in missing)) +
                "\n\nPlease join and then press the button below.",
                reply_markup=keyboard
            )
            return

        file_record = files_collection.find_one({"_id": file_id_str}) if files_collection else None
        if file_record:
            try:
                # Use copy_message instead of forward to preserve file
                await client.copy_message(chat_id=user.id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
            except Exception as e:
                logger.exception("Failed copying file to user")
                await message.reply(f"❌ Sorry, file bhejte waqt ek error aa gaya.\n`Error: {e}`")
        else:
            await message.reply("🤔 File not found! Ho sakta hai link galat ya expire ho gaya ho.")
    else:
        await message.reply("**Hello! Mai ek File-to-Link bot hu.**\n\nMujhe koi bhi file bhejo, aur mai aapko uska ek shareable link dunga.")

# Accept uploads in private and in groups (so admins can upload from groups)
@app.on_message((filters.private | filters.group) & (filters.document | filters.video | filters.photo | filters.audio | filters.voice | filters.sticker))
async def file_handler(client: Client, message: Message):
    user = message.from_user
    if not user:
        return

    bot_mode = await get_bot_mode()
    if bot_mode == "private" and user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Abhi sirf Admins hi files upload kar sakte hain.")
        return

    # If force-subscribe is configured, ensure user is member of required channels before allowing upload
    is_member_all, missing = await is_user_member(client, user.id)
    if not is_member_all:
        buttons = []
        for ch in missing:
            btn_url = f"https://t.me/{ch}" if not ch.lstrip('-').isdigit() else None
            if btn_url:
                buttons.append([InlineKeyboardButton(f"🔗 Join {ch}", url=btn_url)])
            else:
                join_url = f"https://t.me/{UPDATE_CHANNEL}" if UPDATE_CHANNEL else "https://t.me/"
                buttons.append([InlineKeyboardButton("🔗 Join Channel", url=join_url)])
        await message.reply(
            "🔒 Aapko pehle in channels ko join karna padega before uploading:\n" +
            ("\n".join(f"- {m}" for m in missing)),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    status_msg = await message.reply("⏳ Please wait, file upload kar raha hu...", quote=True)

    try:
        if not LOG_CHANNEL:
            raise ValueError("LOG_CHANNEL is not configured. Set LOG_CHANNEL environment variable to a channel or chat id where files will be stored.")

        file_unique_id = parse_media_file_unique_id(message)
        if not file_unique_id:
            logger.warning("Could not determine file_unique_id for incoming media; proceeding without duplicate check.")
        else:
            # check duplicates
            if files_collection:
                existing = files_collection.find_one({"file_unique_id": file_unique_id})
                if existing:
                    # found duplicate: return existing share link
                    bot_username = (await client.get_me()).username or ""
                    share_link = _make_share_link(bot_username, existing['_id'])
                    await status_msg.edit_text(
                        f"✅ **File already uploaded before!**\n\n🔗 Existing Link: `{share_link}`",
                        disable_web_page_preview=True
                    )
                    # Update uploads_collection with this uploader
                    try:
                        if uploads_collection:
                            uploads_collection.update_one(
                                {"file_id": existing['_id']},
                                {"$addToSet": {"uploaders": user.id}},
                                upsert=True
                            )
                    except Exception as e:
                        logger.debug(f"Failed to update uploads_collection for duplicate upload: {e}")
                    return

        # Forward/copy to LOG_CHANNEL and store metadata
        forwarded_message = await message.forward(LOG_CHANNEL)

        # create a unique random short id; ensure no collision (try a few times)
        file_id_str = generate_random_string()
        attempts = 0
        while files_collection and files_collection.find_one({"_id": file_id_str}):
            file_id_str = generate_random_string()
            attempts += 1
            if attempts > 8:
                file_id_str = generate_random_string(10)
                break

        record = {
            "_id": file_id_str,
            "message_id": forwarded_message.message_id if hasattr(forwarded_message, "message_id") else forwarded_message.id,
            "date": int(time.time()),
            "uploader": user.id,
        }
        if file_unique_id:
            record["file_unique_id"] = file_unique_id

        # optional metadata
        try:
            if message.document:
                record["file_name"] = message.document.file_name
                record["file_size"] = message.document.file_size
                record["mime_type"] = message.document.mime_type
            if message.video:
                record["file_name"] = getattr(message.video, "file_name", record.get("file_name"))
                record["file_size"] = message.video.file_size
            if message.audio:
                record["file_name"] = getattr(message.audio, "file_name", record.get("file_name"))
                record["file_size"] = message.audio.file_size
            # photos and others intentionally lighter
        except Exception:
            pass

        if files_collection:
            try:
                files_collection.insert_one(record)
            except pymongo_errors.DuplicateKeyError:
                # rare race: another process inserted same file_unique_id; query and return that
                existing = files_collection.find_one({"file_unique_id": file_unique_id}) if file_unique_id else None
                if existing:
                    bot_username = (await client.get_me()).username or ""
                    share_link = _make_share_link(bot_username, existing['_id'])
                    await status_msg.edit_text(
                        f"✅ **File already uploaded before!**\n\n🔗 Existing Link: `{share_link}`",
                        disable_web_page_preview=True
                    )
                    return
                else:
                    raise
            except Exception as e:
                logger.exception("Failed to insert file record to DB")

        # record uploader info separately (to compute distinct uploaders later)
        try:
            if uploads_collection:
                uploads_collection.update_one(
                    {"file_id": file_id_str},
                    {"$set": {"first_uploader": user.id, "first_upload_ts": int(time.time())}, "$addToSet": {"uploaders": user.id}},
                    upsert=True
                )
        except Exception as e:
            logger.debug(f"Could not update uploads_collection: {e}")

        bot_username = (await client.get_me()).username or ""
        share_link = _make_share_link(bot_username, file_id_str)
        await status_msg.edit_text(
            f"✅ **Link Generated Successfully!**\n\n🔗 Your Link: `{share_link}`",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.exception("File handling error")
        try:
            await status_msg.edit_text(f"❌ **Error!**\n\nKuch galat ho gaya. Please try again.\n`Details: {e}`")
        except Exception:
            logger.error("Failed to edit status message after error.")

@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas is command ko use karne ki permission nahi hai.")
        return

    current_mode = await get_bot_mode()

    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])

    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"Abhi bot ka file upload mode **{current_mode.upper()}** hai.\n\n"
        f"**Public:** Koi bhi file bhej kar link bana sakta hai.\n"
        f"**Private:** Sirf admins hi file bhej sakte hain.\n\n"
        f"Naya mode select karein:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("Permission Denied!", show_alert=True)
        return

    new_mode = callback_query.data.split("_")[2]

    if settings_collection:
        settings_collection.update_one(
            {"_id": "bot_mode"},
            {"$set": {"mode": new_mode}},
            upsert=True
        )

    await callback_query.answer(f"Mode successfully {new_mode.upper()} par set ho gaya hai!", show_alert=True)

    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])

    try:
        await callback_query.message.edit_text(
            f"⚙️ **Bot Settings**\n\n"
            f"✅ Bot ka file upload mode ab **{new_mode.upper()}** hai.\n\n"
            f"Naya mode select karein:",
            reply_markup=keyboard
        )
    except Exception:
        logger.debug("Could not edit settings message (maybe deleted).")

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    # file_id_str may contain underscores, so only split first 2 parts
    try:
        file_id_str = callback_query.data.split("_", 2)[2]
    except Exception:
        await callback_query.answer("Invalid data.", show_alert=True)
        return

    is_member_all, missing = await is_user_member(client, user_id)
    if is_member_all:
        await callback_query.answer("Thanks for joining! File bhej raha hu...", show_alert=True)
        file_record = files_collection.find_one({"_id": file_id_str}) if files_collection else None
        if file_record:
            try:
                await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                try:
                    await callback_query.message.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.exception("Failed copying file after join")
                await callback_query.message.edit_text(f"❌ File bhejte waqt error aa gaya.\n`Error: {e}`")
        else:
            await callback_query.message.edit_text("🤔 File not found!")
    else:
        msg = "Aapne abhi tak join nahi kiya hai. Please join these channels and try again:\n\n"
        msg += "\n".join(f"- {m}" for m in missing)
        buttons = []
        for ch in missing:
            btn_url = f"https://t.me/{ch}" if not ch.lstrip('-').isdigit() else None
            if btn_url:
                buttons.append([InlineKeyboardButton(f"🔗 Join {ch}", url=btn_url)])
            else:
                join_url = f"https://t.me/{UPDATE_CHANNEL}" if UPDATE_CHANNEL else "https://t.me/"
                buttons.append([InlineKeyboardButton("🔗 Join Channel", url=join_url)])
        buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])
        await callback_query.answer("Aapne abhi tak channel join nahi kiya hai.", show_alert=True)
        try:
            await callback_query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            logger.error("Failed to update callback message when user hasn't joined required channels.")

# --- Stats command (Admin only) ---
@app.on_message(filters.command("stats") & filters.private)
async def stats_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ Aapke paas is command ko use karne ki permission nahi hai.")
        return

    try:
        total_files = files_collection.count_documents({}) if files_collection else 0
        if uploads_collection:
            # count distinct uploaders across uploads_collection
            distinct_uploaders = uploads_collection.aggregate([
                {"$unwind": "$uploaders"},
                {"$group": {"_id": "$uploaders"}},
                {"$count": "total"}
            ])
            distinct_count = 0
            for doc in distinct_uploaders:
                distinct_count = doc.get("total", 0)
        else:
            distinct_count = 0

        text = (
            f"📊 **Bot Stats**\n\n"
            f"Total Files Stored: `{total_files}`\n"
            f"Total Distinct Uploaders: `{distinct_count}`\n"
        )
        await message.reply(text)
    except Exception as e:
        logger.exception("Failed to gather stats")
        await message.reply(f"❌ Error while fetching stats: `{e}`")

# --- Bot Startup ---
if __name__ == "__main__":
    if not ADMINS:
        logger.warning("WARNING: ADMIN_IDS is not set. Settings command kaam nahi karega.")
    if not LOG_CHANNEL:
        logger.warning("WARNING: LOG_CHANNEL is not configured. File forwarding will fail until it's set.")
    if not FORCE_CHANNELS:
        logger.info("No FORCE_CHANNELS configured: force-subscribe disabled.")

    logger.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logger.info("Bot is starting...")
    try:
        app.run()
    except Exception as e:
        logger.exception("Bot crashed on run")
    logger.info("Bot has stopped.")
