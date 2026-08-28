import os
import json
import hmac
import hashlib
import time
import requests
import psycopg2
from psycopg2 import pool
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify, render_template_string
from functools import wraps
from datetime import datetime
import logging
import sys

# ───────────────────────── Configuration & Logging ─────────────────────────
app = Flask(__name__)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Constants
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

# Validate critical vars
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN not set!")
if not DATABASE_URL:
    logger.error("❌ DATABASE_URL not set!")

DONATION_AMOUNTS = [100, 200, 500, 1000]
SUGGESTION_PRICE = 100
DAILY_POINTS = 100

# Timeouts (in seconds)
DB_TIMEOUT = 10
TELEGRAM_TIMEOUT = 12
REQUEST_TIMEOUT = 15

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 1.0

# ───────────────────────── Retry Decorator ─────────────────────────
def retry_on_error(max_attempts=MAX_RETRIES, delay=RETRY_DELAY):
    """Decorator to retry failed operations with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (psycopg2.OperationalError, psycopg2.DatabaseError, requests.RequestException) as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        wait_time = delay * (2 ** attempt)
                        logger.warning(f"{func.__name__} attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")
            raise last_error or Exception(f"{func.__name__} failed")
        return wrapper
    return decorator

# ───────────────────────── Data Layer (PostgreSQL) ─────────────────────────
db_pool = None

def init_pool():
    global db_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is not set, skipping pool creation.")
        return
    try:
        db_pool = pool.SimpleConnectionPool(
            2, 50,
            DATABASE_URL,
            connect_timeout=DB_TIMEOUT
        )
        logger.info("✅ Database connection pool ready.")
    except Exception as e:
        logger.error(f"❌ Failed to create the database pool: {e}")
        db_pool = None

def get_connection(timeout=DB_TIMEOUT):
    """Get a valid connection from the pool with validation"""
    if db_pool is None:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=timeout)
            return conn
        except Exception as e:
            logger.error(f"Failed to create fallback connection: {e}")
            raise
    
    try:
        conn = db_pool.getconn()
        # Validate connection is alive
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
        except:
            conn.close()
            conn = db_pool.getconn()
        return conn
    except pool.PoolError as e:
        logger.error(f"Pool exhausted: {e}")
        raise

def release_connection(conn):
    """Safely release a connection back to the pool"""
    if conn is None:
        return
    try:
        if db_pool is None:
            conn.close()
        else:
            db_pool.putconn(conn)
    except Exception as e:
        logger.error(f"Error releasing connection: {e}")
        try:
            conn.close()
        except:
            pass

def execute_db_query(query, params=None, fetch_one=False, fetch_all=False):
    """Generic database query executor with error handling"""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(query, params or ())
        
        if fetch_one:
            result = cur.fetchone()
        elif fetch_all:
            result = cur.fetchall()
        else:
            result = None
        
        conn.commit()
        cur.close()
        return result
    except psycopg2.Error as e:
        logger.error(f"Database error: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    except Exception as e:
        logger.error(f"Unexpected error in execute_db_query: {e}")
        raise
    finally:
        if conn:
            release_connection(conn)

def init_db():
    """Creates the required tables if they don't exist yet."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is not set, skipping database connection.")
        return
    
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS registrations (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS donations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount INTEGER NOT NULL,
                telegram_payment_charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS suggestions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                content TEXT,
                telegram_payment_charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS pending_actions (
                user_id BIGINT PRIMARY KEY,
                action TEXT NOT NULL,
                charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS user_points (
                user_id BIGINT PRIMARY KEY,
                total_points INTEGER NOT NULL DEFAULT 0,
                last_claim_date DATE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_donations_user_id ON donations(user_id)""",
            """CREATE INDEX IF NOT EXISTS idx_suggestions_user_id ON suggestions(user_id)""",
            """CREATE INDEX IF NOT EXISTS idx_registrations_created ON registrations(created_at)""",
        ]

        for table_sql in tables:
            try:
                cur.execute(table_sql)
            except psycopg2.Error as e:
                if "already exists" in str(e):
                    pass
                else:
                    logger.error(f"Error creating table: {e}")

        conn.commit()
        cur.close()
        logger.info("✅ Database is ready.")
    except Exception as e:
        logger.error(f"❌ Failed to set up the database: {e}")
    finally:
        if conn:
            release_connection(conn)

# ───────────────────────── User Operations ─────────────────────────
@retry_on_error(max_attempts=2)
def upsert_user(user_id, username=None, first_name=None):
    """Saves/updates the last known name for anyone who has interacted with the bot."""
    if not user_id:
        logger.warning("upsert_user called with empty user_id")
        return False
    
    try:
        execute_db_query("""
            INSERT INTO users (user_id, username, first_name, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                updated_at = NOW()
        """, (int(user_id), username or "", first_name or ""))
        return True
    except Exception as e:
        logger.error(f"upsert_user error: {e}")
        return False

def get_count():
    """Get the count of registered users with error handling"""
    try:
        result = execute_db_query("SELECT COUNT(*) FROM registrations", fetch_one=True)
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"get_count error: {e}")
        return 0

def is_registered(user_id):
    """Check if user is registered"""
    if not user_id:
        return False
    try:
        result = execute_db_query(
            "SELECT 1 FROM registrations WHERE user_id = %s",
            (int(user_id),),
            fetch_one=True
        )
        return result is not None
    except Exception as e:
        logger.error(f"is_registered error: {e}")
        return False

@retry_on_error(max_attempts=2)
def register_user(user_id):
    """Registers the user, returns True if it succeeded"""
    if not user_id:
        return False
    
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO registrations (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (int(user_id),)
        )
        added = cur.rowcount > 0
        conn.commit()
        cur.close()
        return added
    except Exception as e:
        logger.error(f"register_user error: {e}")
        return False
    finally:
        if conn:
            release_connection(conn)

@retry_on_error(max_attempts=2)
def record_donation(user_id, amount, charge_id):
    """Records a successful support payment"""
    if not user_id or not isinstance(amount, int) or amount <= 0:
        logger.warning(f"Invalid donation params: user_id={user_id}, amount={amount}")
        return False
    
    try:
        execute_db_query("""
            INSERT INTO donations (user_id, amount, telegram_payment_charge_id)
            VALUES (%s, %s, %s)
        """, (int(user_id), amount, charge_id or ""))
        return True
    except Exception as e:
        logger.error(f"record_donation error: {e}")
        return False

def get_donations_stats():
    """Returns (number of donations, total stars)"""
    try:
        result = execute_db_query(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM donations",
            fetch_one=True
        )
        return result if result else (0, 0)
    except Exception as e:
        logger.error(f"get_donations_stats error: {e}")
        return (0, 0)

def get_registered_users_with_donations():
    """Returns list of registered members with their totals"""
    try:
        result = execute_db_query("""
            SELECT
                r.user_id,
                COALESCE(NULLIF(u.username, ''), NULLIF(u.first_name, ''), 'Member') AS display_name,
                COALESCE(SUM(d.amount), 0) AS total_donated,
                r.created_at
            FROM registrations r
            LEFT JOIN users u ON u.user_id = r.user_id
            LEFT JOIN donations d ON d.user_id = r.user_id
            GROUP BY r.user_id, u.username, u.first_name, r.created_at
            ORDER BY r.created_at ASC
        """, fetch_all=True)
        
        if not result:
            return []
        
        return [
            {"user_id": row[0], "name": row[1], "donated": int(row[2])}
            for row in result
        ]
    except Exception as e:
        logger.error(f"get_registered_users_with_donations error: {e}")
        return []

def mask_name(name):
    """Blanks out every character of a name except the first one."""
    if not name:
        return "*"
    name = str(name).strip()
    if len(name) <= 1:
        return name
    return name[0] + "*" * (len(name) - 1)

# ───────────────────────── Suggestions ─────────────────────────
def set_pending_action(user_id, action, charge_id=None):
    """Set pending action for user"""
    if not user_id or not action:
        return False
    
    try:
        execute_db_query("""
            INSERT INTO pending_actions (user_id, action, charge_id, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET action = EXCLUDED.action,
                charge_id = EXCLUDED.charge_id,
                created_at = NOW()
        """, (int(user_id), action, charge_id or ""))
        return True
    except Exception as e:
        logger.error(f"set_pending_action error: {e}")
        return False

def get_pending_action(user_id):
    """Get pending action for user"""
    if not user_id:
        return None
    
    try:
        result = execute_db_query(
            "SELECT action, charge_id FROM pending_actions WHERE user_id = %s",
            (int(user_id),),
            fetch_one=True
        )
        return {"action": result[0], "charge_id": result[1]} if result else None
    except Exception as e:
        logger.error(f"get_pending_action error: {e}")
        return None

def clear_pending_action(user_id):
    """Clear pending action for user"""
    if not user_id:
        return False
    
    try:
        execute_db_query(
            "DELETE FROM pending_actions WHERE user_id = %s",
            (int(user_id),)
        )
        return True
    except Exception as e:
        logger.error(f"clear_pending_action error: {e}")
        return False

@retry_on_error(max_attempts=2)
def record_suggestion(user_id, content, charge_id):
    """Record a user suggestion"""
    if not user_id or not content:
        logger.warning(f"Invalid suggestion params: user_id={user_id}, content_len={len(content) if content else 0}")
        return False
    
    try:
        content = str(content)[:5000]
        execute_db_query("""
            INSERT INTO suggestions (user_id, content, telegram_payment_charge_id)
            VALUES (%s, %s, %s)
        """, (int(user_id), content, charge_id or ""))
        return True
    except Exception as e:
        logger.error(f"record_suggestion error: {e}")
        return False

def get_suggestions_count():
    """Get count of suggestions"""
    try:
        result = execute_db_query(
            "SELECT COUNT(*) FROM suggestions",
            fetch_one=True
        )
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"get_suggestions_count error: {e}")
        return 0

# ───────────────────────── Unsubscribe ─────────────────────────
@retry_on_error(max_attempts=2)
def unregister_user(user_id):
    """Delete user from registrations"""
    if not user_id:
        return False
    
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM registrations WHERE user_id = %s", (int(user_id),))
        removed = cur.rowcount > 0
        conn.commit()
        cur.close()
        return removed
    except Exception as e:
        logger.error(f"unregister_user error: {e}")
        return False
    finally:
        if conn:
            release_connection(conn)

# ───────────────────────── Daily Points ─────────────────────────
@retry_on_error(max_attempts=2)
def claim_daily_points(user_id):
    """Try to grant daily points"""
    if not user_id:
        return False, 0
    
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_points (user_id, total_points, last_claim_date)
            VALUES (%s, %s, CURRENT_DATE)
            ON CONFLICT (user_id) DO UPDATE
            SET total_points = user_points.total_points + %s,
                last_claim_date = CURRENT_DATE
            WHERE user_points.last_claim_date IS DISTINCT FROM CURRENT_DATE
            RETURNING total_points
        """, (int(user_id), DAILY_POINTS, DAILY_POINTS))
        
        row = cur.fetchone()
        if row:
            conn.commit()
            cur.close()
            return True, row[0]

        cur.execute("SELECT total_points FROM user_points WHERE user_id = %s", (int(user_id),))
        existing = cur.fetchone()
        conn.commit()
        cur.close()
        return False, (existing[0] if existing else 0)
    except Exception as e:
        logger.error(f"claim_daily_points error: {e}")
        return False, 0
    finally:
        if conn:
            release_connection(conn)

def get_user_points(user_id):
    """Get user total points"""
    if not user_id:
        return 0
    
    try:
        result = execute_db_query(
            "SELECT total_points FROM user_points WHERE user_id = %s",
            (int(user_id),),
            fetch_one=True
        )
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"get_user_points error: {e}")
        return 0

def get_leaderboard(limit=10):
    """Get top point earners"""
    if not isinstance(limit, int) or limit <= 0 or limit > 100:
        limit = 10
    
    try:
        result = execute_db_query("""
            SELECT
                p.user_id,
                COALESCE(NULLIF(u.username, ''), NULLIF(u.first_name, ''), 'Member') AS display_name,
                p.total_points
            FROM user_points p
            LEFT JOIN users u ON u.user_id = p.user_id
            WHERE p.total_points > 0
            ORDER BY p.total_points DESC, p.user_id ASC
            LIMIT %s
        """, (limit,), fetch_all=True)
        
        return [{"user_id": r[0], "name": r[1], "points": r[2]} for r in result] if result else []
    except Exception as e:
        logger.error(f"get_leaderboard error: {e}")
        return []

# ───────────────────────── Telegram Helpers ─────────────────────────
def send_message(chat_id, text, reply_markup=None):
    """Send a Telegram message with timeout and error handling"""
    if not chat_id or not text:
        logger.warning("send_message called with empty params")
        return False
    
    try:
        payload = {
            "chat_id": int(chat_id),
            "text": str(text)[:4096]
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
            timeout=TELEGRAM_TIMEOUT
        )
        response.raise_for_status()
        return True
    except requests.Timeout:
        logger.error(f"Telegram sendMessage timeout for chat_id {chat_id}")
        return False
    except Exception as e:
        logger.error(f"send_message error: {e}")
        return False

def answer_callback(callback_id, text, show_alert=False):
    """Answer a callback query"""
    if not callback_id:
        return False
    
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={
                "callback_query_id": str(callback_id),
                "text": str(text)[:200],
                "show_alert": bool(show_alert)
            },
            timeout=TELEGRAM_TIMEOUT
        )
        return True
    except Exception as e:
        logger.error(f"answer_callback error: {e}")
        return False

def send_invoice(chat_id, amount, title, description, payload_str):
    """Send a Telegram Stars invoice"""
    if not chat_id or not isinstance(amount, int) or amount <= 0:
        logger.warning(f"Invalid invoice params: chat_id={chat_id}, amount={amount}")
        return False
    
    try:
        payload = {
            "chat_id": int(chat_id),
            "title": str(title)[:32],
            "description": str(description)[:255],
            "payload": str(payload_str)[:128],
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": str(title)[:32], "amount": amount}],
        }
        
        response = requests.post(
            f"{TELEGRAM_API}/sendInvoice",
            json=payload,
            timeout=TELEGRAM_TIMEOUT
        )
        response.raise_for_status()
        logger.info(f"Invoice sent: {payload_str}")
        return True
    except Exception as e:
        logger.error(f"send_invoice error: {e}")
        return False

def send_donation_invoice(chat_id, amount):
    """Send a donation invoice"""
    return send_invoice(
        chat_id, amount,
        "Support Betrothed 💍",
        f"Your support of {amount} Telegram Stars helps us keep the bot running 🙏",
        f"donate_{amount}_{chat_id}"
    )

def send_suggestion_invoice(chat_id):
    """Send a suggestion invoice"""
    return send_invoice(
        chat_id, SUGGESTION_PRICE,
        "Suggest a new idea 💡",
        f"Pay {SUGGESTION_PRICE} Stars to share your idea with us 💡",
        f"suggest_{SUGGESTION_PRICE}_{chat_id}"
    )

def create_invoice_link(amount, title, description, payload_str):
    """Create a payment link for Mini App"""
    if not isinstance(amount, int) or amount <= 0:
        logger.warning(f"Invalid amount for invoice link: {amount}")
        return None
    
    try:
        payload = {
            "title": str(title)[:32],
            "description": str(description)[:255],
            "payload": str(payload_str)[:128],
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": str(title)[:32], "amount": amount}],
        }
        
        response = requests.post(
            f"{TELEGRAM_API}/createInvoiceLink",
            json=payload,
            timeout=TELEGRAM_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        
        if data.get("ok"):
            return data["result"]
        
        logger.error(f"Telegram API error: {data}")
        return None
    except requests.Timeout:
        logger.error("Telegram createInvoiceLink timeout")
        return None
    except Exception as e:
        logger.error(f"create_invoice_link error: {e}")
        return None

def answer_pre_checkout(pre_checkout_query_id, ok=True, error_message=None):
    """Answer pre-checkout query within 10 seconds"""
    if not pre_checkout_query_id:
        return False
    
    try:
        payload = {
            "pre_checkout_query_id": str(pre_checkout_query_id),
            "ok": bool(ok)
        }
        if error_message:
            payload["error_message"] = str(error_message)[:200]
        
        requests.post(
            f"{TELEGRAM_API}/answerPreCheckoutQuery",
            json=payload,
            timeout=TELEGRAM_TIMEOUT
        )
        return True
    except Exception as e:
        logger.error(f"answer_pre_checkout error: {e}")
        return False

def donation_keyboard():
    """Generate donation keyboard"""
    keyboard = [
        [{"text": f"⭐ Support with {amount} Stars", "callback_data": f"donate_{amount}"}]
        for amount in DONATION_AMOUNTS
    ]
    return {"inline_keyboard": keyboard}

def unsubscribe_confirm_keyboard():
    """Generate unsubscribe confirmation keyboard"""
    return {
        "inline_keyboard": [
            [{"text": "✅ Yes, unsubscribe me", "callback_data": "confirm_unsubscribe"}],
            [{"text": "🙅 No, cancel that", "callback_data": "cancel_unsubscribe"}],
        ]
    }

def build_leaderboard_text():
    """Build leaderboard text"""
    top = get_leaderboard(10)
    if not top:
        return "Nobody has earned points yet 🙂 Be the first to claim yours today!"
    
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Points leaderboard:\n"]
    for i, u in enumerate(top):
        rank_icon = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{rank_icon} {mask_name(u['name'])} — {u['points']} pts")
    return "\n".join(lines)

def notify_admin_new_suggestion(user_id, username, content):
    """Notify admin about new suggestion"""
    if not ADMIN_CHAT_ID:
        return False
    
    try:
        who = f"@{username}" if username else f"id:{user_id}"
        content_preview = str(content)[:500]
        return send_message(
            ADMIN_CHAT_ID,
            f"💡 New suggestion from {who}\n\n{content_preview}"
        )
    except Exception as e:
        logger.error(f"notify_admin_new_suggestion error: {e}")
        return False

# ───────────────────────── WebApp Validation ─────────────────────────
def validate_init_data(init_data: str, bot_token: str):
    """Verify WebApp initData HMAC signature"""
    if not init_data or not bot_token:
        return None
    
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        
        if not received_hash:
            return None
        
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("WebApp initData validation failed")
            return None
        
        return parsed
    except Exception as e:
        logger.error(f"validate_init_data error: {e}")
        return None

# ───────────────────────── Mini App HTML ─────────────────────────
MINI_APP_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>Betrothed</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;0,700;1,600&family=Work+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #1b1017;
    --ink-2: #100a0e;
    --wine: #5c2138;
    --wine-soft: rgba(201, 161, 90, 0.10);
    --card: rgba(243, 233, 216, 0.045);
    --card-line: rgba(201, 161, 90, 0.22);
    --parchment: #f3e9d8;
    --muted: #9c8a91;
    --gold: #c9a15a;
    --gold-bright: #e3c07f;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; overscroll-behavior: none; }
  body {
    background:
      radial-gradient(120% 70% at 50% -8%, rgba(92, 33, 56, 0.55) 0%, transparent 55%),
      var(--ink);
    background-color: var(--ink);
    font-family: 'Work Sans', 'Segoe UI', Tahoma, Arial, sans-serif;
    color: var(--parchment);
  }

  .app {
    height: 100dvh;
    width: 100%;
    max-width: 480px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding-top: env(safe-area-inset-top, 0px);
    position: relative;
    border-inline: 1px solid rgba(201, 161, 90, 0.14);
  }

  .letterhead {
    text-align: center;
    padding: 20px 16px 10px;
    flex-shrink: 0;
    position: relative;
    z-index: 1;
  }
  .letterhead .eyebrow {
    font-size: 10.5px;
    letter-spacing: 3.5px;
    text-transform: uppercase;
    color: var(--gold);
    margin: 0 0 4px;
  }
  .letterhead .brand {
    font-family: 'Cormorant Garamond', serif;
    font-weight: 600;
    font-size: 26px;
    letter-spacing: 0.5px;
    margin: 0;
  }
  .letterhead .brand em { font-style: italic; color: var(--gold-bright); }
  .rule {
    width: 92px;
    height: 1px;
    margin: 12px auto 0;
    background: linear-gradient(90deg, transparent, var(--gold), transparent);
  }

  .view {
    display: none;
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: 6px 24px 28px;
    position: relative;
    z-index: 1;
    animation: settle 0.4s ease;
  }
  .view.active { display: flex; flex-direction: column; }
  @keyframes settle { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  #view-home { align-items: center; justify-content: center; text-align: center; }
  .seal {
    position: relative;
    width: 176px;
    height: 176px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 18px;
  }
  .seal svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .seal .count {
    font-family: 'Cormorant Garamond', serif;
    font-weight: 700;
    font-size: clamp(46px, 15vw, 58px);
    line-height: 1;
    color: var(--parchment);
    position: relative;
    transition: transform 0.25s ease;
  }
  .eyebrow-label {
    color: var(--gold);
    font-size: 11px;
    letter-spacing: 2.6px;
    text-transform: uppercase;
    margin: 4px 0 0;
  }
  .count-desc {
    color: var(--muted);
    font-family: 'Cormorant Garamond', serif;
    font-style: italic;
    font-size: 17px;
    line-height: 1.6;
    margin: 18px 0 0;
    max-width: 280px;
  }

  #view-community { padding-top: 14px; }
  .ledger-summary {
    display: flex;
    border: 1px solid var(--card-line);
    border-radius: 4px;
    background: var(--card);
    margin-bottom: 20px;
    flex-shrink: 0;
    overflow: hidden;
  }
  .ledger-summary .stat {
    flex: 1;
    text-align: center;
    padding: 16px 8px;
  }
  .ledger-summary .stat + .stat { border-left: 1px solid var(--card-line); }
  .ledger-summary .stat b {
    display: block;
    font-family: 'Cormorant Garamond', serif;
    font-weight: 700;
    font-size: 25px;
    color: var(--gold-bright);
  }
  .ledger-summary .stat span {
    font-size: 10.5px;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: var(--muted);
  }

  .section-label {
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--gold);
    margin: 2px 2px 10px;
  }
  .ledger {
    border-top: 1px solid var(--card-line);
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 13px 2px;
    border-bottom: 1px dashed var(--card-line);
  }
  .row .initial {
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    border: 1px solid var(--gold);
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Cormorant Garamond', serif;
    font-weight: 600;
    font-size: 14px;
    color: var(--gold-bright);
  }
  .row .name {
    flex: 1;
    font-size: 14px;
    letter-spacing: 0.3px;
  }
  .row .donated {
    font-size: 13px;
    font-weight: 600;
    color: var(--gold-bright);
    white-space: nowrap;
  }
  .row .donated.zero { color: var(--muted); font-weight: 400; }
  .empty {
    text-align: center;
    color: var(--muted);
    font-family: 'Cormorant Garamond', serif;
    font-style: italic;
    font-size: 16px;
    padding: 46px 10px;
  }

  #view-support { padding-top: 14px; }
  .donate-btn {
    width: 100%;
    padding: 15px 16px;
    margin-bottom: 10px;
    border: 1px solid var(--card-line);
    background: var(--card);
    color: var(--parchment);
    border-radius: 4px;
    font-family: 'Cormorant Garamond', serif;
    font-weight: 600;
    font-size: 17px;
    letter-spacing: 0.4px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .donate-btn:active { background: var(--wine-soft); border-color: var(--gold); }
  .donate-btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .support-status {
    text-align: center;
    color: var(--muted);
    font-family: 'Cormorant Garamond', serif;
    font-style: italic;
    font-size: 15px;
    margin-top: 6px;
    min-height: 20px;
  }

  .tabbar {
    flex-shrink: 0;
    display: flex;
    border-top: 1px solid var(--card-line);
    background: rgba(16, 10, 14, 0.9);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    padding: 4px 16px calc(4px + env(safe-area-inset-bottom, 0px));
    position: relative;
    z-index: 1;
  }
  .tab {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    background: none;
    border: none;
    color: var(--muted);
    font-family: inherit;
    font-size: 11px;
    letter-spacing: 0.6px;
    padding: 12px 8px 10px;
    cursor: pointer;
    position: relative;
    transition: color 0.15s ease;
  }
  .tab svg { width: 18px; height: 18px; }
  .tab.active { color: var(--gold-bright); }
  .tab.active::after {
    content: "";
    position: absolute;
    bottom: 0;
    left: 50%;
    transform: translateX(-50%);
    width: 22px;
    height: 2px;
    background: var(--gold);
  }
</style>
</head>
<body>
  <div class="app">

    <header class="letterhead">
      <p class="eyebrow">A registry of intentions</p>
      <h1 class="brand">Betroth<em>ed</em></h1>
      <div class="rule"></div>
    </header>

    <main id="view-home" class="view active">
      <div class="seal">
        <svg viewBox="0 0 176 176" fill="none">
          <circle cx="88" cy="88" r="86" stroke="#c9a15a" stroke-width="1" opacity="0.35"/>
          <circle cx="88" cy="88" r="74" stroke="#c9a15a" stroke-width="1" stroke-dasharray="2 5" opacity="0.6"/>
          <circle cx="70" cy="88" r="20" stroke="#e3c07f" stroke-width="1.4" opacity="0.85"/>
          <circle cx="106" cy="88" r="20" stroke="#e3c07f" stroke-width="1.4" opacity="0.85"/>
        </svg>
        <div class="count" id="count">{{ count }}</div>
      </div>
      <p class="eyebrow-label">Have declared themselves ready</p>
      <p class="count-desc">The registry lengthens by the day. Add your name, and let luck take its course.</p>
    </main>

    <main id="view-community" class="view">
      <div class="ledger-summary">
        <div class="stat"><b id="d-count">0</b><span>Gifts given</span></div>
        <div class="stat"><b id="d-total">0</b><span>Stars total</span></div>
      </div>
      <div class="section-label">Registered members</div>
      <div class="ledger" id="users-list">
        <div class="empty">Loading the ledger…</div>
      </div>
    </main>

    <main id="view-support" class="view">
      <p class="section-label">Support the registry</p>
      <p class="count-desc" style="margin:0 0 22px;">
        Every star helps keep Betrothed running and growing 💛
      </p>
      <div id="donate-options"></div>
      <p class="support-status" id="support-status"></p>
    </main>

    <nav class="tabbar">
      <button class="tab active" data-view="home">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
          <circle cx="9" cy="12" r="4.6"/><circle cx="15" cy="12" r="4.6"/>
        </svg>
        Registry
      </button>
      <button class="tab" data-view="community">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
          <path d="M4 19c0-3.6 3-6 8-6s8 2.4 8 6"/><circle cx="12" cy="7.5" r="3.4"/>
        </svg>
        Members
      </button>
      <button class="tab" data-view="support">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
          <path d="M12 21s-7-4.4-9.5-8.6C.7 8.5 3 5 6.6 5c2 0 3.6 1.2 5.4 3 1.8-1.8 3.4-3 5.4-3 3.6 0 5.9 3.5 4.1 7.4C19 16.6 12 21 12 21z"/>
        </svg>
        Support
      </button>
    </nav>
  </div>

  <script>
    const tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
    if (tg) {
      try {
        tg.ready();
        tg.expand();
        if (tg.setHeaderColor) tg.setHeaderColor('#100a0e');
        if (tg.setBackgroundColor) tg.setBackgroundColor('#100a0e');
        if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      } catch (e) {
        console.error('Telegram API error:', e);
      }
    }

    const DONATION_AMOUNTS = [100, 200, 500, 1000];
    const API_TIMEOUT = 8000;

    const tabs = document.querySelectorAll('.tab');
    const views = {
      home: document.getElementById('view-home'),
      community: document.getElementById('view-community'),
      support: document.getElementById('view-support'),
    };

    function showView(name) {
      Object.entries(views).forEach(([key, el]) => el.classList.toggle('active', key === name));
      tabs.forEach(t => t.classList.toggle('active', t.dataset.view === name));
      if (name === 'community') loadCommunity();
      if (name === 'support') renderDonateOptions();
    }
    tabs.forEach(t => t.addEventListener('click', () => showView(t.dataset.view)));

    function initialOf(name) {
      const clean = (name || '').replace(/\*/g, '');
      return (clean.charAt(0) || '?').toUpperCase();
    }

    async function fetchWithTimeout(url, options = {}) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), options.timeout || API_TIMEOUT);
      try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        clearTimeout(timeout);
        return response;
      } catch (e) {
        clearTimeout(timeout);
        throw e;
      }
    }

    async function refreshCount() {
      try {
        const res = await fetchWithTimeout('/api/count');
        if (!res.ok) throw new Error('API error');
        const data = await res.json();
        const el = document.getElementById('count');
        if (el && el.textContent != data.count) {
          el.textContent = data.count;
          el.style.transform = 'scale(1.12)';
          setTimeout(() => { el.style.transform = 'scale(1)'; }, 200);
        }
      } catch (e) {
        console.error('Could not refresh the count:', e);
      }
    }

    async function loadCommunity() {
      try {
        const [statsRes, usersRes] = await Promise.all([
          fetchWithTimeout('/api/donations'),
          fetchWithTimeout('/api/users')
        ]);
        
        if (!statsRes.ok || !usersRes.ok) throw new Error('API error');
        
        const stats = await statsRes.json();
        const usersData = await usersRes.json();
        
        const countEl = document.getElementById('d-count');
        const totalEl = document.getElementById('d-total');
        if (countEl) countEl.textContent = stats.donations_count;
        if (totalEl) totalEl.textContent = stats.stars_total;

        const list = document.getElementById('users-list');
        if (!list) return;
        
        if (!usersData.users || usersData.users.length === 0) {
          list.innerHTML = '<div class="empty">No one has registered yet 🙂</div>';
          return;
        }
        list.innerHTML = usersData.users.map(u => `
          <div class="row">
            <span class="initial">${initialOf(u.masked_name)}</span>
            <span class="name">${u.masked_name}</span>
            <span class="donated ${u.donated === 0 ? 'zero' : ''}">${u.donated} ⭐</span>
          </div>
        `).join('');
      } catch (e) {
        console.error('Could not load the members list:', e);
        const list = document.getElementById('users-list');
        if (list) list.innerHTML = '<div class="empty">Error loading members. Please try again.</div>';
      }
    }

    function renderDonateOptions() {
      const wrap = document.getElementById('donate-options');
      if (!wrap || wrap.dataset.rendered === '1') return;
      wrap.dataset.rendered = '1';
      wrap.innerHTML = DONATION_AMOUNTS.map(a =>
        `<button class="donate-btn" data-amount="${a}">⭐ Support with ${a} Stars</button>`
      ).join('');
      wrap.querySelectorAll('.donate-btn').forEach(btn => {
        btn.addEventListener('click', () => startDonation(parseInt(btn.dataset.amount, 10), btn));
      });
    }

    function setStatus(text) {
      const el = document.getElementById('support-status');
      if (el) el.textContent = text || '';
    }

    async function startDonation(amount, btn) {
      if (!tg || !tg.initData) {
        setStatus('Please open this from inside Telegram.');
        return;
      }
      
      if (!DONATION_AMOUNTS.includes(amount)) {
        setStatus('Invalid amount.');
        return;
      }
      
      const buttons = document.querySelectorAll('.donate-btn');
      buttons.forEach(b => b.disabled = true);
      setStatus('Preparing your invoice…');

      try {
        const res = await fetchWithTimeout('/api/create-invoice', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ amount, init_data: tg.initData }),
          timeout: 10000
        });
        
        if (!res.ok) {
          setStatus('Could not start the payment, please try again.');
          buttons.forEach(b => b.disabled = false);
          return;
        }
        
        const data = await res.json();

        if (!data.ok || !data.link) {
          setStatus('Could not start the payment, please try again.');
          buttons.forEach(b => b.disabled = false);
          return;
        }

        setStatus('');
        if (tg.openInvoice) {
          tg.openInvoice(data.link, (status) => {
            buttons.forEach(b => b.disabled = false);
            if (status === 'paid') {
              setStatus('Thank you for your support 💛');
              if (tg.HapticFeedback) {
                try {
                  tg.HapticFeedback.notificationOccurred('success');
                } catch (e) {}
              }
              loadCommunity();
            } else if (status === 'cancelled') {
              setStatus('');
            } else {
              setStatus('Payment was not completed.');
            }
          });
        }
      } catch (e) {
        console.error('startDonation error:', e);
        setStatus('Something went wrong, please try again.');
        buttons.forEach(b => b.disabled = false);
      }
    }

    refreshCount();
    const refreshInterval = setInterval(refreshCount, 5000);

    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') === 'community') showView('community');
    if (params.get('tab') === 'support') showView('support');
  </script>
</body>
</html>
"""

# ───────────────────────── Flask Routes ─────────────────────────
@app.route("/")
def home():
    """Main app page"""
    try:
        count = get_count()
        return render_template_string(MINI_APP_PAGE, count=count, suggestion_price=SUGGESTION_PRICE)
    except Exception as e:
        logger.error(f"Home route error: {e}")
        return "An error occurred", 500

@app.route("/users")
def users_page():
    """Legacy redirect"""
    try:
        count = get_count()
        return render_template_string(MINI_APP_PAGE, count=count, suggestion_price=SUGGESTION_PRICE)
    except Exception as e:
        logger.error(f"Users page error: {e}")
        return "An error occurred", 500

@app.route("/api/count")
def api_count():
    """Get registration count"""
    return jsonify({"count": get_count()})

@app.route("/api/donations")
def api_donations():
    """Get donation statistics"""
    count, total = get_donations_stats()
    return jsonify({"donations_count": count, "stars_total": total})

@app.route("/api/users")
def api_users():
    """Get registered users list"""
    try:
        users = get_registered_users_with_donations()
        result = [
            {"masked_name": mask_name(u["name"]), "donated": u["donated"]}
            for u in users
        ]
        return jsonify({"users": result})
    except Exception as e:
        logger.error(f"api_users error: {e}")
        return jsonify({"users": [], "error": "Failed to load users"}), 500

@app.route("/api/create-invoice", methods=["POST"])
def api_create_invoice():
    """Create payment invoice for Mini App"""
    try:
        body = request.get_json(force=True, silent=True) or {}
        init_data = body.get("init_data", "")
        amount = body.get("amount")

        # Validate init_data
        parsed = validate_init_data(init_data, BOT_TOKEN)
        if not parsed:
            logger.warning("Invalid initData received")
            return jsonify({"ok": False, "error": "invalid_init_data"}), 401

        # Validate amount
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_amount"}), 400

        if amount not in DONATION_AMOUNTS:
            return jsonify({"ok": False, "error": "invalid_amount"}), 400

        # Extract user info
        try:
            user_info = json.loads(parsed.get("user", "{}"))
            user_id = user_info.get("id")
        except (json.JSONDecodeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_user"}), 400

        if not user_id:
            return jsonify({"ok": False, "error": "no_user"}), 400

        # Save user info
        upsert_user(user_id, username=user_info.get("username"), first_name=user_info.get("first_name"))

        # Create invoice
        link = create_invoice_link(
            amount,
            "Support Betrothed 💍",
            f"Your support of {amount} Telegram Stars helps us keep the bot running 🙏",
            f"donate_{amount}_{user_id}",
        )
        
        if not link:
            return jsonify({"ok": False, "error": "invoice_failed"}), 500

        return jsonify({"ok": True, "link": link})
    except Exception as e:
        logger.error(f"api_create_invoice error: {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

# ───────────────────────── Health Check ─────────────────────────
@app.route("/health")
def health_check():
    """Diagnostic endpoint"""
    result = {"app": "ok", "timestamp": datetime.now().isoformat()}

    # Test database
    if DATABASE_URL:
        try:
            start = time.time()
            result_tuple = execute_db_query("SELECT 1", fetch_one=True)
            latency = round((time.time() - start) * 1000, 1)
            result["database"] = {
                "status": "ok",
                "latency_ms": latency,
                "pool_active": db_pool is not None,
            }
        except Exception as e:
            result["database"] = {"status": "error", "error": str(e)[:100]}
    else:
        result["database"] = {"status": "missing DATABASE_URL"}

    # Test Telegram API
    if BOT_TOKEN:
        try:
            start = time.time()
            r = requests.get(f"{TELEGRAM_API}/getMe", timeout=TELEGRAM_TIMEOUT)
            result["telegram"] = {
                "status": "ok" if r.ok else "error",
                "latency_ms": round((time.time() - start) * 1000, 1),
            }
        except Exception as e:
            result["telegram"] = {"status": "error", "error": str(e)[:100]}
    else:
        result["telegram"] = {"status": "missing BOT_TOKEN"}

    return jsonify(result)

# ───────────────────────── Webhook ─────────────────────────
@app.route(f"/webhook/<bot_token>", methods=["POST"])
def webhook(bot_token):
    """Telegram webhook endpoint"""
    # Verify token matches
    if bot_token != BOT_TOKEN:
        logger.warning(f"Invalid bot token in webhook: {bot_token[:10]}...")
        return jsonify({"ok": False}), 403

    try:
        update = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        logger.error(f"Invalid JSON in webhook: {e}")
        return jsonify({"ok": True}), 200

    try:
        # Pre-checkout query
        if "pre_checkout_query" in update:
            pcq = update["pre_checkout_query"]
            answer_pre_checkout(pcq.get("id"), ok=True)
            return jsonify({"ok": True})

        # Message update
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()

            sender = msg.get("from", {})
            user_id = sender.get("id")
            
            if user_id:
                upsert_user(user_id, username=sender.get("username"), first_name=sender.get("first_name"))

            # Successful payment
            if "successful_payment" in msg and chat_id and user_id:
                sp = msg["successful_payment"]
                amount = sp.get("total_amount", 0)
                charge_id = sp.get("telegram_payment_charge_id")
                invoice_payload = (sp.get("invoice_payload") or "").strip()

                if invoice_payload.startswith("suggest_"):
                    set_pending_action(user_id, "awaiting_suggestion", charge_id)
                    send_message(chat_id, "Payment received 💡\nSend us your idea in a single message 🙏")
                else:
                    record_donation(user_id, amount, charge_id)
                    send_message(chat_id, f"Thank you 🙏💛\nWe've received your support of {amount} Stars ⭐")
                
                return jsonify({"ok": True})

            # Pending suggestion text
            if user_id and text and not text.startswith("/"):
                pending = get_pending_action(user_id)
                if pending and pending["action"] == "awaiting_suggestion":
                    record_suggestion(user_id, text, pending.get("charge_id"))
                    clear_pending_action(user_id)
                    send_message(chat_id, "Your idea has been recorded ✅ Thank you 🙏💡")
                    notify_admin_new_suggestion(user_id, sender.get("username"), text)
                    return jsonify({"ok": True})

            # Commands
            if text.startswith("/start"):
                if "/start suggest" in text:
                    send_suggestion_invoice(chat_id)
                else:
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "✅ I'm ready to marry", "callback_data": "want_marry"}],
                            [{"text": "🌐 Open the registry", "web_app": {"url": f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else "https://example.com"}}],
                            [
                                {"text": "🎁 Daily points", "callback_data": "claim_points"},
                                {"text": "🏆 Leaderboard", "callback_data": "show_leaderboard"},
                            ],
                            [{"text": "⭐ Support the bot", "callback_data": "show_donate"}],
                            [{"text": "💡 Suggest a feature", "callback_data": "propose_idea"}],
                            [{"text": "❌ Unsubscribe", "callback_data": "unsubscribe"}],
                        ]
                    }
                    send_message(chat_id, "Welcome to Betrothed 💍\nTap below to join the registry 😄", keyboard)

            elif text == "/count":
                send_message(chat_id, f"People registered: {get_count()} 💍")
            elif text == "/support":
                send_message(chat_id, "Support the bot with Stars ⭐:", donation_keyboard())
            elif text == "/donations":
                count, total = get_donations_stats()
                send_message(chat_id, f"Gifts: {count}\nStars: {total} ⭐")
            elif text == "/suggest":
                send_suggestion_invoice(chat_id)
            elif text == "/suggestions" and str(chat_id) == str(ADMIN_CHAT_ID):
                send_message(chat_id, f"Suggestions: {get_suggestions_count()} 💡")
            elif text == "/points":
                send_message(chat_id, f"Your points: {get_user_points(user_id)} ⭐")
            elif text == "/leaderboard":
                send_message(chat_id, build_leaderboard_text())
            elif text == "/unsubscribe":
                if is_registered(user_id):
                    send_message(chat_id, "Leave the registry?", unsubscribe_confirm_keyboard())
                else:
                    send_message(chat_id, "You're not registered 🙂")

        # Callback query
        elif "callback_query" in update:
            cq = update["callback_query"]
            sender = cq.get("from", {})
            user_id = sender.get("id")
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            callback_id = cq.get("id")
            data_key = (cq.get("data") or "").strip()

            if user_id:
                upsert_user(user_id, username=sender.get("username"), first_name=sender.get("first_name"))

            if data_key == "want_marry":
                if is_registered(user_id):
                    answer_callback(callback_id, "Already registered 😄")
                else:
                    if register_user(user_id):
                        answer_callback(callback_id, "Registered! 🎉")
                        send_message(chat_id, f"Registered ✅\nTotal ready: {get_count()} 💍")
                    else:
                        answer_callback(callback_id, "Failed, try again 🙏")

            elif data_key == "show_donate":
                answer_callback(callback_id, "")
                send_message(chat_id, "Support with Stars ⭐:", donation_keyboard())

            elif data_key.startswith("donate_"):
                try:
                    amount = int(data_key.split("_")[1])
                    if amount in DONATION_AMOUNTS:
                        answer_callback(callback_id, f"Invoice for {amount} Stars ⭐")
                        send_donation_invoice(chat_id, amount)
                    else:
                        answer_callback(callback_id, "Invalid amount 🙏")
                except (ValueError, IndexError):
                    answer_callback(callback_id, "Invalid amount 🙏")

            elif data_key == "propose_idea":
                answer_callback(callback_id, f"Invoice for {SUGGESTION_PRICE} Stars ⭐")
                send_suggestion_invoice(chat_id)

            elif data_key == "claim_points":
                success, total = claim_daily_points(user_id)
                if success:
                    answer_callback(callback_id, f"🎁 +{DAILY_POINTS} points!")
                    send_message(chat_id, f"Claimed {DAILY_POINTS} points today 🎁\nTotal: {total} ⭐\nCome back tomorrow!")
                else:
                    answer_callback(callback_id, "Already claimed today 🙏", show_alert=True)

            elif data_key == "show_leaderboard":
                answer_callback(callback_id, "")
                send_message(chat_id, build_leaderboard_text())

            elif data_key == "unsubscribe":
                answer_callback(callback_id, "")
                if is_registered(user_id):
                    send_message(chat_id, "Leave the registry?", unsubscribe_confirm_keyboard())
                else:
                    send_message(chat_id, "Not registered 🙂")

            elif data_key == "confirm_unsubscribe":
                if unregister_user(user_id):
                    answer_callback(callback_id, "Unsubscribed ✅")
                    send_message(chat_id, f"Removed from registry 👋\nNow: {get_count()} 💍")
                else:
                    answer_callback(callback_id, "Not registered 🙂")

            elif data_key == "cancel_unsubscribe":
                answer_callback(callback_id, "No changes made 👍")

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)

    return jsonify({"ok": True}), 200

# ───────────────────────── Startup ─────────────────────────
def set_webhook():
    """Configure Telegram webhook"""
    if not RAILWAY_DOMAIN or not BOT_TOKEN:
        logger.warning(f"⚠️  Missing domain or BOT_TOKEN for webhook setup")
        logger.warning(f"   RAILWAY_PUBLIC_DOMAIN: {RAILWAY_DOMAIN or 'NOT SET'}")
        logger.warning(f"   BOT_TOKEN: {'SET' if BOT_TOKEN else 'NOT SET'}")
        return

    url = f"https://{RAILWAY_DOMAIN}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={
                "url": url,
                "allowed_updates": json.dumps(["message", "callback_query", "pre_checkout_query"]),
            },
            timeout=TELEGRAM_TIMEOUT,
        )
        if r.ok:
            logger.info(f"✅ Webhook set: {url} -> {r.status_code}")
        else:
            logger.error(f"❌ Webhook error: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")

# Initialize on startup
logger.info("========== Initializing Bot ==========")
init_pool()
init_db()
set_webhook()
logger.info("✅ Bot initialization complete!")

# Application entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
