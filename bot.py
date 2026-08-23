```python
import os
import json
import time
import requests
import psycopg2
from psycopg2 import OperationalError
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template_string
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# ───────────────────────── تجهيز DATABASE_URL ─────────────────────────
def build_database_url():
    """يبني DATABASE_URL من البيئة، ويعدل خيارات الاتصال لـ Railway."""
    url = os.environ.get("DATABASE_URL", "").strip()

    if not url:
        host = os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST")
        port = os.environ.get("PGPORT") or os.environ.get("POSTGRES_PORT") or "5432"
        user = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER") or "postgres"
        password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD")
        dbname = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB") or "railway"

        if host and password:
            url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        else:
            return ""

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    # استخدام prefer بدلاً من require لتجنب فشل SSL في الاتصالات الداخلية على Railway
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "sslmode" not in query:
            query["sslmode"] = ["prefer"]
        new_query = urlencode(query, doseq=True)
        url = urlunparse(parsed._replace(query=new_query))
    except Exception as e:
        print(f"[DB] تحذير أثناء تعديل DATABASE_URL: {e}")

    return url

# ───────────────────────── اتصال آمن وديناميكي ─────────────────────────
@contextmanager
def get_db_connection():
    """Context manager يقرأ الرابط ديناميكياً في كل مرّة لاتصال مضمون."""
    db_url = build_database_url()
    if not db_url:
        raise OperationalError("DATABASE_URL غير موجود في البيئة")

    conn = None
    try:
        conn = psycopg2.connect(
            db_url,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        yield conn
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

def init_db():
    if not build_database_url():
        print("[DB] ⚠️ DATABASE_URL غير موجود. البوت سيعمل بدون حفظ.")
        return

    for attempt in range(3):
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS shanqla (
                            user_id BIGINT PRIMARY KEY,
                            username TEXT,
                            first_name TEXT,
                            count INTEGER NOT NULL,
                            created_at TIMESTAMP DEFAULT NOW(),
                            updated_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS pending_actions (
                            user_id BIGINT PRIMARY KEY,
                            action TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
            print("[DB] ✅ تم تجهيز قاعدة البيانات بنجاح.")
            return
        except Exception as e:
            print(f"[DB] ❌ محاولة {attempt+1}/3 فشلت: {e}")
            time.sleep(2)

    print("[DB] ❌ فشل تجهيز قاعدة البيانات بعد 3 محاولات.")

# ───────────────────────── دوال قاعدة البيانات ─────────────────────────
def set_pending_action(user_id, action):
    if not build_database_url():
        return False
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pending_actions (user_id, action, created_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET action = EXCLUDED.action, created_at = NOW()
                """, (user_id, action))
        return True
    except Exception as e:
        print(f"[DB] ❌ set_pending_action: {e}")
        return False

def get_pending_action(user_id):
    if not build_database_url():
        return None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT action FROM pending_actions WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"[DB] ❌ get_pending_action: {e}")
        return None

def clear_pending_action(user_id):
    if not build_database_url():
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pending_actions WHERE user_id = %s", (user_id,))
    except Exception as e:
        print(f"[DB] ❌ clear_pending_action: {e}")

def save_shanqla_count(user_id, count, username=None, first_name=None):
    if not build_database_url():
        return False, "DATABASE_URL غير موجود. أضف المتغير من لوحة Railway."

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO shanqla (user_id, username, first_name, count, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET count = EXCLUDED.count,
                        username = COALESCE(EXCLUDED.username, shanqla.username),
                        first_name = COALESCE(EXCLUDED.first_name, shanqla.first_name),
                        updated_at = NOW()
                """, (user_id, username, first_name, count))

                cur.execute("SELECT count FROM shanqla WHERE user_id = %s", (user_id,))
                confirm_row = cur.fetchone()

            if confirm_row and confirm_row[0] == count:
                print(f"[DB] ✅ تم الحفظ: user_id={user_id} count={count}")
                return True, None

            return False, "الحفظ تم لكن التأكيد فشل"
    except OperationalError as e:
        error_text = str(e)
        if "password authentication failed" in error_text:
            return False, "كلمة مرور قاعدة البيانات غير صحيحة."
        if "could not connect" in error_text or "Connection refused" in error_text:
            return False, "تعذر الاتصال بقاعدة البيانات. تأكد من عمل خدمة Postgres."
        return False, f"OperationalError: {error_text}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def check_db_connection():
    if not build_database_url():
        return False, "DATABASE_URL مش موجود إطلاقاً في Variables."

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = 'shanqla'
                    )
                """)
                table_exists = cur.fetchone()[0]

            if not table_exists:
                return False, "الاتصال شغال، بس جدول shanqla مش موجود."
            return True, "الاتصال بقاعدة البيانات شغال وجدول shanqla موجود ✅"
    except OperationalError as e:
        error_text = str(e)
        if "password authentication failed" in error_text:
            return False, "كلمة مرور قاعدة البيانات غلط (password authentication failed)."
        return False, f"OperationalError: {error_text}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def get_user_count(user_id):
    if not build_database_url():
        return None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count FROM shanqla WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"[DB] ❌ get_user_count: {e}")
        return None

def get_algeria_stats():
    if not build_database_url():
        return 0, 0
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), COALESCE(SUM(count), 0) FROM shanqla")
                users_count, total = cur.fetchone()
                return users_count, total
    except Exception as e:
        print(f"[DB] ❌ get_algeria_stats: {e}")
        return 0, 0

def get_all_users_ranked():
    if not build_database_url():
        return []
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, username, first_name, count, created_at
                    FROM shanqla
                    ORDER BY count DESC, created_at ASC
                """)
                rows = cur.fetchall()
                return [
                    {"user_id": r[0], "username": r[1], "first_name": r[2], "count": r[3]}
                    for r in rows
                ]
    except Exception as e:
        print(f"[DB] ❌ get_all_users_ranked: {e}")
        return []

def mask_name(username, first_name):
    name = username or first_name or "مستخدم"
    name = str(name).strip()
    if len(name) <= 1:
        return name
    return name[0] + "*" * (len(name) - 1)

# ───────────────────────── دوال تيليجرام ─────────────────────────
def send_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_API:
        print("BOT_TOKEN غير موجود، ما بقدر أبعت رسالة")
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"send_message error: {e}")

def answer_callback(callback_id, text="", show_alert=False):
    if not TELEGRAM_API:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
            timeout=10,
        )
    except Exception as e:
        print(f"answer_callback error: {e}")

def main_keyboard(site_url):
    keyboard = [
        [{"text": "✍️ سجّل شنقلتي", "callback_data": "enter_shanqla"}],
        [{"text": "🇩🇿 شحال عند الشعب الجزائري؟", "callback_data": "algeria_shanqla"}],
    ]
    if site_url:
        keyboard.append([{"text": "🌐 افتح الموقع", "web_app": {"url": site_url}}])
    return {"inline_keyboard": keyboard}

# ───────────────────────── واجهة الويب ─────────────────────────
MINI_APP_PAGE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>شنقلة فدار 🇩🇿</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&family=Tajawal:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-1: #1c2f2a;
    --bg-2: #0c1512;
    --card: rgba(247, 237, 224, 0.05);
    --card-line: rgba(247, 237, 224, 0.10);
    --cream: #f2f0e6;
    --muted: #9db3ab;
    --green: #59b389;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; margin: 0; overscroll-behavior: none; }
  body {
    background: radial-gradient(130% 95% at 50% -5%, var(--bg-1) 0%, var(--bg-2) 65%);
    font-family: 'Tajawal', 'Segoe UI', Tahoma, Arial, sans-serif;
    color: var(--cream);
  }
  .app {
    height: 100dvh; width: 100%; max-width: 480px; margin: 0 auto;
    display: flex; flex-direction: column; overflow: hidden;
    padding-top: env(safe-area-inset-top, 0px); position: relative;
  }
  .glow {
    position: absolute; top: -120px; right: -80px; width: 320px; height: 320px;
    background: radial-gradient(circle, rgba(89,179,137,0.18), transparent 70%);
    pointer-events: none; z-index: 0;
  }
  .topbar {
    display: flex; align-items: center; justify-content: center; gap: 9px;
    padding: 16px 16px 6px; flex-shrink: 0; position: relative; z-index: 1;
  }
  .topbar svg { width: 21px; height: 21px; color: var(--green); flex-shrink: 0; }
  .topbar span { font-family: 'Amiri', serif; font-size: 19px; font-weight: 700; }
  .view { display: none; flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 8px 22px 28px; position: relative; z-index: 1; animation: fadeIn 0.35s ease; }
  .view.active { display: flex; flex-direction: column; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  #view-home { align-items: center; justify-content: center; text-align: center; }
  .flourish { width: 128px; height: 14px; opacity: 0.6; margin: 0 auto 24px; display: block; }
  .hero-label { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
  .count {
    font-family: 'Amiri', serif; font-size: clamp(56px, 18vw, 84px); line-height: 1;
    font-weight: 700; color: var(--green); margin: 0; transition: transform 0.25s ease;
  }
  .count-desc { color: var(--muted); font-size: 14px; line-height: 1.7; margin: 16px 0 0; max-width: 270px; }
  .cta-btn {
    margin-top: 22px; background: var(--green); color: #0c1512; border: none;
    font-family: inherit; font-weight: 900; font-size: 14.5px; padding: 13px 26px;
    border-radius: 14px; cursor: pointer;
  }
  #view-users { padding-top: 16px; }
  .users-summary {
    display: flex; justify-content: space-around; background: var(--card);
    border: 1px solid var(--card-line); border-radius: 18px; padding: 18px 10px;
    margin-bottom: 18px; flex-shrink: 0;
  }
  .stat { text-align: center; }
  .stat b { display: block; font-family: 'Amiri', serif; font-size: 25px; color: var(--green); }
  .stat span { font-size: 11px; color: var(--muted); }
  .section-label { font-size: 13px; color: var(--muted); margin: 4px 2px 10px; }
  .list { background: var(--card); border: 1px solid var(--card-line); border-radius: 18px; overflow: hidden; }
  .row { display: flex; justify-content: space-between; align-items: center; padding: 14px 17px; border-bottom: 1px solid var(--card-line); }
  .row:last-child { border-bottom: none; }
  .row .rank { color: var(--muted); font-size: 12px; width: 22px; flex-shrink: 0; }
  .row .name { font-size: 14.5px; flex: 1; }
  .row .amount { font-size: 13.5px; font-weight: 700; color: var(--green); white-space: nowrap; }
  .empty { text-align: center; color: var(--muted); font-size: 13.5px; padding: 44px 10px; }
  .tabbar {
    flex-shrink: 0; display: flex; gap: 10px; border-top: 1px solid var(--card-line);
    background: rgba(12, 21, 18, 0.88); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0px)); position: relative; z-index: 1;
  }
  .tab {
    flex: 1; display: flex; align-items: center; justify-content: center; gap: 7px;
    background: none; border: 1px solid transparent; color: var(--muted); font-family: inherit;
    font-size: 13px; font-weight: 700; padding: 12px 8px; border-radius: 14px; cursor: pointer;
  }
  .tab svg { width: 19px; height: 19px; flex-shrink: 0; }
  .tab.active { color: var(--green); background: rgba(89, 179, 137, 0.10); border-color: rgba(89, 179, 137, 0.25); }
</style>
</head>
<body>
  <div class="app">
    <div class="glow"></div>
    <div class="topbar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
        <path d="M12 3v18M5 12h14"/>
      </svg>
      <span>شنقلة فدار 🇩🇿</span>
    </div>
    <main id="view-home" class="view active">
      <svg class="flourish" viewBox="0 0 128 14" fill="none" stroke="#59b389" stroke-width="1">
        <line x1="0" y1="7" x2="48" y2="7"/><circle cx="64" cy="7" r="4"/><line x1="80" y1="7" x2="128" y2="7"/>
      </svg>
      <div class="hero-label">مجموع شنقلة فدار عند الشعب الجزائري</div>
      <div class="count" id="count">{{ total }}</div>
      <div class="count-desc">كل رقم هون مدخّل يدوياً من صاحبه، سجّل رقمك أنت كمان عبر البوت 👇</div>
      <button class="cta-btn" onclick="sendToBot()">سجّل رقمك بالبوت</button>
    </main>
    <main id="view-users" class="view">
      <div class="users-summary">
        <div class="stat"><b id="u-count">0</b><span>شخص سجّل رقمه</span></div>
        <div class="stat"><b id="u-total">0</b><span>المجموع الكلي</span></div>
      </div>
      <div class="section-label">الترتيب</div>
      <div class="list" id="users-list">
        <div class="empty">جاري التحميل...</div>
      </div>
    </main>
    <nav class="tabbar">
      <button class="tab active" data-view="home">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M4 11.5 12 5l8 6.5"/><path d="M6 10.5V19h12v-8.5"/>
        </svg>
        الرئيسية
      </button>
      <button class="tab" data-view="users">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.6 3-6.2 7-6.2s7 2.6 7 6.2"/>
        </svg>
        المستخدمون
      </button>
    </nav>
  </div>
  <script>
    if (window.Telegram && window.Telegram.WebApp) {
      const tg = window.Telegram.WebApp;
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) { try { tg.setHeaderColor('#0c1512'); } catch (e) {} }
      if (tg.setBackgroundColor) { try { tg.setBackgroundColor('#0c1512'); } catch (e) {} }
      if (tg.disableVerticalSwipes) { try { tg.disableVerticalSwipes(); } catch (e) {} }
    }
    function sendToBot() {
      if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.sendData) {
        window.Telegram.WebApp.sendData('enter_shanqla');
      }
    }
    const tabs = document.querySelectorAll('.tab');
    const views = { home: document.getElementById('view-home'), users: document.getElementById('view-users') };
    function showView(name) {
      Object.entries(views).forEach(([key, el]) => el.classList.toggle('active', key === name));
      tabs.forEach(t => t.classList.toggle('active', t.dataset.view === name));
      if (name === 'users') loadUsers();
    }
    tabs.forEach(t => t.addEventListener('click', () => showView(t.dataset.view)));
    async function refreshTotal() {
      try {
        const res = await fetch('/api/total');
        const data = await res.json();
        const el = document.getElementById('count');
        if (el.textContent != data.total) {
          el.textContent = data.total;
          el.style.transform = 'scale(1.12)';
          setTimeout(() => { el.style.transform = 'scale(1)'; }, 200);
        }
      } catch (e) { console.error(e); }
    }
    async function loadUsers() {
      try {
        const res = await fetch('/api/users');
        const data = await res.json();
        document.getElementById('u-count').textContent = data.users_count;
        document.getElementById('u-total').textContent = data.total;
        const list = document.getElementById('users-list');
        if (!data.users || data.users.length === 0) {
          list.innerHTML = '<div class="empty">ما في حدا سجّل رقمه لسا 🙂</div>';
          return;
        }
        list.innerHTML = data.users.map((u, i) => `
          <div class="row">
            <span class="rank">${i + 1}</span>
            <span class="name">${u.masked_name}</span>
            <span class="amount">${u.count}</span>
          </div>
        `).join('');
      } catch (e) { console.error(e); }
    }
    refreshTotal();
    setInterval(refreshTotal, 5000);
    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') === 'users') showView('users');
  </script>
</body>
</html>
"""

@app.route("/")
def home():
    _, total = get_algeria_stats()
    return render_template_string(MINI_APP_PAGE, total=total)

@app.route("/health")
def health():
    ok, msg = check_db_connection()
    return jsonify({
        "database_url_set": bool(build_database_url()),
        "ok": ok,
        "message": msg
    })

@app.route("/api/total")
def api_total():
    _, total = get_algeria_stats()
    return jsonify({"total": total})

@app.route("/api/users")
def api_users():
    users_count, total = get_algeria_stats()
    ranked = get_all_users_ranked()
    result = [
        {"masked_name": mask_name(u["username"], u["first_name"]), "count": u["count"]}
        for u in ranked
    ]
    return jsonify({"users_count": users_count, "total": total, "users": result})

# ───────────────────────── الويب هوك ─────────────────────────
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "BOT_TOKEN missing"}), 500

    update = request.get_json(force=True, silent=True) or {}
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    site_url = f"https://{domain}" if domain else ""

    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").strip()
            sender = msg.get("from", {})
            user_id = sender.get("id")
            username = sender.get("username")
            first_name = sender.get("first_name")

            # Web App sendData
            web_app_data = msg.get("web_app_data")
            if web_app_data and web_app_data.get("data") == "enter_shanqla":
                set_pending_action(user_id, "awaiting_number")
                send_message(chat_id, "طيب، ابعتلي رقمك (عدد) بس، كم عندك شنقلة فدار؟ 😄")
                return jsonify({"ok": True})

            # انتظار رقم
            pending = get_pending_action(user_id) if user_id else None
            if pending == "awaiting_number" and text and not text.startswith("/"):
                cleaned = text.replace(",", "").replace("٬", "")
                arabic_digits = "٠١٢٣٤٥٦٧٨٩"
                cleaned = "".join(
                    str(arabic_digits.index(ch)) if ch in arabic_digits else ch
                    for ch in cleaned
                )
                if cleaned.isdigit():
                    count = int(cleaned)
                    if count < 0 or count > 1_000_000:
                        send_message(chat_id, "رقم غير منطقي 😅 اكتب رقم بين 0 و1,000,000.")
                    else:
                        saved, error_text = save_shanqla_count(user_id, count, username, first_name)
                        clear_pending_action(user_id)
                        if saved:
                            _, total = get_algeria_stats()
                            send_message(
                                chat_id,
                                f"تمام ✅ سجّلنا إنه عندك {count} شنقلة فدار.\n"
                                f"🇩🇿 مجموع الشعب الجزائري الآن: {total}",
                            )
                        else:
                            send_message(
                                chat_id,
                                "صار خطأ بالحفظ 🙏\nتفاصيل الخطأ:\n"
                                f"`{error_text}`\n\nابعت هاد النص لمطوّر البوت.",
                            )
                else:
                    send_message(chat_id, "لازم تكتب رقم فقط (مثلاً: 42) 🙏")
                return jsonify({"ok": True})

            if text.startswith("/start"):
                send_message(
                    chat_id,
                    "أهلاً فيك 👋\nهاد البوت بيحسب كم عند كل واحد من \"شنقلة فدار\"، وأنت بتكتب رقمك بنفسك.\n"
                    "اضغط زر تحت:",
                    main_keyboard(site_url),
                )
            elif text in ("/شنقلتي", "/my"):
                existing = get_user_count(user_id)
                set_pending_action(user_id, "awaiting_number")
                if existing is not None:
                    send_message(chat_id, f"رقمك المسجل حالياً: {existing} شنقلة فدار.\nابعتلي رقم جديد لو بدك تعدّله.")
                else:
                    send_message(chat_id, "لسا ما سجّلت رقمك. اكتبلي كم عندك شنقلة فدار؟")
            elif text in ("/الجزائر", "/algeria"):
                users_count, total = get_algeria_stats()
                send_message(
                    chat_id,
                    f"🇩🇿 الشعب الجزائري عنده {total} شنقلة فدار!\n(حسب {users_count} شخص سجّلوا رقمهم لحد الآن)",
                )
            elif text in ("/الموقع", "/site") and site_url:
                send_message(chat_id, f"🌐 شوف الموقع من هون:\n{site_url}")
            elif text in ("/تشخيص", "/debug", "/diag"):
                ok, msg = check_db_connection()
                icon = "✅" if ok else "❌"
                send_message(chat_id, f"{icon} فحص قاعدة البيانات:\n{msg}")
            else:
                send_message(
                    chat_id,
                    "استخدم الأزرار تحت، أو اكتب /شنقلتي أو /الجزائر 👇",
                    main_keyboard(site_url),
                )

        elif "callback_query" in update:
            cq = update["callback_query"]
            sender = cq.get("from", {})
            user_id = sender.get("id")
            chat_id = cq["message"]["chat"]["id"]
            callback_id = cq["id"]
            data_key = cq.get("data", "")

            if data_key == "enter_shanqla":
                answer_callback(callback_id)
                set_pending_action(user_id, "awaiting_number")
                send_message(chat_id, "طيب، ابعتلي رقمك (عدد) بس، كم عندك شنقلة فدار؟ 😄")
            elif data_key == "algeria_shanqla":
                answer_callback(callback_id)
                users_count, total = get_algeria_stats()
                send_message(
                    chat_id,
                    f"🇩🇿 الشعب الجزائري عنده {total} شنقلة فدار!\n(حسب {users_count} شخص سجّلوا رقمهم لحد الآن)",
                )
    except Exception as e:
        print(f"[Webhook] خطأ غير متوقع: {e}")

    return jsonify({"ok": True})

# ───────────────────────── تفعيل الويب هوك ─────────────────────────
def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not domain or not BOT_TOKEN:
        print("[Webhook] BOT_TOKEN أو RAILWAY_PUBLIC_DOMAIN غير موجودين.")
        return
    url = f"https://{domain}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": url, "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=10,
        )
        print(f"[Webhook] set to: {url} -> {r.json()}")
    except Exception as e:
        print(f"[Webhook] فشل: {e}")

# تشغيل التهيئة
init_db()
set_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

```
