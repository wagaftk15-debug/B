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

app = Flask(__name__)

# Bot token is read from an environment variable called BOT_TOKEN
# (add it under Railway -> Variables)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Database connection string (add it under Railway -> Variables as DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Admin chat id (optional) so they get pinged on every new suggestion
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# Available support amounts, in Telegram Stars
DONATION_AMOUNTS = [100, 200, 500, 1000]

# Price of submitting a new idea / feature suggestion, in Telegram Stars
SUGGESTION_PRICE = 100

# Daily bonus points awarded per claim
DAILY_POINTS = 100


# ───────────────────────── data layer (PostgreSQL) ─────────────────────────
# FIX: use a connection pool instead of opening/closing a brand-new TCP+TLS
# connection to Postgres on every single call. This was the main source of
# slowness / occasional failures to load (each API call could add hundreds of
# ms to seconds of pure connection-setup latency, on top of the query itself).
db_pool = None


def init_pool():
    global db_pool
    if not DATABASE_URL:
        print("DATABASE_URL is not set, skipping pool creation.")
        return
    try:
        db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)
        print("Database connection pool ready.")
    except Exception as e:
        print(f"Failed to create the database pool: {e}")


def get_connection():
    if db_pool is None:
        # fallback so the app doesn't crash if the pool failed to init,
        # though this path will be slow (same as before)
        return psycopg2.connect(DATABASE_URL)
    return db_pool.getconn()


def release_connection(conn):
    try:
        if db_pool is None:
            conn.close()
        else:
            db_pool.putconn(conn)
    except Exception as e:
        print(f"release_connection error: {e}")


def init_db():
    """Creates the required tables if they don't exist yet. Runs once on startup."""
    if not DATABASE_URL:
        print("DATABASE_URL is not set, skipping database connection.")
        return
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # General table for every user who has ever interacted with the bot
        # (stores their last known name/username)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS donations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount INTEGER NOT NULL,
                telegram_payment_charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

        # Paid idea / feature suggestions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                content TEXT,
                telegram_payment_charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

        # Pending state: tracks what the bot is waiting for next from each user
        # (e.g. the suggestion text, right after payment)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_actions (
                user_id BIGINT PRIMARY KEY,
                action TEXT NOT NULL,
                charge_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

        # User points (daily bonus claimed via a dedicated button)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_points (
                user_id BIGINT PRIMARY KEY,
                total_points INTEGER NOT NULL DEFAULT 0,
                last_claim_date DATE
            )
            """
        )

        conn.commit()
        cur.close()
        print("Database is ready.")
    except Exception as e:
        print(f"Failed to set up the database: {e}")
    finally:
        if conn is not None:
            release_connection(conn)


def upsert_user(user_id, username=None, first_name=None):
    """Saves/updates the last known name for anyone who has interacted with the bot."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, username, first_name, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                updated_at = NOW()
            """,
            (user_id, username, first_name),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"upsert_user error: {e}")
    finally:
        if conn is not None:
            release_connection(conn)


def get_count():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM registrations")
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception as e:
        print(f"get_count error: {e}")
        return 0
    finally:
        if conn is not None:
            release_connection(conn)


def is_registered(user_id):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM registrations WHERE user_id = %s", (user_id,))
        exists = cur.fetchone() is not None
        cur.close()
        return exists
    except Exception as e:
        print(f"is_registered error: {e}")
        return False
    finally:
        if conn is not None:
            release_connection(conn)


def register_user(user_id):
    """Registers the user, returns True if it succeeded, False if already registered."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO registrations (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )
        added = cur.rowcount > 0
        conn.commit()
        cur.close()
        return added
    except Exception as e:
        print(f"register_user error: {e}")
        return False
    finally:
        if conn is not None:
            release_connection(conn)


def record_donation(user_id, amount, charge_id):
    """Records a successful support payment in the donations table."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO donations (user_id, amount, telegram_payment_charge_id)
            VALUES (%s, %s, %s)
            """,
            (user_id, amount, charge_id),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"record_donation error: {e}")
        return False
    finally:
        if conn is not None:
            release_connection(conn)


def get_donations_stats():
    """Returns (number of donations, total stars) from the donations table."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM donations")
        count, total = cur.fetchone()
        cur.close()
        return count, total
    except Exception as e:
        print(f"get_donations_stats error: {e}")
        return 0, 0
    finally:
        if conn is not None:
            release_connection(conn)


def get_registered_users_with_donations():
    """
    Returns the list of registered members with each one's display name
    (from the users table) and total stars donated, ordered by registration date.
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
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
            """
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {"user_id": row[0], "name": row[1], "donated": int(row[2])}
            for row in rows
        ]
    except Exception as e:
        print(f"get_registered_users_with_donations error: {e}")
        return []
    finally:
        if conn is not None:
            release_connection(conn)


def mask_name(name):
    """
    Blanks out every character of a name except the first one.
    Example: "Amanda" -> "A*****"
    """
    if not name:
        return "*"
    name = str(name).strip()
    if len(name) <= 1:
        return name
    return name[0] + "*" * (len(name) - 1)


# ───────────────────────── idea suggestions ─────────────────────────
def set_pending_action(user_id, action, charge_id=None):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_actions (user_id, action, charge_id, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET action = EXCLUDED.action,
                charge_id = EXCLUDED.charge_id,
                created_at = NOW()
            """,
            (user_id, action, charge_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"set_pending_action error: {e}")
    finally:
        if conn is not None:
            release_connection(conn)


def get_pending_action(user_id):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT action, charge_id FROM pending_actions WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {"action": row[0], "charge_id": row[1]}
        return None
    except Exception as e:
        print(f"get_pending_action error: {e}")
        return None
    finally:
        if conn is not None:
            release_connection(conn)


def clear_pending_action(user_id):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM pending_actions WHERE user_id = %s", (user_id,))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"clear_pending_action error: {e}")
    finally:
        if conn is not None:
            release_connection(conn)


def record_suggestion(user_id, content, charge_id):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO suggestions (user_id, content, telegram_payment_charge_id)
            VALUES (%s, %s, %s)
            """,
            (user_id, content, charge_id),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"record_suggestion error: {e}")
        return False
    finally:
        if conn is not None:
            release_connection(conn)


def get_suggestions_count():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM suggestions")
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception as e:
        print(f"get_suggestions_count error: {e}")
        return 0
    finally:
        if conn is not None:
            release_connection(conn)


# ───────────────────────── unsubscribing (opting out) ─────────────────────────
def unregister_user(user_id):
    """Deletes the user from the registrations table. Returns True if they were removed."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM registrations WHERE user_id = %s", (user_id,))
        removed = cur.rowcount > 0
        conn.commit()
        cur.close()
        return removed
    except Exception as e:
        print(f"unregister_user error: {e}")
        return False
    finally:
        if conn is not None:
            release_connection(conn)


# ───────────────────────── daily points system ─────────────────────────
def claim_daily_points(user_id):
    """
    Tries to grant the user DAILY_POINTS for today.
    Returns (True, new_total) if this is the first claim today.
    Returns (False, current_total) if they already claimed today.
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_points (user_id, total_points, last_claim_date)
            VALUES (%s, %s, CURRENT_DATE)
            ON CONFLICT (user_id) DO UPDATE
            SET total_points = user_points.total_points + %s,
                last_claim_date = CURRENT_DATE
            WHERE user_points.last_claim_date IS DISTINCT FROM CURRENT_DATE
            RETURNING total_points
            """,
            (user_id, DAILY_POINTS, DAILY_POINTS),
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            cur.close()
            return True, row[0]

        # Nothing was updated, meaning they already claimed today - fetch their current total
        cur.execute("SELECT total_points FROM user_points WHERE user_id = %s", (user_id,))
        existing = cur.fetchone()
        conn.commit()
        cur.close()
        return False, (existing[0] if existing else 0)
    except Exception as e:
        print(f"claim_daily_points error: {e}")
        return False, 0
    finally:
        if conn is not None:
            release_connection(conn)


def get_user_points(user_id):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT total_points FROM user_points WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"get_user_points error: {e}")
        return 0
    finally:
        if conn is not None:
            release_connection(conn)


def get_leaderboard(limit=10):
    """Returns the top point earners (name + total points)."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                p.user_id,
                COALESCE(NULLIF(u.username, ''), NULLIF(u.first_name, ''), 'Member') AS display_name,
                p.total_points
            FROM user_points p
            LEFT JOIN users u ON u.user_id = p.user_id
            WHERE p.total_points > 0
            ORDER BY p.total_points DESC, p.user_id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [{"user_id": r[0], "name": r[1], "points": r[2]} for r in rows]
    except Exception as e:
        print(f"get_leaderboard error: {e}")
        return []
    finally:
        if conn is not None:
            release_connection(conn)


# ───────────────────────── Telegram helpers ─────────────────────────
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"send_message error: {e}")


def answer_callback(callback_id, text, show_alert=False):
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
            timeout=10,
        )
    except Exception as e:
        print(f"answer_callback error: {e}")


def send_invoice(chat_id, amount, title, description, payload_str):
    """
    Sends a Telegram Stars (XTR) invoice. provider_token must be empty for Stars.
    payload_str is stored inside the invoice and comes back to us after a
    successful payment, so we can tell which kind of purchase it was
    (a plain donation, or a paid suggestion).
    """
    payload = {
        "chat_id": chat_id,
        "title": title,
        "description": description,
        "payload": payload_str,
        "provider_token": "",  # must be empty when using the XTR (Stars) currency
        "currency": "XTR",
        "prices": [{"label": title, "amount": amount}],
    }
    try:
        r = requests.post(f"{TELEGRAM_API}/sendInvoice", json=payload, timeout=10)
        print(f"send_invoice({payload_str}) -> {r.json()}")
    except Exception as e:
        print(f"send_invoice error: {e}")


def send_donation_invoice(chat_id, amount):
    send_invoice(
        chat_id,
        amount,
        "Support Betrothed 💍",
        f"Your support of {amount} Telegram Stars helps us keep the bot running and growing 🙏",
        f"donate_{amount}_{chat_id}",
    )


def send_suggestion_invoice(chat_id):
    send_invoice(
        chat_id,
        SUGGESTION_PRICE,
        "Suggest a new idea 💡",
        f"Pay {SUGGESTION_PRICE} Stars to send us your idea or suggestion for the bot, our team reviews every one",
        f"suggest_{SUGGESTION_PRICE}_{chat_id}",
    )


def create_invoice_link(amount, title, description, payload_str):
    """
    Like send_invoice, but instead of pushing the invoice into a chat, it returns
    a payment link. Used by the Mini App so users can pay for a donation without
    leaving the web view (opened client-side via Telegram.WebApp.openInvoice).
    """
    payload = {
        "title": title,
        "description": description,
        "payload": payload_str,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": title, "amount": amount}],
    }
    try:
        r = requests.post(f"{TELEGRAM_API}/createInvoiceLink", json=payload, timeout=10)
        data = r.json()
        if data.get("ok"):
            return data["result"]
        print(f"create_invoice_link error: {data}")
        return None
    except Exception as e:
        print(f"create_invoice_link error: {e}")
        return None


def answer_pre_checkout(pre_checkout_query_id, ok=True, error_message=None):
    """Must answer pre_checkout_query within 10 seconds, or the payment is auto-rejected."""
    payload = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
    if error_message:
        payload["error_message"] = error_message
    try:
        requests.post(f"{TELEGRAM_API}/answerPreCheckoutQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"answer_pre_checkout error: {e}")


def donation_keyboard():
    keyboard = [
        [{"text": f"⭐ Support with {amount} Stars", "callback_data": f"donate_{amount}"}]
        for amount in DONATION_AMOUNTS
    ]
    return {"inline_keyboard": keyboard}


def unsubscribe_confirm_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "✅ Yes, unsubscribe me", "callback_data": "confirm_unsubscribe"}],
            [{"text": "🙅 No, cancel that", "callback_data": "cancel_unsubscribe"}],
        ]
    }


def build_leaderboard_text():
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
    if not ADMIN_CHAT_ID:
        return
    who = f"@{username}" if username else f"id:{user_id}"
    send_message(
        ADMIN_CHAT_ID,
        f"💡 New suggestion from {who}\n\n{content}",
    )


# ───────────────────────── Telegram WebApp initData validation ─────────────────────────
def validate_init_data(init_data: str, bot_token: str):
    """
    Verifies the HMAC signature of the initData string the Mini App sends back,
    proving it actually came from Telegram and identifies a real user.
    Returns the parsed key/value dict on success, or None if invalid/missing.
    """
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
            return None
        return parsed
    except Exception as e:
        print(f"validate_init_data error: {e}")
        return None


# ───────────────────────── web app (fullscreen mini app) ─────────────────────────
# Design: a keepsake wedding ledger. Deep wine-and-ink page, aged-gold rules and
# a wax-seal medallion around the headline number, styled like an engraved
# invitation rather than a stats dashboard.
MINI_APP_PAGE = """
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

  /* ───── letterhead ───── */
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

  /* ───── pages ───── */
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

  /* --- home page: the seal --- */
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

  /* --- community page: the guest ledger --- */
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

  /* --- support page: donate with Stars --- */
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
  .donate-btn:disabled { opacity: 0.55; }
  .support-status {
    text-align: center;
    color: var(--muted);
    font-family: 'Cormorant Garamond', serif;
    font-style: italic;
    font-size: 15px;
    margin-top: 6px;
    min-height: 20px;
  }

  /* ───── navigation ───── */
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
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) { try { tg.setHeaderColor('#100a0e'); } catch (e) {} }
      if (tg.setBackgroundColor) { try { tg.setBackgroundColor('#100a0e'); } catch (e) {} }
      if (tg.disableVerticalSwipes) { try { tg.disableVerticalSwipes(); } catch (e) {} }
    }

    const DONATION_AMOUNTS = [100, 200, 500, 1000];

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
      const clean = (name || '').replace(/\\*/g, '');
      return (clean.charAt(0) || '?').toUpperCase();
    }

    async function refreshCount() {
      try {
        const res = await fetch('/api/count');
        const data = await res.json();
        const el = document.getElementById('count');
        if (el.textContent != data.count) {
          el.textContent = data.count;
          el.style.transform = 'scale(1.12)';
          setTimeout(() => { el.style.transform = 'scale(1)'; }, 200);
        }
      } catch (e) { console.error('Could not refresh the count', e); }
    }

    async function loadCommunity() {
      try {
        const [statsRes, usersRes] = await Promise.all([fetch('/api/donations'), fetch('/api/users')]);
        const stats = await statsRes.json();
        const usersData = await usersRes.json();
        document.getElementById('d-count').textContent = stats.donations_count;
        document.getElementById('d-total').textContent = stats.stars_total;

        const list = document.getElementById('users-list');
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
      } catch (e) { console.error('Could not load the members list', e); }
    }

    function renderDonateOptions() {
      const wrap = document.getElementById('donate-options');
      if (wrap.dataset.rendered === '1') return;
      wrap.dataset.rendered = '1';
      wrap.innerHTML = DONATION_AMOUNTS.map(a =>
        `<button class="donate-btn" data-amount="${a}">⭐ Support with ${a} Stars</button>`
      ).join('');
      wrap.querySelectorAll('.donate-btn').forEach(btn => {
        btn.addEventListener('click', () => startDonation(parseInt(btn.dataset.amount, 10), btn));
      });
    }

    function setStatus(text) {
      document.getElementById('support-status').textContent = text || '';
    }

    async function startDonation(amount, btn) {
      if (!tg || !tg.initData) {
        setStatus('Please open this from inside Telegram.');
        return;
      }
      const buttons = document.querySelectorAll('.donate-btn');
      buttons.forEach(b => b.disabled = true);
      setStatus('Preparing your invoice…');

      try {
        const res = await fetch('/api/create-invoice', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ amount, init_data: tg.initData })
        });
        const data = await res.json();

        if (!data.ok || !data.link) {
          setStatus('Could not start the payment, please try again.');
          buttons.forEach(b => b.disabled = false);
          return;
        }

        setStatus('');
        tg.openInvoice(data.link, (status) => {
          buttons.forEach(b => b.disabled = false);
          if (status === 'paid') {
            setStatus('Thank you for your support 💛');
            if (tg.HapticFeedback) { try { tg.HapticFeedback.notificationOccurred('success'); } catch (e) {} }
            loadCommunity();
          } else if (status === 'cancelled') {
            setStatus('');
          } else {
            setStatus('Payment was not completed.');
          }
        });
      } catch (e) {
        console.error('startDonation error', e);
        setStatus('Something went wrong, please try again.');
        buttons.forEach(b => b.disabled = false);
      }
    }

    refreshCount();
    setInterval(refreshCount, 5000);

    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') === 'community') showView('community');
    if (params.get('tab') === 'support') showView('support');
  </script>
</body>
</html>
"""


@app.route("/")
def home():
    count = get_count()
    return render_template_string(MINI_APP_PAGE, count=count, suggestion_price=SUGGESTION_PRICE)


@app.route("/users")
def users_page():
    """Legacy link kept for compatibility: same app, opened on the members tab."""
    count = get_count()
    return render_template_string(MINI_APP_PAGE, count=count, suggestion_price=SUGGESTION_PRICE)


@app.route("/api/count")
def api_count():
    return jsonify({"count": get_count()})


@app.route("/api/donations")
def api_donations():
    count, total = get_donations_stats()
    return jsonify({"donations_count": count, "stars_total": total})


@app.route("/api/users")
def api_users():
    """List of registered members with masked names (only the first letter shown) and total stars given."""
    users = get_registered_users_with_donations()
    result = [
        {"masked_name": mask_name(u["name"]), "donated": u["donated"]}
        for u in users
    ]
    return jsonify({"users": result})


@app.route("/api/create-invoice", methods=["POST"])
def api_create_invoice():
    """
    Called from the Support tab inside the Mini App. Validates the Telegram
    WebApp initData (so we know the request really came from that Telegram user),
    then returns a Stars invoice link the client opens with Telegram.WebApp.openInvoice.
    The actual payment confirmation still arrives at /webhook/<token> as a normal
    successful_payment update, so donations are recorded exactly like bot-side ones.
    """
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get("init_data", "")
    amount = body.get("amount")

    parsed = validate_init_data(init_data, BOT_TOKEN)
    if not parsed:
        return jsonify({"ok": False, "error": "invalid_init_data"}), 401

    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_amount"}), 400

    if amount not in DONATION_AMOUNTS:
        return jsonify({"ok": False, "error": "invalid_amount"}), 400

    try:
        user_info = json.loads(parsed.get("user", "{}"))
        user_id = user_info.get("id")
    except Exception:
        user_id = None

    if not user_id:
        return jsonify({"ok": False, "error": "no_user"}), 400

    upsert_user(user_id, username=user_info.get("username"), first_name=user_info.get("first_name"))

    link = create_invoice_link(
        amount,
        "Support Betrothed 💍",
        f"Your support of {amount} Telegram Stars helps us keep the bot running and growing 🙏",
        f"donate_{amount}_{user_id}",
    )
    if not link:
        return jsonify({"ok": False, "error": "invoice_failed"}), 500

    return jsonify({"ok": True, "link": link})


# ───────────────────────── diagnostics ─────────────────────────
@app.route("/health")
def health_check():
    """
    Diagnostic endpoint: tells you exactly where the slowness/failure comes from
    (the database, the Telegram API, or neither). Open it in a browser at
    https://<your-domain>/health
    """
    result = {"app": "ok"}

    # Test the database connection and measure how long it takes
    if DATABASE_URL:
        conn = None
        try:
            start = time.time()
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            result["database"] = {
                "status": "ok",
                "latency_ms": round((time.time() - start) * 1000, 1),
                "pool_active": db_pool is not None,
            }
        except Exception as e:
            result["database"] = {"status": "error", "error": str(e)}
        finally:
            if conn is not None:
                release_connection(conn)
    else:
        result["database"] = {"status": "missing DATABASE_URL"}

    # Test the connection to the Telegram API
    if BOT_TOKEN:
        try:
            start = time.time()
            r = requests.get(f"{TELEGRAM_API}/getMe", timeout=5)
            result["telegram"] = {
                "status": "ok" if r.ok else "error",
                "latency_ms": round((time.time() - start) * 1000, 1),
            }
        except Exception as e:
            result["telegram"] = {"status": "error", "error": str(e)}
    else:
        result["telegram"] = {"status": "missing BOT_TOKEN"}

    return jsonify(result)


# ───────────────────────── webhook (receives bot updates) ─────────────────────────
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    # Pre-checkout confirmation request - must be answered within 10 seconds
    if "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        answer_pre_checkout(pcq["id"], ok=True)
        return jsonify({"ok": True})

    # A regular text message (like /start) or a successful-payment notification
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "") or ""

        sender = msg.get("from", {})
        user_id = sender.get("id")
        if user_id:
            upsert_user(user_id, username=sender.get("username"), first_name=sender.get("first_name"))

        # Successful Telegram Stars payment (support gift or suggestion purchase)
        if "successful_payment" in msg:
            sp = msg["successful_payment"]
            amount = sp.get("total_amount", 0)
            charge_id = sp.get("telegram_payment_charge_id")
            invoice_payload = sp.get("invoice_payload", "") or ""

            if invoice_payload.startswith("suggest_"):
                # This was a suggestion payment: wait for their next message and store it as the suggestion content
                set_pending_action(user_id, "awaiting_suggestion", charge_id)
                send_message(
                    chat_id,
                    "Payment received 💡\nNow send us your idea or suggestion in a single message, and we'll pass it straight to the dev team 🙏",
                )
            else:
                record_donation(user_id, amount, charge_id)
                send_message(
                    chat_id,
                    f"Thank you, truly 🙏💛\nWe've received your support of {amount} Stars ⭐️\nMay it come back to you tenfold!",
                )
            return jsonify({"ok": True})

        # If there's a paid suggestion waiting on its text, and this message isn't a command (doesn't start with /)
        pending = get_pending_action(user_id) if user_id else None
        if pending and pending["action"] == "awaiting_suggestion" and text and not text.startswith("/"):
            record_suggestion(user_id, text, pending.get("charge_id"))
            clear_pending_action(user_id)
            send_message(chat_id, "Your idea has been recorded ✅ Thank you for taking the time to share it 🙏💡")
            notify_admin_new_suggestion(user_id, sender.get("username"), text)
            return jsonify({"ok": True})

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            if start_payload == "suggest":
                # Came from the "Suggest a feature" button on the site: send the suggestion invoice directly
                send_suggestion_invoice(chat_id)
            else:
                site_url = f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')}"
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ I'm ready to marry", "callback_data": "want_marry"}],
                        [{"text": "🌐 Open the registry", "web_app": {"url": site_url}}],
                        [
                            {"text": "🎁 Daily points", "callback_data": "claim_points"},
                            {"text": "🏆 Leaderboard", "callback_data": "show_leaderboard"},
                        ],
                        [{"text": "⭐ Support the bot", "callback_data": "show_donate"}],
                        [{"text": "💡 Suggest a feature", "callback_data": "propose_idea"}],
                        [{"text": "❌ Unsubscribe", "callback_data": "unsubscribe"}],
                    ]
                }
                send_message(
                    chat_id,
                    "Welcome to Betrothed 💍\n"
                    "Tap the button below to add your name to the registry of people ready for marriage 😄",
                    keyboard,
                )

        elif text in ("/count",):
            send_message(chat_id, f"People registered so far: {get_count()} 💍")

        elif text in ("/support", "/donate"):
            send_message(
                chat_id,
                "You can support the bot with Telegram Stars ⭐ pick the amount that suits you:",
                donation_keyboard(),
            )

        elif text in ("/donations",):
            count, total = get_donations_stats()
            send_message(
                chat_id,
                f"Number of gifts received: {count}\nTotal Stars received: {total} ⭐️",
            )

        elif text in ("/suggest",):
            send_suggestion_invoice(chat_id)

        elif text in ("/suggestions",) and str(chat_id) == str(ADMIN_CHAT_ID):
            send_message(chat_id, f"Suggestions received so far: {get_suggestions_count()} 💡")

        elif text in ("/points",):
            send_message(chat_id, f"Your total points: {get_user_points(user_id)} ⭐")

        elif text in ("/leaderboard",):
            send_message(chat_id, build_leaderboard_text())

        elif text in ("/unsubscribe",):
            if is_registered(user_id):
                send_message(
                    chat_id,
                    "Are you sure you want to leave the marriage registry?",
                    unsubscribe_confirm_keyboard(),
                )
            else:
                send_message(chat_id, "You're not on the registry to begin with 🙂")

    # Button press (Callback Query)
    elif "callback_query" in update:
        cq = update["callback_query"]
        sender = cq.get("from", {})
        user_id = sender["id"]
        chat_id = cq["message"]["chat"]["id"]
        callback_id = cq["id"]
        data_key = cq.get("data", "")

        upsert_user(user_id, username=sender.get("username"), first_name=sender.get("first_name"))

        if data_key == "want_marry":
            if is_registered(user_id):
                answer_callback(callback_id, "You're already registered 😄")
            else:
                added = register_user(user_id)
                if added:
                    answer_callback(callback_id, "You're registered! Congratulations in advance 🎉")
                    send_message(
                        chat_id,
                        f"You're registered ✅\nPeople ready for marriage so far: {get_count()} 💍",
                    )
                else:
                    answer_callback(callback_id, "Something went wrong, please try again 🙏")

        elif data_key == "show_donate":
            answer_callback(callback_id, "")
            send_message(
                chat_id,
                "You can support the bot with Telegram Stars ⭐ pick the amount that suits you:",
                donation_keyboard(),
            )

        elif data_key.startswith("donate_"):
            try:
                amount = int(data_key.split("_")[1])
            except (IndexError, ValueError):
                amount = 0
            if amount in DONATION_AMOUNTS:
                answer_callback(callback_id, f"Preparing an invoice for {amount} Stars ⭐")
                send_donation_invoice(chat_id, amount)
            else:
                answer_callback(callback_id, "That's not a valid amount 🙏")

        elif data_key == "propose_idea":
            answer_callback(callback_id, f"Preparing an invoice for {SUGGESTION_PRICE} Stars ⭐")
            send_suggestion_invoice(chat_id)

        elif data_key == "claim_points":
            success, total = claim_daily_points(user_id)
            if success:
                answer_callback(callback_id, f"🎁 +{DAILY_POINTS} points!")
                send_message(
                    chat_id,
                    f"You've claimed {DAILY_POINTS} points today 🎁\nYour total is now: {total} ⭐\nCome back tomorrow for more!",
                )
            else:
                answer_callback(
                    callback_id,
                    "You've already claimed your points today, come back tomorrow 🙏",
                    show_alert=True,
                )

        elif data_key == "show_leaderboard":
            answer_callback(callback_id, "")
            send_message(chat_id, build_leaderboard_text())

        elif data_key == "unsubscribe":
            answer_callback(callback_id, "")
            if is_registered(user_id):
                send_message(
                    chat_id,
                    "Are you sure you want to leave the marriage registry?",
                    unsubscribe_confirm_keyboard(),
                )
            else:
                send_message(chat_id, "You're not on the registry to begin with 🙂")

        elif data_key == "confirm_unsubscribe":
            removed = unregister_user(user_id)
            if removed:
                answer_callback(callback_id, "You've been unsubscribed ✅")
                send_message(
                    chat_id,
                    f"You've been removed from the registry 👋\nPeople ready for marriage now: {get_count()} 💍",
                )
            else:
                answer_callback(callback_id, "You weren't registered to begin with 🙂")

        elif data_key == "cancel_unsubscribe":
            answer_callback(callback_id, "Understood, no changes made 👍")

    return jsonify({"ok": True})


# ───────────────────────── automatic webhook setup ─────────────────────────
def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not domain or not BOT_TOKEN:
        print("BOT_TOKEN or RAILWAY_PUBLIC_DOMAIN is missing, skipping automatic webhook setup.")
        return
    url = f"https://{domain}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={
                "url": url,
                "allowed_updates": json.dumps(
                    ["message", "callback_query", "pre_checkout_query"]
                ),
            },
            timeout=10,
        )
        print(f"Webhook set to: {url} -> {r.json()}")
    except Exception as e:
        print(f"Failed to set the webhook: {e}")


# Runs when the module is imported (works with both gunicorn and running it directly)
init_pool()
init_db()
set_webhook()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
