# -*- coding:utf-8 -*-

import asyncio
import json
import logging
import os
import random
import string
import time
import zipfile
import sqlite3
from datetime import datetime, timedelta
import aiofiles
import aiohttp
import threading
import hashlib
import base64
from cryptography.fernet import Fernet
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.errors import UserIsBlockedError, PeerIdInvalidError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Define and create necessary directories
all_media_dir = "Media"
if not os.path.exists(all_media_dir):
    os.makedirs(all_media_dir)

# Configure logging
LOG_FILE = "bot.log"
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Setup logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

console = Console()
SETTINGS_FILE = "settings.json"
STATE_FILE = "bot_state.json"
SESSIONS_DIR = "user_sessions"
DB_FILE = "bot_queue.db"  # Database for media queue

if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# Store active user clients
ACTIVE_USER_CLIENTS = {}
# Store bot client globally
BOT_CLIENT = None
# Media queue instance
MEDIA_QUEUE = None

# Add a global variable for bot config
BOT_CONFIG = None

# Get encryption key from environment variable
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    # Ensure the key is 32 bytes for Fernet
    key_hash = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    ENCRYPTION_KEY = base64.urlsafe_b64encode(key_hash)
else:
    # Generate a key if not set
    logger.warning("⚠️ ENCRYPTION_KEY not found in env! Generating temporary key. Previous encrypted data will be UNREADABLE.")
    ENCRYPTION_KEY = Fernet.generate_key()

# Initialize Fernet cipher
cipher = Fernet(ENCRYPTION_KEY)

def encrypt_data(data: str) -> str:
    """Encrypt sensitive data"""
    if not data:
        return ""
    encrypted = cipher.encrypt(data.encode())
    return encrypted.decode()

def decrypt_data(encrypted_data: str) -> str:
    """Decrypt sensitive data"""
    if not encrypted_data:
        return ""
    try:
        decrypted = cipher.decrypt(encrypted_data.encode())
        return decrypted.decode()
    except:
        return ""

# ===== DATABASE CLASS WITH ENCRYPTION =====
class MediaQueue:
    """Database-based queue for pending media with encryption support"""
    
    def __init__(self):
        self._lock = threading.RLock()
        self.init_database()
    
    def _get_connection(self):
        """Get thread-safe database connection with encryption"""
        with self._lock:
            conn = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            
            # Set encryption key if provided
            encryption_key = os.getenv("DB_ENCRYPTION_KEY")
            if encryption_key:
                # Sanitize key to prevent SQL syntax errors
                safe_key = encryption_key.replace("'", "''")
                conn.execute(f"PRAGMA key='{safe_key}'")
            
            return conn
    
    def init_database(self):
        """Initialize SQLite database for media queue"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            # Create media_queue table
            c.execute('''
                CREATE TABLE IF NOT EXISTS media_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_string TEXT NOT NULL,
                    api_id INTEGER NOT NULL,
                    api_hash TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    sender_username TEXT,
                    sender_id INTEGER,
                    received_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retry_count INTEGER DEFAULT 0,
                    last_attempt TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    ttl_seconds INTEGER,
                    download_time TIMESTAMP,
                    UNIQUE(user_id, message_id, chat_id)
                )
            ''')
            
            # Create processed_media table
            c.execute('''
                CREATE TABLE IF NOT EXISTS processed_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    sender_username TEXT,
                    processed_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    channel_sent BOOLEAN DEFAULT 0,
                    file_path TEXT
                )
            ''')
            
            # Create last_seen table
            c.execute('''
                CREATE TABLE IF NOT EXISTS last_seen (
                    user_id INTEGER,
                    chat_id INTEGER NOT NULL,
                    last_message_id INTEGER DEFAULT 0,
                    last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, chat_id)
                )
            ''')
            
            # Create allowed_users table for access control
            c.execute('''
                CREATE TABLE IF NOT EXISTS allowed_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    added_by INTEGER,
                    added_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                )
            ''')
            
            # Create indexes
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_media_queue_user_status 
                ON media_queue(user_id, status)
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_media_queue_status 
                ON media_queue(status)
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_last_seen_user_chat 
                ON last_seen(user_id, chat_id)
            ''')
            
            conn.commit()
            console.print("[green]✓ Database initialization complete[/green]")
            
        except Exception as e:
            console.print(f"[red]Database initialization error: {e}[/red]")
            logger.error(f"Database initialization error: {e}")
        finally:
            if conn:
                conn.close()
    
    async def add_to_queue(self, user_id: int, session_string: str, api_id: int, api_hash: str,
                    message_id: int, chat_id: int, media_type: str, sender_username: str = None,
                    sender_id: int = None, ttl_seconds: int = None):
        """Add media to queue for offline processing with encrypted sensitive data"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            # Check if already exists
            c.execute('''
                SELECT id FROM media_queue 
                WHERE user_id = ? AND message_id = ? AND chat_id = ?
            ''', (user_id, message_id, chat_id))
            
            if c.fetchone():
                console.print(f"[yellow]Media already in queue (user {user_id}, message {message_id})[/yellow]")
                return None
            
            # Encrypt sensitive data
            encrypted_session = encrypt_data(session_string)
            encrypted_api_hash = encrypt_data(api_hash)
            
            # Insert new record
            c.execute('''
                INSERT INTO media_queue 
                (user_id, session_string, api_id, api_hash, message_id, chat_id, 
                 media_type, sender_username, sender_id, status, ttl_seconds, download_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, CURRENT_TIMESTAMP)
            ''', (user_id, encrypted_session, api_id, encrypted_api_hash, message_id, chat_id, 
                  media_type, sender_username, sender_id, ttl_seconds))
            
            queue_id = c.lastrowid
            conn.commit()
            
            console.print(f"[green]✓ Media added to queue (ID: {queue_id})[/green]")
            logger.info(f"Media added to queue: ID {queue_id} for user {user_id}")
            return queue_id
            
        except Exception as e:
            console.print(f"[red]Error adding to queue: {e}[/red]")
            logger.error(f"Error adding to queue: {e}")
            return None
        finally:
            if conn:
                conn.close()
    
    async def get_pending_media(self, limit: int = 20) -> list:
        """Get pending media items for processing with decryption"""
        conn = None
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            c.execute('''
                SELECT * FROM media_queue 
                WHERE status = 'pending' 
                ORDER BY download_time ASC 
                LIMIT ?
            ''', (limit,))
            
            rows = c.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                # Decrypt sensitive data
                if item.get('session_string'):
                    item['session_string'] = decrypt_data(item['session_string'])
                if item.get('api_hash'):
                    item['api_hash'] = decrypt_data(item['api_hash'])
                result.append(item)
            
            return result
            
        except Exception as e:
            console.print(f"[red]Error getting pending media: {e}[/red]")
            logger.error(f"Error getting pending media: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    async def get_pending_media_for_user(self, user_id: int, limit: int = 20) -> list:
        """Get pending media items for a specific user with decryption"""
        conn = None
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            c.execute('''
                SELECT * FROM media_queue 
                WHERE status = 'pending' AND user_id = ?
                ORDER BY download_time ASC 
                LIMIT ?
            ''', (user_id, limit))
            
            rows = c.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                # Decrypt sensitive data
                if item.get('session_string'):
                    item['session_string'] = decrypt_data(item['session_string'])
                if item.get('api_hash'):
                    item['api_hash'] = decrypt_data(item['api_hash'])
                result.append(item)
            
            return result
            
        except Exception as e:
            console.print(f"[red]Error getting pending media for user {user_id}: {e}[/red]")
            logger.error(f"Error getting pending media for user {user_id}: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    async def update_status(self, queue_id: int, status: str, retry_count: int = None):
        """Update media queue status"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            if retry_count is not None:
                c.execute('''
                    UPDATE media_queue 
                    SET status = ?, retry_count = ?, last_attempt = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (status, retry_count, queue_id))
            else:
                c.execute('''
                    UPDATE media_queue 
                    SET status = ?, last_attempt = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (status, queue_id))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error updating queue status: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def mark_as_processed(self, queue_id: int, user_id: int, message_id: int, chat_id: int,
                         media_type: str, sender_username: str = None, channel_sent: bool = False,
                         file_path: str = None):
        """Mark media as processed"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            c.execute('''
                INSERT INTO processed_media 
                (queue_id, user_id, message_id, chat_id, media_type, 
                 sender_username, channel_sent, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (queue_id, user_id, message_id, chat_id, media_type, 
                  sender_username, channel_sent, file_path))
            
            c.execute('''
                UPDATE media_queue 
                SET status = 'processed' 
                WHERE id = ?
            ''', (queue_id,))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error marking as processed: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def update_last_seen(self, user_id: int, chat_id: int, last_message_id: int):
        """Update last seen message for a user in a chat"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            c.execute('''
                INSERT OR REPLACE INTO last_seen 
                (user_id, chat_id, last_message_id, last_check)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, chat_id, last_message_id))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error updating last seen: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def get_last_seen(self, user_id: int, chat_id: int):
        """Get last seen message ID for a user in a chat"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            c.execute('''
                SELECT last_message_id FROM last_seen 
                WHERE user_id = ? AND chat_id = ?
            ''', (user_id, chat_id))
            
            result = c.fetchone()
            return result[0] if result else 0
            
        except Exception as e:
            logger.error(f"Error getting last seen: {e}")
            return 0
        finally:
            if conn:
                conn.close()
    
    async def get_queue_stats(self):
        """Get queue statistics"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            stats = {}
            
            # Count by status
            c.execute('SELECT status, COUNT(*) FROM media_queue GROUP BY status')
            for status, count in c.fetchall():
                stats[f'{status}_count'] = count
            
            # Total processed
            c.execute('SELECT COUNT(*) FROM processed_media')
            stats['total_processed'] = c.fetchone()[0]
            
            # Pending by user
            c.execute('''
                SELECT user_id, COUNT(*) 
                FROM media_queue 
                WHERE status="pending" 
                GROUP BY user_id
            ''')
            stats['pending_by_user'] = dict(c.fetchall())
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting queue stats: {e}")
            return {}
        finally:
            if conn:
                conn.close()
    
    # ===== ACCESS CONTROL METHODS =====
    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed to use the bot"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            # Get allowed users from environment variable
            allowed_users_env = os.getenv("ALLOWED_USERS", "")
            if allowed_users_env:
                allowed_users = [int(uid.strip()) for uid in allowed_users_env.split(",") if uid.strip()]
                if user_id in allowed_users:
                    return True
            
            # Check database
            c.execute('''
                SELECT user_id FROM allowed_users 
                WHERE user_id = ? AND is_active = 1
            ''', (user_id,))
            
            result = c.fetchone()
            return result is not None
            
        except Exception as e:
            logger.error(f"Error checking user access: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def add_allowed_user(self, user_id: int, username: str, added_by: int) -> bool:
        """Add user to allowed list"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            c.execute('''
                INSERT OR REPLACE INTO allowed_users 
                (user_id, username, added_by, added_time, is_active)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1)
            ''', (user_id, username, added_by))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error adding allowed user: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def remove_allowed_user(self, user_id: int) -> bool:
        """Remove user from allowed list"""
        conn = None
        try:
            conn = self._get_connection()
            c = conn.cursor()
            
            c.execute('''
                UPDATE allowed_users 
                SET is_active = 0 
                WHERE user_id = ?
            ''', (user_id,))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error removing allowed user: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    async def get_allowed_users(self) -> list:
        """Get list of all allowed users"""
        conn = None
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            c.execute('''
                SELECT user_id, username, added_by, added_time 
                FROM allowed_users 
                WHERE is_active = 1
                ORDER BY added_time DESC
            ''')
            
            rows = c.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"Error getting allowed users: {e}")
            return []
        finally:
            if conn:
                conn.close()

async def load_config():
    """Load configuration from environment variables or settings file"""
    # First try environment variables
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    admin_id = os.getenv("ADMIN_ID")
    bot_token = os.getenv("BOT_TOKEN", "")
    session_name = os.getenv("SESSION_NAME", "self_destruct")
    channel_id = os.getenv("CHANNEL_ID")
    
    # If environment variables are not set, try settings file
    if not all([api_id, api_hash, admin_id, bot_token]):
        console.print("[yellow]Environment variables not fully set. Checking settings file...[/yellow]")
        if os.path.exists(SETTINGS_FILE):
            async with aiofiles.open(SETTINGS_FILE, mode="r") as file:
                settings = json.loads(await file.read())
            
            api_id = api_id or settings.get("api_id")
            api_hash = api_hash or settings.get("api_hash")
            admin_id = admin_id or settings.get("admin_id")
            bot_token = bot_token or settings.get("bot_token", "")
            session_name = session_name or settings.get("session_name", "self_destruct")
            channel_id = channel_id or settings.get("channel_id")
        
        # If still not set, create new config
        if not all([api_id, api_hash, admin_id, bot_token]):
            return await create_new_config()
    
    # Store bot config globally
    global BOT_CONFIG
    BOT_CONFIG = {
        "api_id": api_id,
        "api_hash": api_hash,
        "bot_token": bot_token
    }
    
    return api_id, api_hash, admin_id, bot_token, session_name, channel_id


async def create_new_config():
    """Create new configuration file from user input"""
    console.print("[yellow]No configuration found. Let's create one.[/yellow]")
    
    console.print("\n[cyan]=== Bot Configuration ===[/cyan]")
    console.print("This bot will only use Bot Token for login.")
    console.print("Users can login with their own accounts using /login command.")
    
    api_id = input("Enter your API_ID: ").strip()
    api_hash = input("Enter your API_HASH: ").strip()
    bot_token = input("Enter your Bot Token: ").strip()
    admin_id = input("Enter the Admin ID: ").strip()
    channel_id = input("Enter the Channel ID where files should be saved (e.g., -1001234567890): ").strip()
    session_name = input("Enter session name for bot (default: 'self_destruct'): ").strip()
    
    if not session_name:
        session_name = "self_destruct"
    
    settings = {
        "api_id": api_id,
        "api_hash": api_hash,
        "admin_id": admin_id,
        "bot_token": bot_token,
        "session_name": session_name,
        "channel_id": channel_id
    }
    
    async with aiofiles.open(SETTINGS_FILE, mode="w") as file:
        await file.write(json.dumps(settings, indent=4))
    
    console.print("\n[green]Configuration saved![/green]")
    
    # Store bot config globally
    global BOT_CONFIG
    BOT_CONFIG = {
        "api_id": api_id,
        "api_hash": api_hash,
        "bot_token": bot_token
    }
    
    return api_id, api_hash, admin_id, bot_token, session_name, channel_id


async def update_channel_id(new_channel_id):
    """Update global channel ID in settings file"""
    if os.path.exists(SETTINGS_FILE):
        async with aiofiles.open(SETTINGS_FILE, mode="r") as file:
            settings = json.loads(await file.read())
        
        settings["channel_id"] = new_channel_id
        
        async with aiofiles.open(SETTINGS_FILE, mode="w") as file:
            await file.write(json.dumps(settings, indent=4))
        
        return True
    return False


async def update_user_channel_id(user_id, new_channel_id, state):
    """Update channel ID for a specific user in state"""
    if str(user_id) in state.get("user_sessions", {}):
        state["user_sessions"][str(user_id)]["channel_id"] = new_channel_id
        await save_state(state)
        return True
    return False


async def get_user_channel_id(user_id, state):
    """Get channel ID for a specific user"""
    user_session = state.get("user_sessions", {}).get(str(user_id))
    if user_session:
        return user_session.get("channel_id")
    return None


async def load_state():
    """Load bot state from file"""
    if os.path.exists(STATE_FILE):
        async with aiofiles.open(STATE_FILE, mode="r") as file:
            state = json.loads(await file.read())
    else:
        state = {"letter_counter": 0, "user_folders": {}, "user_sessions": {}, "login_sessions": {}}
    
    # Initialize global forwarding setting if not exists
    if "global_forwarding_enabled" not in state:
        state["global_forwarding_enabled"] = True  # Default: enabled
    
    return state


async def save_state(state):
    """Save bot state to file"""
    async with aiofiles.open(STATE_FILE, mode="w") as file:
        await file.write(json.dumps(state, indent=4))


def get_user_session_file(user_id):
    """Get session file path for a user"""
    return os.path.join(SESSIONS_DIR, f"user_{user_id}.session")


class RichDownloadProgress:
    """Enhanced progress bar for downloads"""
    def __init__(self, filename, total_size):
        self.filename = filename
        self.total_size = total_size
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console
        )
        self.task = self.progress.add_task(f"[cyan]Downloading {filename}", total=total_size)
        self.progress.start()
    
    def update(self, downloaded):
        self.progress.update(self.task, completed=downloaded)
    
    def close(self):
        self.progress.stop()

async def is_admin(event, admin_id):
    """Check if sender is admin"""
    if admin_id is None:
        return False

    try:
        return event.sender_id == int(admin_id)
    except Exception:
        return False

async def safe_notify(bot_client, user_id, text):
    """
    Safely notify a user.
    If bot cannot DM user, silently ignore.
    """
    try:
        await bot_client.send_message(
            user_id,
            text,
            silent=True
        )

    except (UserIsBlockedError, PeerIdInvalidError):
        # User blocked bot OR never started bot
        return

    except ValueError as e:
        # Entity not found (VERY COMMON)
        if "Could not find the input entity" in str(e):
            return

    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s while notifying {user_id}")

    except Exception as e:
        logger.debug(f"safe_notify skipped for {user_id}: {e}")


async def check_missed_media(user_id, user_client, state):
    """Check for missed self-destructing media (last 48 hours) when coming back online"""
    console.print(f"[cyan]Checking missed media for user {user_id} (last 48 hours)...[/cyan]")
    
    try:
        dialogs = await user_client.get_dialogs(limit=50)
        
        total_found = 0
        total_queued = 0
        
        # Calculate time 48 hours ago
        time_48h_ago = datetime.now() - timedelta(hours=48)
        console.print(f"[cyan]Checking messages since: {time_48h_ago}[/cyan]")
        
        for dialog in dialogs:
            if dialog.is_user and dialog.entity.bot is False:
                chat_id = dialog.entity.id
                last_seen_id = await MEDIA_QUEUE.get_last_seen(user_id, chat_id)
                
                console.print(f"[yellow]Checking chat with {dialog.entity.username or dialog.entity.first_name}[/yellow]")
                
                try:
                    # Get messages from last 48 hours
                    messages = []
                    async for message in user_client.iter_messages(
                        dialog.entity, 
                        limit=200,  # Increased limit to scan more messages
                        offset_date=time_48h_ago,
                        reverse=True  # Get newest first
                    ):
                        messages.append(message)
                    
                    console.print(f"[cyan]Found {len(messages)} messages in last 48 hours[/cyan]")
                    
                    for message in messages:
                        # Skip if message is older than last seen
                        if message.id <= last_seen_id:
                            continue
                            
                        ttl = None
                        if hasattr(message, 'media') and message.media:
                            if hasattr(message.media, 'ttl_seconds'):
                                ttl = message.media.ttl_seconds
                            elif hasattr(message.media, 'photo') and hasattr(message.media.photo, 'ttl_seconds'):
                                ttl = message.media.photo.ttl_seconds
                            elif hasattr(message.media, 'document') and hasattr(message.media.document, 'ttl_seconds'):
                                ttl = message.media.document.ttl_seconds
                            elif hasattr(message.media, 'video') and hasattr(message.media.video, 'ttl_seconds'):
                                ttl = message.media.video.ttl_seconds
                        
                        if ttl and ttl > 0:
                            total_found += 1
                            console.print(f"[green]Found self-destructing media (TTL: {ttl}s) from {message.date}[/green]")
                            
                            session_string = user_client.session.save()
                            sender = await message.get_sender()
                            sender_username = sender.username if sender and hasattr(sender, 'username') else "Unknown"
                            sender_id = sender.id if sender else None
                            
                            # Determine media type
                            media_type = "unknown"
                            if message.photo:
                                media_type = "jpg"
                            elif message.video:
                                media_type = "mp4"
                            elif message.document:
                                media_type = "bin"
                            elif message.audio:
                                media_type = "mp3"
                            elif message.voice:
                                media_type = "ogg"
                            elif message.video_note:
                                media_type = "mp4"
                            
                            queue_id = await MEDIA_QUEUE.add_to_queue(
                                user_id,
                                session_string,
                                user_client.api_id,
                                user_client.api_hash,
                                message.id,
                                chat_id,
                                media_type,
                                sender_username,
                                sender_id,
                                ttl
                            )
                            
                            if queue_id:
                                total_queued += 1
                                console.print(f"[yellow]Queued missed media (ID: {queue_id})[/yellow]")
                            else:
                                console.print(f"[red]Failed to queue media (message ID: {message.id})[/red]")
                    
                    if messages:
                        last_msg_id = max([msg.id for msg in messages]) if messages else last_seen_id
                        await MEDIA_QUEUE.update_last_seen(user_id, chat_id, last_msg_id)
                        
                except Exception as e:
                    console.print(f"[red]Error checking chat {chat_id}: {e}[/red]")
        
        console.print(f"[green]Found {total_found} self-destructing media in last 48 hours, queued {total_queued}[/green]")
        return total_found
        
    except Exception as e:
        console.print(f"[red]Error checking missed media: {e}[/red]")
        return 0

async def process_queued_media():
    """Process queued media when bot comes online"""
    console.print("[cyan]Processing queued media...[/cyan]")
    
    pending_items = await MEDIA_QUEUE.get_pending_media(limit=10)
    
    if not pending_items:
        console.print("[green]No pending media in queue[/green]")
        return
    
    console.print(f"[yellow]Found {len(pending_items)} pending media items[/yellow]")
    
    processed_count = 0
    failed_count = 0
    
    for item in pending_items:
        try:
            console.print(f"[cyan]Processing queue item {item['id']} for user {item['user_id']}[/cyan]")
            
            # Check retry count
            if item['retry_count'] >= 3:
                console.print(f"[red]Item {item['id']} exceeded max retries[/red]")
                await MEDIA_QUEUE.update_status(item['id'], 'failed')
                failed_count += 1
                continue
            
            await MEDIA_QUEUE.update_status(item['id'], 'processing', item['retry_count'] + 1)
            
            # Load user client
            session = StringSession(item['session_string'])
            user_client = TelegramClient(session, item['api_id'], item['api_hash'])
            
            await user_client.connect()
            
            if await user_client.is_user_authorized():
                try:
                    # First, ensure we have the entity in cache by getting dialogs
                    console.print(f"[cyan]Loading dialogs for user {item['user_id']}...[/cyan]")
                    
                    # Get a few dialogs to populate entity cache
                    try:
                        dialogs = await user_client.get_dialogs(limit=10)
                        console.print(f"[cyan]Loaded {len(dialogs)} dialogs[/cyan]")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Could not load dialogs: {e}[/yellow]")
                    
                    # Try multiple methods to get the entity
                    entity = None
                    
                    # Method 1: Try to get entity by peer
                    try:
                        # Check if chat_id is negative (channel/group) or positive (user)
                        if item['chat_id'] < 0:
                            # For channels/groups
                            entity = await user_client.get_entity(item['chat_id'])
                        else:
                            # For users, we need to resolve the user
                            # Try to get from recent dialogs first
                            for dialog in dialogs:
                                if dialog.entity.id == item['chat_id']:
                                    entity = dialog.entity
                                    break
                            
                            # If not found in dialogs, try to get directly
                            if not entity:
                                entity = await user_client.get_entity(item['chat_id'])
                        
                        console.print(f"[green]Found entity: {entity.id}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]Method 1 failed to get entity: {e}[/yellow]")
                    
                    # Method 2: Try using input peer if we have sender info
                    if not entity and item.get('sender_username'):
                        try:
                            entity = await user_client.get_entity(item['sender_username'])
                            console.print(f"[green]Found entity by username: {item['sender_username']}[/green]")
                        except Exception as e:
                            console.print(f"[yellow]Method 2 failed to get entity by username: {e}[/yellow]")
                    
                    # Method 3: Try using peer ID directly
                    if not entity:
                        try:
                            from telethon.tl.types import InputPeerUser
                            
                            # Since we don't have access_hash, try to get it from dialogs
                            access_hash = None
                            for dialog in dialogs:
                                if hasattr(dialog.entity, 'access_hash') and dialog.entity.id == item['chat_id']:
                                    access_hash = dialog.entity.access_hash
                                    break
                            
                            if access_hash:
                                entity = InputPeerUser(user_id=item['chat_id'], access_hash=access_hash)
                                console.print(f"[green]Created InputPeerUser with access_hash[/green]")
                            else:
                                # Last resort: try to get the entity without access_hash
                                entity = item['chat_id']
                                console.print(f"[yellow]Using chat_id directly as entity[/yellow]")
                        except Exception as e:
                            console.print(f"[yellow]Method 3 failed: {e}[/yellow]")
                    
                    if not entity:
                        console.print(f"[red]Could not resolve entity for chat_id {item['chat_id']}[/red]")
                        await MEDIA_QUEUE.update_status(item['id'], 'pending')
                        failed_count += 1
                        await user_client.disconnect()
                        continue
                    
                    # Now try to get the message
                    try:
                        message = await user_client.get_messages(entity, ids=item['message_id'])
                        
                        if message and message.media:
                            console.print(f"[green]Found queued message {item['message_id']}[/green]")
                            
                            # Check TTL first
                            ttl = None
                            if hasattr(message, 'media') and message.media:
                                if hasattr(message.media, 'ttl_seconds'):
                                    ttl = message.media.ttl_seconds
                                elif hasattr(message.media, 'photo') and hasattr(message.media.photo, 'ttl_seconds'):
                                    ttl = message.media.photo.ttl_seconds
                                elif hasattr(message.media, 'document') and hasattr(message.media.document, 'ttl_seconds'):
                                    ttl = message.media.document.ttl_seconds
                            
                            # Check if media is still available (not expired)
                            if ttl and ttl <= 0:
                                console.print(f"[yellow]Media expired (TTL: {ttl}), skipping[/yellow]")
                                await MEDIA_QUEUE.mark_as_processed(
                                    item['id'],
                                    item['user_id'],
                                    item['message_id'],
                                    item['chat_id'],
                                    item['media_type'],
                                    item['sender_username'],
                                    False,
                                    None
                                )
                                await MEDIA_QUEUE.update_status(item['id'], 'expired')
                                failed_count += 1
                                continue
                            
                            # Download media
                            timestamp = int(time.time())
                            random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
                            filename = f"{timestamp}_{random_str}.{item['media_type']}"
                            file_path = os.path.join(all_media_dir, f"temp_{filename}")
                            
                            file_size = message.file.size if message.file else 0
                            progress = RichDownloadProgress(filename, file_size) if file_size > 0 else None
                            
                            # Download with retry
                            max_retries = 2
                            for retry in range(max_retries):
                                try:
                                    await message.download_media(
                                        file=file_path,
                                        progress_callback=lambda c, t: progress.update(c) if progress else None
                                    )
                                    break
                                except Exception as download_error:
                                    if retry == max_retries - 1:
                                        raise download_error
                                    console.print(f"[yellow]Download failed, retry {retry + 1}/{max_retries}[/yellow]")
                                    await asyncio.sleep(1)
                            
                            if progress:
                                progress.close()
                            
                            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                                state = await load_state()
                                
                                # Process the downloaded file
                                success = await user_downloader_queue(
                                    user_client,
                                    file_path,
                                    item['sender_username'] or "Unknown",
                                    item['user_id'],
                                    state,
                                    item['media_type'],
                                    ttl or item.get('ttl_seconds')
                                )
                                
                                if success:
                                    await MEDIA_QUEUE.mark_as_processed(
                                        item['id'],
                                        item['user_id'],
                                        item['message_id'],
                                        item['chat_id'],
                                        item['media_type'],
                                        item['sender_username'],
                                        True,
                                        file_path
                                    )
                                    
                                    await MEDIA_QUEUE.update_last_seen(item['user_id'], item['chat_id'], item['message_id'])
                                    processed_count += 1
                                    console.print(f"[green]✓ Processed queued media {item['id']}[/green]")
                                else:
                                    await MEDIA_QUEUE.update_status(item['id'], 'pending')
                                    failed_count += 1
                                    
                                    # Clean up failed file
                                    try:
                                        if os.path.exists(file_path):
                                            os.remove(file_path)
                                    except:
                                        pass
                            else:
                                console.print(f"[red]Downloaded file is empty or doesn't exist[/red]")
                                await MEDIA_QUEUE.update_status(item['id'], 'pending')
                                failed_count += 1
                        else:
                            console.print(f"[yellow]Message not found or has no media: {item['message_id']}[/yellow]")
                            await MEDIA_QUEUE.update_status(item['id'], 'failed')
                            failed_count += 1
                            
                    except Exception as e:
                        console.print(f"[red]Error downloading message: {e}[/red]")
                        await MEDIA_QUEUE.update_status(item['id'], 'pending')
                        failed_count += 1
                        
                except Exception as e:
                    console.print(f"[red]Error processing message: {e}[/red]")
                    await MEDIA_QUEUE.update_status(item['id'], 'pending')
                    failed_count += 1
            else:
                console.print(f"[red]User {item['user_id']} not authorized[/red]")
                await MEDIA_QUEUE.update_status(item['id'], 'failed')
                failed_count += 1
            
            await user_client.disconnect()
            await asyncio.sleep(2)  # Increased delay between processing
            
        except Exception as e:
            console.print(f"[red]Error processing queued media: {e}[/red]")
            await MEDIA_QUEUE.update_status(item['id'], 'failed' if item['retry_count'] >= 2 else 'pending')
            failed_count += 1
    
    console.print(f"[green]Processed {processed_count} items, failed {failed_count}[/green]")


async def user_downloader_queue(user_client, file_path, sender_username, user_id, state, media_type, ttl):
    """Process downloaded media from queue"""
    try:
        # Get user's channel from state
        user_session = state.get("user_sessions", {}).get(str(user_id))
        user_channel_id = user_session.get("channel_id") if user_session else None
        
        # Send to user's channel if set
        if user_channel_id:
            try:
                success = await send_to_user_channel(user_client, file_path, sender_username, user_channel_id)
                if success:
                    console.print(f"[green]✓ Media sent to user's channel {user_channel_id}[/green]")
            except Exception as e:
                console.print(f"[red]Error sending to user channel: {e}[/red]")
        
        # Also send to admin channel if available
        global BOT_CLIENT
        if BOT_CLIENT and hasattr(BOT_CLIENT, 'channel_id') and BOT_CLIENT.channel_id:
            try:
                success = await send_to_admin_channel(BOT_CLIENT, file_path, sender_username, BOT_CLIENT.channel_id)
                if success:
                    console.print(f"[green]✓ Media sent to admin's channel {BOT_CLIENT.channel_id}[/green]")
            except Exception as e:
                console.print(f"[red]Error sending to admin channel: {e}[/red]")
        
        # Organize file
        final_path = await organize_and_save_file(file_path, sender_username, user_id, state, media_type)
        
        console.print(f"[green]✓ Successfully processed queued media (TTL: {ttl}s)[/green]")
        return True
        
    except Exception as e:
        console.print(f"[red]Error processing media file from queue: {e}[/red]")
        return False


async def organize_and_save_file(file_path, sender_username, user_id, state, media_type):
    """Organize and save file to proper folder"""
    try:
        user_folder_key = f"{sender_username}_{user_id}"
        
        if user_folder_key in state["user_folders"]:
            user_folder_name = state["user_folders"][user_folder_key]
        else:
            counter = state["letter_counter"]
            letter = string.ascii_uppercase[counter % 26]
            user_folder_name = f"{counter:02d} - {letter} - @{sender_username} - {user_id}"
            state["user_folders"][user_folder_key] = user_folder_name
            state["letter_counter"] += 1
            await save_state(state)
        
        user_folder_path = os.path.join(all_media_dir, user_folder_name)
        os.makedirs(user_folder_path, exist_ok=True)
        
        timestamp = int(time.time())
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        final_filename = f"{timestamp}_{random_str}.{media_type}"
        final_path = os.path.join(user_folder_path, final_filename)
        
        os.rename(file_path, final_path)
        
        console.print(f"[green]✓ File organized: {final_path}[/green]")
        return final_path
        
    except Exception as e:
        console.print(f"[red]Error organizing file: {e}[/red]")
        return file_path


async def handle_start(event, admin_id):
    """Handle /start command"""
    if event.is_private:
        welcome_message = (
            "🤖 **Welcome to Self-Destructing Media Downloader Bot!**\n\n"
            "This bot can download media files and save them to a channel.\n\n"
            "**Features:**\n"
            "• Download photos, videos, documents\n"
            "• Send files to your personal channel\n"
            "• Progress tracking\n"
            "• Offline media recovery (NEW!)\n"
            "• Queue system for missed media\n\n"
            "**For Users:**\n"
            "1. Use /login to login with your own account\n"
            "2. Use /setmychannel to set your personal channel\n"
            "3. Use /checkmissed to find missed media (last 48h)\n"
            "4. Your self-destructing media will be saved to your channel\n\n"
            "**For Admin:**\n"
            "Use /help to see all available commands.\n\n"
            "Enjoy using the bot!"
        )
        await event.reply(welcome_message, parse_mode='markdown')


async def handle_login(event, admin_id, bot_client, state):
    """Handle /login command for user session login with access control"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    
    # Check access control
    if not await MEDIA_QUEUE.is_user_allowed(user_id):
        # Get admin username for contact information
        admin_contact = "the administrator"
        try:
            admin_entity = await bot_client.get_entity(int(admin_id))
            if admin_entity.username:
                admin_contact = f"@{admin_entity.username}"
            else:
                # If no username, use first name
                admin_contact = admin_entity.first_name or "the administrator"
        except Exception as e:
            console.print(f"[yellow]Could not get admin info: {e}[/yellow]")
        
        await event.reply(
            "❌ **Access Denied**\n\n"
            "You are not authorized to use this bot.\n"
            f"Please contact {admin_contact} for access."
        )
        return
    
    user_session_file = get_user_session_file(user_id)
    
    # Check if user already has a session file OR is in user_sessions
    if os.path.exists(user_session_file):
        # Try to load and verify the session
        try:
            async with aiofiles.open(user_session_file, mode="r") as f:
                session_string = await f.read()
            
            if session_string:
                # Try to connect and verify session
                session = StringSession(session_string)
                user_client = TelegramClient(
                    session,
                    BOT_CONFIG["api_id"] if BOT_CONFIG else int(os.getenv("API_ID")),
                    BOT_CONFIG["api_hash"] if BOT_CONFIG else os.getenv("API_HASH")
                )
                
                await user_client.connect()
                
                if await user_client.is_user_authorized():
                    # User is already logged in
                    me = await user_client.get_me()
                    
                    # Ensure user is in state
                    if "user_sessions" not in state:
                        state["user_sessions"] = {}
                    
                    if str(user_id) not in state["user_sessions"]:
                        state["user_sessions"][str(user_id)] = {
                            "api_id": user_client.api_id,
                            "api_hash": user_client.api_hash,
                            "username": me.username,
                            "phone": me.phone,
                            "first_name": me.first_name,
                            "last_name": me.last_name,
                            "session_file": user_session_file,
                            "login_time": time.time(),
                            "channel_id": None
                        }
                        await save_state(state)
                    
                    # Setup handlers and store in active clients
                    await setup_user_client_handlers(user_client, user_id, bot_client, state)
                    
                    global ACTIVE_USER_CLIENTS
                    ACTIVE_USER_CLIENTS[str(user_id)] = user_client
                    
                    await user_client.start()
                    
                    await event.reply(
                        "✅ You are already logged in!\n"
                        "You can now receive and save self-destructing media from your account.\n\n"
                        "Set your personal channel: /setmychannel\n"
                        "Check your status: /mystatus\n"
                        "Check missed media (last 48h): /checkmissed\n\n"
                        "To logout, use /logout command."
                    )
                    await user_client.disconnect()
                    return
                else:
                    # Session is invalid, delete it
                    await user_client.disconnect()
                    os.remove(user_session_file)
        except Exception as e:
            console.print(f"[yellow]Session verification failed for user {user_id}: {e}[/yellow]")
            # Session might be corrupted, delete it
            try:
                if os.path.exists(user_session_file):
                    os.remove(user_session_file)
            except:
                pass
    
    # Start login process - Step 1
    message = (
        "**1. Send Your API ID.**\n\n"
        "**NOTE: You must use your own API credentials.**\n\n"
        "To get API ID and API HASH, visit: https://my.telegram.org"
    )
    
    # Initialize login session
    if "login_sessions" not in state:
        state["login_sessions"] = {}
    
    state["login_sessions"][str(user_id)] = {
        "step": "api_id",
        "api_id": None,
        "api_hash": None,
        "phone": None,
        "phone_code_hash": None,
        "temp_client": None
    }
    
    await save_state(state)
    await event.reply(message, parse_mode='markdown')

async def handle_api_id(event, admin_id, state):
    """Handle API ID input"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    user_data = state.get("login_sessions", {}).get(str(user_id))
    
    if not user_data or user_data.get("step") != "api_id":
        return
    
    text = event.text.strip()
    
    # Check if user wants to cancel
    if text.lower() == "/cancel":
        await handle_cancel(event, admin_id, state)
        return
    
    # Try to parse as integer
    try:
        api_id = int(text)
        
        # Validate API ID (should be a positive integer)
        if api_id <= 0:
            await event.reply("❌ API ID must be a positive number.\nExample: `1234567`\nEnter /cancel to cancel", parse_mode='markdown')
            return
        
        # Store in state
        state["login_sessions"][str(user_id)]["api_id"] = api_id
        state["login_sessions"][str(user_id)]["step"] = "api_hash"
        await save_state(state)
        
        await event.reply(
            "✅ **API ID saved!**\n\n"
            "**2. Now Send Me Your API HASH**\n\n"
            "Enter /cancel to cancel the process\n"
            "Example: `a1b2c3d4e5f67890abcdef1234567890`"
        )
        
    except ValueError:
        await event.reply("❌ Invalid API_ID. Please send a valid number.\nExample: `1234567`\nEnter /cancel to cancel", parse_mode='markdown')


async def handle_api_hash(event, admin_id, state):
    """Handle API Hash input"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    user_data = state.get("login_sessions", {}).get(str(user_id))
    
    if not user_data or user_data.get("step") != "api_hash":
        await event.reply("❌ Please complete previous steps first.")
        return
    
    text = event.text.strip()
    
    # Check if user wants to cancel
    if text.lower() == "/cancel":
        await handle_cancel(event, admin_id, state)
        return
    
    # The text is the API hash
    api_hash = text
    
    # Update state
    state["login_sessions"][str(user_id)]["api_hash"] = api_hash
    state["login_sessions"][str(user_id)]["step"] = "phone"
    await save_state(state)
    
    await event.reply(
        "**3. Please send your phone number which includes country code**\n"
        "Example: +13124562345, +9171828181889\n\n"
        "Enter /cancel to cancel the process"
    )


async def handle_phone(event, admin_id, state, bot_client):
    """Handle phone number input"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    user_data = state.get("login_sessions", {}).get(str(user_id))
    
    if not user_data or user_data.get("step") != "phone":
        await event.reply("❌ Please complete previous steps first.")
        return
    
    text = event.text.strip()
    
    # Check if user wants to cancel
    if text.lower() == "/cancel":
        await handle_cancel(event, admin_id, state)
        return
    
    phone = text
    
    # Validate phone format
    if not phone.startswith('+'):
        await event.reply("❌ Phone number must start with country code (e.g., +1, +91)\nExample: `+1234567890`", parse_mode='markdown')
        return
    
    try:
        api_id = user_data["api_id"]
        api_hash = user_data["api_hash"]
        
        # Create a unique session for this user
        session = StringSession()
        user_client = TelegramClient(session, api_id, api_hash)
        
        # Store in state
        state["login_sessions"][str(user_id)]["phone"] = phone
        state["login_sessions"][str(user_id)]["step"] = "code"
        await save_state(state)
        
        # Send code request
        await user_client.connect()
        sent_code = await user_client.send_code_request(phone)
        
        # Save session string to state (this is serializable)
        session_string = user_client.session.save()
        state["login_sessions"][str(user_id)]["session_string"] = session_string
        state["login_sessions"][str(user_id)]["phone_code_hash"] = sent_code.phone_code_hash
        await save_state(state)
        
        # Disconnect the client for now
        await user_client.disconnect()
        
        await event.reply(
            "**4. Sending OTP...**\n\n"
            "**5. Please check for an OTP in official telegram account. If you got it, send OTP here after reading the below format.**\n\n"
            "If OTP is 12345, please send it as `1 2 3 4 5`.\n\n"
            "Enter /cancel to cancel The Process"
        )
        
    except Exception as e:
        error_msg = str(e).lower()
        if "phone" in error_msg and "invalid" in error_msg:
            await event.reply("❌ Invalid phone number. Please check and try again.\nExample: `+1234567890`", parse_mode='markdown')
        elif "flood" in error_msg:
            await event.reply("❌ Too many attempts. Please wait before trying again.")
        else:
            await event.reply(f"❌ Error: {str(e)}")
        
        # Clean up
        if str(user_id) in state["login_sessions"]:
            del state["login_sessions"][str(user_id)]
        await save_state(state)


async def handle_code(event, admin_id, state):
    """Handle verification code input"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    user_data = state.get("login_sessions", {}).get(str(user_id))
    
    if not user_data or user_data.get("step") != "code":
        await event.reply("❌ Please complete previous steps first.")
        return
    
    # Check if user wants to cancel
    if event.text.lower() == "/cancel":
        await handle_cancel(event, admin_id, state)
        return
    
    # Extract code (remove spaces if user sent with spaces)
    code = event.text.strip().replace(' ', '')
    
    if not code.isdigit():
        await event.reply("❌ Invalid OTP format. Please send only numbers.\nExample: `1 2 3 4 5` or `12345`", parse_mode='markdown')
        return
    
    if len(code) < 4 or len(code) > 6:
        await event.reply("❌ OTP should be 4-6 digits. Please check and try again.")
        return
    
    try:
        phone = user_data["phone"]
        phone_code_hash = user_data["phone_code_hash"]
        session_string = user_data.get("session_string")
        
        if not session_string:
            await event.reply("❌ Session data missing. Please restart login with /login")
            return
        
        # Recreate client from session string
        session = StringSession(session_string)
        user_client = TelegramClient(
            session,
            user_data["api_id"],
            user_data["api_hash"]
        )
        
        await user_client.connect()
        
        # Try to sign in
        try:
            await user_client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash
            )
            
            # Login successful
            await complete_user_login(event, user_id, user_client, state)
            
        except Exception as e:
            error_msg = str(e).lower()
            # Check if 2FA is required
            if "two-steps" in error_msg or "2fa" in error_msg or "password" in error_msg:
                # Save updated session string
                new_session_string = user_client.session.save()
                state["login_sessions"][str(user_id)]["session_string"] = new_session_string
                state["login_sessions"][str(user_id)]["step"] = "2fa"
                await save_state(state)
                
                await event.reply(
                    "**6. Your account has enabled two-step verification. Please provide the password.**\n\n"
                    "Enter /cancel to cancel The Process"
                )
                await user_client.disconnect()
            else:
                # Invalid code
                if "code" in error_msg and "invalid" in error_msg:
                    await event.reply("❌ Invalid OTP. Please check and try again.\nIf OTP is 12345, send as `1 2 3 4 5`", parse_mode='markdown')
                elif "code" in error_msg and "expired" in error_msg:
                    await event.reply("❌ OTP expired. Please restart login process with /login")
                    # Clean up
                    if str(user_id) in state["login_sessions"]:
                        del state["login_sessions"][str(user_id)]
                    await save_state(state)
                else:
                    await event.reply(f"❌ Error: {str(e)}")
                
                await user_client.disconnect()
        
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        if 'user_client' in locals():
            try:
                await user_client.disconnect()
            except:
                pass


async def handle_2fa(event, admin_id, state):
    """Handle 2FA password input"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    user_data = state.get("login_sessions", {}).get(str(user_id))
    
    if not user_data or user_data.get("step") != "2fa":
        await event.reply("❌ Please complete previous steps first.")
        return
    
    # Check if user wants to cancel
    if event.text.lower() == "/cancel":
        await handle_cancel(event, admin_id, state)
        return
    
    password = event.text.strip()
    
    if not password:
        await event.reply("❌ Please provide your 2FA password.\nEnter /cancel to cancel", parse_mode='markdown')
        return
    
    try:
        session_string = user_data.get("session_string")
        
        if not session_string:
            await event.reply("❌ Session data missing. Please restart login with /login")
            return
        
        # Recreate client from session string
        session = StringSession(session_string)
        user_client = TelegramClient(
            session,
            user_data["api_id"],
            user_data["api_hash"]
        )
        
        await user_client.connect()
        
        # Complete sign in with 2FA
        await user_client.sign_in(password=password)
        
        # Login successful
        await complete_user_login(event, user_id, user_client, state)
        
    except Exception as e:
        error_msg = str(e).lower()
        if "password" in error_msg and "invalid" in error_msg:
            await event.reply("❌ Invalid 2FA password. Please try again.")
        else:
            await event.reply(f"❌ Error: {str(e)}")
        
        if 'user_client' in locals():
            try:
                await user_client.disconnect()
            except:
                pass


async def setup_user_client_handlers(user_client, user_id, bot_client, state):
    """Setup event handlers for user client to catch self-destructing media"""
    
    console.print(f"[yellow]Setting up handlers for user {user_id}[/yellow]")
    
    @user_client.on(events.NewMessage(incoming=True))
    async def user_media_handler(event):
        try:
            # 🚫 Ignore outgoing messages
            if event.out:
                return

            # 🚫 Ignore non-private chats
            if not event.is_private:
                return

            # 🚫 Ignore messages without media
            if not event.media:
                return

            msg = event.message

            # ✅ Check for TTL (self-destructing media)
            ttl = None
            if hasattr(msg, 'media') and msg.media:
                if hasattr(msg.media, 'ttl_seconds'):
                    ttl = msg.media.ttl_seconds
                elif hasattr(msg.media, 'photo') and hasattr(msg.media.photo, 'ttl_seconds'):
                    ttl = msg.media.photo.ttl_seconds
                elif hasattr(msg.media, 'document') and hasattr(msg.media.document, 'ttl_seconds'):
                    ttl = msg.media.document.ttl_seconds
                elif hasattr(msg.media, 'video') and hasattr(msg.media.video, 'ttl_seconds'):
                    ttl = msg.media.video.ttl_seconds
                elif hasattr(msg.media, 'video_note') and hasattr(msg.media.video_note, 'ttl_seconds'):
                    ttl = msg.media.video_note.ttl_seconds
                elif hasattr(msg.media, 'voice') and hasattr(msg.media.voice, 'ttl_seconds'):
                    ttl = msg.media.voice.ttl_seconds
                elif hasattr(msg.media, 'audio') and hasattr(msg.media.audio, 'ttl_seconds'):
                    ttl = msg.media.audio.ttl_seconds

            # ❌ Ignore normal media (no TTL) - SILENTLY
            if not ttl:
                return  # ✅ No console print, just return

            # ✅ Only log self-destructing media
            console.print(f"[green]⚠️ Self-destructing media detected for user {user_id} (TTL: {ttl}s)[/green]")
            
            # Get sender info for queue
            try:
                sender = await event.get_sender()
                sender_username = sender.username if sender.username else "Unknown"
                sender_id = sender.id if sender.id else None
            except:
                sender_username = "Unknown"
                sender_id = None
            
            # Determine media type
            if event.photo:
                media_type = "jpg"
            elif event.video:
                media_type = "mp4"
            elif event.document:
                media_type = "bin"
            elif event.audio:
                media_type = "mp3"
            elif event.voice:
                media_type = "ogg"
            elif event.video_note:
                media_type = "mp4"
            else:
                media_type = "unknown"
            
            # ✅ Download immediately
            await user_downloader(
                event,
                user_client,
                bot_client,
                all_media_dir,
                state
            )

            console.print(f"[magenta]✅ Media saved for user {user_id}[/magenta]")
            
            # Update last seen
            await MEDIA_QUEUE.update_last_seen(user_id, event.chat_id, event.id)
            
            # ✅ Log the successful save
            logger.info(f"User {user_id} saved self-destructing media (TTL: {ttl}s)")
            
            # Also add to queue as backup
            session_string = user_client.session.save()
            await MEDIA_QUEUE.add_to_queue(
                user_id,
                session_string,
                user_client.api_id,
                user_client.api_hash,
                event.id,
                event.chat_id,
                media_type,
                sender_username,
                sender_id,
                ttl
            )

        except Exception as e:
            # Only log actual errors
            console.print(f"[red]❌ Error in user media handler for {user_id}: {e}[/red]")
            logger.error(f"User media handler error for {user_id}: {e}")


async def complete_user_login(event, user_id, user_client, state):
    """Complete user login process"""
    try:
        # Get user info
        me = await user_client.get_me()
        
        # Save session to file
        session_string = user_client.session.save()
        user_session_file = get_user_session_file(user_id)
        
        async with aiofiles.open(user_session_file, mode="w") as f:
            await f.write(session_string)
        
        # Update state
        if "user_sessions" not in state:
            state["user_sessions"] = {}
        
        state["user_sessions"][str(user_id)] = {
            "api_id": user_client.api_id,
            "api_hash": user_client.api_hash,
            "username": me.username,
            "phone": me.phone,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "session_file": user_session_file,
            "login_time": time.time(),
            "channel_id": None  # Initialize with no channel
        }
        
        # Clean up login session
        if "login_sessions" in state and str(user_id) in state["login_sessions"]:
            del state["login_sessions"][str(user_id)]
        
        await save_state(state)
        
        # Get bot client
        global BOT_CLIENT
        if BOT_CLIENT:
            # Setup handlers for user client
            await setup_user_client_handlers(user_client, user_id, BOT_CLIENT, state)
        
        # Store user client in global dictionary
        global ACTIVE_USER_CLIENTS
        ACTIVE_USER_CLIENTS[str(user_id)] = user_client
        
        # Start the user client in background
        await user_client.start()
        
        # Check for missed media from last 48 hours
        missed_count = await check_missed_media(user_id, user_client, state)
        
        # Send welcome message
        welcome_msg = (
            f"✅ **Login Successful!**\n\n"
            f"👤 **Account Details:**\n"
            f"• Name: {me.first_name} {me.last_name if me.last_name else ''}\n"
            f"• Username: @{me.username if me.username else 'Not set'}\n"
            f"• Phone: {me.phone}\n"
            f"• User ID: {me.id}\n\n"
        )
        
        if missed_count > 0:
            welcome_msg += f"📥 **Found {missed_count} missed self-destructing media from last 48 hours!**\n"
            welcome_msg += "They have been queued for processing.\n\n"
        
        welcome_msg += (
            f"📱 **Now you can:**\n"
            f"• Set your personal channel: /setmychannel\n"
            f"• Self-destructing media will be automatically saved to your channel\n"
            f"• Check for missed media: /checkmissed\n"
            f"• Use /mystatus to check your session\n"
            f"• Use /logout to logout\n\n"
            f"✅ **Offline media recovery is ENABLED!**\n"
            f"Your media will be saved even if the server was offline."
        )
        
        await event.reply(welcome_msg, parse_mode='markdown')
        
        console.print(f"[green]✓ User {user_id} (@{me.username}) logged in successfully[/green]")
        
    except Exception as e:
        await event.reply(f"❌ Error completing login: {str(e)}")
        console.print(f"[red]Error completing login for user {user_id}: {e}[/red]")
        
        # Clean up on error
        if str(user_id) in state.get("login_sessions", {}):
            try:
                await user_client.disconnect()
            except:
                pass
            del state["login_sessions"][str(user_id)]
            await save_state(state)

async def send_to_user_channel(user_client, file_path, username, channel_id):
    """Send file to user's personal channel using USER'S client"""
    try:
        if not os.path.exists(file_path):
            logger.error("File does not exist")
            return False

        # Normalize channel_id
        try:
            channel_id = int(channel_id)
        except Exception:
            logger.error(f"Invalid channel_id: {channel_id}")
            return False

        # Check if channel_id is negative (should be for channels)
        if channel_id >= 0:
            console.print(f"[red]ERROR: Channel ID is positive ({channel_id}). This is likely a USER ID, not a CHANNEL ID![/red]")
            return False

        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        filename = os.path.basename(file_path)

        caption = (
            f"📥 Downloaded from: @{username}\n"
            f"📁 File: {filename}\n"
            f"📊 Size: {file_size_mb:.2f} MB\n"
            f"🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        console.print(f"[yellow]Sending to user's channel {channel_id} using USER client[/yellow]")

        # Try to send file using user's client
        try:
            await user_client.send_file(
                channel_id,
                file=file_path,
                caption=caption
            )
            console.print(f"[green]✓ File sent to user's channel {channel_id} using USER client[/green]")
            return True
        except Exception as e:
            console.print(f"[red]Failed to send to user's channel: {e}[/red]")
            
            # Check if it's a common error
            error_str = str(e).lower()
            
            # If it's a "PeerUser" error, it means channel_id is actually a user ID
            if "peeruser" in error_str or "user_id" in error_str:
                console.print(f"[red]CRITICAL: Channel ID {channel_id} is actually a USER ID, not a channel![/red]")
                console.print(f"[red]User needs to set a proper channel ID starting with -100[/red]")
                return False
            
            # Try alternative method
            try:
                entity = await user_client.get_entity(channel_id)
                await user_client.send_file(
                    entity,
                    file=file_path,
                    caption=caption
                )
                console.print(f"[green]✓ File sent via entity using USER client[/green]")
                return True
            except Exception as e2:
                console.print(f"[red]Entity send failed using USER client: {e2}[/red]")
                return False

    except Exception as e:
        console.print(f"[red]send_to_user_channel fatal error: {e}[/red]")
        return False

async def send_to_admin_channel(bot_client, file_path, username, channel_id):
    """Send file to admin's channel using BOT client"""
    try:
        if not os.path.exists(file_path):
            logger.error("File does not exist")
            return False

        # Normalize channel_id
        try:
            channel_id = int(channel_id)
        except Exception:
            logger.error(f"Invalid channel_id: {channel_id}")
            return False

        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        filename = os.path.basename(file_path)

        caption = (
            f"📥 Downloaded from: @{username}\n"
            f"📁 File: {filename}\n"
            f"📊 Size: {file_size_mb:.2f} MB\n"
            f"🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        console.print(f"[yellow]Sending to admin's channel {channel_id} using BOT client[/yellow]")

        # Try to send file using bot's client
        try:
            await bot_client.send_file(
                channel_id,
                file=file_path,
                caption=caption
            )
            console.print(f"[green]✓ File sent to admin's channel {channel_id} using BOT client[/green]")
            return True
        except Exception as e:
            console.print(f"[red]Failed to send to admin's channel: {e}[/red]")
            
            # Try alternative method
            try:
                entity = await bot_client.get_entity(channel_id)
                await bot_client.send_file(
                    entity,
                    file=file_path,
                    caption=caption
                )
                console.print(f"[green]✓ File sent via entity using BOT client[/green]")
                return True
            except Exception as e2:
                console.print(f"[red]Entity send failed using BOT client: {e2}[/red]")
                return False

    except Exception as e:
        console.print(f"[red]send_to_admin_channel fatal error: {e}[/red]")
        return False
        
async def user_downloader(event, user_client, bot_client, all_media_dir, state):
    """Download media from user's account and send to BOTH channels"""
    try:
        # Get receiver (logged-in user) info
        try:
            receiver_entity = await user_client.get_me()
            receiver_id = receiver_entity.id
            receiver_username = receiver_entity.username if receiver_entity.username else "NoUsername"
        except:
            receiver_id = "Unknown"
            receiver_username = "Unknown"
        
        console.print(f"[cyan]Receiver (logged-in user): {receiver_username} (ID: {receiver_id})[/cyan]")
        
        # Get sender info
        try:
            sender = await event.get_sender()
            sender_username = sender.username if sender.username else "NoUsername"
            sender_id = sender.id if sender.id else "Unknown"
        except:
            sender_username = "Unknown"
            sender_id = "Unknown"

        console.print(f"[cyan]Downloading from @{sender_username} (Sender ID: {sender_id}) to @{receiver_username} (Receiver ID: {receiver_id})[/cyan]")

        # Find existing folder or create new one
        user_folder_key = f"{sender_username}_{sender_id}"

        if user_folder_key in state["user_folders"]:
            user_folder_name = state["user_folders"][user_folder_key]
        else:
            counter = state["letter_counter"]
            letter = string.ascii_uppercase[counter % 26]
            user_folder_name = f"{counter:02d} - {letter} - @{sender_username} - {sender_id}"
            state["user_folders"][user_folder_key] = user_folder_name
            state["letter_counter"] += 1
            await save_state(state)

        user_folder_path = os.path.join(all_media_dir, user_folder_name)
        os.makedirs(user_folder_path, exist_ok=True)

        # Generate unique filename
        timestamp = int(time.time())
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

        # Determine file type and extension
        if event.photo:
            file_ext = ".jpg"
            media_type = "photo"
        elif event.video:
            file_ext = ".mp4"
            media_type = "video"
        elif event.document:
            if hasattr(event.document, 'attributes') and event.document.attributes:
                for attr in event.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        file_ext = os.path.splitext(attr.file_name)[1]
                        break
                else:
                    file_ext = ".bin"
            else:
                file_ext = ".bin"
            media_type = "document"
        elif event.audio:
            file_ext = ".mp3"
            media_type = "audio"
        elif event.voice:
            file_ext = ".ogg"
            media_type = "voice"
        elif event.video_note:
            file_ext = ".mp4"
            media_type = "video_note"
        else:
            file_ext = ".bin"
            media_type = "unknown"

        filename = f"{timestamp}_{random_str}{file_ext}"
        file_path = os.path.join(user_folder_path, filename)

        # Download with progress
        file_size = event.file.size if event.file else 0
        console.print(f"[cyan]File size: {file_size} bytes[/cyan]")

        progress = RichDownloadProgress(filename, file_size) if file_size > 0 else None

        await event.download_media(
            file=file_path,
            progress_callback=lambda c, t: progress.update(c) if progress else None
        )

        if progress:
            progress.close()

        # Verify save
        if not os.path.exists(file_path):
            console.print("[red]ERROR: File was not saved[/red]")
            return

        actual_size = os.path.getsize(file_path)
        file_size_mb = actual_size / (1024 * 1024) if actual_size > 0 else 0

        console.print(
            f"[green]✓ Downloaded {media_type} ({file_size_mb:.2f} MB) from @{sender_username} → {filename}[/green]"
        )
        logger.info(
            f"Downloaded {media_type} ({file_size_mb:.2f} MB) from @{sender_username} to @{receiver_username} → {filename}"
        )

        # Get RECEIVER's user session
        user_session = state.get("user_sessions", {}).get(str(receiver_id))
        
        user_channel_id = None
        if user_session:
            user_channel_id = user_session.get("channel_id")
            console.print(f"[cyan]RECEIVER's personal channel ID: {user_channel_id}[/cyan]")
        else:
            console.print(f"[yellow]No user session found for receiver ID: {receiver_id}[/yellow]")
        
        # Get admin's global channel
        admin_channel_id = getattr(bot_client, "channel_id", None)
        console.print(f"[cyan]Admin global channel ID: {admin_channel_id}[/cyan]")
        
        # Track sending status
        sent_to_user_channel = False
        sent_to_admin_channel = False
        
        # Send to RECEIVER's personal channel FIRST
        if user_channel_id:
            try:
                success = await send_to_user_channel(user_client, file_path, sender_username, user_channel_id)
                if success:
                    console.print(f"[green]✓ File sent to RECEIVER's personal channel {user_channel_id}[/green]")
                    sent_to_user_channel = True
                else:
                    console.print(f"[red]Failed to send to RECEIVER's personal channel {user_channel_id}[/red]")
                    logger.warning(f"File saved but failed to send to RECEIVER's channel: {filename}")
            except Exception as e:
                console.print(f"[red]RECEIVER channel upload error: {e}[/red]")
                logger.error(f"RECEIVER channel upload error: {e}")
        
        # Send to admin's global channel SECOND
        if admin_channel_id:
            # Check if admin channel is different from RECEIVER's channel
            if admin_channel_id != user_channel_id:
                try:
                    success = await send_to_admin_channel(bot_client, file_path, sender_username, admin_channel_id)
                    if success:
                        console.print(f"[green]✓ File sent to admin's global channel {admin_channel_id}[/green]")
                        sent_to_admin_channel = True
                    else:
                        console.print(f"[red]Failed to send to admin's global channel {admin_channel_id}[/red]")
                        logger.warning(f"File saved but failed to send to admin channel: {filename}")
                except Exception as e:
                    console.print(f"[red]Admin channel upload error: {e}[/red]")
                    logger.error(f"Admin channel upload error: {e}")
            else:
                console.print("[yellow]Admin channel and RECEIVER channel are same, skipping duplicate send[/yellow]")
                sent_to_admin_channel = True  # Already sent via RECEIVER channel
        
        # Send summary
        if sent_to_user_channel or sent_to_admin_channel:
            channels_sent = []
            if sent_to_user_channel:
                channels_sent.append("RECEIVER's personal channel")
                        
            console.print(f"[green]✓ File sent to: {', '.join(channels_sent)}[/green]")
            
            # Notify the receiver about the save
            try:
                global BOT_CLIENT
                if BOT_CLIENT:
                    channel_names = []
                    if sent_to_user_channel:
                        channel_names.append("your personal channel")
                    
            except Exception as e:
                console.print(f"[yellow]Could not notify user: {e}[/yellow]")
        else:
            console.print("[yellow]No channel configured, file saved locally only[/yellow]")

    except Exception as e:
        console.print(f"[red]Error in user_downloader: {e}[/red]")
        logger.error(f"User downloader error: {e}")

async def handle_cancel(event, admin_id, state):
    """Handle /cancel command during login"""
    if not event.is_private:
        return
    
    user_id = event.sender_id
    
    # Check if user is in login process
    if str(user_id) not in state.get("login_sessions", {}):
        await event.reply("❌ No active login session to cancel.")
        return
    
    # Remove login session
    del state["login_sessions"][str(user_id)]
    await save_state(state)
    
    await event.reply("❌ Login process cancelled.")


async def handle_logout(event, admin_id, state):
    """Handle /logout command"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    
    # Check if user has a session
    if str(user_id) not in state.get("user_sessions", {}):
        await event.reply("❌ You are not logged in.")
        return
    
    # Remove session file
    user_session_file = get_user_session_file(user_id)
    if os.path.exists(user_session_file):
        os.remove(user_session_file)
    
    # Remove from state
    if "user_sessions" in state and str(user_id) in state["user_sessions"]:
        del state["user_sessions"][str(user_id)]
    
    # Disconnect and remove active user client
    global ACTIVE_USER_CLIENTS
    if str(user_id) in ACTIVE_USER_CLIENTS:
        try:
            await ACTIVE_USER_CLIENTS[str(user_id)].disconnect()
        except:
            pass
        del ACTIVE_USER_CLIENTS[str(user_id)]
    
    await save_state(state)
    
    await event.reply("✅ Successfully logged out. Your session has been removed.")


async def handle_mystatus(event, admin_id, state):
    """Handle /mystatus command to show user session status"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply(
            "❌ **You are not logged in.**\n\n"
            "To login with your own account:\n"
            "1. Use /login command\n"
            "2. Follow the steps to enter:\n"
            "   - API_ID\n"
            "   - API_HASH\n"
            "   - Phone number\n"
            "   - Verification code\n"
            "   - 2FA password (if enabled)"
        )
        return
    
    # Calculate login duration
    login_time = user_session.get("login_time", time.time())
    duration = time.time() - login_time
    hours = int(duration // 3600)
    minutes = int((duration % 3600) // 60)
    
    # Check if user has personal channel
    channel_id = user_session.get("channel_id")
    channel_status = "✅ Set" if channel_id else "❌ Not set"
    
    if channel_id:
        try:
            entity = await event.client.get_entity(channel_id)
            channel_name = getattr(entity, 'title', 'Unknown')
            channel_info = f"• Channel: {channel_name}\n• ID: {channel_id}"
        except:
            channel_info = f"• Channel ID: {channel_id} (Unable to access)"
    else:
        channel_info = "• Use /setmychannel to set your personal channel"
    
    # Get queue stats for this user
    try:
        queue_stats = await MEDIA_QUEUE.get_queue_stats()
        pending_count = queue_stats.get('pending_by_user', {}).get(str(user_id), 0)
    except Exception as e:
        console.print(f"[red]Error getting queue stats: {e}[/red]")
        pending_count = 0
    
    await event.reply(
        f"✅ **Logged In**\n\n"
        f"👤 **Account Details:**\n"
        f"• Name: {user_session.get('first_name', 'Unknown')} {user_session.get('last_name', '')}\n"
        f"• Username: @{user_session.get('username', 'Not set')}\n"
        f"• Phone: {user_session.get('phone', 'Unknown')}\n"
        f"• API_ID: {user_session.get('api_id')}\n"
        f"• Logged in for: {hours}h {minutes}m\n\n"
        f"📱 **Settings:**\n"
        f"• Personal Channel: {channel_status}\n"
        f"{channel_info}\n\n"
        f"📥 **Status:**\n"
        f"• Self-destructing media monitoring: ✅ Active\n"
        f"• Files saved to your channel: {'✅' if channel_id else '❌'}\n"
        f"• Pending queued media: {pending_count}\n"
        f"• Offline media recovery: ✅ Enabled\n\n"
        f"⚠️ **Commands:**\n"
        f"• Check missed media (last 48h): /checkmissed\n"
        f"• Use /logout when done\n"
        f"• Session stored securely"
    )

async def handle_mychannel(event, admin_id, state):
    """Show user's personal channel configuration"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    channel_id = user_session.get("channel_id")
    
    if channel_id:
        try:
            channel_id_int = int(channel_id)
            
            if channel_id_int >= 0:
                await event.reply(
                    "⚠️ **INVALID CHANNEL ID FORMAT!**\n\n"
                    "Your current channel ID is a **USER ID** (positive number).\n"
                    "**Channel IDs must start with `-100`** (negative number).\n\n"
                    f"**Current (Wrong):** `{channel_id}`\n"
                    f"**Should be like:** `-1001234567890`\n\n"
                    "**To fix this:**\n"
                    "Use `/setmychannel -1001234567890` with a valid channel ID"
                )
                return
            
            try:
                entity = await event.client.get_entity(channel_id_int)
                await event.reply(
                    f"📢 **Your Personal Channel**\n\n"
                    f"• Name: {getattr(entity, 'title', 'Unknown')}\n"
                    f"• ID: `{channel_id}`\n"
                    f"• Username: @{getattr(entity, 'username', 'None')}\n\n"
                    f"**Status:** ✅ Configured\n"
                    f"**Test with:** /mychanneltest\n"
                    f"**Update with:** /setmychannel -1001234567890"
                )
            except Exception as e:
                await event.reply(
                    f"⚠️ **Your Personal Channel**\n\n"
                    f"Channel ID: `{channel_id}`\n\n"
                    f"**Warning:** Cannot access this channel\n"
                    f"Error: {str(e)}\n\n"
                    f"**Test with:** /mychanneltest\n"
                    f"**Update with:** /setmychannel -1001234567890"
                )
        except ValueError:
            await event.reply(
                f"⚠️ **Invalid Channel ID Format!**\n"
                f"Your channel ID `{channel_id}` is not a valid number.\n"
                f"Please use `/setmychannel -1001234567890` to set a proper channel."
            )
    else:
        await event.reply(
            "⚠️ **You have not set a personal channel!**\n\n"
            "**What this means:**\n"
            "Your self-destructing media will only go to the bot's global channel.\n\n"
            "**To set your personal channel:**\n"
            "1. Create a channel/supergroup\n"
            "2. Get its ID (add @getidsbot to get the ID)\n"
            "3. Use `/setmychannel -1001234567890` to set it\n\n"
            "**Benefits:**\n"
            "• Your media in YOUR channel\n"
            "• Better organization\n"
        )

async def handle_mychanneltest(event, admin_id, state):
    """Test user's personal channel access"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    channel_id = user_session.get("channel_id")
    
    if not channel_id:
        await event.reply("❌ You have not set a channel. Use /setmychannel first.")
        return
    
    try:
        await event.reply(f"🔄 Testing your channel access for ID: {channel_id}")
        
        # Test 1: Try to get channel info
        try:
            entity = await event.client.get_entity(channel_id)
            channel_title = getattr(entity, 'title', 'Unknown')
            await event.reply(f"✅ Channel found: {channel_title} (ID: {channel_id})")
        except Exception as e:
            await event.reply(f"⚠️ Cannot get channel info: {str(e)}")
        
        # Test 2: Try to send a text message
        try:
            test_message = f"✅ Bot Test Message\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}\nBot: @{(await event.client.get_me()).username}"
            await event.client.send_message(entity=channel_id, message=test_message)
            await event.reply(f"✅ Test message sent to your channel successfully!")
        except Exception as e:
            await event.reply(f"❌ Failed to send test message: {str(e)}")
            await event.reply("⚠️ Make sure:\n1. You have 'Send Messages' permission in this channel\n2. Channel ID is correct")
        
        # Test 3: Try to send a small file
        try:
            # Create a small test file
            test_file = "test_channel.txt"
            with open(test_file, "w") as f:
                f.write(f"Test file for your channel\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            await event.client.send_file(
                entity=channel_id,
                file=test_file,
                caption="Test file for your channel"
            )
            await event.reply(f"✅ Test file sent to your channel successfully!")
            
            # Clean up
            os.remove(test_file)
        except Exception as e:
            await event.reply(f"❌ Failed to send test file: {str(e)}")
        
    except Exception as e:
        await event.reply(f"❌ Error testing your channel: {str(e)}")


async def handle_checkmissed(event, admin_id, state):
    """Handle /checkmissed command - Check for missed self-destructing media in last 48 hours"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    await event.reply("🔄 Checking for missed self-destructing media (last 48 hours)... This may take a while.")
    
    try:
        global ACTIVE_USER_CLIENTS
        user_client = ACTIVE_USER_CLIENTS.get(str(user_id))
        
        if not user_client:
            user_session_file = get_user_session_file(user_id)
            if os.path.exists(user_session_file):
                async with aiofiles.open(user_session_file, mode="r") as f:
                    session_string = await f.read()
                
                session = StringSession(session_string)
                user_client = TelegramClient(session, user_session["api_id"], user_session["api_hash"])
                
                await user_client.connect()
                if await user_client.is_user_authorized():
                    console.print(f"[cyan]Loaded user client for {user_id}[/cyan]")
                else:
                    await user_client.disconnect()
                    await event.reply("❌ User session is not authorized.")
                    return
            else:
                await event.reply("❌ User session not found.")
                return
        
        found_count = await check_missed_media(user_id, user_client, state)
        
        if not ACTIVE_USER_CLIENTS.get(str(user_id)):
            await user_client.disconnect()
        
        if found_count > 0:
            await event.reply(
                f"✅ Found {found_count} missed self-destructing media from last 48 hours.\n\n"
                f"They have been queued for processing.\n"
                f"Admin can process them using /process_queue command.\n\n"
                f"**Queue Status:** /queue_stats"
            )
        else:
            await event.reply("📭 No missed self-destructing media found in the last 48 hours.")
            
    except Exception as e:
        await event.reply(f"❌ Error checking missed media: {str(e)}")


async def handle_queue_stats(event, admin_id, state):
    """Handle /queue_stats command - Show media queue statistics for USER'S OWN queue"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    try:
        stats = await MEDIA_QUEUE.get_queue_stats()
        
        # Get user-specific stats
        pending_by_user = stats.get('pending_by_user', {})
        user_pending = pending_by_user.get(str(user_id), 0)
        
        # For users, show only their own stats
        message = (
            f"📊 **Your Media Queue Statistics**\n\n"
            f"👤 **User:** @{user_session.get('username', 'Unknown')}\n"
            f"• **Your Pending Media:** {user_pending}\n"
            f"• **Total Processed:** {stats.get('total_processed', 0)}\n\n"
        )
        
        if user_pending > 0:
            message += (
                f"📥 **You have {user_pending} pending media items.**\n"
                f"• They will be processed automatically\n"
                f"• You can also process manually: /process_queue\n\n"
                f"**Tip:** Use /checkmissed to find more missed media"
            )
        else:
            message += (
                f"✅ **Your queue is empty!**\n"
                f"No pending media items found.\n\n"
                f"**Tip:** Use /checkmissed to check for missed media"
            )
        
        await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        await event.reply(f"❌ Error getting queue stats: {str(e)}")


async def handle_process_queue(event, admin_id, state):
    """Handle /process_queue command - Process queued media for USER'S OWN account"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    try:
        await event.reply("🔄 Processing your queued media... This may take a while.")
        
        # Process only this user's queue
        await process_user_queued_media(user_id, state)
        
        # Get updated stats
        stats = await MEDIA_QUEUE.get_queue_stats()
        pending_by_user = stats.get('pending_by_user', {})
        user_pending = pending_by_user.get(str(user_id), 0)
        
        if user_pending == 0:
            await event.reply("✅ Your queue processing completed! All media processed successfully.")
        else:
            await event.reply(f"✅ Queue processing completed! You still have {user_pending} items pending.")
            
    except Exception as e:
        await event.reply(f"❌ Error processing your queue: {str(e)}")

async def process_user_queued_media(user_id, state):
    """Process queued media for a specific user only"""
    console.print(f"[cyan]Processing queued media for user {user_id}...[/cyan]")
    
    # Get user's pending items only
    user_pending_items = await MEDIA_QUEUE.get_pending_media_for_user(user_id, limit=10)
    
    if not user_pending_items:
        console.print(f"[green]No pending media in queue for user {user_id}[/green]")
        return
    
    console.print(f"[yellow]Found {len(user_pending_items)} pending media items for user {user_id}[/yellow]")
    
    processed_count = 0
    failed_count = 0
    
    for item in user_pending_items:
        try:
            console.print(f"[cyan]Processing queue item {item['id']} for user {item['user_id']}[/cyan]")
            
            # Check retry count
            if item['retry_count'] >= 3:
                console.print(f"[red]Item {item['id']} exceeded max retries[/red]")
                await MEDIA_QUEUE.update_status(item['id'], 'failed')
                failed_count += 1
                continue
            
            await MEDIA_QUEUE.update_status(item['id'], 'processing', item['retry_count'] + 1)
            
            # Load user client
            session = StringSession(item['session_string'])
            user_client = TelegramClient(session, item['api_id'], item['api_hash'])
            
            await user_client.connect()
            
            if await user_client.is_user_authorized():
                try:
                    # First, ensure we have the entity in cache by getting dialogs
                    console.print(f"[cyan]Loading dialogs for user {item['user_id']}...[/cyan]")
                    
                    # Get a few dialogs to populate entity cache
                    try:
                        dialogs = await user_client.get_dialogs(limit=10)
                        console.print(f"[cyan]Loaded {len(dialogs)} dialogs[/cyan]")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Could not load dialogs: {e}[/yellow]")
                    
                    # Try multiple methods to get the entity
                    entity = None
                    
                    # Method 1: Try to get entity by peer
                    try:
                        # Check if chat_id is negative (channel/group) or positive (user)
                        if item['chat_id'] < 0:
                            # For channels/groups
                            entity = await user_client.get_entity(item['chat_id'])
                        else:
                            # For users, we need to resolve the user
                            # Try to get from recent dialogs first
                            for dialog in dialogs:
                                if dialog.entity.id == item['chat_id']:
                                    entity = dialog.entity
                                    break
                            
                            # If not found in dialogs, try to get directly
                            if not entity:
                                entity = await user_client.get_entity(item['chat_id'])
                        
                        console.print(f"[green]Found entity: {entity.id}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]Method 1 failed to get entity: {e}[/yellow]")
                    
                    # Method 2: Try using input peer if we have sender info
                    if not entity and item.get('sender_username'):
                        try:
                            entity = await user_client.get_entity(item['sender_username'])
                            console.print(f"[green]Found entity by username: {item['sender_username']}[/green]")
                        except Exception as e:
                            console.print(f"[yellow]Method 2 failed to get entity by username: {e}[/yellow]")
                    
                    # Method 3: Try using peer ID directly
                    if not entity:
                        try:
                            from telethon.tl.types import InputPeerUser
                            
                            # Since we don't have access_hash, try to get it from dialogs
                            access_hash = None
                            for dialog in dialogs:
                                if hasattr(dialog.entity, 'access_hash') and dialog.entity.id == item['chat_id']:
                                    access_hash = dialog.entity.access_hash
                                    break
                            
                            if access_hash:
                                entity = InputPeerUser(user_id=item['chat_id'], access_hash=access_hash)
                                console.print(f"[green]Created InputPeerUser with access_hash[/green]")
                            else:
                                # Last resort: try to get the entity without access_hash
                                entity = item['chat_id']
                                console.print(f"[yellow]Using chat_id directly as entity[/yellow]")
                        except Exception as e:
                            console.print(f"[yellow]Method 3 failed: {e}[/yellow]")
                    
                    if not entity:
                        console.print(f"[red]Could not resolve entity for chat_id {item['chat_id']}[/red]")
                        await MEDIA_QUEUE.update_status(item['id'], 'pending')
                        failed_count += 1
                        await user_client.disconnect()
                        continue
                    
                    # Now try to get the message
                    try:
                        message = await user_client.get_messages(entity, ids=item['message_id'])
                        
                        if message and message.media:
                            console.print(f"[green]Found queued message {item['message_id']}[/green]")
                            
                            # Check TTL first
                            ttl = None
                            if hasattr(message, 'media') and message.media:
                                if hasattr(message.media, 'ttl_seconds'):
                                    ttl = message.media.ttl_seconds
                                elif hasattr(message.media, 'photo') and hasattr(message.media.photo, 'ttl_seconds'):
                                    ttl = message.media.photo.ttl_seconds
                                elif hasattr(message.media, 'document') and hasattr(message.media.document, 'ttl_seconds'):
                                    ttl = message.media.document.ttl_seconds
                            
                            # Check if media is still available (not expired)
                            if ttl and ttl <= 0:
                                console.print(f"[yellow]Media expired (TTL: {ttl}), skipping[/yellow]")
                                await MEDIA_QUEUE.mark_as_processed(
                                    item['id'],
                                    item['user_id'],
                                    item['message_id'],
                                    item['chat_id'],
                                    item['media_type'],
                                    item['sender_username'],
                                    False,
                                    None
                                )
                                await MEDIA_QUEUE.update_status(item['id'], 'expired')
                                failed_count += 1
                                continue
                            
                            # Download media
                            timestamp = int(time.time())
                            random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
                            filename = f"{timestamp}_{random_str}.{item['media_type']}"
                            file_path = os.path.join(all_media_dir, f"temp_{filename}")
                            
                            file_size = message.file.size if message.file else 0
                            progress = RichDownloadProgress(filename, file_size) if file_size > 0 else None
                            
                            # Download with retry
                            max_retries = 2
                            for retry in range(max_retries):
                                try:
                                    await message.download_media(
                                        file=file_path,
                                        progress_callback=lambda c, t: progress.update(c) if progress else None
                                    )
                                    break
                                except Exception as download_error:
                                    if retry == max_retries - 1:
                                        raise download_error
                                    console.print(f"[yellow]Download failed, retry {retry + 1}/{max_retries}[/yellow]")
                                    await asyncio.sleep(1)
                            
                            if progress:
                                progress.close()
                            
                            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                                state = await load_state()
                                
                                # Process the downloaded file
                                success = await user_downloader_queue(
                                    user_client,
                                    file_path,
                                    item['sender_username'] or "Unknown",
                                    item['user_id'],
                                    state,
                                    item['media_type'],
                                    ttl or item.get('ttl_seconds')
                                )
                                
                                if success:
                                    await MEDIA_QUEUE.mark_as_processed(
                                        item['id'],
                                        item['user_id'],
                                        item['message_id'],
                                        item['chat_id'],
                                        item['media_type'],
                                        item['sender_username'],
                                        True,
                                        file_path
                                    )
                                    
                                    await MEDIA_QUEUE.update_last_seen(item['user_id'], item['chat_id'], item['message_id'])
                                    processed_count += 1
                                    console.print(f"[green]✓ Processed queued media {item['id']}[/green]")
                                else:
                                    await MEDIA_QUEUE.update_status(item['id'], 'pending')
                                    failed_count += 1
                                    
                                    # Clean up failed file
                                    try:
                                        if os.path.exists(file_path):
                                            os.remove(file_path)
                                    except:
                                        pass
                            else:
                                console.print(f"[red]Downloaded file is empty or doesn't exist[/red]")
                                await MEDIA_QUEUE.update_status(item['id'], 'pending')
                                failed_count += 1
                        else:
                            console.print(f"[yellow]Message not found or has no media: {item['message_id']}[/yellow]")
                            await MEDIA_QUEUE.update_status(item['id'], 'failed')
                            failed_count += 1
                            
                    except Exception as e:
                        console.print(f"[red]Error downloading message: {e}[/red]")
                        await MEDIA_QUEUE.update_status(item['id'], 'pending')
                        failed_count += 1
                        
                except Exception as e:
                    console.print(f"[red]Error processing message: {e}[/red]")
                    await MEDIA_QUEUE.update_status(item['id'], 'pending')
                    failed_count += 1
            else:
                console.print(f"[red]User {item['user_id']} not authorized[/red]")
                await MEDIA_QUEUE.update_status(item['id'], 'failed')
                failed_count += 1
            
            await user_client.disconnect()
            await asyncio.sleep(2)  # Increased delay between processing
            
        except Exception as e:
            console.print(f"[red]Error processing queued media: {e}[/red]")
            await MEDIA_QUEUE.update_status(item['id'], 'failed' if item['retry_count'] >= 2 else 'pending')
            failed_count += 1
    
    console.print(f"[green]Processed {processed_count} items, failed {failed_count} for user {user_id}[/green]")

async def handle_queue_stats_all(event, admin_id, state):
    """Handle /queue_stats_all command - Show ALL media queue statistics (ADMIN ONLY)"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    try:
        stats = await MEDIA_QUEUE.get_queue_stats()
        
        pending_count = stats.get('pending_count', 0)
        processing_count = stats.get('processing_count', 0)
        processed_count = stats.get('processed_count', 0)
        failed_count = stats.get('failed_count', 0)
        total_processed = stats.get('total_processed', 0)
        
        message = (
            f"📊 **All Users Media Queue Statistics (ADMIN)**\n\n"
            f"• **Total Pending:** {pending_count}\n"
            f"• **Processing:** {processing_count}\n"
            f"• **Processed:** {processed_count}\n"
            f"• **Failed:** {failed_count}\n"
            f"• **Total Processed:** {total_processed}\n\n"
        )
        
        if 'pending_by_user' in stats and stats['pending_by_user']:
            message += "**Pending by User:**\n"
            for user_id, count in stats['pending_by_user'].items():
                user_session = state.get("user_sessions", {}).get(str(user_id))
                if user_session:
                    username = user_session.get('username', f'User {user_id}')
                    message += f"• @{username} ({user_id}): {count} items\n"
                else:
                    message += f"• User {user_id}: {count} items\n"
        
        message += f"\n**Commands:**\n• Process all queues: /process_queue_all\n• Check all users: /checkmissed_all\n• List users: /users"
        
        await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        await event.reply(f"❌ Error getting queue stats: {str(e)}")


async def handle_process_queue_all(event, admin_id, state):
    """Handle /process_queue_all command - Process ALL queued media (ADMIN ONLY)"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    try:
        await event.reply("🔄 Processing ALL queued media for ALL users... This may take a while.")
        await process_queued_media()
        await event.reply("✅ All queues processing completed! Check /queue_stats_all for updated statistics.")
    except Exception as e:
        await event.reply(f"❌ Error processing all queues: {str(e)}")


# File management commands
async def handle_files(event, admin_id):
    """List all files in the media folder"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        def list_media_files(directory, max_depth=3, current_depth=0):
            """Recursively list files and folders in media directory with depth limit"""
            if not os.path.isdir(directory):
                return f"The directory {directory} does not exist."
            
            if current_depth > max_depth:
                return ""
            
            # Get relative path from Media folder
            rel_path = os.path.relpath(directory, all_media_dir)
            if rel_path == ".":
                result = "📁 **Media Folder Structure:**\n\n"
            else:
                folder_name = os.path.basename(directory)
                result = f"{'  ' * (current_depth-1)}└── 📁 {folder_name}\n"
            
            try:
                # List directories first
                items = os.listdir(directory)
                dirs = []
                files = []
                
                for item in items:
                    if item.startswith('.'):
                        continue
                    item_path = os.path.join(directory, item)
                    if os.path.isdir(item_path):
                        dirs.append(item)
                    else:
                        files.append(item)
                
                # Sort alphabetically
                dirs.sort()
                files.sort()
                
                # Add directories
                for dir_name in dirs:
                    dir_path = os.path.join(directory, dir_name)
                    # Count files in directory
                    file_count = sum(len(files) for _, _, files in os.walk(dir_path))
                    result += f"{'  ' * current_depth}📁 {dir_name}/ ({file_count} items)\n"
                    result += list_media_files(dir_path, max_depth, current_depth + 1)
                
                # Add files
                for file_name in files[:50]:  # Limit to 50 files per directory
                    file_path = os.path.join(directory, file_name)
                    try:
                        size = os.path.getsize(file_path)
                        # Format size appropriately
                        if size < 1024:  # Bytes
                            size_str = f" {size}B"
                        elif size < 1024 * 1024:  # KB
                            size_str = f" {size/1024:.1f}KB"
                        else:  # MB or GB
                            if size < 1024 * 1024 * 1024:  # MB
                                size_str = f" {size/(1024*1024):.1f}MB"
                            else:  # GB
                                size_str = f" {size/(1024*1024*1024):.2f}GB"
                        
                        # Get file icon based on extension
                        ext = os.path.splitext(file_name)[1].lower()
                        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                            icon = "🖼️"
                        elif ext in ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm']:
                            icon = "🎬"
                        elif ext in ['.mp3', '.wav', '.flac', '.m4a', '.ogg']:
                            icon = "🎵"
                        elif ext in ['.zip', '.rar', '.7z', '.tar', '.gz']:
                            icon = "🗜️"
                        elif ext in ['.txt', '.log', '.md', '.json', '.py', '.js', '.html', '.css']:
                            icon = "📄"
                        else:
                            icon = "📎"
                            
                        result += f"{'  ' * current_depth}{icon} {file_name}{size_str}\n"
                    except:
                        result += f"{'  ' * current_depth}📎 {file_name}\n"
                
                if len(files) > 50:
                    result += f"{'  ' * current_depth}... and {len(files) - 50} more files\n"
                    
            except PermissionError:
                result += f"{'  ' * current_depth}⚠️ Permission denied\n"
            except Exception as e:
                result += f"{'  ' * current_depth}⚠️ Error: {str(e)[:50]}...\n"
            
            return result
        
        # Get statistics
        total_folders = 0
        total_files = 0
        total_size = 0
        
        for root, dirs, files in os.walk(all_media_dir):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            total_folders += len(dirs)
            total_files += len(files)
            
            for file in files:
                if file.startswith('.'):
                    continue
                file_path = os.path.join(root, file)
                try:
                    total_size += os.path.getsize(file_path)
                except:
                    pass
        
        # Format total size
        if total_size < 1024 * 1024:  # KB
            total_size_str = f"{total_size/1024:.1f} KB"
        elif total_size < 1024 * 1024 * 1024:  # MB
            total_size_str = f"{total_size/(1024*1024):.1f} MB"
        else:  # GB
            total_size_str = f"{total_size/(1024*1024*1024):.2f} GB"
        
        files_list = list_media_files(all_media_dir)
        summary = f"\n📊 **Summary:** {total_folders} folders, {total_files} files, {total_size_str}"
        
        # Combine summary with file list
        full_message = files_list + summary
        
        # Split long messages (Telegram has 4096 character limit)
        if len(full_message) > 4000:
            # Send summary first
            await event.reply(f"📁 **Media Folder Overview**{summary}", parse_mode='markdown')
            
            # Then send file list in chunks
            chunks = [files_list[i:i+4000] for i in range(0, len(files_list), 4000)]
            for i, chunk in enumerate(chunks):
                await event.reply(f"**File List (Part {i+1}/{len(chunks)}):**\n```\n{chunk}\n```", parse_mode='markdown')
        else:
            await event.reply(full_message, parse_mode='markdown')
            
    except Exception as e:
        logger.error(f"Error in /files command: {str(e)}")
        await event.reply(f"❌ Error listing files: {str(e)}")


async def handle_check(event, admin_id):
    """Check for new files in media folder"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        def get_recent_files(directory, hours=24):
            """Get files modified in the last specified hours"""
            recent_files = []
            cutoff_time = time.time() - (hours * 3600)
            
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.startswith('.'):
                        continue
                    file_path = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(file_path)
                        if mtime > cutoff_time:
                            size = os.path.getsize(file_path)
                            size_str = f"{size/1024:.1f} KB" if size < 1024*1024 else f"{size/(1024*1024):.1f} MB"
                            recent_files.append((file_path, mtime, size_str))
                    except:
                        continue
            
            # Sort by modification time (newest first)
            recent_files.sort(key=lambda x: x[1], reverse=True)
            return recent_files
        
        recent_files = get_recent_files(all_media_dir, hours=24)
        
        if recent_files:
            message = "📁 **Recently Modified Files (Last 24 Hours):**\n\n"
            for file_path, mtime, size_str in recent_files[:20]:  # Limit to 20 files
                rel_path = os.path.relpath(file_path, all_media_dir)
                timestamp = time.strftime('%Y-%m-d %H:%M', time.localtime(mtime))
                message += f"• `{rel_path}`\n  📏 {size_str} | 🕒 {timestamp}\n\n"
            
            if len(recent_files) > 20:
                message += f"\n... and {len(recent_files) - 20} more files"
            
            await event.reply(message, parse_mode='markdown')
        else:
            await event.reply("📭 No files modified in the last 24 hours.")
            
    except Exception as e:
        logger.error(f"Error in /check command: {str(e)}")
        await event.reply(f"❌ Error checking files: {str(e)}")

async def handle_download(event, admin_id):
    """Download a specific file"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    try:
        # Extract file path from command
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ **Usage:** `/download <file_path>`\n"
                "**Example:** `/download Media/00 - A - @username - 12345/file.jpg`\n\n"
                "**Note:** Use `/files` command to see available files and their paths.",
                parse_mode='markdown'
            )
            return
        
        file_path = ' '.join(args[1:]).strip()
        
        # Check if it's an absolute path
        if os.path.isabs(file_path):
            # Absolute path provided, use as is
            pass
        # Check if it's relative to Media folder
        elif not file_path.startswith("Media/") and not file_path.startswith("Media\\"):
            # Try with Media folder prefix
            file_path = os.path.join("Media", file_path)
        
        console.print(f"[cyan]Looking for file: {file_path}[/cyan]")
        
        # Check if file exists
        if not os.path.exists(file_path):
            # Try alternative search
            await event.reply(f"❌ File not found: `{file_path}`\n\n**Searching for file...**", parse_mode='markdown')
            
            # Search in Media folder recursively
            found_files = []
            for root, dirs, files in os.walk("Media"):
                for file in files:
                    if file_path in os.path.join(root, file) or file_path in file:
                        found_files.append(os.path.join(root, file))
            
            if found_files:
                if len(found_files) == 1:
                    file_path = found_files[0]
                    await event.reply(f"✅ Found file: `{file_path}`\n\nProceeding with download...", parse_mode='markdown')
                else:
                    message = f"🔍 **Multiple files found containing '{file_path}':**\n\n"
                    for i, f in enumerate(found_files[:10], 1):
                        message += f"{i}. `{f}`\n"
                    
                    if len(found_files) > 10:
                        message += f"\n... and {len(found_files) - 10} more files"
                    
                    message += "\n\n**Please use the full path from the list above.**"
                    await event.reply(message, parse_mode='markdown')
                    return
            else:
                # Show Media folder structure
                await event.reply(
                    f"❌ **File not found!**\n\n"
                    f"**Search Path:** `{file_path}`\n"
                    f"**Media Folder:** `{os.path.abspath('Media')}`\n\n"
                    f"**Try:**\n"
                    f"1. Use `/files` to list all available files\n"
                    f"2. Copy the exact file path from `/files` output\n"
                    f"3. Use `/download <exact_path>`"
                )
                return
        
        # Check if it's a directory
        if os.path.isdir(file_path):
            # Count files in directory
            file_count = sum([len(files) for r, d, files in os.walk(file_path)])
            await event.reply(
                f"❌ `{file_path}` is a directory (contains {file_count} files).\n\n"
                f"To download all files from this directory, use:\n"
                f"`/download_zip {os.path.relpath(file_path, 'Media') if file_path.startswith('Media') else file_path}`",
                parse_mode='markdown'
            )
            return
        
        # Check file size
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size > 1500 * 1024 * 1024:  # 1.5GB
            await event.reply(
                f"⚠️ File is too large ({file_size_mb:.1f} MB).\n"
                f"Telegram bots have a 2GB file size limit.\n\n"
                f"Consider using `/download_zip` for large files."
            )
            return
        
        # Send the file
        await event.reply(f"📤 **Downloading file...**\n`{file_path}`\nSize: {file_size_mb:.2f} MB", parse_mode='markdown')
        
        # Show progress for large files
        if file_size > 10 * 1024 * 1024:  # 10MB
            progress = RichDownloadProgress(os.path.basename(file_path), file_size)
            progress.update(1)
            await asyncio.sleep(0.5)
            progress.close()
        
        # Get file info for caption
        file_extension = os.path.splitext(file_path)[1].lower()
        file_types = {
            '.jpg': '🖼️ Photo', '.jpeg': '🖼️ Photo', '.png': '🖼️ Photo', 
            '.gif': '🖼️ GIF', '.bmp': '🖼️ Image', '.webp': '🖼️ Image',
            '.mp4': '🎬 Video', '.avi': '🎬 Video', '.mkv': '🎬 Video', 
            '.mov': '🎬 Video', '.wmv': '🎬 Video', '.flv': '🎬 Video', '.webm': '🎬 Video',
            '.mp3': '🎵 Audio', '.wav': '🎵 Audio', '.flac': '🎵 Audio', 
            '.m4a': '🎵 Audio', '.ogg': '🎵 Audio',
            '.zip': '🗜️ Archive', '.rar': '🗜️ Archive', '.7z': '🗜️ Archive',
            '.txt': '📄 Text', '.log': '📄 Log', '.md': '📄 Markdown',
            '.json': '📄 JSON', '.py': '🐍 Python'
        }
        
        file_type = file_types.get(file_extension, '📎 File')
        
        caption = (
            f"{file_type}\n"
            f"📁 File: {os.path.basename(file_path)}\n"
            f"📊 Size: {file_size_mb:.2f} MB\n"
            f"📍 Path: {os.path.relpath(file_path, 'Media') if file_path.startswith('Media') else file_path}\n"
            f"🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        await event.client.send_file(
            event.chat_id,
            file_path,
            caption=caption,
            force_document=True  # Force as document to avoid compression
        )
        
        await event.reply(f"✅ **Download complete!**\n`{file_path}`", parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error in /download command: {str(e)}")
        await event.reply(
            f"❌ **Error downloading file:**\n"
            f"`{str(e)[:200]}`\n\n"
            f"**Debug Info:**\n"
            f"• File Path: `{file_path}`\n"
            f"• File Exists: `{os.path.exists(file_path) if 'file_path' in locals() else 'Unknown'}`\n"
            f"• Is Directory: `{os.path.isdir(file_path) if 'file_path' in locals() else 'Unknown'}`"
        )

async def handle_download_zip(event, admin_id):
    """Download a folder as ZIP"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    try:
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ **Usage:** `/download_zip <folder_path>`\n"
                "**Example:** `/download_zip Media/00 - A - @username - 12345`\n\n"
                "**Note:** This command creates a ZIP archive of the specified folder.",
                parse_mode='markdown'
            )
            return
        
        folder_path = ' '.join(args[1:]).strip()
        
        # Add Media prefix if not already
        if not folder_path.startswith("Media/") and not folder_path.startswith("Media\\"):
            folder_path = os.path.join("Media", folder_path)
        
        if not os.path.exists(folder_path):
            await event.reply(f"❌ Folder not found: `{folder_path}`", parse_mode='markdown')
            return
        
        if not os.path.isdir(folder_path):
            await event.reply(f"❌ `{folder_path}` is not a directory.", parse_mode='markdown')
            return
        
        # Count files in folder
        total_files = 0
        total_size = 0
        for root, dirs, files in os.walk(folder_path):
            total_files += len(files)
            for file in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, file))
                except:
                    pass
        
        total_size_mb = total_size / (1024 * 1024)
        
        if total_files == 0:
            await event.reply(f"❌ Folder is empty: `{folder_path}`", parse_mode='markdown')
            return
        
        await event.reply(
            f"📦 **Creating ZIP archive...**\n\n"
            f"• Folder: `{folder_path}`\n"
            f"• Files: {total_files}\n"
            f"• Size: {total_size_mb:.1f} MB\n\n"
            f"This may take a moment..."
        )
        
        # Create ZIP file
        timestamp = int(time.time())
        folder_name = os.path.basename(folder_path.rstrip('/\\'))
        zip_filename = f"{folder_name}_{timestamp}.zip"
        
        total_zipped = 0
        zip_size = 0
        
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, folder_path)
                    
                    try:
                        zipf.write(file_path, arcname)
                        total_zipped += 1
                        zip_size += os.path.getsize(file_path)
                        
                        # Update progress every 10 files
                        if total_zipped % 10 == 0:
                            await event.edit(
                                f"📦 **ZIP Progress...**\n"
                                f"• Files added: {total_zipped}/{total_files}\n"
                                f"• Current size: {zip_size/(1024*1024):.1f} MB"
                            )
                    except Exception as e:
                        logger.error(f"Error adding {file_path} to ZIP: {str(e)}")
        
        final_zip_size = os.path.getsize(zip_filename)
        final_zip_size_mb = final_zip_size / (1024 * 1024)
        
        await event.reply(
            f"✅ **ZIP created successfully!**\n\n"
            f"• Folder: `{folder_path}`\n"
            f"• Files: {total_zipped}/{total_files}\n"
            f"• ZIP Size: {final_zip_size_mb:.1f} MB\n\n"
            f"Sending ZIP file..."
        )
        
        # Check if ZIP is too large
        if final_zip_size > 1900 * 1024 * 1024:  # 1.9GB
            await event.reply(
                f"⚠️ ZIP file is too large ({final_zip_size_mb:.1f} MB).\n"
                f"Telegram bots have a 2GB file size limit.\n\n"
                f"Consider splitting the folder into smaller parts."
            )
            os.remove(zip_filename)
            return
        
        # Send ZIP file
        await event.client.send_file(
            event.chat_id,
            zip_filename,
            caption=f"📦 ZIP Archive\n"
                   f"📁 Folder: {folder_name}\n"
                   f"📊 Files: {total_zipped}\n"
                   f"📏 Size: {final_zip_size_mb:.1f} MB\n"
                   f"🕒 Created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            force_document=True
        )
        
        # Clean up
        os.remove(zip_filename)
        
    except Exception as e:
        logger.error(f"Error in /download_zip command: {str(e)}")
        await event.reply(f"❌ Error creating ZIP: {str(e)}")
        
        # Clean up on error
        try:
            if os.path.exists(zip_filename):
                os.remove(zip_filename)
        except:
            pass

async def handle_delete(event, admin_id):
    """Delete a specific file"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        # Extract file path from command
        args = event.text.split()
        if len(args) < 2:
            await event.reply("❌ Usage: /delete <file_path>\nExample: /delete Media/file.jpg")
            return
        
        file_path = ' '.join(args[1:]).strip()
        
        # Check if file exists
        if not os.path.exists(file_path):
            await event.reply(f"❌ File not found: `{file_path}`", parse_mode='markdown')
            return
        
        # Check if it's a directory
        if os.path.isdir(file_path):
            await event.reply(f"❌ `{file_path}` is a directory. Use /deletedir to delete directories.", parse_mode='markdown')
            return
        
        # Get file info before deletion
        file_size = os.path.getsize(file_path)
        file_size_str = f"{file_size/1024:.1f} KB" if file_size < 1024*1024 else f"{file_size/(1024*1024):.1f} MB"
        
        # Confirm deletion
        await event.reply(
            f"⚠️ Are you sure you want to delete this file?\n\n"
            f"📁 `{file_path}`\n"
            f"📏 Size: {file_size_str}\n\n"
            f"Type `/confirm_delete {file_path}` to confirm.",
            parse_mode='markdown'
        )
        
    except Exception as e:
        logger.error(f"Error in /delete command: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")


async def handle_confirm_delete(event, admin_id):
    """Confirm and delete a file"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        # Extract file path from command
        args = event.text.split()
        if len(args) < 2:
            return
        
        file_path = ' '.join(args[1:]).strip()
        
        # Check if file exists
        if not os.path.exists(file_path):
            await event.reply(f"❌ File not found: `{file_path}`", parse_mode='markdown')
            return
        
        # Delete the file
        os.remove(file_path)
        await event.reply(f"✅ Successfully deleted: `{file_path}`", parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error in /confirm_delete command: {str(e)}")
        await event.reply(f"❌ Error deleting file: {str(e)}")


async def handle_all(event, admin_id):
    """Download all media files from media folder"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        # Get all media files
        media_files = []
        total_size = 0
        
        for root, dirs, files in os.walk(all_media_dir):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',
                                         '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm')):
                    file_path = os.path.join(root, file)
                    size = os.path.getsize(file_path)
                    total_size += size
                    media_files.append((file_path, size))
        
        if not media_files:
            await event.reply("📭 No media files found in the media folder.")
            return
        
        # Sort by size (smallest first)
        media_files.sort(key=lambda x: x[1])
        
        total_mb = total_size / (1024 * 1024)
        await event.reply(
            f"📊 Found {len(media_files)} media files ({total_mb:.1f} MB total).\n"
            f"⚠️ Sending all files... This may take a while.\n"
            f"Files will be sent in batches."
        )
        
        # Send files in batches
        sent_count = 0
        batch_size = 10
        
        for i in range(0, len(media_files), batch_size):
            batch = media_files[i:i + batch_size]
            
            # Send batch of files
            for file_path, size in batch:
                try:
                    file_size_mb = size / (1024 * 1024)
                    await event.client.send_file(
                        event.chat_id,
                        file_path,
                        caption=f"📁 {os.path.basename(file_path)} ({file_size_mb:.1f} MB)",
                        allow_cache=False
                    )
                    sent_count += 1
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Error sending file {file_path}: {str(e)}")
                    await event.reply(f"❌ Failed to send: {os.path.basename(file_path)}")
            
            # Update progress
            if i + batch_size < len(media_files):
                await event.reply(f"📤 Sent {sent_count}/{len(media_files)} files...")
                await asyncio.sleep(5)
        
        await event.reply(f"✅ Successfully sent {sent_count}/{len(media_files)} media files.")
        
    except Exception as e:
        logger.error(f"Error in /all command: {str(e)}")
        await event.reply(f"❌ Error sending media files: {str(e)}")


async def handle_zip(event, admin_id):
    """Create and send a ZIP archive"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        # Create temporary ZIP file
        timestamp = int(time.time())
        zip_filename = f"media_backup_{timestamp}.zip"
        
        await event.reply("📦 Creating ZIP archive... This may take a while.")
        
        # Create ZIP file
        total_files = 0
        total_size = 0
        
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(all_media_dir):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                
                for file in files:
                    if file.startswith('.'):
                        continue
                    
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, all_media_dir)
                    
                    try:
                        zipf.write(file_path, arcname)
                        total_files += 1
                        total_size += os.path.getsize(file_path)
                    except Exception as e:
                        logger.error(f"Error adding {file_path} to ZIP: {str(e)}")
        
        if total_files == 0:
            await event.reply("📭 No files to add to ZIP archive.")
            os.remove(zip_filename)
            return
        
        zip_size = os.path.getsize(zip_filename)
        zip_size_mb = zip_size / (1024 * 1024)
        
        await event.reply(
            f"📦 ZIP archive created successfully!\n"
            f"• Files: {total_files}\n"
            f"• Size: {zip_size_mb:.1f} MB"
        )
        
        # Check if ZIP is too large for Telegram
        if zip_size > 1900 * 1024 * 1024:  # 1.9GB
            await event.reply(
                f"⚠️ ZIP file is too large ({zip_size_mb:.1f} MB).\n"
                f"Telegram bots have a 2GB file size limit.\n"
                f"Consider creating multiple smaller ZIP files."
            )
            os.remove(zip_filename)
            return
        
        # Send the ZIP file
        await event.client.send_file(
            event.chat_id,
            zip_filename,
            caption=f"📦 Media Backup\n"
                   f"📁 Files: {total_files}\n"
                   f"📏 Size: {zip_size_mb:.1f} MB\n"
                   f"🕒 Created: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        # Clean up
        os.remove(zip_filename)
        
    except Exception as e:
        logger.error(f"Error in /zip command: {str(e)}")
        await event.reply(f"❌ Error creating ZIP: {str(e)}")
        
        # Clean up on error
        try:
            if os.path.exists(zip_filename):
                os.remove(zip_filename)
        except:
            pass

async def handle_ping(event):
    """Handle ping command with multiple endpoints"""

    # Admin check
    try:
        admin_id = int(event.client.admin_id)
    except Exception:
        return

    if event.sender_id != admin_id:
        return

    endpoints = [
        ("Google", "https://www.google.com"),
        ("Telegram", "https://api.telegram.org"),
        ("Cloudflare", "https://1.1.1.1")
    ]

    results = []

    for name, url in endpoints:
        try:
            start_time = time.perf_counter()
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(url):
                    ping_time = round((time.perf_counter() - start_time) * 1000)
                    results.append(f"{name}: {ping_time} ms")
        except Exception as e:
            results.append(f"{name}: Failed")

    await event.reply(
        "📡 **Ping Results**\n\n" + "\n".join(results),
        parse_mode="markdown"
    )

async def handle_setgchannel(event, admin_id, state):
    """Admin only: Set global channel for BOT files"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    try:
        # Extract channel ID from command
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ Usage: /setgchannel <channel_id>\n"
                "Example: /setgchannel -1001234567890\n\n"
                "**What this does:**\n"
                "• Sets the BOT'S GLOBAL channel\n"
                "• All files downloaded by bot go here\n"
                "• Users' self-destructing media are COPIED here\n"
                "• Uses BOT account to send files"
            )
            return
        
        channel_input = args[1].strip()
        
        # Validate channel ID format
        if not channel_input.startswith('-100'):
            await event.reply(
                "❌ **Invalid Channel ID Format!**\n\n"
                "**Channel IDs must start with `-100`**\n"
                "Example: `-1001234567890`\n\n"
                "**How to get Channel ID:**\n"
                "1. Add @getidsbot to your channel\n"
                "2. Send any message in channel\n"
                "3. Bot will reply with your channel ID\n"
                "4. Copy the ID (it will look like -1001234567890)"
            )
            return
        
        # Check if it's a valid number
        try:
            channel_id_int = int(channel_input)
        except ValueError:
            await event.reply(
                "❌ **Invalid Channel ID!**\n"
                "Channel ID must be a number.\n"
                "Example: `-1001234567890`"
            )
            return
        
        # Check if channel ID is negative (channel/supergroup)
        if channel_id_int >= 0:
            await event.reply(
                "❌ **This is NOT a Channel ID!**\n\n"
                "You entered a **User ID** (positive number).\n"
                "Channel IDs are **negative numbers** starting with -100.\n\n"
                "**Your Input:** `{}`\n"
                "**Expected Format:** `-1001234567890`".format(channel_input)
            )
            return
        
        try:
            channel_entity = await event.client.get_entity(channel_id_int)
            
            # Calculate proper channel ID
            if channel_entity.id > 0:
                # If entity.id is positive (e.g., 123456789)
                # Convert to -100123456789
                channel_id = int("-100" + str(channel_entity.id))
            else:
                # Already in negative format
                channel_id = channel_entity.id
            
            # Verify it's actually a channel/supergroup
            from telethon.tl.types import Channel
            
            if isinstance(channel_entity, Channel):
                channel_type = "Channel" if channel_entity.broadcast else "Supergroup"
                
                # Admin sets global channel
                success = await update_channel_id(str(channel_id))
                
                if success:
                    # Update the client's config
                    event.client.channel_id = channel_id
                    
                    await event.reply(
                        f"✅ **Bot's Global Channel set successfully!**\n\n"
                        f"📢 **BOT'S GLOBAL CHANNEL**\n"
                        f"• Name: {getattr(channel_entity, 'title', 'Unknown')}\n"
                        f"• ID: `{channel_id}`\n"
                        f"• Username: @{getattr(channel_entity, 'username', 'None')}\n"
                        f"• Type: {channel_type}\n\n"
                        f"**What this does:**\n"
                        f"1. Files downloaded by the bot go here\n"
                        f"2. Users' self-destructing media are COPIED here\n"
                        f"3. Serves as backup/archive for ALL users\n"
                        f"4. Uses BOT account to send files\n\n"
                        f"**Test with:** /testchannel\n"
                        f"**View with:** /currentchannel"
                    )
                else:
                    await event.reply("❌ Failed to update global settings.")
            else:
                await event.reply(
                    "❌ **Not a valid Channel/Supergroup!**\n"
                    "The entity you provided is not a channel or supergroup.\n"
                    "Please provide a valid channel ID starting with -100."
                )
                
        except Exception as e:
            logger.error(f"Error accessing channel {channel_input}: {str(e)}")
            
            # Could not access channel, but save anyway if format is correct
            success = await update_channel_id(str(channel_id_int))
            
            if success:
                event.client.channel_id = channel_id_int
                
                await event.reply(
                    f"⚠️ **Bot's Global Channel set with warning**\n\n"
                    f"📢 **BOT'S GLOBAL CHANNEL**\n"
                    f"• Channel ID: `{channel_id_int}`\n\n"
                    f"**Warning:** Could not verify channel access\n"
                    f"Make sure the bot is added as admin to this channel.\n"
                    f"Add @{(await event.client.get_me()).username} as admin.\n\n"
                    f"**Test with:** /testchannel"
                )
            else:
                await event.reply("❌ Failed to update global settings.")
            
    except Exception as e:
        logger.error(f"Error in /setgchannel command: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")

async def handle_setmychannel(event, admin_id, state):
    """Users only: Set personal channel for YOUR self-destructing media"""
    if not event.is_private:
        await event.reply("❌ Please use this command in private chat.")
        return
    
    user_id = event.sender_id
    user_session = state.get("user_sessions", {}).get(str(user_id))
    
    if not user_session:
        await event.reply("❌ You are not logged in. Use /login first.")
        return
    
    try:
        # Extract channel ID from command
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ Usage: /setmychannel <channel_id>\n"
                "Example: /setmychannel -1001234567890\n\n"
                "**What this does:**\n"
                "• Sets YOUR PERSONAL channel\n"
                "• Your self-destructing media goes here\n"
                "• Uses YOUR account to send files\n"
                "• Also copied to bot's global channel\n\n"
                "**How to get Channel ID:**\n"
                "1. Add @getidsbot to your channel\n"
                "2. Send any message\n"
                "3. Copy the ID (starts with -100)"
            )
            return
        
        channel_input = args[1].strip()
        
        # Validate channel ID format
        if not channel_input.startswith('-100'):
            await event.reply(
                "❌ **Invalid Channel ID Format!**\n\n"
                "**Channel IDs must start with `-100`**\n"
                "Example: `-1001234567890`\n\n"
                "**How to get Channel ID:**\n"
                "1. Add @getidsbot to your channel\n"
                "2. Send any message\n"
                "3. Copy the ID (starts with -100)\n\n"
                "**Note:** DO NOT use your user ID (positive number)"
            )
            return
        
        # Check if it's a valid number
        try:
            channel_id_int = int(channel_input)
        except ValueError:
            await event.reply(
                "❌ **Invalid Channel ID!**\n"
                "Channel ID must be a number.\n"
                "Example: `-1001234567890`"
            )
            return
        
        # Check if channel ID is negative (channel/supergroup)
        if channel_id_int >= 0:
            await event.reply(
                "❌ **This is NOT a Channel ID!**\n\n"
                "You entered a **User ID** (positive number).\n"
                "Channel IDs are **negative numbers** starting with -100.\n\n"
                "**Your Input:** `{}`\n"
                "**Expected Format:** `-1001234567890`".format(channel_input)
            )
            return
        
        # Get user's client to access the channel
        user_session_file = get_user_session_file(user_id)
        
        if not os.path.exists(user_session_file):
            await event.reply("❌ User session not found. Please login again with /login")
            return
        
        # Load user client
        async with aiofiles.open(user_session_file, mode="r") as f:
            session_string = await f.read()
        
        session = StringSession(session_string)
        user_client = TelegramClient(session, user_session["api_id"], user_session["api_hash"])
        
        await user_client.connect()
        
        if not await user_client.is_user_authorized():
            await user_client.disconnect()
            await event.reply("❌ Your session is not authorized. Please login again with /login")
            return
        
        try:
            channel_entity = await user_client.get_entity(channel_id_int)
            
            # Calculate proper channel ID
            if channel_entity.id > 0:
                channel_id = int("-100" + str(channel_entity.id))
            else:
                channel_id = channel_entity.id
            
            # Verify it's actually a channel/supergroup
            from telethon.tl.types import Channel
            
            if isinstance(channel_entity, Channel):
                channel_type = "Channel" if channel_entity.broadcast else "Supergroup"
                
                # User sets personal channel
                success = await update_user_channel_id(user_id, channel_id, state)
                
                if success:
                    await event.reply(
                        f"✅ **Your Personal Channel set successfully!**\n\n"
                        f"📢 **YOUR PERSONAL CHANNEL**\n"
                        f"• Name: {getattr(channel_entity, 'title', 'Unknown')}\n"
                        f"• ID: `{channel_id}`\n"
                        f"• Username: @{getattr(channel_entity, 'username', 'None')}\n"
                        f"• Type: {channel_type}\n\n"
                        f"**What this does:**\n"
                        f"1. Your self-destructing media goes here\n"
                        f"2. Works even when bot was offline\n\n"
                        f"**Test with:** /mychanneltest\n"
                        f"**View with:** /mychannel"
                    )
                else:
                    await event.reply("❌ Failed to update your channel.")
            else:
                await event.reply(
                    "❌ **Not a valid Channel/Supergroup!**\n"
                    "The entity you provided is not a channel or supergroup.\n"
                    "Please provide a valid channel ID starting with -100."
                )
            
        except Exception as e:
            logger.error(f"User error accessing channel {channel_input}: {str(e)}")
            
            # Could not access channel, but save anyway
            success = await update_user_channel_id(user_id, channel_id_int, state)
            
            if success:
                await event.reply(
                    f"⚠️ **Personal Channel set with warning**\n\n"
                    f"📢 **YOUR PERSONAL CHANNEL**\n"
                    f"• Channel ID: `{channel_id_int}`\n\n"
                    f"**Warning:** Could not verify channel access\n"
                    f"Make sure you have 'Send Messages' permission in this channel.\n\n"
                    f"**Test with:** /mychanneltest\n\n"
                    f"**Note:** If channel access fails, media won't be saved!"
                )
            else:
                await event.reply("❌ Failed to update your channel.")
        
        await user_client.disconnect()
        
    except Exception as e:
        logger.error(f"Error in user channel setup: {str(e)}")
        await event.reply(f"❌ Error setting your personal channel: {str(e)}")

async def handle_testchannel(event, admin_id):
    """Test channel access by sending a test message"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    channel_id = getattr(event.client, 'channel_id', None)
    
    if not channel_id:
        await event.reply("❌ No channel configured. Use /setmychannel first.")
        return
    
    try:
        await event.reply(f"🔄 Testing channel access for ID: {channel_id}")
        
        # Test 1: Try to get channel info
        try:
            entity = await event.client.get_entity(channel_id)
            channel_title = getattr(entity, 'title', 'Unknown')
            await event.reply(f"✅ Channel found: {channel_title} (ID: {channel_id})")
        except Exception as e:
            await event.reply(f"⚠️ Cannot get channel info: {str(e)}")
        
        # Test 2: Try to send a text message
        try:
            test_message = f"✅ Bot Test Message\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}\nBot: @{(await event.client.get_me()).username}"
            await event.client.send_message(entity=channel_id, message=test_message)
            await event.reply(f"✅ Test message sent to channel successfully!")
        except Exception as e:
            await event.reply(f"❌ Failed to send test message: {str(e)}")
            await event.reply("⚠️ Make sure:\n1. Bot is added to channel\n2. Bot has 'Send Messages' permission\n3. Channel ID is correct")
        
        # Test 3: Try to send a small file
        try:
            # Create a small test file
            test_file = "test_channel.txt"
            with open(test_file, "w") as f:
                f.write(f"Test file for channel\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            await event.client.send_file(
                entity=channel_id,
                file=test_file,
                caption="Test file for channel"
            )
            await event.reply(f"✅ Test file sent to channel successfully!")
            
            # Clean up
            os.remove(test_file)
        except Exception as e:
            await event.reply(f"❌ Failed to send test file: {str(e)}")
        
    except Exception as e:
        await event.reply(f"❌ Error testing channel: {str(e)}")


async def handle_currentchannel(event, admin_id, state):
    """Show current BOT'S GLOBAL channel configuration (admin only)"""
    if not await is_admin(event, admin_id):
        # For users, redirect to mychannel
        await handle_mychannel(event, admin_id, state)
        return
    
    # Admin sees global channel
    channel_id = getattr(event.client, 'channel_id', None)
    
    if channel_id:
        try:
            entity = await event.client.get_entity(channel_id)
            await event.reply(
                f"📢 **Bot's Global Channel Configuration**\n\n"
                f"• Channel: {getattr(entity, 'title', 'Unknown')}\n"
                f"• ID: {channel_id}\n"
                f"• Username: @{getattr(entity, 'username', 'None')}\n\n"
                f"**What this does:**\n"
                f"• Receives copies of ALL users' self-destructing media\n"
                f"• Uses BOT account to send files\n"
                f"• Serves as backup/archive\n\n"
                f"**Note:** Users set their own channels with /setmychannel"
            )
        except Exception as e:
            await event.reply(
                f"⚠️ Channel ID is set to {channel_id}, but I can't access it.\n"
                f"Error: {str(e)}\n"
                f"Make sure the bot is added as admin to this channel.\n"
                f"Use /setgchannel to update the channel."
            )
    else:
        await event.reply(
            "⚠️ **No global channel configured!**\n"
            "Files from the bot are only being saved locally, not sent to any channel.\n"
            "Use /setgchannel <channel_id> to configure the bot's global channel.\n\n"
            "**Note:** Users can set their own personal channels with /setmychannel"
        )

async def handle_help_full(event, admin_id, state):
    """Send complete help documentation as text file"""
    
    # Get bot info for documentation
    try:
        me = await event.client.get_me()
        bot_username = me.username
    except:
        bot_username = "Unknown"
    
    # Get admin username instead of just ID
    admin_username = f"User ID: {admin_id}"  # Default if can't get username
    try:
        admin_entity = await event.client.get_entity(int(admin_id))
        if admin_entity.username:
            admin_username = f"@{admin_entity.username}"
        elif admin_entity.first_name:
            admin_username = f"{admin_entity.first_name} (ID: {admin_id})"
        else:
            admin_username = f"User ID: {admin_id}"
    except Exception as e:
        console.print(f"[yellow]Could not get admin info: {e}[/yellow]")
        admin_username = f"User ID: {admin_id}"
    
    # Create comprehensive documentation
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    
    full_help = f"""╔══════════════════════════════════════════════╗
║     SELF-DESTRUCTING MEDIA DOWNLOADER BOT      ║
║           COMPLETE DOCUMENTATION v2.0          ║
╚══════════════════════════════════════════════╝

📅 Generated: {timestamp}
🤖 Bot: @{bot_username}
👑 Admin: {admin_username}
🔐 Security: Database Encryption ✓

==================================================
                     TABLE OF CONTENTS
==================================================
1. Overview & Features
2. User Commands Guide
3. Admin Commands Guide
4. Channel Setup Guide
5. Media Recovery System
6. Queue Management
7. File Management
8. Security Features
9. Troubleshooting
10. Technical Specifications

==================================================
                1. OVERVIEW & FEATURES
==================================================

📌 WHAT THIS BOT DOES:
• Automatically saves self-destructing media
• Works with photos, videos, documents, audio
• Dual channel system (User's + Admin's channel)
• Offline media recovery (48-hour scan)
• Automatic queue processing
• Encrypted database storage

🎯 KEY FEATURES:
✓ No manual downloading required
✓ Media saved even when bot was offline
✓ Automatic retry for failed downloads
✓ Organized file storage by sender
✓ Encrypted session storage
✓ Access control system
✓ Log management

==================================================
              2. USER COMMANDS GUIDE
==================================================

🔐 ACCOUNT MANAGEMENT:
/login           - Login with your Telegram account
/logout          - Logout from your account
/mystatus        - Check your login status
/cancel          - Cancel current login process

📢 CHANNEL SETUP (MANDATORY):
/setmychannel    - Set YOUR PERSONAL channel (REQUIRED)
                  Example: /setmychannel -1001234567890
/mychannel       - Show your channel configuration
/mychanneltest   - Test your channel access

🔍 MEDIA RECOVERY:
/checkmissed     - Check for missed media (last 48 hours)
/queue_stats     - Show your pending media queue
/process_queue   - Process your queued media

📚 GUIDANCE:
/savetips        - Detailed tips for saving media
/help            - Show quick help

==================================================
              3. ADMIN COMMANDS GUIDE
==================================================

👥 USER MANAGEMENT:
/users           - List all logged-in users
/checkmissed_all - Check missed media for ALL users
/checkmissed <id>- Check missed media for specific user

🔐 ACCESS CONTROL:
/allow_user <id> - Allow user to use the bot
/disallow_user <id> - Remove user from allowed list
/allowed_users   - Show all allowed users

📢 BOT CONFIGURATION:
/setgchannel     - Set BOT'S GLOBAL channel
/currentchannel  - Show current bot channel
/testchannel     - Test global channel access

🔧 FORWARDING CONTROL:
/globalforward enable  - Enable forwarding to admin channel
/globalforward disable - Disable forwarding to admin channel
/globalforward status  - Show current forwarding status

📊 QUEUE MANAGEMENT:
/queue_stats_all      - Show ALL queue statistics
/process_queue_all    - Process ALL queued media

==================================================
              4. CHANNEL SETUP GUIDE
==================================================

🚨 IMPORTANT: Channel ID vs User ID
❌ USER ID: Positive number (e.g., 123456789)
✅ CHANNEL ID: Negative number starting with -100 (e.g., -1001234567890)

📋 SETUP STEPS:
1. Create a Telegram channel/supergroup
2. Add @getidsbot to your channel
3. Send any message in the channel
4. @getidsbot will reply with your Channel ID
5. Copy the ID (looks like -1001234567890)
6. Use: /setmychannel -1001234567890
7. Test with: /mychanneltest

🔧 DUAL CHANNEL SYSTEM:
1. YOUR PERSONAL CHANNEL: Set with /setmychannel
   • Media sent using YOUR account
   • You control this channel
   
2. ADMIN'S GLOBAL CHANNEL: Set by admin with /setgchannel
   • Media sent using BOT account
   • Backup/archive of all users' media
   • Can be disabled with /globalforward disable

==================================================
          5. MEDIA RECOVERY SYSTEM
==================================================

🔄 HOW IT WORKS:
1. When bot is online: Media saved immediately
2. When bot is offline: Media added to queue
3. On startup: Bot processes queued media
4. Manual check: /checkmissed scans last 48h

📊 QUEUE SYSTEM:
• Max retries: 3 attempts per media item
• Batch processing: 10 items at a time
• Status tracking: pending/processing/processed/failed
• Automatic cleanup: Expired media marked as failed

🔍 /CHECKMISSED COMMAND:
• Scans last 48 hours of chats
• Only checks private conversations
• Detects self-destructing media with TTL
• Queues found media for processing
• Admin can check all users with /checkmissed_all

==================================================
            6. FILE MANAGEMENT
==================================================

📁 FILE BROWSING:
/files          - List all files in Media folder
/check          - Check for new files (last 24h)

⬇️ DOWNLOADING:
/download <path>- Download specific file
                 Example: /download Media/00 - A - @user/file.jpg
/download_zip <folder> - Download folder as ZIP
/all           - Download all media files
/zip           - Create ZIP archive of entire Media folder

🗑️ DELETION:
/delete <path>  - Delete specific file
/confirm_delete <path> - Confirm file deletion

💾 ORGANIZATION:
• Files organized by sender: "00 - A - @username - user_id"
• Automatic folder creation
• Unique filenames with timestamps

==================================================
            7. LOG MANAGEMENT
==================================================

📋 LOG VIEWING:
/logs [lines] [search] - View bot logs
                  Default: 50 lines
                  Example: /logs 100 error

🗃️ LOG MANAGEMENT:
/clearlogs      - Clear log file (creates backup)
/download_logs  - Download entire log file
/loglevel <level> - Change log level

📊 LOG LEVELS:
• DEBUG - Detailed debugging information
• INFO - Normal operational messages
• WARNING - Warning messages
• ERROR - Error messages

==================================================
            8. SECURITY FEATURES
==================================================

🔐 ENCRYPTION:
• Database encryption (SQLite encryption)
• Session string encryption (Fernet AES-256)
• Environment variable protection
• Secure key storage

👮 ACCESS CONTROL:
• User whitelist system
• Admin-only commands protected
• Session validation
• Command rate limiting

📁 FILE SECURITY:
• Secure file permissions
• Input validation
• Path traversal protection
• File size limits

🔒 DATA PROTECTION:
• No sensitive data in logs
• Encrypted database
• Secure session storage
• Regular backups

==================================================
            9. TROUBLESHOOTING
==================================================

❌ LOGIN FAILS:
1. Check API ID and API hash from my.telegram.org
2. Ensure correct phone number format (+country code)
3. Verify OTP code format (1 2 3 4 5)
4. Check 2FA password if enabled

❌ CHANNEL SETUP FAILS:
1. Verify channel ID starts with -100
2. Ensure bot/user has admin rights in channel
3. Test with /mychanneltest or /testchannel
4. Check if channel is supergroup (required for bots)

❌ MEDIA NOT SAVING:
1. Check if user is logged in with /mystatus
2. Verify channel is set with /mychannel
3. Ensure media has TTL (self-destructing)
4. Check queue with /queue_stats
5. Process queue with /process_queue

❌ BOT NOT RESPONDING:
1. Check bot status with /ping
2. View logs with /logs
3. Restart the bot
4. Check server internet connection

==================================================
         10. TECHNICAL SPECIFICATIONS
==================================================

🖥️ SYSTEM REQUIREMENTS:
• Python 3.7+
• SQLite 3
• 1GB RAM minimum
• 10GB storage minimum

📦 DEPENDENCIES:
• telethon - Telegram client library
• cryptography - Encryption library
• aiofiles - Async file operations
• rich - Console interface
• python-dotenv - Environment variables

⚙️ CONFIGURATION:
• Bot Token: Required for bot login
• API ID/Hash: From my.telegram.org
• Channel ID: For media storage
• Encryption Keys: For data protection

🔄 AUTOMATIC PROCESSES:
• Queue processing every 5 minutes
• Auto-check missed media on startup
• Periodic auto-check every 1 hour
• Log rotation (10MB max, 5 backups)

📈 PERFORMANCE:
• Max 10 concurrent downloads
• 3 retry attempts per media
• 48-hour media recovery window
• 2GB file size limit

==================================================
                  SUPPORT
==================================================

📞 CONTACT ADMIN:
• Admin: {admin_username}
• Use /status for system information
• Provide logs with /download_logs if needed

🐛 BUG REPORTS:
• Include error message
• Steps to reproduce
• Log output
• User ID and timestamp

🆘 EMERGENCY:
• Stop bot process
• Backup database
• Check logs
• Contact developer

==================================================
                 DISCLAIMER
==================================================

⚠️ LEGAL:
• Use only for legitimate purposes
• Respect privacy laws
• Follow Telegram Terms of Service
• Obtain consent when required

🔒 PRIVACY:
• User data is encrypted
• No data sharing with third parties
• Regular data cleanup
• Secure storage

🔄 UPDATES:
• Regular security updates
• Feature improvements
• Bug fixes
• Performance optimizations

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
END OF DOCUMENTATION • {timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    # Save to file
    help_filename = f"help_documentation_{int(time.time())}.txt"
    with open(help_filename, 'w', encoding='utf-8') as f:
        f.write(full_help)
    
    # Send the file
    await event.reply("📚 Sending complete documentation...")
    await event.client.send_file(
        event.chat_id,
        help_filename,
        caption=f"📚 Complete Bot Documentation\nGenerated: {timestamp}\nBot: @{bot_username}"
    )
    
    # Clean up
    try:
        os.remove(help_filename)
    except:
        pass
        
async def handle_help(event, admin_id, state):
    """Show help message based on user role"""
    
    # Check if user is admin
    is_admin_user = await is_admin(event, admin_id)
    
    if not is_admin_user:
        # ===== USER HELP =====
        user_help = """
🤖 **Self-Destructing Media Downloader Bot - User Help**

📌 **BASIC COMMANDS:**
/start - Start the bot and see welcome message
/help - Show this help message
/login - Login with your Telegram account
/logout - Logout from your account
/cancel - Cancel current login process
/savetips - Tips for saving self-destructing media

👤 **ACCOUNT MANAGEMENT:**
/mystatus - Check your login status & configuration
/mychannel - Show your personal channel configuration
/mychanneltest - Test your channel access

📢 **CHANNEL SETUP (REQUIRED):**
/setmychannel <channel_id> - Set your personal channel
Example: `/setmychannel -1001234567890`

🔍 **MEDIA RECOVERY:**
/checkmissed - Check for missed self-destructing media (last 48h)
/queue_stats - Show your pending media queue
/process_queue - Process your queued media

📊 **QUEUE SYSTEM:**
• Automatically saves media when bot is offline
• Processes up to 10 items at a time
• Retries failed downloads automatically

⚠️ **IMPORTANT NOTES:**
• You need a CHANNEL ID (starts with -100), not USER ID
• Use /savetips for detailed instructions
• Media is saved to YOUR channel using YOUR account

Need more help? Contact the administrator.
"""
        await event.reply(user_help)
        return
    
    # ===== ADMIN HELP =====
    admin_help = """
🤖 **Self-Destructing Media Downloader Bot - Admin Help**

👑 **ADMIN PRIVILEGES:**
You have access to ALL commands below.

📌 **BASIC COMMANDS:**
/start - Start the bot
/help - Show this help
/help_full - Get complete documentation as file
/ping - Check network latency
/status - Show download statistics
/savetips - Tips for saving media

👥 **USER MANAGEMENT:**
/users - List all logged-in users
/checkmissed_all - Check missed media for ALL users
/checkmissed <user_id> - Check specific user's missed media

🔐 **ACCESS CONTROL:**
/allow_user <user_id> - Allow user to use bot
/disallow_user <user_id> - Remove user from allowed list
/allowed_users - Show allowed users list

📢 **CHANNEL MANAGEMENT:**
/setgchannel <channel_id> - Set BOT'S GLOBAL channel
/currentchannel - Show current bot channel
/testchannel - Test global channel access
/globalforward <enable/disable/status> - Control forwarding

📁 **FILE MANAGEMENT:**
/files - List all files in Media folder
/check - Check for new files (last 24h)
/download <path> - Download specific file
/download_zip <folder> - Download folder as ZIP
/all - Download all media files
/zip - Create ZIP archive of entire Media folder
/delete <path> - Delete specific file
/confirm_delete <path> - Confirm file deletion

📊 **QUEUE MANAGEMENT:**
/queue_stats - Your personal queue stats
/process_queue - Process your personal queue
/queue_stats_all - ALL users queue statistics
/process_queue_all - Process ALL queued media

📋 **LOG MANAGEMENT:**
/logs [lines] [search] - View bot logs (default: 50 lines)
/clearlogs - Clear log file (creates backup)
/download_logs - Download entire log file
/loglevel <level> - Change log level (DEBUG/INFO/WARNING/ERROR)

👤 **USER COMMANDS (Also work for admin):**
/login - Login with account
/logout - Logout from account
/mystatus - Check account status
/setmychannel - Set personal channel
/mychannel - Check personal channel
/mychanneltest - Test personal channel
/checkmissed - Check missed media
/queue_stats - Queue statistics
/process_queue - Process queue

⚙️ **TECHNICAL DETAILS:**
• Database: SQLite with encryption
• Queue: Automatic retry system
• Offline Recovery: 48-hour scan
• Encryption: AES-256 (Fernet)
• Session: Bot token only
• Media: Dual channel system

Use /help_full for complete documentation file.
"""
    await event.reply(admin_help)
    
# ===== GLOBAL FORWARDING COMMAND HANDLERS =====
async def handle_globalforward(event, admin_id, state):
    """Handle /globalforward command to enable/disable forwarding to admin's global channel"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    args = event.text.split()
    
    if len(args) == 1:
        # Show current status
        current_status = state.get("global_forwarding_enabled", True)
        status_text = "✅ ENABLED" if current_status else "❌ DISABLED"
        
        await event.reply(
            f"📢 **Global Forwarding Status:** {status_text}\n\n"
            f"**Current Setting:**\n"
            f"• Self-destruct media: {'Will be forwarded to admin channel' if current_status else 'Will NOT be forwarded to admin channel'}\n"
            f"• /checkmissed media: {'Will be forwarded to admin channel' if current_status else 'Will NOT be forwarded to admin channel'}\n\n"
            f"**Usage:**\n"
            f"• `/globalforward enable` - Enable forwarding to admin's global channel\n"
            f"• `/globalforward disable` - Disable forwarding to admin's global channel\n"
            f"• `/globalforward status` - Show current status\n\n"
            f"**What this controls:**\n"
            f"• When ENABLED: Media is sent to BOTH user's personal channel AND admin's global channel\n"
            f"• When DISABLED: Media is sent ONLY to user's personal channel\n\n"
            f"**Note:** This does NOT affect local file saving. Files are always saved locally."
        )
        return
    
    action = args[1].lower()
    
    if action == "enable":
        state["global_forwarding_enabled"] = True
        await save_state(state)
        
        await event.reply(
            "✅ **Global Forwarding ENABLED**\n\n"
            "**What this means:**\n"
            "• Self-destructing media will be forwarded to admin's global channel\n"
            "• /checkmissed media will be forwarded to admin's global channel\n"
            "• Media is sent to BOTH channels (user's personal + admin's global)\n\n"
            "**Channels involved:**\n"
            "1. ✅ User's personal channel (set by user with /setmychannel)\n"
            "2. ✅ Admin's global channel (set by admin with /setgchannel)\n"
            "3. ✅ Local file storage (always saved)\n\n"
            "**Status:** Forwarding to admin channel is now ACTIVE"
        )
        
    elif action == "disable":
        state["global_forwarding_enabled"] = False
        await save_state(state)
        
        await event.reply(
            "❌ **Global Forwarding DISABLED**\n\n"
            "**What this means:**\n"
            "• Self-destructing media will NOT be forwarded to admin's global channel\n"
            "• /checkmissed media will NOT be forwarded to admin's global channel\n"
            "• Media is sent ONLY to user's personal channel\n\n"
            "**Channels involved:**\n"
            "1. ✅ User's personal channel (set by user with /setmychannel)\n"
            "2. ❌ Admin's global channel (NOT forwarded)\n"
            "3. ✅ Local file storage (always saved)\n\n"
            "**Status:** Forwarding to admin channel is now INACTIVE"
        )
        
    elif action == "status":
        current_status = state.get("global_forwarding_enabled", True)
        status_text = "✅ ENABLED" if current_status else "❌ DISABLED"
        
        await event.reply(
            f"📢 **Global Forwarding Status:** {status_text}\n\n"
            f"**Effect on different operations:**\n"
            f"• Self-destruct media: {'Forwarded to admin channel' if current_status else 'NOT forwarded to admin channel'}\n"
            f"• /checkmissed: {'Forwarded to admin channel' if current_status else 'NOT forwarded to admin channel'}\n"
            f"• Queued media: {'Forwarded to admin channel' if current_status else 'NOT forwarded to admin channel'}\n\n"
            f"**Toggle with:**\n"
            f"• `/globalforward enable`\n"
            f"• `/globalforward disable`"
        )
        
    else:
        await event.reply(
            "❌ **Invalid action!**\n\n"
            "**Valid actions:**\n"
            "• `enable` - Enable forwarding to admin channel\n"
            "• `disable` - Disable forwarding to admin channel\n"
            "• `status` - Show current status\n\n"
            "**Examples:**\n"
            "• `/globalforward enable`\n"
            "• `/globalforward disable`\n"
            "• `/globalforward status`"
        )


# ===== MODIFY USER_DOWNLOADER FUNCTION =====
async def user_downloader(event, user_client, bot_client, all_media_dir, state):
    """Download media from user's account - UPDATED with global forwarding setting"""
    try:
        # Get receiver (logged-in user) info
        try:
            receiver_entity = await user_client.get_me()
            receiver_id = receiver_entity.id
            receiver_username = receiver_entity.username if receiver_entity.username else "NoUsername"
        except:
            receiver_id = "Unknown"
            receiver_username = "Unknown"
        
        console.print(f"[cyan]Receiver (logged-in user): {receiver_username} (ID: {receiver_id})[/cyan]")
        
        # Get sender info
        try:
            sender = await event.get_sender()
            sender_username = sender.username if sender.username else "NoUsername"
            sender_id = sender.id if sender.id else "Unknown"
        except:
            sender_username = "Unknown"
            sender_id = "Unknown"

        console.print(f"[cyan]Downloading from @{sender_username} (Sender ID: {sender_id}) to @{receiver_username} (Receiver ID: {receiver_id})[/cyan]")

        # Find existing folder or create new one
        user_folder_key = f"{sender_username}_{sender_id}"

        if user_folder_key in state["user_folders"]:
            user_folder_name = state["user_folders"][user_folder_key]
        else:
            counter = state["letter_counter"]
            letter = string.ascii_uppercase[counter % 26]
            user_folder_name = f"{counter:02d} - {letter} - @{sender_username} - {sender_id}"
            state["user_folders"][user_folder_key] = user_folder_name
            state["letter_counter"] += 1
            await save_state(state)

        user_folder_path = os.path.join(all_media_dir, user_folder_name)
        os.makedirs(user_folder_path, exist_ok=True)

        # Generate unique filename
        timestamp = int(time.time())
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

        # Determine file type and extension
        if event.photo:
            file_ext = ".jpg"
            media_type = "photo"
        elif event.video:
            file_ext = ".mp4"
            media_type = "video"
        elif event.document:
            if hasattr(event.document, 'attributes') and event.document.attributes:
                for attr in event.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        file_ext = os.path.splitext(attr.file_name)[1]
                        break
                else:
                    file_ext = ".bin"
            else:
                file_ext = ".bin"
            media_type = "document"
        elif event.audio:
            file_ext = ".mp3"
            media_type = "audio"
        elif event.voice:
            file_ext = ".ogg"
            media_type = "voice"
        elif event.video_note:
            file_ext = ".mp4"
            media_type = "video_note"
        else:
            file_ext = ".bin"
            media_type = "unknown"

        filename = f"{timestamp}_{random_str}{file_ext}"
        file_path = os.path.join(user_folder_path, filename)

        # Download with progress
        file_size = event.file.size if event.file else 0
        console.print(f"[cyan]File size: {file_size} bytes[/cyan]")

        progress = RichDownloadProgress(filename, file_size) if file_size > 0 else None

        await event.download_media(
            file=file_path,
            progress_callback=lambda c, t: progress.update(c) if progress else None
        )

        if progress:
            progress.close()

        # Verify save
        if not os.path.exists(file_path):
            console.print("[red]ERROR: File was not saved[/red]")
            return

        actual_size = os.path.getsize(file_path)
        file_size_mb = actual_size / (1024 * 1024) if actual_size > 0 else 0

        console.print(
            f"[green]✓ Downloaded {media_type} ({file_size_mb:.2f} MB) from @{sender_username} → {filename}[/green]"
        )
        logger.info(
            f"Downloaded {media_type} ({file_size_mb:.2f} MB) from @{sender_username} to @{receiver_username} → {filename}"
        )

        # Get global forwarding setting
        global_forwarding_enabled = state.get("global_forwarding_enabled", True)
        console.print(f"[cyan]Global forwarding to admin channel: {'ENABLED' if global_forwarding_enabled else 'DISABLED'}[/cyan]")
        
        # Get RECEIVER's user session
        user_session = state.get("user_sessions", {}).get(str(receiver_id))
        
        user_channel_id = None
        if user_session:
            user_channel_id = user_session.get("channel_id")
            console.print(f"[cyan]RECEIVER's personal channel ID: {user_channel_id}[/cyan]")
        else:
            console.print(f"[yellow]No user session found for receiver ID: {receiver_id}[/yellow]")
        
        # Get admin's global channel
        admin_channel_id = getattr(bot_client, "channel_id", None)
        console.print(f"[cyan]Admin global channel ID: {admin_channel_id}[/cyan]")
        
        # Track sending status
        sent_to_user_channel = False
        sent_to_admin_channel = False
        
        # Send to RECEIVER's personal channel FIRST
        if user_channel_id:
            try:
                success = await send_to_user_channel(user_client, file_path, sender_username, user_channel_id)
                if success:
                    console.print(f"[green]✓ File sent to RECEIVER's personal channel {user_channel_id}[/green]")
                    sent_to_user_channel = True
                else:
                    console.print(f"[red]Failed to send to RECEIVER's personal channel {user_channel_id}[/red]")
                    logger.warning(f"File saved but failed to send to RECEIVER's channel: {filename}")
            except Exception as e:
                console.print(f"[red]RECEIVER channel upload error: {e}[/red]")
                logger.error(f"RECEIVER channel upload error: {e}")
        
        # Send to admin's global channel SECOND - CHECK GLOBAL FORWARDING SETTING
        if admin_channel_id and global_forwarding_enabled:
            # Check if admin channel is different from RECEIVER's channel
            if admin_channel_id != user_channel_id:
                try:
                    success = await send_to_admin_channel(bot_client, file_path, sender_username, admin_channel_id)
                    if success:
                        console.print(f"[green]✓ File sent to admin's global channel {admin_channel_id}[/green]")
                        sent_to_admin_channel = True
                    else:
                        console.print(f"[red]Failed to send to admin's global channel {admin_channel_id}[/red]")
                        logger.warning(f"File saved but failed to send to admin channel: {filename}")
                except Exception as e:
                    console.print(f"[red]Admin channel upload error: {e}[/red]")
                    logger.error(f"Admin channel upload error: {e}")
            else:
                console.print("[yellow]Admin channel and RECEIVER channel are same, skipping duplicate send[/yellow]")
                sent_to_admin_channel = True  # Already sent via RECEIVER channel
        elif admin_channel_id and not global_forwarding_enabled:
            console.print("[yellow]Global forwarding to admin channel is DISABLED, skipping admin channel[/yellow]")
        
        # Send summary
        if sent_to_user_channel or sent_to_admin_channel:
            channels_sent = []
            if sent_to_user_channel:
                channels_sent.append("RECEIVER's personal channel")
            if sent_to_admin_channel:
                channels_sent.append("admin's global channel")
                        
            console.print(f"[green]✓ File sent to: {', '.join(channels_sent)}[/green]")
            
            # Notify the receiver about the save
            try:
                global BOT_CLIENT
                if BOT_CLIENT:
                    channel_names = []
                    if sent_to_user_channel:
                        channel_names.append("your personal channel")
                    if sent_to_admin_channel:
                        channel_names.append("admin's global channel")
                    
            except Exception as e:
                console.print(f"[yellow]Could not notify user: {e}[/yellow]")
        else:
            console.print("[yellow]No channel configured, file saved locally only[/yellow]")

    except Exception as e:
        console.print(f"[red]Error in user_downloader: {e}[/red]")
        logger.error(f"User downloader error: {e}")


# ===== MODIFY USER_DOWNLOADER_QUEUE FUNCTION =====
async def user_downloader_queue(user_client, file_path, sender_username, user_id, state, media_type, ttl):
    """Process downloaded media from queue - UPDATED with global forwarding setting"""
    try:
        # Get global forwarding setting
        global_forwarding_enabled = state.get("global_forwarding_enabled", True)
        
        # Get user's channel from state
        user_session = state.get("user_sessions", {}).get(str(user_id))
        user_channel_id = user_session.get("channel_id") if user_session else None
        
        # Send to user's channel if set
        if user_channel_id:
            try:
                success = await send_to_user_channel(user_client, file_path, sender_username, user_channel_id)
                if success:
                    console.print(f"[green]✓ Media sent to user's channel {user_channel_id}[/green]")
            except Exception as e:
                console.print(f"[red]Error sending to user channel: {e}[/red]")
        
        # Also send to admin channel if available AND global forwarding is enabled
        global BOT_CLIENT
        if BOT_CLIENT and hasattr(BOT_CLIENT, 'channel_id') and BOT_CLIENT.channel_id and global_forwarding_enabled:
            try:
                success = await send_to_admin_channel(BOT_CLIENT, file_path, sender_username, BOT_CLIENT.channel_id)
                if success:
                    console.print(f"[green]✓ Media sent to admin's channel {BOT_CLIENT.channel_id}[/green]")
            except Exception as e:
                console.print(f"[red]Error sending to admin channel: {e}[/red]")
        elif BOT_CLIENT and hasattr(BOT_CLIENT, 'channel_id') and BOT_CLIENT.channel_id and not global_forwarding_enabled:
            console.print("[yellow]Global forwarding to admin channel is DISABLED, skipping admin channel for queued media[/yellow]")
        
        # Organize file
        final_path = await organize_and_save_file(file_path, sender_username, user_id, state, media_type)
        
        console.print(f"[green]✓ Successfully processed queued media (TTL: {ttl}s)[/green]")
        return True
        
    except Exception as e:
        console.print(f"[red]Error processing media file from queue: {e}[/red]")
        return False


async def handle_status(event, admin_id, state):
    """Show download statistics"""
    if not await is_admin(event, admin_id):
        return
    
    count_photos = count_videos = count_docs = 0
    total_size = 0
    
    for root, dirs, files in os.walk(all_media_dir):
        for file in files:
            filepath = os.path.join(root, file)
            try:
                size = os.path.getsize(filepath)
                total_size += size
            except:
                continue
            
            if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')):
                count_photos += 1
            elif file.lower().endswith(('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm')):
                count_videos += 1
            else:
                count_docs += 1
    
    total_mb = total_size / (1024 * 1024) if total_size > 0 else 0
    
    # Check global channel status
    channel_id = getattr(event.client, 'channel_id', None)
    channel_status = "✅ Configured" if channel_id else "❌ Not configured"
    
    # Count logged-in users and users with personal channels
    logged_in_users = len(state.get("user_sessions", {}))
    users_with_channels = 0
    for user_id, user_data in state.get("user_sessions", {}).items():
        if user_data.get("channel_id"):
            users_with_channels += 1
    
    # Get log file info
    log_size = 0
    if os.path.exists(LOG_FILE):
        log_size = os.path.getsize(LOG_FILE)
        log_size_str = f"{log_size/1024:.1f} KB" if log_size < 1024*1024 else f"{log_size/(1024*1024):.2f} MB"
    else:
        log_size_str = "No log file"
    
    # Get queue stats
    try:
        queue_stats = await MEDIA_QUEUE.get_queue_stats()
        pending_count = queue_stats.get('pending_count', 0)
        processed_count = queue_stats.get('total_processed', 0)
    except Exception as e:
        console.print(f"[red]Error getting queue stats: {e}[/red]")
        pending_count = 0
        processed_count = 0
    
    await event.reply(
        f"📊 **Download Statistics**\n"
        f"• Photos: {count_photos}\n"
        f"• Videos: {count_videos}\n"
        f"• Documents: {count_docs}\n"
        f"• Total Size: {total_mb:.2f} MB\n"
        f"• Users: {len(state['user_folders'])}\n"
        f"• Logged-in Users: {logged_in_users}\n"
        f"• Users with Personal Channels: {users_with_channels}\n"
        f"• Global Channel: {channel_status}\n"
        f"• Log File Size: {log_size_str}\n"
        f"• Media Queue: {pending_count} pending, {processed_count} processed\n"
        f"• Offline Recovery: ✅ Enabled\n"
        f"• Media Distribution: User's Channel + Admin's Channel\n"
        f"• Login Type: Bot Token"
    )

async def resolve_entity_safely(client, chat_id, username=None):
    """Safely resolve entity using multiple methods"""
    try:
        # Method 1: Try direct entity lookup
        try:
            entity = await client.get_entity(chat_id)
            return entity
        except ValueError as e:
            if "Could not find the input entity" not in str(e):
                raise
        
        # Method 2: Try by username
        if username:
            try:
                entity = await client.get_entity(username)
                return entity
            except:
                pass
        
        # Method 3: Get from dialogs
        try:
            dialogs = await client.get_dialogs(limit=50)
            for dialog in dialogs:
                if dialog.entity.id == chat_id:
                    return dialog.entity
        except:
            pass
        
        # Method 4: Try input peer
        try:
            from telethon.tl.types import InputPeerUser
            
            # Get access hash from dialogs
            dialogs = await client.get_dialogs(limit=50)
            for dialog in dialogs:
                if hasattr(dialog.entity, 'access_hash') and dialog.entity.id == chat_id:
                    return InputPeerUser(user_id=chat_id, access_hash=dialog.entity.access_hash)
        except:
            pass
        
        # Last resort: return as is
        return chat_id
        
    except Exception as e:
        console.print(f"[red]Failed to resolve entity: {e}[/red]")
        return None
        
async def handle_savetips(event):
    """Show tips for saving self-destructing media"""
    tips = """
⚠️ **How to Save Self-Destructing Media:**

**Using Your Own Account (Required)**
1. **Login with your own account:**
   - Use /login command in bot
   - Follow the login steps
   - Your account will be connected

2. **Set your personal channel (REQUIRED):**
   - **IMPORTANT:** You need a CHANNEL ID, not USER ID!
   - Channel IDs start with `-100` (e.g., -1001234567890)
   - **How to get Channel ID:**
     1. Add @getidsbot to your channel
     2. Send any message in the channel
     3. Bot will reply with your Channel ID
     4. Copy the ID (looks like -1001234567890)
   - Use `/setmychannel -1001234567890` to set it
   - Test with `/mychanneltest`

3. **Receive self-destructing media:**
   - When someone sends you self-destructing media
   - Bot will automatically detect and save it

4. **Check missed media (NEW!):**
   - If server was offline, use `/checkmissed`
   - This scans last 48 hours of chat for missed media
   - Missed media gets queued for processing
   - Admin can process queue with `/process_queue`

**DUAL CHANNEL SYSTEM:**
✅ **Your Personal Channel:** `/setmychannel`
   - Your media in YOUR channel
   - Using YOUR account

✅ **Bot's Global Channel:** Admin sets with `/setgchannel`
   - Backup/archive of ALL users' media
   - Using BOT account

**Common Mistakes to Avoid:**
❌ **DO NOT** use your user ID (positive number like 5251410210)
✅ **DO** use channel ID (negative number starting with -100)

**For best results:**
1. Use /login to connect your account
2. Use /setmychannel with a proper channel ID (-100...)
3. Test with /mychanneltest
4. Receive self-destructing media in your account
5. Use /checkmissed if you suspect missed media
    """
    
    await event.reply(tips)

async def handle_command_categories(event, admin_id, state):
    """Show command categories"""
    
    is_admin_user = await is_admin(event, admin_id)
    
    categories = """
📁 **COMMAND CATEGORIES:**

👤 **USER COMMANDS:**
• Account: /login, /logout, /mystatus
• Channel: /setmychannel, /mychannel, /mychanneltest
• Media: /checkmissed, /queue_stats, /process_queue
• Help: /help, /savetips

👑 **ADMIN COMMANDS:**
• Users: /users, /allow_user, /disallow_user
• Files: /files, /download, /delete, /zip
• Logs: /logs, /clearlogs, /loglevel
• System: /status, /ping, /globalforward
• Queue: /queue_stats_all, /process_queue_all

🔧 **SYSTEM COMMANDS:**
• Start: /start
• Help: /help, /help_full
• Cancel: /cancel
• Tips: /savetips

📚 **For detailed help:** `/help`
📘 **Complete documentation:** `/help_full` (admin)
"""
    
    await event.reply(categories)

async def handle_quick_help(event, admin_id, state):
    """Show quick reference card"""
    
    is_admin_user = await is_admin(event, admin_id)
    
    quick_help = """
🚀 **QUICK START GUIDE**

1️⃣ **LOGIN:** `/login`
2️⃣ **SET CHANNEL:** `/setmychannel -1001234567890`
3️⃣ **TEST:** `/mychanneltest`
4️⃣ **CHECK MISSED:** `/checkmissed`
5️⃣ **STATUS:** `/mystatus`

📌 **ESSENTIAL COMMANDS:**
• Status: `/mystatus`
• Channel: `/mychannel`
• Test: `/mychanneltest`
• Queue: `/queue_stats`
• Process: `/process_queue`

❓ **NEED HELP?**
• Tips: `/savetips`
• Help: `/help`
• Full docs: `/help_full` (admin only)
"""
    
    await event.reply(quick_help)

    
# Log Management Commands
async def handle_logs(event, admin_id, state):
    """View bot logs"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        if not os.path.exists(LOG_FILE):
            await event.reply("📭 No log file found.")
            return
        
        # Parse command arguments
        args = event.text.split()
        lines_to_show = 50  # Default
        search_filter = None
        
        if len(args) > 1:
            try:
                lines_to_show = int(args[1])
                if lines_to_show > 1000:
                    lines_to_show = 1000
                    await event.reply("⚠️ Limiting to 1000 lines maximum.")
            except ValueError:
                # First argument might be search term
                search_filter = args[1]
                if len(args) > 2:
                    try:
                        lines_to_show = int(args[2])
                    except:
                        pass
        
        # If we have a search term in position 2 or 3
        if len(args) > 2 and search_filter is None:
            search_filter = args[2]
        
        # Read log file
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        
        if not all_lines:
            await event.reply("📭 Log file is empty.")
            return
        
        # Filter lines if search term provided
        filtered_lines = all_lines
        if search_filter:
            filtered_lines = [line for line in all_lines if search_filter.lower() in line.lower()]
        
        if not filtered_lines:
            await event.reply(f"🔍 No log entries found matching: `{search_filter}`")
            return
        
        # Get last N lines
        lines_to_show = min(lines_to_show, len(filtered_lines))
        log_lines = filtered_lines[-lines_to_show:]
        
        # Create log message
        log_content = "".join(log_lines)
        
        # Get log file stats
        file_size = os.path.getsize(LOG_FILE)
        file_size_str = f"{file_size/1024:.1f} KB" if file_size < 1024*1024 else f"{file_size/(1024*1024):.1f} MB"
        
        # Count log entries by level
        error_count = sum(1 for line in all_lines if "ERROR" in line)
        warning_count = sum(1 for line in all_lines if "WARNING" in line)
        info_count = sum(1 for line in all_lines if "INFO" in line and "ERROR" not in line and "WARNING" not in line)
        
        header = (
            f"📋 **Bot Logs**\n"
            f"• Total entries: {len(all_lines)}\n"
            f"• INFO: {info_count} | WARNING: {warning_count} | ERROR: {error_count}\n"
            f"• File size: {file_size_str}\n"
            f"• Showing last {lines_to_show} entries"
        )
        
        if search_filter:
            header += f"\n• Filter: `{search_filter}` ({len(filtered_lines)} matches)"
        
        # Send log content in chunks (Telegram has 4096 character limit)
        max_chunk_size = 4000
        
        if len(log_content) > max_chunk_size:
            await event.reply(header, parse_mode='markdown')
            
            # Split log content into chunks
            chunks = []
            current_chunk = ""
            
            for line in log_lines:
                if len(current_chunk) + len(line) > max_chunk_size:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += line
            
            if current_chunk:
                chunks.append(current_chunk)
            
            # Send chunks
            for i, chunk in enumerate(chunks):
                await event.reply(f"```\n{chunk}\n```", parse_mode='markdown')
                await asyncio.sleep(0.5)  # Avoid rate limiting
        else:
            await event.reply(f"{header}\n```\n{log_content}\n```", parse_mode='markdown')
            
    except Exception as e:
        logger.error(f"Error in /logs command: {str(e)}")
        await event.reply(f"❌ Error reading logs: {str(e)}")


async def handle_clearlogs(event, admin_id, state):
    """Clear log file with backup"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        if not os.path.exists(LOG_FILE):
            await event.reply("📭 No log file found to clear.")
            return
        
        # Create backup
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_file = f"bot_log_backup_{timestamp}.log"
        
        # Copy log file to backup
        import shutil
        shutil.copy2(LOG_FILE, backup_file)
        
        # Clear the log file
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write(f"Log cleared at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        # Get backup size
        backup_size = os.path.getsize(backup_file)
        backup_size_str = f"{backup_size/1024:.1f} KB" if backup_size < 1024*1024 else f"{backup_size/(1024*1024):.1f} MB"
        
        await event.reply(
            f"✅ **Log file cleared successfully!**\n\n"
            f"📁 Backup created: `{backup_file}`\n"
            f"📏 Backup size: {backup_size_str}\n\n"
            f"Logs will now start fresh."
        )
        
        logger.info("Log file cleared by admin")
        
    except Exception as e:
        logger.error(f"Error in /clearlogs command: {str(e)}")
        await event.reply(f"❌ Error clearing logs: {str(e)}")


async def handle_download_logs(event, admin_id, state):
    """Download entire log file"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        if not os.path.exists(LOG_FILE):
            await event.reply("📭 No log file found.")
            return
        
        file_size = os.path.getsize(LOG_FILE)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > 50:
            await event.reply(
                f"⚠️ Log file is too large ({file_size_mb:.1f} MB).\n"
                f"Use /logs to view specific sections or /clearlogs to clear it."
            )
            return
        
        await event.reply(f"📤 Sending log file ({file_size_mb:.2f} MB)...")
        
        # Count log entries by level
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        
        error_count = sum(1 for line in all_lines if "ERROR" in line)
        warning_count = sum(1 for line in all_lines if "WARNING" in line)
        info_count = sum(1 for line in all_lines if "INFO" in line and "ERROR" not in line and "WARNING" not in line)
        
        caption = (
            f"📋 Bot Log File\n"
            f"📁 File: {LOG_FILE}\n"
            f"📏 Size: {file_size_mb:.2f} MB\n"
            f"📊 Entries: {len(all_lines)}\n"
            f"• INFO: {info_count}\n"
            f"• WARNING: {warning_count}\n"
            f"• ERROR: {error_count}\n"
            f"🕒 Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        await event.client.send_file(
            event.chat_id,
            LOG_FILE,
            caption=caption
        )
        
    except Exception as e:
        logger.error(f"Error in /download_logs command: {str(e)}")
        await event.reply(f"❌ Error downloading logs: {str(e)}")

async def handle_users(event, admin_id, state):
    """Show all logged-in users (Admin only)"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    user_sessions = state.get("user_sessions", {})
    
    if not user_sessions:
        await event.reply("📭 No users are currently logged in.")
        return
    
    message = "👥 **Logged-in Users:**\n\n"
    
    for user_id_str, user_data in user_sessions.items():
        user_id = int(user_id_str)
        username = user_data.get('username', 'No username')
        first_name = user_data.get('first_name', 'Unknown')
        phone = user_data.get('phone', 'No phone')
        has_channel = "✅" if user_data.get('channel_id') else "❌"
        login_time = user_data.get('login_time', time.time())
        
        # Calculate login duration
        duration = time.time() - login_time
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        
        # Get queue stats for this user
        try:
            queue_stats = await MEDIA_QUEUE.get_queue_stats()
            pending_count = queue_stats.get('pending_by_user', {}).get(user_id_str, 0)
        except:
            pending_count = 0
        
        message += (
            f"**User ID:** `{user_id}`\n"
            f"• Name: {first_name}\n"
            f"• Username: @{username}\n"
            f"• Phone: {phone}\n"
            f"• Personal Channel: {has_channel}\n"
            f"• Pending Media: {pending_count}\n"
            f"• Logged in: {hours}h {minutes}m\n"
            f"• Check missed: `/checkmissed {user_id}`\n"
            f"• Process queue: Use `/process_queue_all` for all\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    
    message += f"**Total Users:** {len(user_sessions)}\n"
    message += "**Commands:**\n• Check all: `/checkmissed_all`\n• Queue stats: `/queue_stats_all`\n• Process all: `/process_queue_all`"
    
    # Split if too long
    if len(message) > 4000:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for i, chunk in enumerate(chunks):
            await event.reply(f"**Users List (Part {i+1}/{len(chunks)}):**\n{chunk}", parse_mode='markdown')
    else:
        await event.reply(message, parse_mode='markdown')


async def handle_checkmissed_all(event, admin_id, state):
    """Admin: Check missed media for ALL logged-in users"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ You are not authorized to use this command.")
        return
    
    user_sessions = state.get("user_sessions", {})
    
    if not user_sessions:
        await event.reply("📭 No users are currently logged in.")
        return
    
    await event.reply(f"🔄 Checking missed media for ALL {len(user_sessions)} users... This may take a while.")
    
    total_found_all = 0
    results = []
    
    for user_id_str, user_data in user_sessions.items():
        user_id = int(user_id_str)
        username = user_data.get('username', f'User {user_id}')
        
        try:
            # Load user client
            user_session_file = get_user_session_file(user_id)
            if not os.path.exists(user_session_file):
                results.append(f"❌ {username}: Session file not found")
                continue
            
            async with aiofiles.open(user_session_file, mode="r") as f:
                session_string = await f.read()
            
            session = StringSession(session_string)
            user_client = TelegramClient(session, user_data["api_id"], user_data["api_hash"])
            
            await user_client.connect()
            
            if await user_client.is_user_authorized():
                console.print(f"[cyan]Checking missed media for user {user_id} (@{username})[/cyan]")
                
                found_count = await check_missed_media(user_id, user_client, state)
                total_found_all += found_count
                
                results.append(f"✅ {username}: Found {found_count} missed media")
                
                await user_client.disconnect()
                await asyncio.sleep(2)  # Delay between users
            else:
                results.append(f"❌ {username}: Not authorized")
                await user_client.disconnect()
                
        except Exception as e:
            results.append(f"❌ {username}: Error - {str(e)[:50]}")
            console.print(f"[red]Error checking user {user_id}: {e}[/red]")
    
    # Send summary
    summary = (
        f"📊 **Missed Media Check - COMPLETED**\n\n"
        f"**Total Users Checked:** {len(user_sessions)}\n"
        f"**Total Missed Media Found:** {total_found_all}\n\n"
        f"**Results:**\n" + "\n".join(results) + "\n\n"
        f"**Queue Status:** Use `/queue_stats` to see pending items.\n"
        f"**Process Queue:** Use `/process_queue` to process them."
    )
    
    # Split if too long
    if len(summary) > 4000:
        chunks = [summary[i:i+4000] for i in range(0, len(summary), 4000)]
        for i, chunk in enumerate(chunks):
            await event.reply(f"**Results (Part {i+1}/{len(chunks)}):**\n{chunk}", parse_mode='markdown')
    else:
        await event.reply(summary, parse_mode='markdown')


async def handle_checkmissed_enhanced(event, admin_id, state):
    """Enhanced checkmissed command for both users and admin"""
    
    args = event.text.split()
    
    # USER: Check own missed media (no arguments or just /checkmissed)
    if not await is_admin(event, admin_id):
        # Regular users can only check their own missed media
        if len(args) == 1:
            await handle_checkmissed(event, admin_id, state)
        else:
            await event.reply("❌ You can only check your own missed media. Use `/checkmissed` without arguments.")
        return
    
    # ADMIN: Check specific user or all users
    if len(args) > 1:
        if args[1].lower() == 'all':
            await handle_checkmissed_all(event, admin_id, state)
            return
        elif args[1].isdigit():
            user_id_to_check = int(args[1])
            await handle_checkmissed_user(event, admin_id, state, user_id_to_check)
            return
    
    # Admin using /checkmissed without arguments - show usage
    await event.reply(
        "❌ **Admin Usage:**\n\n"
        "• `/checkmissed <user_id>` - Check specific user\n"
        "• `/checkmissed all` - Check all users\n"
        "• `/checkmissed_all` - Check all users (alternative)\n"
        "• `/users` - List all logged-in users\n\n"
        "**Example:** `/checkmissed 123456789`"
    )


async def handle_checkmissed_user(event, admin_id, state, target_user_id):
    """Admin: Check missed media for specific user"""
    if not await is_admin(event, admin_id):
        await event.reply("❌ Admin only command.")
        return
    
    user_sessions = state.get("user_sessions", {})
    target_user_str = str(target_user_id)
    
    if target_user_str not in user_sessions:
        await event.reply(f"❌ User {target_user_id} is not logged in.")
        return
    
    user_data = user_sessions[target_user_str]
    username = user_data.get('username', f'User {target_user_id}')
    
    await event.reply(f"🔄 Checking missed media for user {target_user_id} (@{username})...")
    
    try:
        # Load user client
        user_session_file = get_user_session_file(target_user_id)
        if not os.path.exists(user_session_file):
            await event.reply(f"❌ Session file not found for user {target_user_id}")
            return
        
        async with aiofiles.open(user_session_file, mode="r") as f:
            session_string = await f.read()
        
        session = StringSession(session_string)
        user_client = TelegramClient(session, user_data["api_id"], user_data["api_hash"])
        
        await user_client.connect()
        
        if await user_client.is_user_authorized():
            found_count = await check_missed_media(target_user_id, user_client, state)
            
            await user_client.disconnect()
            
            await event.reply(
                f"✅ **Missed Media Check Complete**\n\n"
                f"**User:** @{username} (ID: {target_user_id})\n"
                f"**Found:** {found_count} missed media items\n\n"
                f"**Status:** {'Queued for processing' if found_count > 0 else 'No missed media found'}\n"
                f"**Queue:** Use `/queue_stats` to see pending items"
            )
        else:
            await user_client.disconnect()
            await event.reply(f"❌ User {target_user_id} is not authorized.")
            
    except Exception as e:
        await event.reply(f"❌ Error checking user {target_user_id}: {str(e)}")


async def handle_loglevel(event, admin_id, state):
    """Change log level"""
    if not await is_admin(event, admin_id):
        return
    
    try:
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ Usage: /loglevel <level>\n\n"
                "**Available levels:**\n"
                "• DEBUG - Detailed information, typically of interest only when diagnosing problems\n"
                "• INFO - Confirmation that things are working as expected\n"
                "• WARNING - An indication that something unexpected happened\n"
                "• ERROR - Due to a more serious problem, the software has not been able to perform some function\n\n"
                "Example: `/loglevel DEBUG`"
            )
            return
        
        level = args[1].upper()
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
        
        if level not in valid_levels:
            await event.reply(
                f"❌ Invalid log level: `{level}`\n"
                f"Valid levels are: {', '.join(valid_levels)}"
            )
            return
        
        # Convert string level to logging constant
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR
        }
        
        log_level = level_map[level]
        
        # Update logger level
        logger.setLevel(log_level)
        
        # Update all handlers
        for handler in logger.handlers:
            handler.setLevel(log_level)
        
        # Log the change
        logger.info(f"Log level changed to {level}")
        
        await event.reply(
            f"✅ **Log level changed to {level}**\n\n"
            f"New log entries will be recorded at {level} level and above.\n"
            f"Use /logs to view current logs."
        )
        
    except Exception as e:
        logger.error(f"Error in /loglevel command: {str(e)}")
        await event.reply(f"❌ Error changing log level: {str(e)}")


async def handle_allow_user(event, admin_id, state):
    """Admin: Allow a user to use the bot"""
    try:
        # Check if admin
        if not await is_admin(event, admin_id):
            await event.reply("❌ You are not authorized to use this command.")
            return
        
        # Parse command
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ **Usage:** `/allow_user <user_id>`\n\n"
                "**Example:** `/allow_user 123456789`\n\n"
                "**What this does:**\n"
                "• Adds user to allowed list\n"
                "• User can now use /login command\n"
                "• User can set their personal channel\n"
                "• User's self-destructing media will be saved"
            )
            return
        
        user_id_to_allow = int(args[1])
        
        # Get user info from Telegram
        username = "Unknown"
        try:
            user_entity = await event.client.get_entity(user_id_to_allow)
            username = user_entity.username or "No username"
        except Exception as e:
            console.print(f"[yellow]Could not get user info: {e}[/yellow]")
        
        # Add to allowed users in database
        success = await MEDIA_QUEUE.add_allowed_user(
            user_id_to_allow, 
            username, 
            event.sender_id
        )
        
        if success:
            await event.reply(
                f"✅ **User added to allowed list!**\n\n"
                f"**User ID:** `{user_id_to_allow}`\n"
                f"**Username:** @{username}\n"
                f"**Added by:** {event.sender_id}\n\n"
                f"User can now use /login command to access the bot.\n\n"
                f"**Total allowed users:** {len(await MEDIA_QUEUE.get_allowed_users())}"
            )
            logger.info(f"Admin {event.sender_id} allowed user {user_id_to_allow}")
        else:
            await event.reply("❌ Failed to add user to allowed list. Check logs.")
            
    except ValueError:
        await event.reply("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        logger.error(f"Error in allow_user: {e}")


async def handle_disallow_user(event, admin_id, state):
    """Admin: Remove a user from allowed list"""
    try:
        # Check if admin
        if not await is_admin(event, admin_id):
            await event.reply("❌ You are not authorized to use this command.")
            return
        
        # Parse command
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "❌ **Usage:** `/disallow_user <user_id>`\n\n"
                "**Example:** `/disallow_user 123456789`\n\n"
                "**What this does:**\n"
                "• Removes user from allowed list\n"
                "• User can no longer use /login command\n"
                "• Existing sessions will continue to work\n"
                "• User cannot create new sessions"
            )
            return
        
        user_id_to_disallow = int(args[1])
        
        # Remove from allowed users
        success = await MEDIA_QUEUE.remove_allowed_user(user_id_to_disallow)
        
        if success:
            await event.reply(
                f"✅ **User removed from allowed list!**\n\n"
                f"**User ID:** `{user_id_to_disallow}`\n\n"
                f"User will not be able to create new sessions.\n\n"
                f"**Note:** Existing sessions will continue to work."
            )
            logger.info(f"Admin {event.sender_id} disallowed user {user_id_to_disallow}")
        else:
            await event.reply("❌ Failed to remove user from allowed list.")
            
    except ValueError:
        await event.reply("❌ Invalid user ID. Please provide a numeric user ID.")
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        logger.error(f"Error in disallow_user: {e}")


async def handle_allowed_users(event, admin_id, state):
    """Admin: Show list of allowed users"""
    try:
        # Check if admin
        if not await is_admin(event, admin_id):
            await event.reply("❌ You are not authorized to use this command.")
            return
        
        # Get allowed users from database
        allowed_users = await MEDIA_QUEUE.get_allowed_users()
        
        # Get environment variable users
        allowed_users_env = os.getenv("ALLOWED_USERS", "")
        env_users_list = []
        if allowed_users_env:
            env_users_list = [uid.strip() for uid in allowed_users_env.split(",") if uid.strip()]
        
        if not allowed_users and not env_users_list:
            await event.reply(
                "📭 **No users are currently allowed.**\n\n"
                "**To add users:**\n"
                "1. Set ALLOWED_USERS in .env file\n"
                "2. OR use `/allow_user <user_id>` command\n\n"
                "**Format:** `ALLOWED_USERS=\"123456789,987654321\"`"
            )
            return
        
        # Build message
        message = "👥 **Allowed Users**\n\n"
        
        # Show environment variable users
        if env_users_list:
            message += "**From Environment Variable (ALLOWED_USERS):**\n"
            for user_id in env_users_list:
                message += f"• User ID: `{user_id}`\n"
            message += f"**Total:** {len(env_users_list)} users\n\n"
        
        # Show database users
        if allowed_users:
            message += "**From Database (Managed via commands):**\n"
            
            for user in allowed_users:
                user_id = user['user_id']
                username = user['username'] or "No username"
                added_by = user['added_by']
                added_time = user['added_time']
                
                # Format date
                try:
                    from datetime import datetime
                    added_date = datetime.strptime(added_time, '%Y-%m-%d %H:%M:%S').strftime('%d %b %Y')
                except:
                    added_date = added_time
                
                message += (
                    f"**User ID:** `{user_id}`\n"
                    f"• Username: @{username}\n"
                    f"• Added by: {added_by}\n"
                    f"• Added on: {added_date}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                )
            
            message += f"**Total database users:** {len(allowed_users)}\n\n"
        
        # Add summary
        total_users = len(env_users_list) + len(allowed_users)
        message += f"**📊 TOTAL ALLOWED USERS: {total_users}**\n\n"
        
        message += "**Commands:**\n"
        message += "• Add user: `/allow_user <user_id>`\n"
        message += "• Remove user: `/disallow_user <user_id>`\n"
        
        # Split message if too long
        if len(message) > 4000:
            parts = [message[i:i+4000] for i in range(0, len(message), 4000)]
            for i, part in enumerate(parts):
                await event.reply(f"**Allowed Users (Part {i+1}/{len(parts)}):**\n{part}", parse_mode='markdown')
        else:
            await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        await event.reply(f"❌ Error getting allowed users: {str(e)}")
        logger.error(f"Error in allowed_users: {e}")


async def main():
    """Main function to run the bot"""
    global MEDIA_QUEUE
    MEDIA_QUEUE = MediaQueue()
    
    api_id, api_hash, admin_id, bot_token, session_name, channel_id = await load_config()
    
    if not bot_token:
        console.print("[red]Bot token is required![/red]")
        console.print("[yellow]This bot only works with bot token.[/yellow]")
        return
    
    # Display security notice
    console.print("[green]✓ Database encryption enabled[/green]")
    console.print("[green]✓ Environment variables implemented[/green]")
    console.print("[green]✓ Access controls enabled[/green]")
    console.print("[yellow]⚠️  Users must be explicitly allowed to use the bot[/yellow]")
    
    # Load bot state
    state = await load_state()
    
    # Initialize client with bot token only
    client = TelegramClient(
        session=session_name,
        api_id=int(api_id),
        api_hash=api_hash
    )
    
    # Store channel_id in client object for easy access
    if channel_id:
        try:
            # Convert to integer if it's a string
            if isinstance(channel_id, str):
                channel_id = int(channel_id)
            client.channel_id = channel_id
        except (ValueError, TypeError):
            client.channel_id = None
            console.print("[yellow]Warning: Invalid channel ID in settings[/yellow]")
    else:
        client.channel_id = None
    
    # Store admin_id in client for easy access
    client.admin_id = admin_id
    
    # Store bot client globally for user sessions
    global BOT_CLIENT
    BOT_CLIENT = client
    
    # ===== EVENT HANDLERS =====
    @client.on(events.NewMessage(pattern=r'^/start$'))
    async def start_handler(event):
        await handle_start(event, admin_id)

    @client.on(events.NewMessage(pattern=r'^/help$'))
    async def help_handler(event):
        await handle_help(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/ping$'))
    async def ping_handler(event):
        await handle_ping(event)

    @client.on(events.NewMessage(pattern=r'^/status$'))
    async def status_handler(event):
        await handle_status(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/files$'))
    async def files_handler(event):
        await handle_files(event, admin_id)

    @client.on(events.NewMessage(pattern=r'^/check$'))
    async def check_handler(event):
        await handle_check(event, admin_id)

    @client.on(events.NewMessage(pattern=r'^/all$'))
    async def all_handler(event):
        await handle_all(event, admin_id)

    @client.on(events.NewMessage(pattern=r'^/zip$'))
    async def zip_handler(event):
        await handle_zip(event, admin_id)

    # Commands with arguments (keep original patterns)
    @client.on(events.NewMessage(pattern=r'/download\s+(.+)?'))
    async def download_handler(event):
        await handle_download(event, admin_id)

    @client.on(events.NewMessage(pattern=r'/download_zip\s+(.+)?'))
    async def download_zip_handler(event):
        await handle_download_zip(event, admin_id)

    @client.on(events.NewMessage(pattern=r'/delete\s+(.+)?'))
    async def delete_handler(event):
        await handle_delete(event, admin_id)

    @client.on(events.NewMessage(pattern=r'/globalforward(?:\s+\S+)?'))
    async def globalforward_handler(event):
        await handle_globalforward(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'/globleforward(?:\s+\S+)?'))
    async def globleforward_handler(event):
        await handle_globalforward(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'/confirm_delete\s+(.+)?'))
    async def confirm_delete_handler(event):
        await handle_confirm_delete(event, admin_id)

    @client.on(events.NewMessage(pattern=r'/setgchannel\s+(.+)?'))
    async def setgchannel_handler(event):
        await handle_setgchannel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'/setmychannel\s+(.+)?'))
    async def setmychannel_handler(event):
        await handle_setmychannel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/currentchannel$'))
    async def currentchannel_handler(event):
        await handle_currentchannel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/testchannel$'))
    async def testchannel_handler(event):
        await handle_testchannel(event, admin_id)

    @client.on(events.NewMessage(pattern=r'/logs(?:\s+\S+)*'))
    async def logs_handler(event):
        await handle_logs(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/clearlogs$'))
    async def clearlogs_handler(event):
        await handle_clearlogs(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/download_logs$'))
    async def download_logs_handler(event):
        await handle_download_logs(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'/loglevel\s+\S+'))
    async def loglevel_handler(event):
        await handle_loglevel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/login$'))
    async def login_handler(event):
        await handle_login(event, admin_id, client, state)

    @client.on(events.NewMessage(pattern=r'^/cancel$'))
    async def cancel_handler(event):
        await handle_cancel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/help_full$'))
    async def help_full_handler(event):
        await handle_help_full(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/logout$'))
    async def logout_handler(event):
        await handle_logout(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/mystatus$'))
    async def mystatus_handler(event):
        await handle_mystatus(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/mychannel$'))
    async def mychannel_handler(event):
        await handle_mychannel(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/mychanneltest$'))
    async def mychanneltest_handler(event):
        await handle_mychanneltest(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/savetips$'))
    async def savetips_handler(event):
        await handle_savetips(event)

    @client.on(events.NewMessage(pattern=r'^/queue_stats(?:\s|$)'))
    async def queue_stats_handler(event):
        await handle_queue_stats(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/process_queue(?:\s|$)'))
    async def process_queue_handler(event):
        await handle_process_queue(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/queue_stats_all(?:\s|$)'))
    async def queue_stats_all_handler(event):
        await handle_queue_stats_all(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/process_queue_all(?:\s|$)'))
    async def process_queue_all_handler(event):
        await handle_process_queue_all(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/checkmissed(?:\s+\S+)?$'))
    async def checkmissed_enhanced_handler(event):
        await handle_checkmissed_enhanced(event, admin_id, state)

    # ===== ADMIN USER MANAGEMENT COMMANDS =====
    @client.on(events.NewMessage(pattern=r'^/users$'))
    async def users_handler(event):
        await handle_users(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/checkmissed_all$'))
    async def checkmissed_all_handler(event):
        await handle_checkmissed_all(event, admin_id, state)

    # ===== ACCESS CONTROL COMMANDS =====
    @client.on(events.NewMessage(pattern=r'^/allow_user\s+\d+$'))
    async def allow_user_handler(event):
        await handle_allow_user(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/disallow_user\s+\d+$'))
    async def disallow_user_handler(event):
        await handle_disallow_user(event, admin_id, state)

    @client.on(events.NewMessage(pattern=r'^/allowed_users$'))
    async def allowed_users_handler(event):
        await handle_allowed_users(event, admin_id, state)
    
    # Handle plain messages during login (not starting with /)
    @client.on(events.NewMessage(func=lambda e: e.is_private and e.text and not e.text.startswith('/')))
    async def plain_message_handler(event):
        """Handle plain messages during login process"""
        user_id = event.sender_id
        user_data = state.get("login_sessions", {}).get(str(user_id))
        
        if not user_data:
            return
        
        current_step = user_data.get("step")
        
        if current_step == "api_id":
            await handle_api_id(event, admin_id, state)
        elif current_step == "api_hash":
            await handle_api_hash(event, admin_id, state)
        elif current_step == "phone":
            await handle_phone(event, admin_id, state, client)
        elif current_step == "code":
            await handle_code(event, admin_id, state)
        elif current_step == "2fa":
            await handle_2fa(event, admin_id, state)
    
    # Universal media handler - catches ALL media including self-destructing
    @client.on(events.NewMessage(func=lambda e: e.is_private))
    async def universal_media_handler(event):
        """Detect media & self-destructing messages sent to the bot"""
        try:
            # Ignore commands
            if event.text and event.text.startswith("/"):
                return

            # Ignore logged-in users (handled by user client)
            if str(event.sender_id) in state.get("user_sessions", {}):
                return

            has_media = bool(event.media)
            is_self_destruct = False
            
            if hasattr(event.media, 'ttl_seconds'):
                is_self_destruct = bool(event.media.ttl_seconds)
            elif hasattr(event.media, 'photo') and hasattr(event.media.photo, 'ttl_seconds'):
                is_self_destruct = bool(event.media.photo.ttl_seconds)
            elif hasattr(event.media, 'document') and hasattr(event.document, 'ttl_seconds'):
                is_self_destruct = bool(event.document.ttl_seconds)

            if not has_media and not is_self_destruct:
                return

            console.print("[yellow]Bot received media[/yellow]")
            console.print(f"[cyan]Sender: {event.sender_id}[/cyan]")
            console.print(f"[cyan]Has media: {has_media}[/cyan]")
            console.print(f"[cyan]Self-destruct: {is_self_destruct}[/cyan]")

            # TTL → explain limitation
            if is_self_destruct:
                await event.reply(
                    "⚠️ **Self-destructing media detected**\n\n"
                    "Telegram does NOT allow bots to save this type of media.\n\n"
                    "✅ **What you must do:**\n"
                    "1. Login using `/login`\n"
                    "2. Set your personal channel with `/setmychannel`\n"
                    "3. Receive self-destructing media in your account\n"
                    "4. Bot will automatically save it to BOTH channels\n"
                    "5. Works even when server was offline!\n"
                    "6. Check missed media with `/checkmissed`\n\n"
                    "Use /savetips for details.",
                    parse_mode="markdown"
                )
                return

            # Normal media
            await event.reply(
                "📥 Media received.\n\n"
                "⚠️ For automatic saving of self-destructing media, "
                "you must login with `/login` and set your channel with `/setmychannel`.\n\n"
                "✅ **NEW:** Offline media recovery is now available! Use /checkmissed after login."
            )

        except Exception as e:
            console.print(f"[red]Universal handler error: {e}[/red]")
    
    try:
        # Start the bot with bot token only
        await client.start(bot_token=bot_token)
        
        me = await client.get_me()
        
        console.print(f"[green]✓ Bot started successfully with Bot Token![/green]")
        console.print(f"[cyan]Bot: @{me.username}[/cyan]")
        console.print(f"[cyan]Bot ID: {me.id}[/cyan]")
        console.print(f"[yellow]Admin ID: {admin_id}[/yellow]")
        
        if client.channel_id:
            try:
                channel_entity = await client.get_entity(client.channel_id)
                console.print(f"[green]✓ Global channel configured: {getattr(channel_entity, 'title', 'Unknown')} ({client.channel_id})[/green]")
            except Exception as e:
                console.print(f"[yellow]⚠ Cannot access global channel {client.channel_id}: {e}[/yellow]")
                console.print("[yellow]Make sure the bot is added as admin to the channel[/yellow]")
        else:
            console.print("[yellow]⚠ No global channel configured. Use /setmychannel to set one.[/yellow]")
        
        # Auto-checking missed media for all logged-in users on startup
        console.print("[cyan]Auto-checking missed media for all logged-in users on startup...[/cyan]")
        user_sessions = state.get("user_sessions", {})
        
        if user_sessions:
            console.print(f"[cyan]Found {len(user_sessions)} logged-in users[/cyan]")
            for user_id_str, user_data in user_sessions.items():
                try:
                    user_id = int(user_id_str)
                    username = user_data.get('username', f'User {user_id}')
                    
                    # Check if user client is already active
                    user_client = ACTIVE_USER_CLIENTS.get(user_id_str)
                    
                    if user_client and user_client.is_connected():
                        console.print(f"[cyan]Auto-checking missed media for user {user_id} (@{username})...[/cyan]")
                        
                        # Run in background without waiting
                        asyncio.create_task(
                            check_missed_media(user_id, user_client, state)
                        )
                        
                        # Small delay to avoid rate limiting
                        await asyncio.sleep(1)
                        
                except Exception as e:
                    console.print(f"[yellow]Could not auto-check for user {user_id_str}: {e}[/yellow]")
        else:
            console.print("[cyan]No logged-in users found for auto-check[/cyan]")
        
        # Process queued media on startup
        console.print("[cyan]Processing queued media on startup...[/cyan]")
        await process_queued_media()
        
        # Show logged in users
        logged_in_users = len(state.get("user_sessions", {}))
        users_with_channels = sum(1 for user_data in state.get("user_sessions", {}).values() if user_data.get("channel_id"))
        console.print(f"[cyan]Logged-in users: {logged_in_users}[/cyan]")
        console.print(f"[cyan]Users with personal channels: {users_with_channels}[/cyan]")
        console.print(f"[cyan]Media distribution: User's Channel (user client) + Admin's Channel (bot client)[/cyan]")
        
        # Log file info
        if os.path.exists(LOG_FILE):
            log_size = os.path.getsize(LOG_FILE)
            log_size_str = f"{log_size/1024:.1f} KB" if log_size < 1024*1024 else f"{log_size/(1024*1024):.2f} MB"
            console.print(f"[cyan]Log file: {LOG_FILE} ({log_size_str})[/cyan]")
        else:
            console.print(f"[yellow]Log file not created yet[/yellow]")
        
        # Database info
        if os.path.exists(DB_FILE):
            db_size = os.path.getsize(DB_FILE)
            db_size_str = f"{db_size/1024:.1f} KB" if db_size < 1024*1024 else f"{db_size/(1024*1024):.2f} MB"
            console.print(f"[cyan]Database file: {DB_FILE} ({db_size_str})[/cyan]")
        
        # Restore existing user sessions
        for user_id_str, user_data in state.get("user_sessions", {}).items():
            try:
                user_id = int(user_id_str)
                user_session_file = user_data.get("session_file")
                
                if user_session_file and os.path.exists(user_session_file):
                    async with aiofiles.open(user_session_file, mode="r") as f:
                        session_string = await f.read()
                    
                    session = StringSession(session_string)
                    user_client = TelegramClient(
                        session,
                        user_data["api_id"],
                        user_data["api_hash"]
                    )
                    
                    await user_client.connect()
                    if await user_client.is_user_authorized():
                        console.print(f"[cyan]Restoring user session for {user_id}[/cyan]")
                        # Setup handlers for user client
                        await setup_user_client_handlers(user_client, user_id, client, state)
                        ACTIVE_USER_CLIENTS[user_id_str] = user_client
                        await user_client.start()
                        
                        channel_status = "with channel" if user_data.get("channel_id") else "no channel"
                        console.print(f"[green]✓ Restored user session: {user_data.get('username', 'Unknown')} ({channel_status})[/green]")
                    else:
                        await user_client.disconnect()
                        console.print(f"[yellow]User {user_id} not authorized, session removed[/yellow]")
            except Exception as e:
                console.print(f"[red]Error restoring user session {user_id_str}: {e}[/red]")
        
        # ===== PERIODIC TASKS =====
        async def periodic_queue_processor():
            """Process queued media every 5 minutes"""
            while True:
                try:
                    if client.is_connected():
                        console.print("[cyan]Running periodic queue processor...[/cyan]")
                        await process_queued_media()
                    await asyncio.sleep(300)  # Run every 5 minutes
                except Exception as e:
                    console.print(f"[red]Queue processor error: {e}[/red]")
                    await asyncio.sleep(60)
        
        async def periodic_auto_check_missed(state):
            """Periodically check missed media for all logged-in users"""
            while True:
                try:
                    await asyncio.sleep(3600)  # 1 hour
                    
                    console.print("[cyan]Running periodic auto-check for all users...[/cyan]")
                    
                    user_sessions = state.get("user_sessions", {})
                    for user_id_str, user_data in user_sessions.items():
                        try:
                            user_id = int(user_id_str)
                            user_client = ACTIVE_USER_CLIENTS.get(user_id_str)
                            
                            if user_client and user_client.is_connected():
                                console.print(f"[cyan]Auto-checking user {user_id}...[/cyan]")
                                await check_missed_media(user_id, user_client, state)
                                await asyncio.sleep(30)  # 30 seconds delay between users
                                
                        except Exception as e:
                            console.print(f"[yellow]Auto-check failed for user {user_id_str}: {e}[/yellow]")
                            
                except Exception as e:
                    console.print(f"[red]Periodic auto-check error: {e}[/red]")
        
        # Start periodic queue processor
        asyncio.create_task(periodic_queue_processor())
        
        # Start periodic auto-check for missed media (every 1 hour)
        asyncio.create_task(periodic_auto_check_missed(state))
        
        console.print("[green]Bot is ready! Users can login with /login and set personal channels.[/green]")
        console.print("[green]✓ Offline media recovery is ENABLED![/green]")
        console.print("[green]✓ Queue system is ACTIVE![/green]")
        console.print("[green]✓ Auto-check on startup ENABLED![/green]")
        console.print("[green]✓ Periodic auto-check (every 1 hour) ENABLED![/green]")
        console.print("[green]✓ Database encryption ENABLED![/green]")
        console.print("[green]✓ Access controls ENABLED![/green]")
        console.print("[yellow]Commands: /checkmissed, /queue_stats, /process_queue, /users[/yellow]")
        console.print("[yellow]Access control: /allow_user, /disallow_user, /allowed_users[/yellow]")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        console.print(f"[red]✗ Fatal error: {e}[/red]")
        logger.critical(f"Bot crashed: {e}")
    finally:
        # Disconnect all user clients
        for user_id_str, user_client in ACTIVE_USER_CLIENTS.items():
            try:
                await user_client.disconnect()
            except:
                pass
        
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Bot stopped by user[/yellow]")
    except Exception as e:
        console.print(f"[red]Unhandled exception: {e}[/red]")