# ═══════════════════════════════════════════════════════════
#  بوت مسابقة الكود — من يكتب أفضل كود؟
#  المتطلبات: python-telegram-bot==21.6 · aiohttp==3.10.5 · psycopg2-binary==2.9.9
#  المتغيرات: BOT_TOKEN · ADMIN_CHAT_ID · WEBAPP_URL (أو RAILWAY_PUBLIC_DOMAIN)
#             DATABASE_URL (اختياري، وإلا SQLite) · PORT · TZ_OFFSET
# ═══════════════════════════════════════════════════════════
import os, sys, json, hmac, hashlib, time, asyncio, sqlite3, logging, threading, html, datetime
from contextlib import contextmanager
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
                      MenuButtonWebApp, BotCommand, BotCommandScopeChat)
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults
from telegram.error import RetryAfter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("code-contest")

# ═══════════ 1) الإعدادات ═══════════
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "contest.db")
TITLE = os.environ.get("APP_TITLE", "CodeArena")
try:
    PORT = int(os.environ.get("PORT", "8080") or 8080)
except ValueError:
    PORT = 8080
try:
    TZ_OFFSET = float(os.environ.get("TZ_OFFSET", "1"))
except ValueError:
    TZ_OFFSET = 1.0
_raw = (os.environ.get("WEBAPP_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")).strip().rstrip("/")
if _raw.startswith("http://"):
    _raw = "https://" + _raw[7:]
elif _raw and not _raw.startswith("https://"):
    _raw = "https://" + _raw
WEBAPP_URL = _raw
APP_URL = f"{WEBAPP_URL}/app?v={int(time.time())}" if WEBAPP_URL else ""

MAX_CODE = 20000                 # أقصى طول للكود المرسل (حرف)
ADMIN_W = 3                      # وزن علامة المشرف (0-10) مقابل كل تصويت = 1
POINTS = [3, 2, 1]               # نقاط المراكز الثلاثة الأولى
MAIN_APP, WEB_RUNNER, BG = None, None, []

# ═══════════ 2) قاعدة البيانات (PostgreSQL وإلا SQLite) ═══════════
USE_PG, pool, sq = False, None, None
sq_lock = threading.RLock()


class Cur:
    def __init__(self, cur, pg):
        self.cur, self.pg = cur, pg

    def execute(self, sql, p=()):
        self.cur.execute(sql.replace("?", "%s") if self.pg else sql, p)

    def fetchone(self):
        r = self.cur.fetchone()
        return dict(r) if r is not None else None

    def fetchall(self):
        return [dict(r) for r in self.cur.fetchall()]

    @property
    def rowcount(self):
        return self.cur.rowcount

    def insert_id(self, sql, p=()):
        if self.pg:
            self.execute(sql + " RETURNING id", p)
            return self.fetchone()["id"]
        self.execute(sql, p)
        return self.cur.lastrowid


@contextmanager
def cursor():
    if USE_PG:
        from psycopg2.extras import RealDictCursor
        conn = pool.getconn()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as c:
                    yield Cur(c, True)
        finally:
            pool.putconn(conn)
    else:
        with sq_lock:
            with sq:
                c = sq.cursor()
                try:
                    yield Cur(c, False)
                finally:
                    c.close()


def init_db():
    global USE_PG, pool, sq
    u = os.environ.get("DATABASE_URL")
    if u:
        try:
            from psycopg2.pool import ThreadedConnectionPool
            pool = ThreadedConnectionPool(1, 20, u, connect_timeout=8)
            USE_PG = True
            log.info("Connected to PostgreSQL")
        except Exception as e:
            log.warning("PostgreSQL failed: %s", str(e)[:150])
    if not USE_PG:
        sq = sqlite3.connect(DB_PATH, check_same_thread=False)
        sq.row_factory = sqlite3.Row
        log.warning("Using SQLite: %s", os.path.abspath(DB_PATH))
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS c_users(
            id BIGINT PRIMARY KEY, name TEXT, username TEXT, banned INT NOT NULL DEFAULT 0, created_at BIGINT)""")
        cur.execute(f"CREATE TABLE IF NOT EXISTS c_cats(id {pk}, name TEXT NOT NULL)")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS c_rounds(
            id {pk}, cat_id INT, title TEXT, body TEXT, created_at BIGINT, ends_at BIGINT,
            status TEXT NOT NULL DEFAULT 'open')""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS c_subs(
            id {pk}, round_id INT NOT NULL, user_id BIGINT NOT NULL, code TEXT NOT NULL,
            created_at BIGINT, updated_at BIGINT, admin_score INT NOT NULL DEFAULT 0,
            UNIQUE(round_id, user_id))""")
        cur.execute("CREATE TABLE IF NOT EXISTS c_votes(sub_id INT NOT NULL, user_id BIGINT NOT NULL, "
                    "PRIMARY KEY(sub_id, user_id))")
        cur.execute("""CREATE TABLE IF NOT EXISTS c_results(
            round_id INT NOT NULL, pos INT NOT NULL, user_id BIGINT NOT NULL, points INT NOT NULL,
            PRIMARY KEY(round_id, pos))""")
    log.info("DB ready (%s)", "PostgreSQL" if USE_PG else "SQLite")


def _local(ts):
    return datetime.datetime.fromtimestamp(ts + TZ_OFFSET * 3600, datetime.timezone.utc)


# ═══════════ 3) منطق المسابقة ═══════════
class Err(Exception):
    pass


def ensure_user(cur, tg):
    cur.execute("""INSERT INTO c_users(id,name,username,created_at) VALUES(?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET name=excluded.name, username=excluded.username""",
                (tg["id"], tg["name"], tg["username"], int(time.time())))


def close_round(rid):
    """يغلق باب الإرسال ويفتح التصويت. يرجع True إذا كانت أول مرة."""
    with cursor() as cur:
        cur.execute("UPDATE c_rounds SET status='closed' WHERE id=? AND status='open'", (rid,))
        return cur.rowcount == 1


def ranking(cur, rid):
    """الترتيب = علامة المشرف × ADMIN_W + عدد الأصوات (الأسبق إرسالاً يفوز عند التعادل)."""
    cur.execute("""SELECT s.id, s.user_id, u.name, s.admin_score, s.created_at,
        (SELECT COUNT(*) FROM c_votes v WHERE v.sub_id=s.id) AS votes
        FROM c_subs s JOIN c_users u ON u.id=s.user_id WHERE s.round_id=?""", (rid,))
    rows = cur.fetchall()
    for r in rows:
        r["total"] = r["admin_score"] * ADMIN_W + r["votes"]
    rows.sort(key=lambda r: (-r["total"], r["created_at"] or 0))
    return rows


def finish_round(rid):
    """ينهي المسابقة، يمنح نقاط المراكز الأولى ويرجع (round, top)."""
    with cursor() as cur:
        cur.execute("SELECT r.*, c.name AS cat FROM c_rounds r LEFT JOIN c_cats c ON c.id=r.cat_id WHERE r.id=?", (rid,))
        r = cur.fetchone()
        if not r:
            raise Err("المسابقة غير موجودة")
        if r["status"] == "finished":
            raise Err("هذه المسابقة منتهية مسبقاً")
        top = ranking(cur, rid)[:len(POINTS)]
        cur.execute("DELETE FROM c_results WHERE round_id=?", (rid,))
        for i, t in enumerate(top):
            cur.execute("INSERT INTO c_results(round_id,pos,user_id,points) VALUES(?,?,?,?)",
                        (rid, i + 1, t["user_id"], POINTS[i]))
        cur.execute("UPDATE c_rounds SET status='finished' WHERE id=?", (rid,))
    return r, top


# ═══════════ 4) الـ API (aiohttp) ═══════════
def verify_init_data(init_data, max_age=86400):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        got = d.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not got or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(d.get("auth_date", "0")) > max_age:
            return None
        u = json.loads(d.get("user", "{}"))
        if not u.get("id"):
            return None
        return dict(id=int(u["id"]), username=u.get("username") or "",
                    name=" ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "User")
    except Exception:
        return None


def act_me(tg, b):
    with cursor() as cur:
        ensure_user(cur, tg)
        cur.execute("SELECT * FROM c_cats ORDER BY id")
        cats = cur.fetchall()
        cur.execute("""SELECT r.id, r.cat_id, r.title, r.ends_at, r.status, c.name AS cat,
            (SELECT COUNT(*) FROM c_subs s WHERE s.round_id=r.id) AS n,
            (SELECT COUNT(*) FROM c_subs s WHERE s.round_id=r.id AND s.user_id=?) AS mine
            FROM c_rounds r LEFT JOIN c_cats c ON c.id=r.cat_id ORDER BY r.id DESC LIMIT 40""", (tg["id"],))
        rounds = cur.fetchall()
    return dict(admin=tg["id"] == ADMIN_ID, cats=cats, rounds=rounds, now=int(time.time()), name=tg["name"])


def act_round(tg, b):
    rid = int(b.get("round_id") or 0)
    now = time.time()
    with cursor() as cur:
        cur.execute("SELECT r.*, c.name AS cat FROM c_rounds r LEFT JOIN c_cats c ON c.id=r.cat_id WHERE r.id=?", (rid,))
        r = cur.fetchone()
        if not r:
            raise Err("المسابقة غير موجودة")
        if r["status"] == "open" and now >= r["ends_at"]:
            cur.execute("UPDATE c_rounds SET status='closed' WHERE id=? AND status='open'", (rid,))
            r["status"] = "closed"
        admin = tg["id"] == ADMIN_ID
        cur.execute("SELECT code FROM c_subs WHERE round_id=? AND user_id=?", (rid, tg["id"]))
        mine = cur.fetchone()
        rows = ranking(cur, rid)
        voted = set()
        if rows:
            cur.execute("SELECT sub_id FROM c_votes WHERE user_id=? AND sub_id IN (SELECT id FROM c_subs WHERE round_id=?)",
                        (tg["id"], rid))
            voted = {x["sub_id"] for x in cur.fetchall()}
        cur.execute("SELECT pos,user_id,points FROM c_results WHERE round_id=? ORDER BY pos", (rid,))
        res = cur.fetchall()
    reveal = r["status"] != "open" or admin          # الكود مخفي أثناء المسابقة كي لا ينسخ أحدٌ من أحد
    subs = []
    if reveal:
        cur2 = None
        with cursor() as cur:
            ids = [x["id"] for x in rows]
            codes = {}
            if ids:
                cur.execute("SELECT id, code FROM c_subs WHERE round_id=?", (rid,))
                codes = {x["id"]: x["code"] for x in cur.fetchall()}
        for i, x in enumerate(rows, 1):
            subs.append(dict(id=x["id"], rank=i, name=x["name"], code=codes.get(x["id"], ""), votes=x["votes"],
                             score=x["admin_score"], total=x["total"], me=x["user_id"] == tg["id"],
                             voted=x["id"] in voted))
    return dict(round=dict(id=r["id"], title=r["title"], body=r["body"], cat=r["cat"] or "—", ends_at=r["ends_at"],
                           status=r["status"]), mine=(mine or {}).get("code"), count=len(rows), subs=subs,
                admin=admin, now=int(now), weight=ADMIN_W, winners=[x["user_id"] for x in res])


def act_submit(tg, b):
    rid = int(b.get("round_id") or 0)
    code = str(b.get("code") or "").replace("\r\n", "\n").strip("\n")
    if not code.strip():
        raise Err("اكتب الكود أولاً")
    if len(code) > MAX_CODE:
        raise Err(f"الكود طويل جداً (الحد {MAX_CODE} حرف)")
    now = int(time.time())
    with cursor() as cur:
        ensure_user(cur, tg)
        cur.execute("SELECT status, ends_at FROM c_rounds WHERE id=?", (rid,))
        r = cur.fetchone()
        if not r:
            raise Err("المسابقة غير موجودة")
        if r["status"] != "open" or now >= r["ends_at"]:
            raise Err("انتهى وقت الإرسال")
        cur.execute("""INSERT INTO c_subs(round_id,user_id,code,created_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT (round_id,user_id) DO UPDATE SET code=excluded.code, updated_at=excluded.updated_at""",
                    (rid, tg["id"], code, now, now))
    return {"msg": "✅ تم إرسال الكود (يمكنك تعديله قبل انتهاء الوقت)"}


def act_vote(tg, b):
    sid = int(b.get("sub_id") or 0)
    with cursor() as cur:
        cur.execute("""SELECT s.user_id, r.status FROM c_subs s JOIN c_rounds r ON r.id=s.round_id WHERE s.id=?""", (sid,))
        s = cur.fetchone()
        if not s:
            raise Err("غير موجود")
        if s["status"] != "closed":
            raise Err("التصويت غير متاح الآن")
        if s["user_id"] == tg["id"]:
            raise Err("لا يمكنك التصويت لنفسك")
        cur.execute("DELETE FROM c_votes WHERE sub_id=? AND user_id=?", (sid, tg["id"]))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO c_votes(sub_id,user_id) VALUES(?,?)", (sid, tg["id"]))
    return {}


def act_score(tg, b):
    if tg["id"] != ADMIN_ID:
        raise Err("للمشرف فقط")
    sid = int(b.get("sub_id") or 0)
    sc = max(0, min(10, int(b.get("score") or 0)))
    with cursor() as cur:
        cur.execute("UPDATE c_subs SET admin_score=? WHERE id=?", (sc, sid))
    return {"msg": f"تم تسجيل العلامة {sc}/10"}


def act_board(tg, b):
    cid = int(b.get("cat_id") or 0)
    with cursor() as cur:
        if cid:
            cur.execute("""SELECT u.id, u.name, SUM(x.points) AS p, COUNT(*) AS w FROM c_results x
                JOIN c_users u ON u.id=x.user_id JOIN c_rounds r ON r.id=x.round_id WHERE r.cat_id=?
                GROUP BY u.id, u.name ORDER BY p DESC, w DESC LIMIT 50""", (cid,))
        else:
            cur.execute("""SELECT u.id, u.name, SUM(x.points) AS p, COUNT(*) AS w FROM c_results x
                JOIN c_users u ON u.id=x.user_id GROUP BY u.id, u.name ORDER BY p DESC, w DESC LIMIT 50""")
        rows = cur.fetchall()
    return {"board": [dict(name=r["name"], points=int(r["p"]), wins=int(r["w"]), me=r["id"] == tg["id"]) for r in rows]}


SYNC = {"me": act_me, "round": act_round, "submit": act_submit, "vote": act_vote,
        "score": act_score, "board": act_board}


@web.middleware
async def err_mw(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Err as e:
        return web.json_response({"ok": False, "error": str(e)})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("WEB ERROR on %s", request.path)
        return web.json_response({"ok": False, "error": "تعذّر الاتصال، حاول مجدداً"})


async def web_api(request):
    act = request.match_info["act"]
    tg = verify_init_data(request.headers.get("X-Init-Data", ""))
    if not tg:
        raise web.HTTPUnauthorized()
    if act not in SYNC:
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await asyncio.to_thread(SYNC[act], tg, body)
    return web.json_response({"ok": True, **res})


async def web_page(request):
    return web.Response(text=WEB_HTML.replace("__TITLE__", html.escape(TITLE)), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def web_health(request):
    return web.Response(text="ok")


async def start_web():
    global WEB_RUNNER
    if not WEBAPP_URL:
        log.error("WEBAPP_URL / RAILWAY_PUBLIC_DOMAIN غير مضبوط: لن تعمل واجهة الويب")
        return
    w = web.Application(middlewares=[err_mw])
    w.add_routes([web.get("/", web_health), web.get("/health", web_health), web.get("/app", web_page),
                  web.post("/api/{act}", web_api)])
    WEB_RUNNER = web.AppRunner(w)
    await WEB_RUNNER.setup()
    await web.TCPSite(WEB_RUNNER, "0.0.0.0", PORT).start()
    log.info("Mini App on 0.0.0.0:%s (%s/app)", PORT, WEBAPP_URL)


# ═══════════ 5) أوامر البوت ═══════════
def is_admin(update):
    return bool(ADMIN_ID) and update.effective_user and update.effective_user.id == ADMIN_ID


def open_kb(rid=None):
    if not WEBAPP_URL:
        return None
    url = APP_URL + (f"&r={rid}" if rid else "")
    return InlineKeyboardMarkup([[InlineKeyboardButton("💻 افتح المسابقة", web_app=WebAppInfo(url=url))]])


async def send_many(bot, ids, txt, admin_chat=None, kb=None):
    sent = failed = 0
    for uid in ids:
        for _ in range(2):
            try:
                await bot.send_message(uid, txt, reply_markup=kb)
                sent += 1
                break
            except RetryAfter as e:
                ra = e.retry_after
                await asyncio.sleep(float(ra.total_seconds() if hasattr(ra, "total_seconds") else ra) + 1)
            except Exception:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)
    if admin_chat:
        try:
            await bot.send_message(admin_chat, f"✅ انتهى الإرسال · وصلت {sent} · فشلت {failed}")
        except Exception:
            pass


def all_user_ids():
    with cursor() as cur:
        cur.execute("SELECT id FROM c_users WHERE banned=0 ORDER BY id")
        return [r["id"] for r in cur.fetchall()]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    def reg():
        with cursor() as cur:
            ensure_user(cur, dict(id=u.id, name=u.full_name, username=u.username or ""))
    await asyncio.to_thread(reg)
    text = ("👋 أهلاً بك في <b>" + html.escape(TITLE) + "</b>!\n\n💻 مسابقات برمجة: المشرف ينشر مطلوباً، "
            "الكل يرسل كوده، والتصويت يحدد الأفضل.\nافتح التطبيق لتشارك 👇")
    if u.id == ADMIN_ID:
        text += ("\n\n<b>أوامر المشرف:</b>\n/addcat اسم الفئة\n/cats · /delcat ID\n"
                 "/newround ID_الفئة الدقائق النص\n/rounds · /close ID · /finish ID")
    await update.message.reply_text(text, reply_markup=open_kb())


async def cmd_addcat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    name = " ".join(context.args).strip()[:40]
    if not name:
        await update.message.reply_text("الاستعمال: <code>/addcat Python</code>")
        return

    def add():
        with cursor() as cur:
            return cur.insert_id("INSERT INTO c_cats(name) VALUES(?)", (name,))
    cid = await asyncio.to_thread(add)
    await update.message.reply_text(f"✅ تمت إضافة الفئة <b>{html.escape(name)}</b> (رقم <code>{cid}</code>)")


async def cmd_cats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    def rows():
        with cursor() as cur:
            cur.execute("SELECT * FROM c_cats ORDER BY id")
            return cur.fetchall()
    rs = await asyncio.to_thread(rows)
    await update.message.reply_text("📂 <b>الفئات</b>\n\n" + ("\n".join(
        f"<code>{r['id']}</code> · {html.escape(r['name'])}" for r in rs) or "لا توجد فئات. أضف: /addcat Python"))


async def cmd_delcat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not context.args or not context.args[0].isdigit():
        return

    def rm():
        with cursor() as cur:
            cur.execute("DELETE FROM c_cats WHERE id=?", (int(context.args[0]),))
            return cur.rowcount
    await update.message.reply_text("🗑 تم الحذف" if await asyncio.to_thread(rm) else "غير موجودة")


async def cmd_newround(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newround <ID_الفئة> <الدقائق> <نص المطلوب>"""
    if not is_admin(update):
        return
    parts = (update.message.text or "").split(None, 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        await update.message.reply_text(
            "الاستعمال:\n<code>/newround 1 60 اكتب دالة تعكس نصاً بدون استعمال reverse</code>\n"
            "← الفئة رقم 1، المدة 60 دقيقة. (أرقام الفئات: /cats)\nيمكن كتابة المطلوب على عدة أسطر.")
        return
    cid, mins, body = int(parts[1]), max(1, int(parts[2])), parts[3].strip()

    def create():
        now = int(time.time())
        with cursor() as cur:
            cur.execute("SELECT name FROM c_cats WHERE id=?", (cid,))
            c = cur.fetchone()
            if not c:
                return None
            rid = cur.insert_id("INSERT INTO c_rounds(cat_id,title,body,created_at,ends_at) VALUES(?,?,?,?,?)",
                                (cid, body.split("\n")[0][:70], body, now, now + mins * 60))
            return rid, c["name"]
    res = await asyncio.to_thread(create)
    if not res:
        await update.message.reply_text("❌ فئة غير موجودة. أضفها بـ /addcat")
        return
    rid, cname = res
    ids = await asyncio.to_thread(all_user_ids)
    msg = (f"🏁 <b>مسابقة جديدة #{rid}</b> · {html.escape(cname)}\n\n{html.escape(body)}\n\n"
           f"⏳ المدة: {mins} دقيقة (حتى {_local(time.time() + mins * 60).strftime('%H:%M')})")
    await update.message.reply_text(f"✅ تم إنشاء المسابقة #{rid} وبدأ إرسالها لـ {len(ids)} مستخدم.")
    BG.append(asyncio.create_task(send_many(context.bot, ids, msg, update.effective_chat.id, open_kb(rid))))


async def cmd_rounds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    def rows():
        with cursor() as cur:
            cur.execute("""SELECT r.id, r.title, r.status, r.ends_at, c.name AS cat,
                (SELECT COUNT(*) FROM c_subs s WHERE s.round_id=r.id) AS n
                FROM c_rounds r LEFT JOIN c_cats c ON c.id=r.cat_id ORDER BY r.id DESC LIMIT 15""")
            return cur.fetchall()
    rs = await asyncio.to_thread(rows)
    st = {"open": "🟢 مفتوحة", "closed": "🗳 تصويت", "finished": "🏁 منتهية"}
    await update.message.reply_text("📋 <b>المسابقات</b>\n\n" + ("\n\n".join(
        f"<b>#{r['id']}</b> · {html.escape(r['cat'] or '—')} · {st.get(r['status'], r['status'])} · {r['n']} مشارك\n"
        f"   {html.escape(r['title'] or '')}\n   /close {r['id']} · /finish {r['id']}" for r in rs) or "لا توجد مسابقات."))


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/close ID ← يغلق الإرسال ويفتح التصويت"""
    if not is_admin(update) or not context.args or not context.args[0].isdigit():
        return
    rid = int(context.args[0])
    if not await asyncio.to_thread(close_round, rid):
        await update.message.reply_text("المسابقة غير مفتوحة أو غير موجودة.")
        return
    await announce_voting(context.bot, rid)
    await update.message.reply_text(f"🗳 أُغلق الإرسال وفُتح التصويت للمسابقة #{rid}.\nعند الانتهاء: /finish {rid}")


async def announce_voting(bot, rid):
    ids = await asyncio.to_thread(all_user_ids)
    BG.append(asyncio.create_task(send_many(
        bot, ids, f"🗳 انتهى وقت المسابقة #{rid}! افتح التطبيق، شاهد الأكواد وصوّت لأفضلها.", None, open_kb(rid))))


async def cmd_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/finish ID ← يعلن الفائزين ويمنح النقاط"""
    if not is_admin(update) or not context.args or not context.args[0].isdigit():
        return
    rid = int(context.args[0])
    try:
        r, top = await asyncio.to_thread(finish_round, rid)
    except Err as e:
        await update.message.reply_text("❌ " + str(e))
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i]} {html.escape(t['name'] or '—')} · {t['total']} نقطة" for i, t in enumerate(top)]
    msg = (f"🏆 <b>نتيجة المسابقة #{rid}</b> · {html.escape(r['cat'] or '')}\n\n"
           + ("\n".join(lines) if lines else "لم يشارك أحد.") + "\n\nمبروك للفائزين! 🎉")
    ids = await asyncio.to_thread(all_user_ids)
    await update.message.reply_text(msg + f"\n\n(يُرسل الآن إلى {len(ids)} مستخدم)")
    BG.append(asyncio.create_task(send_many(context.bot, ids, msg, None, open_kb())))


async def close_loop():
    """كل 20 ثانية: يغلق المسابقات التي انتهى وقتها ويفتح التصويت."""
    while True:
        await asyncio.sleep(20)
        try:
            BG[:] = [t for t in BG if not t.done()]

            def due():
                with cursor() as cur:
                    cur.execute("SELECT id FROM c_rounds WHERE status='open' AND ends_at<=?", (int(time.time()),))
                    return [r["id"] for r in cur.fetchall()]
            for rid in await asyncio.to_thread(due):
                if await asyncio.to_thread(close_round, rid):
                    await announce_voting(MAIN_APP.bot, rid)
                    if ADMIN_ID:
                        await MAIN_APP.bot.send_message(
                            ADMIN_ID, f"⏰ انتهى وقت المسابقة #{rid}. افتح التطبيق لتعطي العلامات، ثم /finish {rid}")
        except Exception:
            log.exception("close loop")


async def post_init(app: Application):
    global MAIN_APP
    MAIN_APP = app
    await start_web()
    try:
        user_cmds = [BotCommand("start", "🏠 الصفحة الرئيسية")]
        await app.bot.set_my_commands(user_cmds)
        if ADMIN_ID:
            await app.bot.set_my_commands(user_cmds + [
                BotCommand("addcat", "➕ إضافة فئة"), BotCommand("cats", "📂 الفئات"),
                BotCommand("delcat", "🗑 حذف فئة"), BotCommand("newround", "🏁 مسابقة جديدة"),
                BotCommand("rounds", "📋 المسابقات"), BotCommand("close", "🗳 فتح التصويت"),
                BotCommand("finish", "🏆 إعلان الفائزين")], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        if WEBAPP_URL:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="💻 مسابقة", web_app=WebAppInfo(url=APP_URL)))
    except Exception as e:
        log.warning("setup failed: %s", e)
    BG.append(asyncio.create_task(close_loop()))
    if ADMIN_ID:
        try:
            await app.bot.send_message(ADMIN_ID, f"✅ البوت يعمل.\n🌐 {WEBAPP_URL + '/app' if WEBAPP_URL else 'WEBAPP_URL غير مضبوط'}")
        except Exception:
            pass


async def post_shutdown(app: Application):
    for t in BG:
        t.cancel()
    if WEB_RUNNER:
        await WEB_RUNNER.cleanup()


# ═══════════ 6) الواجهة (Mini App) ═══════════
WEB_HTML = r"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>__TITLE__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#1a1c1e;--card:#282b2e;--line:#33363a;--ac:#ffa45b;--tx:#eceef0;--mut:#8b9098;--red:#d94848;--grn:#3ecf8e;--blue:#38b6ff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans Arabic",Tahoma,sans-serif;padding-bottom:calc(80px + env(safe-area-inset-bottom))}
.w{max-width:600px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px}
.h{color:var(--mut);font-size:13px;margin:0}
h2{margin:0 0 8px;font-size:19px}
button{font:inherit;font-weight:700;border:1px solid var(--line);border-radius:12px;padding:12px;width:100%;margin-top:10px;cursor:pointer;color:var(--tx);background:var(--bg)}
button:active{transform:scale(.98)}button:disabled{opacity:.45}
button.pri{background:var(--ac);color:#1e1307;border:0}
button.sm{width:auto;margin:0;padding:7px 14px;font-size:13px}
.chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;margin-bottom:10px}
.chip{flex:none;padding:7px 14px;border-radius:20px;border:1px solid var(--line);background:var(--card);color:var(--mut);font-size:13px;font-weight:700;cursor:pointer}
.chip.on{background:var(--ac);color:#1e1307;border-color:var(--ac)}
.rw{display:flex;justify-content:space-between;align-items:center;gap:10px;cursor:pointer}
.rw b{display:block;font-size:15px}
.st{font-size:11px;font-weight:700;border:1px solid;border-radius:8px;padding:1px 9px;white-space:nowrap}
.st.open{color:var(--grn);border-color:var(--grn)}.st.closed{color:var(--blue);border-color:var(--blue)}.st.finished{color:var(--mut);border-color:var(--line)}
.body{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
textarea{width:100%;min-height:240px;background:#111315;color:#e6e8ea;border:1px solid var(--line);border-radius:12px;padding:12px;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;direction:ltr;text-align:left;resize:vertical;tab-size:4}
pre.code{margin:8px 0 0;background:#111315;border:1px solid var(--line);border-radius:12px;padding:12px;overflow-x:auto;font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;direction:ltr;text-align:left;max-height:340px}
.cd{font:700 22px ui-monospace,Menlo,monospace;direction:ltr;text-align:center;color:var(--ac)}
.sub .top{display:flex;align-items:center;gap:10px}
.rk{width:30px;height:30px;border-radius:9px;background:var(--bg);border:1px solid var(--line);display:grid;place-items:center;font-weight:800;font-size:13px;flex:none}
.rk.g{border-color:var(--ac);color:var(--ac)}
.sub.me{border-color:var(--ac)}
.nm{flex:1;min-width:0;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vt{display:flex;gap:8px;align-items:center;margin-top:8px}
.vt button{margin:0}
select{background:var(--bg);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:8px}
.nav{position:fixed;inset:auto 0 0 0;display:flex;background:#282b2e;border-top:1px solid var(--line);padding:9px 6px calc(8px + env(safe-area-inset-bottom));z-index:5}
.nav div{flex:1;text-align:center;color:var(--mut);font-weight:700;font-size:12px;cursor:pointer;padding:4px}
.nav div.on{color:var(--ac)}.nav span{display:block;font-size:20px}
.lr{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.lr:last-child{border:0}
.lr.me{color:var(--ac)}
#toast{position:fixed;left:50%;bottom:calc(92px + env(safe-area-inset-bottom));transform:translateX(-50%);background:#000c;color:#fff;padding:9px 16px;border-radius:12px;font-size:14px;opacity:0;pointer-events:none;transition:opacity .25s;z-index:40}
#toast.on{opacity:1}
.back{background:none;border:0;color:var(--ac);width:auto;padding:0;margin:0 0 10px;font-size:14px}
</style></head><body>
<div class="w" id="app"><p class="h" style="text-align:center;margin-top:40vh">…</p></div>
<div class="nav" id="nav"></div><div id="toast"></div>
<script>
const tg=window.Telegram.WebApp;try{tg.ready();tg.expand()}catch(e){}
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let ME=null,tab='rounds',cat=0,RID=0,R=null,BOARD=null,bcat=0,skew=0,draft='';
const ST={open:'🟢 مفتوحة',closed:'🗳 تصويت',finished:'🏁 منتهية'};
function toast(m){if(!m)return;const e=$('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),2400)}
async function api(p,b){const r=await fetch('/api/'+p,{method:'POST',headers:{'X-Init-Data':tg.initData,'Content-Type':'application/json'},body:JSON.stringify(b||{})});
  if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();if(!j.ok)throw new Error(j.error||'خطأ');return j}
const now=()=>Date.now()/1000+skew;
async function loadMe(){ME=await api('me');skew=ME.now-Date.now()/1000}
async function loadRound(){R=await api('round',{round_id:RID});skew=R.now-Date.now()/1000}
async function loadBoard(){BOARD=(await api('board',{cat_id:bcat})).board}
async function refresh(){try{
  if(tab==='rounds'){await loadMe();if(RID)await loadRound()}
  else{if(!ME)await loadMe();await loadBoard()}
  draw()}catch(e){toast(e.message)}}
function go(t){tab=t;RID=0;R=null;refresh()}
function openRound(id){RID=id;R=null;draft='';draw();loadRound().then(draw).catch(e=>{toast(e.message);RID=0;draw()})}
function leftTxt(t){let s=Math.max(0,Math.floor(t-now()));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);s%=60;const p=n=>String(n).padStart(2,'0');return p(h)+':'+p(m)+':'+p(s)}
function chips(sel,fn){return '<div class="chips"><div class="chip '+(sel===0?'on':'')+'" onclick="'+fn+'(0)">الكل</div>'+
  ME.cats.map(c=>'<div class="chip '+(sel===c.id?'on':'')+'" onclick="'+fn+'('+c.id+')">'+esc(c.name)+'</div>').join('')+'</div>'}
function setCat(i){cat=i;draw()}
function setBcat(i){bcat=i;refresh()}
function vList(){
  const L=ME.rounds.filter(r=>!cat||r.cat_id===cat);
  return '<h2>💻 المسابقات</h2>'+(ME.cats.length?chips(cat,'setCat'):'')+
   (L.length?L.map(r=>`<div class="card rw" onclick="openRound(${r.id})"><div style="min-width:0"><b>#${r.id} · ${esc(r.title)}</b>
     <span class="h">${esc(r.cat||'—')} · ${r.n} مشارك${r.mine?' · ✅ شاركت':''}</span></div><span class="st ${r.status}">${ST[r.status]}</span></div>`).join(''):
    '<div class="card"><p class="h">لا توجد مسابقات الآن. انتظر إعلان المشرف.</p></div>')}
function subCard(s){
  const g=s.rank<=3?'g':'';
  return `<div class="card sub ${s.me?'me':''}"><div class="top"><span class="rk ${g}">${s.rank}</span><span class="nm">${esc(s.name)}${s.me?' (أنت)':''}</span>
    <span class="h">${s.total} نقطة</span></div>
    <pre class="code">${esc(s.code)}</pre>
    <div class="vt"><span class="h">👍 ${s.votes} · علامة المشرف ${s.score}/10</span><span style="flex:1"></span>
    ${R.round.status==='closed'&&!s.me?`<button class="sm ${s.voted?'pri':''}" onclick="vote(${s.id})">${s.voted?'✓ صوّتّ':'👍 صوّت'}</button>`:''}</div>
    ${R.admin&&R.round.status!=='finished'?`<div class="vt"><select id="sc${s.id}">${[...Array(11).keys()].map(i=>`<option ${i===s.score?'selected':''}>${i}</option>`).join('')}</select>
      <button class="sm" onclick="setScore(${s.id})">حفظ العلامة</button></div>`:''}</div>`}
function vRound(){
  if(!R)return '<p class="h" style="text-align:center;margin-top:30vh">…</p>';
  const r=R.round,open=r.status==='open'&&now()<r.ends_at;
  let h=`<button class="back" onclick="RID=0;refresh()">← رجوع</button><div class="card"><div class="rw" style="cursor:default"><h2 style="margin:0">#${r.id} · ${esc(r.cat)}</h2><span class="st ${r.status}">${ST[r.status]}</span></div>
    <div class="body">${esc(r.body)}</div>`;
  if(open)h+=`<div class="h" style="text-align:center">الوقت المتبقي</div><div class="cd" id="cd">${leftTxt(r.ends_at)}</div>`;
  h+='</div>';
  if(open){
    h+=`<div class="card"><h2>✍️ كودك</h2><p class="h">يمكنك تعديل الكود وإعادة الإرسال قبل انتهاء الوقت. أكواد الآخرين مخفية حتى ينتهي الوقت.</p>
      <textarea id="code" dir="ltr" spellcheck="false" placeholder="# اكتب الكود هنا" oninput="draft=this.value">${esc(draft||R.mine||'')}</textarea>
      <button class="pri" onclick="submitCode()">${R.mine?'تحديث الكود':'إرسال الكود'}</button></div>
      <p class="h" style="text-align:center">${R.count} مشارك حتى الآن</p>`;
    if(R.admin&&R.subs.length)h+=R.subs.map(subCard).join('');
  }else{
    h+=`<p class="h" style="margin:4px 2px 10px">الترتيب = علامة المشرف × ${R.weight} + عدد الأصوات${r.status==='closed'?' · التصويت مفتوح':''}</p>`+
      (R.subs.length?R.subs.map(subCard).join(''):'<div class="card"><p class="h">لم يشارك أحد.</p></div>')}
  return h}
function vBoard(){
  return '<h2>🏆 الترتيب</h2>'+(ME&&ME.cats.length?chips(bcat,'setBcat'):'')+'<div class="card">'+
   (BOARD&&BOARD.length?BOARD.map((r,i)=>`<div class="lr ${r.me?'me':''}"><span class="rk ${i<3?'g':''}">${i+1}</span><span class="nm">${esc(r.name)}</span>
     <span class="h">🏅 ${r.wins} · <b style="color:var(--tx)">${r.points}</b> نقطة</span></div>`).join(''):'<p class="h">لا توجد نتائج بعد.</p>')+'</div>'+
   '<p class="h" style="text-align:center">🥇 3 نقاط · 🥈 2 · 🥉 1 لكل مسابقة</p>'}
function draw(){
  $('nav').innerHTML=[['rounds','💻','المسابقات'],['board','🏆','الترتيب']].map(t=>`<div class="${t[0]===tab?'on':''}" onclick="go('${t[0]}')"><span>${t[1]}</span>${t[2]}</div>`).join('');
  $('app').innerHTML='<div class="w" style="padding:0">'+(tab==='board'?vBoard():(RID?vRound():(ME?vList():'')))+'</div>'}
async function submitCode(){const c=$('code').value;
  try{const r=await api('submit',{round_id:RID,code:c});toast(r.msg);draft=c;await loadRound();draw()}catch(e){toast(e.message)}}
async function vote(id){try{await api('vote',{sub_id:id});await loadRound();draw()}catch(e){toast(e.message)}}
async function setScore(id){try{const r=await api('score',{sub_id:id,score:+$('sc'+id).value});toast(r.msg);await loadRound();draw()}catch(e){toast(e.message)}}
setInterval(()=>{if(!R||!RID)return;const e=$('cd');if(!e)return;
  if(now()>=R.round.ends_at){loadRound().then(draw).catch(()=>{});return}
  e.textContent=leftTxt(R.round.ends_at)},1000);
if(!tg.initData){$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">افتح التطبيق من داخل تيليجرام.</p>'}
else{const sp=new URLSearchParams(location.search).get('r');
  loadMe().then(()=>{draw();if(sp)openRound(+sp)}).catch(e=>{$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">'+esc(e.message)+'</p>'})}
</script></body></html>
"""


# ═══════════ 7) التشغيل ═══════════
def main():
    init_db()
    app = (Application.builder().token(BOT_TOKEN).defaults(Defaults(parse_mode=ParseMode.HTML))
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addcat", cmd_addcat))
    app.add_handler(CommandHandler("cats", cmd_cats))
    app.add_handler(CommandHandler("delcat", cmd_delcat))
    app.add_handler(CommandHandler("newround", cmd_newround))
    app.add_handler(CommandHandler("rounds", cmd_rounds))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("finish", cmd_finish))
    log.info("Bot started (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
