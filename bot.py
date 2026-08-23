import os
import json
import random
import hashlib
import requests
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

# التوكن يُقرأ من متغير بيئة اسمه BOT_TOKEN (تُضاف من لوحة Railway → Variables)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# رابط قاعدة البيانات (يُضاف من لوحة Railway → Variables باسم DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# نطاق الرقم العشوائي لكل مستخدم (تقدر تعدله متل ما بدك)
SHANQLA_MIN = 1
SHANQLA_MAX = 999


# ───────────────────────── قاعدة البيانات ─────────────────────────
def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL غير موجود، لن يتم الاتصال بقاعدة البيانات.")
        return
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shanqla (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                count INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        conn.commit()
        cur.close()
        conn.close()
        print("تم تجهيز قاعدة البيانات بنجاح.")
    except Exception as e:
        print(f"فشل تجهيز قاعدة البيانات: {e}")


def generate_shanqla_count(user_id):
    """رقم عشوائي لكن ثابت لكل مستخدم (نفس الشخص دايماً بياخد نفس الرقم)."""
    seed_str = f"shanqla-{user_id}"
    seed = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16)
    rng = random.Random(seed)
    return rng.randint(SHANQLA_MIN, SHANQLA_MAX)


def get_or_create_shanqla(user_id, username=None, first_name=None):
    """يرجع عدد الشنقلات المخزن للمستخدم، وينشئه أول مرة لو مش موجود."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT count FROM shanqla WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            # تحديث الاسم لو تغيّر
            cur.execute(
                "UPDATE shanqla SET username = %s, first_name = %s WHERE user_id = %s",
                (username, first_name, user_id),
            )
            conn.commit()
            cur.close()
            conn.close()
            print(f"[DB] مستخدم موجود مسبقاً: user_id={user_id} count={row[0]}")
            return row[0]

        count = generate_shanqla_count(user_id)
        cur.execute(
            """
            INSERT INTO shanqla (user_id, username, first_name, count)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, username, first_name, count),
        )
        conn.commit()
        # نتأكد فعلياً إنه انحفظ بقراءته مرة ثانية من الداتابيس (مش من الذاكرة)
        cur.execute("SELECT count FROM shanqla WHERE user_id = %s", (user_id,))
        confirm_row = cur.fetchone()
        cur.close()
        conn.close()

        if confirm_row and confirm_row[0] == count:
            print(f"[DB] ✅ تم حفظ مستخدم جديد بنجاح: user_id={user_id} username={username} count={count}")
        else:
            print(f"[DB] ⚠️ تحذير: الإدخال ما تأكدش بعد الحفظ لـ user_id={user_id}")

        return count
    except Exception as e:
        print(f"[DB] ❌ get_or_create_shanqla error: {e}")
        return generate_shanqla_count(user_id)


def get_algeria_stats():
    """مجموع شنقلات كل المستخدمين اللي استعملوا البوت + عددهم."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(count), 0) FROM shanqla")
        users_count, total = cur.fetchone()
        cur.close()
        conn.close()
        print(f"[DB] إحصائية: عدد المستخدمين={users_count} المجموع={total}")
        return users_count, total
    except Exception as e:
        print(f"[DB] ❌ get_algeria_stats error: {e}")
        return 0, 0


def get_all_users_debug(limit=20):
    """يرجع آخر المستخدمين المخزنين، تستخدمها للتأكد اليدوي إن البيانات محفوظة فعلاً."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, first_name, count, created_at FROM shanqla ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB] ❌ get_all_users_debug error: {e}")
        return []


# ───────────────────────── دوال تيليجرام ─────────────────────────
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


def main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "😂 شحال عندي شنقلة فدار؟", "callback_data": "my_shanqla"}],
            [{"text": "🇩🇿 شحال عند الشعب الجزائري؟", "callback_data": "algeria_shanqla"}],
        ]
    }


# ───────────────────────── الويب هوك ─────────────────────────
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "") or ""
        sender = msg.get("from", {})
        user_id = sender.get("id")
        username = sender.get("username")
        first_name = sender.get("first_name")

        if text.startswith("/start"):
            send_message(
                chat_id,
                "أهلاً فيك 👋\nهاد البوت يحسبلك شحال عندك من \"شنقلة فدار\" 😂\nاضغط زر تحت:",
                main_keyboard(),
            )

        elif text in ("/شنقلتي", "/my"):
            count = get_or_create_shanqla(user_id, username, first_name)
            send_message(chat_id, f"عندك {count} شنقلة فدار 😂")

        elif text in ("/الجزائر", "/algeria"):
            users_count, total = get_algeria_stats()
            send_message(
                chat_id,
                f"🇩🇿 الشعب الجزائري عنده {total} شنقلة فدار!\n(حسب {users_count} شخص استعملوا البوت لحد الآن)",
            )

        elif text in ("/تأكيد", "/debug"):
            rows = get_all_users_debug(20)
            if not rows:
                send_message(chat_id, "ما في ولا سجل محفوظ لسا بقاعدة البيانات.")
            else:
                lines = ["📋 آخر السجلات المحفوظة فعلياً بقاعدة البيانات:\n"]
                for r in rows:
                    uid, uname, fname, count, created_at = r
                    who = f"@{uname}" if uname else (fname or f"id:{uid}")
                    lines.append(f"• {who} — {count} شنقلة — {created_at}")
                send_message(chat_id, "\n".join(lines))

        else:
            send_message(
                chat_id,
                "استخدم الأزرار تحت، أو اكتب /شنقلتي أو /الجزائر 👇",
                main_keyboard(),
            )

    elif "callback_query" in update:
        cq = update["callback_query"]
        sender = cq.get("from", {})
        user_id = sender["id"]
        username = sender.get("username")
        first_name = sender.get("first_name")
        chat_id = cq["message"]["chat"]["id"]
        callback_id = cq["id"]
        data_key = cq.get("data", "")

        if data_key == "my_shanqla":
            count = get_or_create_shanqla(user_id, username, first_name)
            answer_callback(callback_id, "")
            send_message(chat_id, f"عندك {count} شنقلة فدار 😂")

        elif data_key == "algeria_shanqla":
            answer_callback(callback_id, "")
            users_count, total = get_algeria_stats()
            send_message(
                chat_id,
                f"🇩🇿 الشعب الجزائري عنده {total} شنقلة فدار!\n(حسب {users_count} شخص استعملوا البوت لحد الآن)",
            )

    return jsonify({"ok": True})


# ───────────────────────── تفعيل الويب هوك تلقائياً ─────────────────────────
def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not domain or not BOT_TOKEN:
        print("BOT_TOKEN أو RAILWAY_PUBLIC_DOMAIN غير موجودين، لن يتم ضبط الويب هوك تلقائياً.")
        return
    url = f"https://{domain}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": url, "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=10,
        )
        print(f"Webhook set to: {url} -> {r.json()}")
    except Exception as e:
        print(f"فشل ضبط الويب هوك: {e}")


init_db()
set_webhook()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
