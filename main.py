# ── تثبيت تلقائي للمكتبات الناقصة (يعمل حتى لو لم يقرأ Railway ملف requirements.txt) ──
import subprocess as _sp, sys as _sys, importlib as _il, shutil as _sh
_NEED = (("aiohttp", "aiohttp==3.10.5"), ("telegram", "python-telegram-bot==21.6"),
         ("psycopg2", "psycopg2-binary==2.9.9"))


def _missing():
    _il.invalidate_caches()
    out = []
    for _m, _p in _NEED:
        try:
            _il.import_module(_m)
        except ImportError:
            out.append(_p)
    return out


_todo = _missing()
if _todo:
    print("Installing missing packages:", _todo, flush=True)
    _py = _sys.executable
    _tries = [[_py, "-m", "pip", "install", "-q", *_todo],
              [_py, "-m", "pip", "install", "-q", "--break-system-packages", *_todo]]
    if _sh.which("uv"):
        _tries.append(["uv", "pip", "install", "-q", "--python", _py, *_todo])
    for _cmd in _tries:
        if _sp.call(_cmd) == 0 and not _missing():
            break
    else:
        _sp.call([_py, "-m", "ensurepip", "--upgrade"])
        _sp.call([_py, "-m", "pip", "install", "-q", *_todo])
    if _missing():
        raise SystemExit("تعذّر تثبيت المكتبات: " + ", ".join(_missing()))

import os, sys, io, gzip, json, hmac, hashlib, time, asyncio, sqlite3, logging, threading, html, secrets, math, random, re, datetime
from contextlib import contextmanager
from urllib.parse import parse_qsl

from aiohttp import web
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool, PoolError
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                      WebAppInfo, MenuButtonWebApp, BotCommand, BotCommandScopeChat)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, MessageHandler, PreCheckoutQueryHandler,
                          ConversationHandler, ContextTypes, Defaults, CallbackQueryHandler, filters)
from telegram.error import BadRequest, RetryAfter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("miner-bot")

# ═══════════ 1) الإعدادات ═══════════
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "miner.db")
TASK_CHANNEL = os.environ.get("TASK_CHANNEL", "")          # مثال: @mychannel (اختياري)
try:
    PORT = int(os.environ.get("PORT", "8080") or 8080)
except ValueError:
    PORT = 8080
_raw = (os.environ.get("WEBAPP_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")).strip().rstrip("/")
if _raw.startswith("http://"):
    _raw = "https://" + _raw[7:]
elif _raw and not _raw.startswith("https://"):
    _raw = "https://" + _raw
WEBAPP_URL = _raw
APP_URL = f"{WEBAPP_URL}/app?v={int(time.time())}" if WEBAPP_URL else ""   # يمنع كاش تيليجرام للصفحة القديمة

# اللعبة (كلها نقاط افتراضية داخل البوت)
ITEMS = {
    "permit": dict(stars=500, title="تصريح التعدين", desc="يفتح لك التعدين في اللعبة (نقاط افتراضية)."),
    "energy_up": dict(stars=59, title="زيادة الطاقة القصوى", desc="+2000 طاقة قصوى و +5 طاقة/دقيقة."),
    "restore": dict(stars=32, title="استعادة الطاقة", desc="يملأ طاقتك للحد الأقصى فوراً."),
    "patron": dict(stars=99, title="راعي التعدين", desc="مكافأة الكتلة ×2 لمدة 24 ساعة (تتراكم)."),
    "support": dict(stars=0, title="دعم المطوّر", desc="دعم اللعبة بالمبلغ الذي تختاره من النجوم. شكراً لك ❤️"),
}
BASE_HR = 1000          # معدل الهاش الأساسي (H/s)
COST = 3.0              # طاقة/ثانية أثناء التعدين (كلما زاد الرقم نزلت الطاقة أسرع)
EARN_DIV = 36000        # أرباح HASH بالثانية = معدل الهاش / هذا الرقم
XP_DIV = 1000           # خبرة بالثانية = معدل الهاش / هذا الرقم (أكبر = ترقية أبطأ)
EASY_LEVELS = 10        # عدد المستويات السهلة
XP_GROWTH = 1.10        # نسبة زيادة الصعوبة بعد المستويات السهلة (10% لكل مستوى)
SUPPORT_MAX = 10000     # أقصى مبلغ دعم بالنجوم
BLOCK_REWARD = 50       # إجمالي مكافأة الكتلة (كل 5 ثوانٍ)
WINNER_PCT = 0.4        # حصة المعدّن الفائز بالقرعة من الكتلة (40%)؛ الباقي (60%) يُوزَّع على كل المعدّنين النشطين بنسبة هاشهم
REF_ENERGY = 1000        # +طاقة قصوى لكل مُحال نشط (عدّن REF_SECS ثانية فعلية على الأقل)
REF_MIN = 1000          # (قديم - لم يعد مستعملاً)
REF_SECS = 3600         # مدة التعدين الفعلي اللازمة ليُحتسب المُحال (ساعة) — يمنع الإحالات الوهمية
REF_MAX = 50            # أقصى عدد مُحالين نشطين تُحتسب لهم المكافأة
DAILY = [1000, 2000, 3000, 5000, 7500, 12000]   # اليوم 7: خادم SLIM لمدة 30 دقيقة
PATRON_SECS = 86400
MAX_SRV = 10            # أقصى عدد من كل نوع خادم دائم
ENERGY_BASE = 2000      # الطاقة القصوى عند المستوى 1
ENERGY_PER_LEVEL = 1000 # تزيد الطاقة القصوى بهذا المقدار مع كل مستوى (مستوى 2 = 3000 ...)
STALE_SECS = 15         # أي معدّن لم تُحدَّث بياناته منذ هذه المدة تُحدَّث تلقائياً من السيرفر

# أوضاع التعدين:
#   mul   = مضاعف الهاش
#   cost  = مضاعف استهلاك الطاقة (عدّله لتوازن اللعبة)
#   stars = سعر فتح الوضع بنجوم تيليجرام (0 = مفتوح مجاناً)
MODES = {1: dict(name="عادي", mul=1.0, cost=1.0, stars=0),
         2: dict(name="تيربو", mul=1.5, cost=2.0, stars=199),
         3: dict(name="سوبر", mul=4.0, cost=5.0, stars=499),
         4: dict(name="نيترو", mul=6.0, cost=8.0, stars=999),
         5: dict(name="بلازما", mul=14.0, cost=18.0, stars=1499),
         6: dict(name="كوازار", mul=20.0, cost=26.0, stars=2499),
         7: dict(name="نجم نابض", mul=34.0, cost=44.0, stars=3999),
         8: dict(name="التفرد", mul=40.0, cost=52.0, stars=6999)}
# أسماء بديلة تُقبل في أمر /gencode mode ...
MODE_ALIASES = {"normal": 1, "turbo": 2, "super": 3, "nitro": 4, "plasma": 5,
                "quasar": 6, "pulsar": 7, "singularity": 8}
for _k, _m in MODES.items():            # كل وضع مدفوع يصبح عنصراً في المتجر
    if _m["stars"] > 0:
        ITEMS[f"mode_{_k}"] = dict(stars=_m["stars"], title=f"وضع {_m['name']}",
                                   desc=f"يفتح وضع التعدين {_m['name']} (×{_m['mul']:g} هاش).")

# الخوادم: hr = زيادة الهاش، cost بنقاط HASH (0 = مكافأة فقط)، mins = مدة مؤقتة
SERVERS = {
    "slim": dict(title="SLIM", hr=500, cost=0, mins=30),
    "nano": dict(title="NANO", hr=800, cost=1000, mins=0),
    "core": dict(title="CORE", hr=3000, cost=5000, mins=0),
    "rack": dict(title="RACK", hr=12000, cost=20000, mins=0),
    "farm": dict(title="FARM", hr=50000, cost=80000, mins=0),
}
DEX_BASE = 0.008        # سعر طاقة واحدة بنقاط HASH
DEX_FEE = 0.03          # عمولة البيع
TASKS = {"channel": {"title": "اشترك في القناة", "reward": 300}} if TASK_CHANNEL else {}
try:
    TZ_OFFSET = float(os.environ.get("TZ_OFFSET", "1"))    # فرق التوقيت عن UTC لتواريخ الأدمن (الجزائر = 1)
except ValueError:
    TZ_OFFSET = 1.0
DEFAULT_BLOCKS = 20_000_000                                # عدد الكتل الافتراضي (الإصدار = الكتل × مكافأة الكتلة)
NOT_SCHEDULED = 4102444800.0                               # = لم يُحدَّد موعد بعد (سنة 2100)
CHAIN = dict(start=NOT_SCHEDULED, blocks=DEFAULT_BLOCKS, done=False)  # يضبطها الأدمن بـ /setstart و /setblocks
DEFAULT_TITLE = "TotalHash"   # عنوان المقدمة الافتراضي (يتغير بالأمر /settitle)
TITLE = DEFAULT_TITLE
SQL_ACTIVE = "mining=1 AND energy-(?-last_tick)>1"
SESS = {}               # الجلسة الفعّالة لكل لاعب {id: session_id} — جلسة واحدة فقط لكل حساب
try:
    _h, _m = (os.environ.get("DAILY_REPORT_TIME", "08:00") or "08:00").replace(".", ":").split(":")
    DAILY_MIN = int(_h) * 60 + int(_m)                      # موعد التقرير اليومي (بتوقيت TZ_OFFSET)
except ValueError:
    DAILY_MIN = 480
DAILY_BACKUP = os.environ.get("DAILY_BACKUP", "1") != "0"   # إرفاق نسخة احتياطية مع التقرير اليومي
BANNED = {}             # المحظورون: {id: السبب} (تُحمَّل من قاعدة البيانات عند التشغيل)


# ═══════════ 2) قاعدة البيانات (PostgreSQL، وإلا SQLite) ═══════════
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


@contextmanager
def cursor():
    if USE_PG:
        deadline = time.time() + 20
        while True:
            try:
                conn = pool.getconn()
            except PoolError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
                continue
            if conn.closed:
                pool.putconn(conn, close=True)
                continue
            break
        broken = False
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as c:
                    yield Cur(c, True)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise
        finally:
            try:
                pool.putconn(conn, close=bool(conn.closed) or broken)
            except Exception:
                pass
    else:
        with sq_lock:
            with sq:
                c = sq.cursor()
                try:
                    yield Cur(c, False)
                finally:
                    c.close()


def add_cols(table, cols):
    """ترقية الجداول القديمة بإضافة الأعمدة الجديدة دون فقدان البيانات."""
    for name, ddl in cols:
        with cursor() as cur:
            if USE_PG:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")
            else:
                cur.execute(f"PRAGMA table_info({table})")
                have = {r["name"] for r in cur.fetchall()}
                if name not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    global USE_PG, pool, sq
    for u in [os.environ.get("DATABASE_URL"), os.environ.get("DATABASE_PUBLIC_URL")]:
        if not u:
            continue
        try:
            pool = ThreadedConnectionPool(2, 30, u, connect_timeout=8, keepalives=1,
                                          keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
            USE_PG = True
            log.info("Connected to PostgreSQL")
            break
        except Exception as e:
            log.warning("PostgreSQL failed: %s", str(e).strip()[:150])
    if not USE_PG:
        sq = sqlite3.connect(DB_PATH, check_same_thread=False)
        sq.row_factory = sqlite3.Row
        log.warning("Using SQLite: %s", os.path.abspath(DB_PATH))
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS m_users(
            id BIGINT PRIMARY KEY, name TEXT, username TEXT,
            permit INT NOT NULL DEFAULT 0, level INT NOT NULL DEFAULT 1,
            xp DOUBLE PRECISION NOT NULL DEFAULT 0, balance DOUBLE PRECISION NOT NULL DEFAULT 0,
            total DOUBLE PRECISION NOT NULL DEFAULT 0,
            energy DOUBLE PRECISION NOT NULL DEFAULT 1000, max_energy INT NOT NULL DEFAULT 1000,
            regen INT NOT NULL DEFAULT 30, mining INT NOT NULL DEFAULT 0,
            last_tick DOUBLE PRECISION NOT NULL DEFAULT 0, streak INT NOT NULL DEFAULT 0,
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0, referrer BIGINT, created_at BIGINT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS m_payments(
            charge_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, item TEXT NOT NULL,
            stars INT NOT NULL, refunded INT NOT NULL DEFAULT 0, created_at BIGINT)""")
        cur.execute("CREATE TABLE IF NOT EXISTS m_tasks(user_id BIGINT, task TEXT, PRIMARY KEY(user_id, task))")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS m_blocks(
            id {pk}, miner_id BIGINT, miner_name TEXT, miner_level INT,
            reward DOUBLE PRECISION, miners INT, created_at BIGINT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS m_codes(
            code TEXT PRIMARY KEY, kind TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            max_uses INT NOT NULL DEFAULT 1, uses INT NOT NULL DEFAULT 0, created_at BIGINT)""")
        cur.execute("CREATE TABLE IF NOT EXISTS m_redeems(code TEXT, user_id BIGINT, PRIMARY KEY(code, user_id))")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS m_servers(
            id {pk}, user_id BIGINT NOT NULL, kind TEXT NOT NULL,
            expires DOUBLE PRECISION NOT NULL DEFAULT 0)""")
        cur.execute("CREATE TABLE IF NOT EXISTS m_settings(k TEXT PRIMARY KEY, v TEXT)")
        cur.execute("""CREATE TABLE IF NOT EXISTS m_week_top(
            week_id INT NOT NULL, pos INT NOT NULL, user_id BIGINT NOT NULL, name TEXT,
            amount DOUBLE PRECISION NOT NULL DEFAULT 0, PRIMARY KEY(week_id, pos))""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS m_sched(
            id {pk}, hhmm INT NOT NULL, days TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
            active INT NOT NULL DEFAULT 1, last_run TEXT NOT NULL DEFAULT '', created_at BIGINT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS m_block_rewards(
            block_id BIGINT NOT NULL, user_id BIGINT NOT NULL, amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY(block_id, user_id))""")
    add_cols("m_users", [("mode", "INT NOT NULL DEFAULT 1"),
                         ("hr", "DOUBLE PRECISION NOT NULL DEFAULT 1000"),
                         ("hashes", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("wins", "INT NOT NULL DEFAULT 0"),
                         ("income", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("patron", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("modes", "TEXT NOT NULL DEFAULT '1'"),
                         ("banned", "INT NOT NULL DEFAULT 0"),
                         ("ban_reason", "TEXT NOT NULL DEFAULT ''"),
                         ("week_base", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("last_win", "BIGINT NOT NULL DEFAULT 0"),
                         ("last_win_at", "BIGINT NOT NULL DEFAULT 0"),
                         ("week_wins", "INT NOT NULL DEFAULT 0"),
                         ("mined_secs", "DOUBLE PRECISION NOT NULL DEFAULT 0")])
    add_cols("m_blocks", [("thr", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                          ("bhash", "TEXT NOT NULL DEFAULT ''"),
                          ("prev_hash", "TEXT NOT NULL DEFAULT ''"),
                          ("miner_mode", "INT NOT NULL DEFAULT 1"),
                          ("shared", "DOUBLE PRECISION NOT NULL DEFAULT 0")])
    with cursor() as cur:          # ترحيل لمرة واحدة: تقدير وقت التعدين السابق من إجمالي الهاش
        cur.execute("UPDATE m_users SET mined_secs = CASE WHEN hr>1 THEN hashes/hr ELSE hashes END "
                    "WHERE mined_secs=0 AND hashes>0")
    for ddl in ("CREATE INDEX IF NOT EXISTS ix_m_total ON m_users (total DESC)",
                "CREATE INDEX IF NOT EXISTS ix_m_ref ON m_users (referrer)",
                "CREATE INDEX IF NOT EXISTS ix_m_srv ON m_servers (user_id)"):
        with cursor() as cur:
            cur.execute(ddl)
    global TITLE
    with cursor() as cur:
        cur.execute("SELECT v FROM m_settings WHERE k='title'")
        r = cur.fetchone()
        if r and r["v"]:
            TITLE = r["v"]
    st = get_setting("start_ts")
    if st is None:                      # أول تشغيل: لا تعدين حتى يحدد الأدمن الموعد بـ /setstart
        st = NOT_SCHEDULED
        set_setting("start_ts", st)
    CHAIN["start"] = float(st)
    try:
        CHAIN["blocks"] = max(1, int(float(get_setting("blocks_total", DEFAULT_BLOCKS))))
    except ValueError:
        CHAIN["blocks"] = DEFAULT_BLOCKS
    CHAIN["done"] = blocks_mined() >= CHAIN["blocks"]
    with cursor() as cur:
        cur.execute("SELECT id, ban_reason FROM m_users WHERE banned=1")
        BANNED.clear()
        BANNED.update({r["id"]: r["ban_reason"] or "" for r in cur.fetchall()})
    log.info("DB ready (%s)", "PostgreSQL" if USE_PG else "SQLite")


def get_setting(k, default=None):
    with cursor() as cur:
        cur.execute("SELECT v FROM m_settings WHERE k=?", (k,))
        r = cur.fetchone()
    return r["v"] if r and r["v"] is not None else default


def set_setting(k, v):
    with cursor() as cur:
        cur.execute("INSERT INTO m_settings(k,v) VALUES(?,?) ON CONFLICT (k) DO UPDATE SET v=excluded.v",
                    (k, str(v)))


def blocks_mined():
    with cursor() as cur:
        cur.execute("SELECT MAX(id) AS m FROM m_blocks")
        return (cur.fetchone() or {}).get("m") or 0


def _local(ts):
    return datetime.datetime.fromtimestamp(ts + TZ_OFFSET * 3600, datetime.timezone.utc)


def fmt_dt(ts):
    return "غير محدد" if ts >= NOT_SCHEDULED else _local(ts).strftime("%H:%M %d.%m.%Y")


def fmt_d(ts):
    return "—" if ts >= NOT_SCHEDULED else _local(ts).strftime("%d.%m.%Y")


def parse_start(args):
    """'12.00 11.11.2026' أو '11.11.2026 12:00' ← طابع زمني UTC (بحسب TZ_OFFSET)"""
    d = t = None
    for a in args:
        parts = re.split(r"[.:/-]", a)
        if len(parts) == 3 and len(parts[2]) == 4:
            d = (int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            t = (int(parts[0]), int(parts[1]))
    if not d:
        raise ValueError("no date")
    h, m = t or (0, 0)
    dt = datetime.datetime(d[2], d[1], d[0], h, m, tzinfo=datetime.timezone.utc)
    return dt.timestamp() - TZ_OFFSET * 3600


def chain_info():
    now = time.time()
    return dict(start=CHAIN["start"], started=now >= CHAIN["start"], done=CHAIN["done"],
                scheduled=CHAIN["start"] < NOT_SCHEDULED,
                start_txt=fmt_dt(CHAIN["start"]), blocks=CHAIN["blocks"])


# ═══════════ 3) منطق التعدين (يُحسب كسولاً عند كل طلب) ═══════════
def unlocked(u):
    """الأوضاع المفتوحة للمستخدم (الوضع 1 مفتوح دائماً)."""
    return {int(x) for x in str(u.get("modes") or "1").split(",") if x.strip().isdigit()} | {1}


def cur_mode(u):
    """الوضع الفعّال: إن لم يكن مفتوحاً للمستخدم يُستخدم الوضع العادي."""
    m = u.get("mode") or 1
    return m if (m in MODES and m in unlocked(u)) else 1


def mode_of(u):
    return MODES[cur_mode(u)]


def hashrate(u):
    return (BASE_HR * (1 + 0.05 * (u["level"] - 1)) + u.get("srv", 0)) * mode_of(u)["mul"]


def tier(mode):
    """الشارة تعتمد على الوضع المفتوح والفعّال للمستخدم (وليس على قوة الهاش).
    ترجع رقم الوضع (2..8) أو 0 للوضع العادي (بدون شارة)."""
    try:
        m = int(mode or 1)
    except (TypeError, ValueError):
        return 0
    return m if (m in MODES and m > 1) else 0


def xp_need(level):
    """أول 10 مستويات سهلة (42 ← 150 خبرة)، وبعدها تزيد الصعوبة 10% لكل مستوى."""
    if level <= EASY_LEVELS:
        return 30 + 12 * level
    return int((30 + 12 * EASY_LEVELS) * XP_GROWTH ** (level - EASY_LEVELS))


def settle(u):
    now = time.time()
    if u["mining"] and (now < CHAIN["start"] or CHAIN["done"]):
        u["mining"] = 0                  # قبل موعد البداية أو بعد اكتمال الكتل لا يوجد تعدين
    dt = max(0.0, now - u["last_tick"])
    if u["mining"]:
        cost = COST * mode_of(u)["cost"]
        hr = hashrate(u)
        run = min(dt, u["energy"] / cost)
        earned = run * hr / EARN_DIV
        u["energy"] = max(0.0, u["energy"] - run * cost)
        u["balance"] += earned
        u["total"] += earned
        u["hashes"] += run * hr
        u["mined_secs"] = u.get("mined_secs", 0) + run
        u["xp"] += run * hr / XP_DIV
        while u["xp"] >= xp_need(u["level"]):
            u["xp"] -= xp_need(u["level"])
            u["level"] += 1
        if u["energy"] <= 0.01 or run < dt:
            u["mining"] = 0
    elif u["energy"] < u.get("xmax", u["max_energy"]):
        u["energy"] = min(u.get("xmax", u["max_energy"]), u["energy"] + u["regen"] / 60 * dt)
    u["last_tick"] = now


class Err(Exception):
    pass


def ensure_user(cur, uid, name, username, ref=None):
    ref_ok = None
    if ref and ref != uid:
        cur.execute("SELECT id FROM m_users WHERE id=?", (ref,))
        if cur.fetchone():
            ref_ok = ref
    cur.execute("""INSERT INTO m_users(id,name,username,referrer,last_tick,created_at,energy) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET name=excluded.name, username=excluded.username""",
                (uid, name, username, ref_ok, time.time(), int(time.time()), ENERGY_BASE))


def xmax_of(u):
    """الطاقة القصوى = أساس المستوى + مشتريات الطاقة (max_energy-1000) + مكافأة الإحالات النشطة."""
    return (ENERGY_BASE + ENERGY_PER_LEVEL * (u["level"] - 1)
            + (u["max_energy"] - 1000) + REF_ENERGY * u.get("refs_act", 0))


def lock_user(cur, uid):
    cur.execute("SELECT * FROM m_users WHERE id=?" + (" FOR UPDATE" if USE_PG else ""), (uid,))
    u = cur.fetchone()
    if not u:
        raise Err("user")
    cur.execute("SELECT kind FROM m_servers WHERE user_id=? AND (expires=0 OR expires>?)", (uid, time.time()))
    u["srv"] = sum(SERVERS[r["kind"]]["hr"] for r in cur.fetchall() if r["kind"] in SERVERS)
    cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE referrer=? AND mined_secs>=? AND banned=0", (uid, REF_SECS))
    u["refs_act"] = min(cur.fetchone()["n"], REF_MAX)
    u["xmax"] = xmax_of(u)                  # الحد الأقصى الفعلي للطاقة
    settle(u)
    u["xmax"] = xmax_of(u)                  # قد يتغير بعد الترقية لمستوى جديد
    return u


def save_user(cur, u):
    # نحفظ الوضع الفعّال (المفتوح فعلاً) كي لا تظهر شارة وضع لم يشتره المستخدم
    cur.execute("""UPDATE m_users SET permit=?,level=?,xp=?,balance=?,total=?,energy=?,max_energy=?,
        regen=?,mining=?,last_tick=?,streak=?,last_daily=?,mode=?,hr=?,hashes=?,wins=?,income=?,patron=?,modes=?,
        mined_secs=? WHERE id=?""",
                (u["permit"], u["level"], u["xp"], u["balance"], u["total"], u["energy"], u["max_energy"],
                 u["regen"], u["mining"], u["last_tick"], u["streak"], u["last_daily"], cur_mode(u),
                 hashrate(u), u["hashes"], u["wins"], u["income"], u["patron"], u["modes"], u.get("mined_secs", 0), u["id"]))


def daily_info(u):
    now, last = time.time(), u["last_daily"]
    streak = u["streak"] if last and now - last < 172800 else 0
    wait = max(0, int(last + 86400 - now)) if last else 0
    return streak, wait


def state(u):
    streak, wait = daily_info(u)
    m = mode_of(u)
    hr = hashrate(u)
    return dict(permit=bool(u["permit"]), level=u["level"], xp=int(u["xp"]), xp_need=xp_need(u["level"]),
                balance=u["balance"], energy=u["energy"], max_energy=u.get("xmax", u["max_energy"]), regen=u["regen"],
                mining=bool(u["mining"]), hashrate=hr, rate=hr / EARN_DIV, cost=COST * m["cost"],
                mode=cur_mode(u), unlocked=sorted(unlocked(u)),
                hashes=u["hashes"], wins=u["wins"], income=u["income"],
                patron_left=max(0, int(u["patron"] - time.time())), srv=u["srv"], tier=tier(cur_mode(u)),
                day_idx=streak % 7, daily_wait=wait)


def add_points(cur, uid, pts):
    cur.execute("UPDATE m_users SET balance=balance+?, total=total+? WHERE id=?", (pts, pts, uid))


def apply_payment(uid, key, charge, stars):
    with cursor() as cur:
        cur.execute("INSERT INTO m_payments(charge_id,user_id,item,stars,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT DO NOTHING", (charge, uid, key, stars, int(time.time())))
        if cur.rowcount != 1:
            return False
        u = lock_user(cur, uid)
        if key == "permit":
            u["permit"] = 1
        elif key == "energy_up":
            u["max_energy"] += 2000
            u["regen"] += 5
        elif key == "restore":
            u["energy"] = max(u["energy"], u.get("xmax", u["max_energy"]))
        elif key == "patron":
            u["patron"] = max(time.time(), u["patron"]) + PATRON_SECS
        elif key.startswith("mode_"):
            s = unlocked(u)
            s.add(int(key[5:]))
            u["modes"] = ",".join(map(str, sorted(s)))
        save_user(cur, u)
    return True


def make_block():
    """كتلة كل 5 ثوانٍ. الفائز بالقرعة (موزونة بالهاش) يأخذ WINNER_PCT من المكافأة،
    والباقي (المكافأة المشتركة) يُوزَّع على كل المعدّنين النشطين بنسبة هاش كل واحد منهم."""
    now = time.time()
    if now < CHAIN["start"] or CHAIN["done"]:
        return
    with cursor() as cur:
        cur.execute("DELETE FROM m_servers WHERE expires>0 AND expires<?", (now,))
        cur.execute("SELECT id,name,level,hr,patron FROM m_users WHERE " + SQL_ACTIVE + " LIMIT 20000", (now,))
        ms = cur.fetchall()
        if not ms:
            return
        total = sum(max(m["hr"], 1.0) for m in ms)
        w = random.choices(ms, weights=[max(m["hr"], 1.0) for m in ms])[0]
        pool = BLOCK_REWARD * (1 - WINNER_PCT)             # المكافأة المشتركة
        k = pool / total                                   # حصة كل 1 هاش
        win = BLOCK_REWARD * WINNER_PCT * (2 if w["patron"] > now else 1)   # مكافأة المعدّن (الراعي ×2)
        cur.execute("SELECT bhash FROM m_blocks ORDER BY id DESC LIMIT 1")
        prev = (cur.fetchone() or {}).get("bhash") or "0" * 64
        bh = "00000" + hashlib.sha256(
            f"{prev}|{w['id']}|{win}|{now}|{secrets.token_hex(4)}".encode()).hexdigest()[5:]
        cur.execute("INSERT INTO m_blocks(miner_id,miner_name,miner_level,reward,miners,thr,created_at,"
                    "bhash,prev_hash,miner_mode,shared) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (w["id"], w["name"], w["level"], win, len(ms), total, int(now), bh, prev, 1, pool))
        cur.execute("SELECT MAX(id) AS m FROM m_blocks")
        bid = (cur.fetchone() or {}).get("m") or 0
        # سجل حصة كل معدّن في هذه الكتلة (ليظهر في «مكافأتك»)
        cur.execute("INSERT INTO m_block_rewards(block_id,user_id,amount) "
                    "SELECT ?, id, hr*?*(CASE WHEN patron>? THEN 2 ELSE 1 END) FROM m_users WHERE "
                    + SQL_ACTIVE, (bid, k, now, now))
        # توزيع المكافأة المشتركة على الجميع دفعة واحدة
        x = "hr*?*(CASE WHEN patron>? THEN 2 ELSE 1 END)"
        cur.execute(f"UPDATE m_users SET balance=balance+{x}, total=total+{x}, income=income+{x} WHERE "
                    + SQL_ACTIVE, (k, now, k, now, k, now, now))
        # مكافأة الفائز (بعد التوزيع كي لا تُكتب فوق رصيده)
        try:
            u = lock_user(cur, w["id"])
        except Err:
            return
        u["balance"] += win
        u["total"] += win
        u["wins"] += 1
        u["income"] += win
        save_user(cur, u)
        cur.execute("UPDATE m_users SET last_win=?, last_win_at=?, week_wins=week_wins+1 WHERE id=?",
                    (bid, int(now), w["id"]))
        cur.execute("UPDATE m_blocks SET miner_level=?, miner_mode=? WHERE id=?", (u["level"], cur_mode(u), bid))
        cur.execute("UPDATE m_block_rewards SET amount=amount+? WHERE block_id=? AND user_id=?",
                    (win, bid, w["id"]))
        cur.execute("DELETE FROM m_blocks WHERE id < ?", (bid - 200,))
        cur.execute("DELETE FROM m_block_rewards WHERE block_id < ?", (bid - 200,))
        if bid >= CHAIN["blocks"]:
            CHAIN["done"] = True


def settle_stale():
    """يحدّث (مستوى/رصيد/طاقة) المعدّنين الذين أغلقوا التطبيق أو أصبح في الخلفية،
    كي تظهر مستوياتهم وأرصدتهم الحقيقية في الكتل والترتيب وبقية الشاشات بدل أن تبقى قديمة."""
    now = time.time()
    with cursor() as cur:
        cur.execute("SELECT id FROM m_users WHERE mining=1 AND last_tick<? ORDER BY last_tick LIMIT 500",
                    (now - STALE_SECS,))
        ids = [r["id"] for r in cur.fetchall()]
    for i in ids:
        try:
            with cursor() as cur:
                u = lock_user(cur, i)
                save_user(cur, u)
        except Err:
            pass
        except Exception:
            log.exception("settle_stale %s", i)


# ═══════════ 3.1) DEX: تبادل HASH ↔ طاقة ═══════════
def dex_price(t):
    return DEX_BASE * (1 + 0.06 * math.sin(t / 1800) + 0.03 * math.sin(t / 421 + 1))


def dex_info():
    now = time.time()
    p, old = dex_price(now), dex_price(now - 3600)
    return dict(price=p, change=(p / old - 1) * 100, fee=DEX_FEE)


# ═══════════ 3.3) الترتيب الأسبوعي ═══════════
# الأسبوع يبدأ كل اثنين 00:00 (بتوقيت TZ_OFFSET). ربح الأسبوع = total - week_base.
WEEK_CACHE = [0.0, None, 0]


def week_id_of(ts):
    return int(((ts + TZ_OFFSET * 3600) - 4 * 86400) // (7 * 86400))


def week_bounds(now=None):
    now = now or time.time()
    wid = week_id_of(now)
    st = wid * 7 * 86400 + 4 * 86400 - TZ_OFFSET * 3600
    return wid, st, st + 7 * 86400


def roll_week():
    """يُغلق الأسبوع المنتهي: يحفظ أفضل 100 في m_week_top ثم يصفّر عدّاد الأسبوع. يرجع (رقم_الأسبوع, الصفوف) أو None."""
    cw = week_id_of(time.time())
    saved = get_setting("week_id")
    if saved is None:                                   # أول تشغيل: يبدأ العدّ من الآن
        with cursor() as cur:
            cur.execute("UPDATE m_users SET week_base=total, week_wins=0")
        set_setting("week_id", cw)
        return None
    sw = int(saved)
    if sw >= cw:
        return None
    with cursor() as cur:
        cur.execute("DELETE FROM m_week_top WHERE week_id=?", (sw,))
        cur.execute("SELECT id,name,total-week_base AS w FROM m_users WHERE banned=0 AND total-week_base>0 "
                    "ORDER BY w DESC LIMIT 100")
        rows = cur.fetchall()
        for i, r in enumerate(rows, 1):
            cur.execute("INSERT INTO m_week_top(week_id,pos,user_id,name,amount) VALUES(?,?,?,?,?)",
                        (sw, i, r["id"], r["name"], r["w"]))
        cur.execute("UPDATE m_users SET week_base=total, week_wins=0")
        cur.execute("INSERT INTO m_settings(k,v) VALUES('week_id',?) ON CONFLICT (k) DO UPDATE SET v=excluded.v",
                    (str(cw),))
    WEEK_CACHE[1] = None
    return sw, rows


def week_top_rows(limit=100):
    with cursor() as cur:
        cur.execute("SELECT id,name,level,mode,total-week_base AS w FROM m_users WHERE banned=0 AND "
                    "total-week_base>0 ORDER BY w DESC LIMIT " + str(int(limit)))
        return cur.fetchall()


def weekly_data(tg):
    now = time.time()
    wid, st, en = week_bounds(now)
    if WEEK_CACHE[1] is None or now - WEEK_CACHE[0] > 20:       # كاش 20 ثانية لتخفيف الضغط
        rows = week_top_rows(100)
        with cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE banned=0 AND total-week_base>0")
            players = cur.fetchone()["n"]
        WEEK_CACHE[:] = [now, rows, players]
    top, players = WEEK_CACHE[1], WEEK_CACHE[2]
    with cursor() as cur:
        cur.execute("SELECT name,level,mode,hr,last_win,last_win_at,week_wins,total-week_base AS w "
                    "FROM m_users WHERE id=?", (tg["id"],))
        me = cur.fetchone() or dict(name=tg["name"], level=1, mode=1, hr=BASE_HR, last_win=0,
                                    last_win_at=0, week_wins=0, w=0)
        w = max(0.0, float(me["w"] or 0))
        cur.execute("SELECT COUNT(*)+1 AS r FROM m_users WHERE banned=0 AND total-week_base>?", (w,))
        rank = cur.fetchone()["r"]
    gap, gap_kind = None, ""
    if rank > 1 and top:                      # كم ينقصك لتتخطى من فوقك (أو لتدخل أفضل 100)
        if rank - 2 < len(top):
            above, gap_kind = float(top[rank - 2]["w"]), "up"
        else:
            above, gap_kind = float(top[-1]["w"]), "top100"
        gap = max(0.0, above - w)
    mode = me["mode"] if me["mode"] in MODES else 1
    return dict(top=[dict(name=r["name"], level=r["level"], tier=tier(r["mode"]), total=r["w"],
                          me=r["id"] == tg["id"]) for r in top],
                rank=rank, mine=dict(name=me["name"], level=me["level"], total=w, tier=tier(mode)),
                left=int(en - now), start=fmt_d(st), end=fmt_d(en - 1),
                start_ts=int(st), end_ts=int(en), players=int(players), gap=gap, gap_kind=gap_kind,
                me=dict(mode=mode, level=me["level"], hr=float(me["hr"] or 0), last_win=int(me["last_win"] or 0),
                        last_win_at=int(me["last_win_at"] or 0), wins=int(me["week_wins"] or 0)))


# ═══════════ 3.2) أكواد الاسترداد ═══════════
CODE_KINDS = {"energy": "طاقة", "hash": "نقاط HASH", "permit": "تصريح التعدين", "mode": "وضع تعدين"}


def parse_amt(s):
    s = s.lower().replace(",", "").strip()
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    return float(s) * mult


def fmt_amt(n):
    n = float(n)
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{int(n // 1_000_000)}M"
    if n >= 1000 and n % 1000 == 0:
        return f"{int(n // 1000)}K"
    return f"{n:g}"


def resolve_mode(s):
    """يقبل رقم الوضع (1-8) أو اسمه بالعربي أو الإنجليزي (turbo, super, ...) ← رقم الوضع أو None"""
    s = (s or "").strip().lower()
    if s.isdigit():
        n = int(s)
        return n if n in MODES else None
    if s in MODE_ALIASES:
        return MODE_ALIASES[s]
    for k, m in MODES.items():
        if m["name"] == s:
            return k
    return None


def prize_text(kind, amount):
    if kind == "mode":
        m = MODES.get(int(amount))
        return "وضع " + (m["name"] if m else str(int(amount)))
    return CODE_KINDS.get(kind, kind) + ("" if kind == "permit" else f" {fmt_amt(amount)}")


def redeem_code(tg, code):
    code = code.strip().upper()
    with cursor() as cur:
        u = touch(cur, tg)
        cur.execute("SELECT * FROM m_codes WHERE code=?" + (" FOR UPDATE" if USE_PG else ""), (code,))
        c = cur.fetchone()
        if not c:
            raise Err("الكود غير صحيح")
        if c["uses"] >= c["max_uses"]:
            raise Err("انتهت صلاحية هذا الكود (اكتمل عدد الاستخدامات)")
        if c["kind"] == "permit" and u["permit"]:
            raise Err("لديك تصريح بالفعل")
        if c["kind"] == "mode":
            mid = int(c["amount"])
            if mid not in MODES:
                raise Err("هذا الكود يشير إلى وضع غير موجود")
            if mid in unlocked(u):
                raise Err("هذا الوضع مفتوح لديك بالفعل")
        cur.execute("INSERT INTO m_redeems(code,user_id) VALUES(?,?) ON CONFLICT DO NOTHING", (code, u["id"]))
        if cur.rowcount != 1:
            raise Err("استخدمت هذا الكود من قبل")
        if c["kind"] == "energy":
            u["energy"] += c["amount"]
            msg = f"⚡ +{fmt_amt(c['amount'])} طاقة"
        elif c["kind"] == "hash":
            u["balance"] += c["amount"]
            u["total"] += c["amount"]
            msg = f"💰 +{fmt_amt(c['amount'])} HASH"
        elif c["kind"] == "mode":
            s = unlocked(u)
            s.add(mid)
            u["modes"] = ",".join(map(str, sorted(s)))
            msg = f"🚀 تم فتح وضع {MODES[mid]['name']}"
        else:
            u["permit"] = 1
            msg = "🔑 تم تفعيل تصريح التعدين"
        cur.execute("UPDATE m_codes SET uses=uses+1 WHERE code=?", (code,))
        save_user(cur, u)
    return msg


# ═══════════ 4) واجهة الـ API (aiohttp) ═══════════
MAIN_APP, WEB_RUNNER, BG = None, None, []


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
        sp = d.get("start_param", "")
        return dict(id=int(u["id"]), username=u.get("username") or "",
                    name=" ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "Miner",
                    ref=int(sp[4:]) if sp.startswith("ref_") and sp[4:].isdigit() else None)
    except Exception:
        return None


def touch(cur, tg):
    ensure_user(cur, tg["id"], tg["name"], tg["username"], tg["ref"])
    return lock_user(cur, tg["id"])


def act_me(tg, b):
    now = time.time()
    with cursor() as cur:
        u = touch(cur, tg)
        save_user(cur, u)
        cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE referrer=?", (u["id"],))
        refs = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE referrer=? AND mined_secs>=? AND banned=0",
                    (u["id"], REF_SECS))
        ref_active = min(cur.fetchone()["n"], REF_MAX)
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(hr),0) AS h FROM m_users WHERE " + SQL_ACTIVE, (now,))
        a = cur.fetchone()
        cur.execute("SELECT MAX(id) AS m FROM m_blocks")
        last_block = (cur.fetchone() or {}).get("m") or 0
        cur.execute("SELECT task FROM m_tasks WHERE user_id=?", (u["id"],))
        done = {r["task"] for r in cur.fetchall()}
        cur.execute("SELECT kind, COUNT(*) AS n FROM m_servers WHERE user_id=? AND expires=0 GROUP BY kind",
                    (u["id"],))
        owned = {r["kind"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT kind, expires FROM m_servers WHERE user_id=? AND expires>?", (u["id"], now))
        temps = [dict(title=SERVERS[r["kind"]]["title"], hr=SERVERS[r["kind"]]["hr"],
                      left=int(r["expires"] - now)) for r in cur.fetchall() if r["kind"] in SERVERS]
    servers = [dict(key=k, title=v["title"], hr=v["hr"], owned=owned.get(k, 0),
                    cost=int(v["cost"] * 1.25 ** owned.get(k, 0)), max=MAX_SRV)
               for k, v in SERVERS.items() if v["cost"] > 0]
    return dict(**state(u), refs=refs, online=a["n"], total_hr=a["h"], difficulty=a["h"] * 0.2,
                block_reward=BLOCK_REWARD * (2 if u["patron"] > now else 1), next_block=last_block + 1,
                ref_energy=REF_ENERGY, ref_min=REF_MIN, ref_hours=REF_SECS // 3600, ref_max=REF_MAX, ref_active=ref_active, daily=DAILY, dex=dex_info(), chain=chain_info(), servers=servers, temps=temps,
                modes={k: dict(name=v["name"], mul=v["mul"], cost=v["cost"], stars=v["stars"]) for k, v in MODES.items()},
                ref_link=f"https://t.me/{MAIN_APP.bot.username}?start=ref_{u['id']}",
                tasks=[dict(id=k, title=v["title"], reward=v["reward"], done=k in done) for k, v in TASKS.items()],
                channel=TASK_CHANNEL, prices={k: v["stars"] for k, v in ITEMS.items()})


def act_mining(tg, b):
    with cursor() as cur:
        u = touch(cur, tg)
        if b.get("on"):
            if time.time() < CHAIN["start"]:
                raise Err("يبدأ التعدين في " + fmt_dt(CHAIN["start"]) if CHAIN["start"] < NOT_SCHEDULED
                          else "لم يبدأ التعدين بعد")
            if CHAIN["done"]:
                raise Err("اكتمل تعدين جميع الكتل")
            if not u["permit"]:
                raise Err("تحتاج تصريح التعدين أولاً")
            if u["energy"] < 1:
                raise Err("لا توجد طاقة كافية")
            u["mining"] = 1
        else:
            u["mining"] = 0
        save_user(cur, u)
    return {}


def act_mode(tg, b):
    try:
        m = int(b.get("mode"))
    except (TypeError, ValueError):
        raise Err("وضع غير صحيح")
    if m not in MODES:
        raise Err("وضع غير صحيح")
    with cursor() as cur:
        u = touch(cur, tg)
        if m not in unlocked(u):
            raise Err("افتح هذا الوضع أولاً")
        u["mode"] = m
        save_user(cur, u)
    return {}


def act_daily(tg, b):
    with cursor() as cur:
        u = touch(cur, tg)
        streak, wait = daily_info(u)
        if wait > 0:
            raise Err("المكافأة التالية لم تحن بعد")
        idx = streak % 7
        if idx < 6:
            u["energy"] += DAILY[idx]          # قد تتجاوز الحد الأقصى كمكافأة
        else:
            sv = SERVERS["slim"]
            cur.execute("INSERT INTO m_servers(user_id,kind,expires) VALUES(?,?,?)",
                        (u["id"], "slim", time.time() + sv["mins"] * 60))
            u["srv"] += sv["hr"]
        u["streak"], u["last_daily"] = streak + 1, time.time()
        save_user(cur, u)
    return {}


def act_server(tg, b):
    key = b.get("key")
    sv = SERVERS.get(key)
    if not sv or sv["cost"] <= 0:
        raise Err("خادم غير معروف")
    with cursor() as cur:
        u = touch(cur, tg)
        cur.execute("SELECT COUNT(*) AS n FROM m_servers WHERE user_id=? AND kind=? AND expires=0",
                    (u["id"], key))
        owned = cur.fetchone()["n"]
        if owned >= MAX_SRV:
            raise Err("وصلت للحد الأقصى من هذا الخادم")
        cost = int(sv["cost"] * 1.25 ** owned)
        if u["balance"] < cost:
            raise Err("رصيد HASH غير كافٍ")
        u["balance"] -= cost
        cur.execute("INSERT INTO m_servers(user_id,kind,expires) VALUES(?,?,0)", (u["id"], key))
        u["srv"] += sv["hr"]
        save_user(cur, u)
    return {"msg": f"✅ تم شراء خادم {sv['title']}"}


def act_dex(tg, b):
    side = b.get("side")
    try:
        amt = float(b.get("amount"))
    except (TypeError, ValueError):
        raise Err("مقدار غير صحيح")
    if not (amt > 0) or amt > 1e12 or amt != amt:
        raise Err("مقدار غير صحيح")
    p = dex_price(time.time())
    with cursor() as cur:
        u = touch(cur, tg)
        if side == "buy":
            if u["balance"] < amt:
                raise Err("رصيد HASH غير كافٍ")
            gain = amt / p
            if u["energy"] + gain > u.get("xmax", u["max_energy"]) + 0.5:
                raise Err("تتجاوز الحد الأقصى للطاقة")
            u["balance"] -= amt
            u["energy"] += gain
            msg = f"✅ +{gain:,.0f} ⚡"
        elif side == "sell":
            if u["energy"] < amt:
                raise Err("طاقة غير كافية")
            got = amt * p * (1 - DEX_FEE)
            u["energy"] -= amt
            u["balance"] += got
            msg = f"✅ +{got:,.4f} HASH"
        else:
            raise Err("عملية غير صحيحة")
        save_user(cur, u)
    return {"msg": msg}


def act_refs(tg, b):
    with cursor() as cur:
        cur.execute("SELECT referrer AS rid, COUNT(*) AS n, SUM(CASE WHEN mined_secs>=? THEN 1 ELSE 0 END) AS act "
                    "FROM m_users WHERE referrer IS NOT NULL GROUP BY referrer ORDER BY n DESC, referrer LIMIT 100",
                    (REF_SECS,))
        rows = cur.fetchall()
        info = {}
        if rows:
            ids = [r["rid"] for r in rows]
            cur.execute("SELECT id,name,level,mode FROM m_users WHERE id IN (" + ",".join("?" * len(ids)) + ")",
                        tuple(ids))
            info = {r["id"]: r for r in cur.fetchall()}
    out = []
    for r in rows:
        u = info.get(r["rid"])
        if u:
            out.append(dict(name=u["name"], level=u["level"], tier=tier(u["mode"]), count=int(r["n"]),
                            active=int(r["act"] or 0), me=r["rid"] == tg["id"]))
    return {"reftop": out}


def act_blocks(tg, b):
    with cursor() as cur:
        cur.execute("SELECT * FROM m_blocks ORDER BY id DESC LIMIT 25")
        rows = cur.fetchall()
        mine = {}
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute("SELECT block_id, amount FROM m_block_rewards WHERE user_id=? AND block_id IN ("
                        + ",".join("?" * len(ids)) + ")", (tg["id"], *ids))
            mine = {r["block_id"]: r["amount"] for r in cur.fetchall()}
    out = []
    for r in rows:
        my = mine.get(r["id"])
        if my is None:                      # كتل قديمة قبل نظام التوزيع
            my = r["reward"] if r["miner_id"] == tg["id"] else 0
        out.append(dict(id=r["id"], miner=r["miner_name"], level=r["miner_level"], reward=r["reward"],
                        t=r["created_at"], miners=r["miners"], thr=r.get("thr") or 0,
                        shared=r.get("shared") or 0, prev=r.get("prev_hash") or "",
                        tier=tier(r.get("miner_mode")), my=my,
                        rare=r["reward"] > BLOCK_REWARD * WINNER_PCT))
    return {"blocks": out}


def _act_top_all(tg, b):
    with cursor() as cur:
        cur.execute("SELECT id,name,level,total,mode FROM m_users WHERE total>0 AND banned=0 ORDER BY total DESC LIMIT 100")
        rows = cur.fetchall()
        cur.execute("SELECT name,level,total,mode FROM m_users WHERE id=?", (tg["id"],))
        me = cur.fetchone() or dict(name=tg["name"], level=1, total=0, mode=1)
        cur.execute("SELECT COUNT(*)+1 AS r FROM m_users WHERE total>? AND banned=0", (me["total"],))
        rank = cur.fetchone()["r"]
        cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE total>0 AND banned=0")
        holders = cur.fetchone()["n"]
    mined = blocks_mined()
    return {"top": [dict(name=r["name"], level=r["level"], total=r["total"], tier=tier(r["mode"]),
                         me=r["id"] == tg["id"]) for r in rows],
            "rank": rank, "mytotal": me["total"],
            "mine": dict(name=me["name"], level=me["level"], total=me["total"], tier=tier(me["mode"])),
            "chain": dict(holders=holders, emission=CHAIN["blocks"] * BLOCK_REWARD,
                          pct=min(100.0, mined / CHAIN["blocks"] * 100), mined=mined, blocks=CHAIN["blocks"],
                          start_date=fmt_d(CHAIN["start"]))}


def act_top(tg, b):
    res = _act_top_all(tg, b)
    if b.get("week"):
        res["week"] = weekly_data(tg)
    return res


def act_redeem(tg, b):
    return {"msg": "✅ " + redeem_code(tg, str(b.get("code", "")))}


def task_done(tg, key):
    with cursor() as cur:
        cur.execute("INSERT INTO m_tasks(user_id,task) VALUES(?,?) ON CONFLICT DO NOTHING", (tg["id"], key))
        if cur.rowcount == 1:
            add_points(cur, tg["id"], TASKS[key]["reward"])


async def act_task(tg, b):
    key = b.get("task")
    if key not in TASKS:
        raise Err("مهمة غير موجودة")
    m = await MAIN_APP.bot.get_chat_member(TASK_CHANNEL, tg["id"])
    if m.status not in ("member", "administrator", "creator"):
        raise Err("لم تشترك بعد")
    await asyncio.to_thread(task_done, tg, key)
    return {}


async def act_invoice(tg, b):
    key = b.get("item")
    it = ITEMS.get(key)
    if not it:
        raise Err("عنصر غير معروف")
    stars = it["stars"]
    if key == "support":
        try:
            stars = int(b.get("stars"))
        except (TypeError, ValueError):
            raise Err("مبلغ غير صحيح")
        if not 1 <= stars <= SUPPORT_MAX:
            raise Err(f"المبلغ من 1 إلى {SUPPORT_MAX} نجمة")
    if key == "permit":
        def has():
            with cursor() as cur:
                ensure_user(cur, tg["id"], tg["name"], tg["username"], tg["ref"])
                cur.execute("SELECT permit FROM m_users WHERE id=?", (tg["id"],))
                return cur.fetchone()["permit"]
        if await asyncio.to_thread(has):
            raise Err("لديك تصريح بالفعل")
    if key.startswith("mode_"):
        def has_mode():
            with cursor() as cur:
                ensure_user(cur, tg["id"], tg["name"], tg["username"], tg["ref"])
                cur.execute("SELECT modes FROM m_users WHERE id=?", (tg["id"],))
                return int(key[5:]) in unlocked(cur.fetchone())
        if await asyncio.to_thread(has_mode):
            raise Err("هذا الوضع مفتوح لديك بالفعل")
    link = await MAIN_APP.bot.create_invoice_link(
        title=it["title"], description=it["desc"], payload=f"{key}:{tg['id']}", provider_token="",
        currency="XTR", prices=[LabeledPrice(it["title"], stars)])
    return {"link": link}


SYNC = {"me": act_me, "mining": act_mining, "mode": act_mode, "daily": act_daily, "server": act_server,
        "dex": act_dex, "refs": act_refs, "blocks": act_blocks, "top": act_top, "redeem": act_redeem}
ASYNC = {"task": act_task, "invoice": act_invoice}


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
    if tg["id"] in BANNED:
        return web.json_response({"ok": False, "banned": True, "reason": BANNED[tg["id"]]})
    sid = request.headers.get("X-Session", "")
    if sid and tg["id"] != ADMIN_ID:                 # جلسة واحدة لكل حساب: الأحدث (فتحاً) تفصل الأقدم
        if request.headers.get("X-Sess-New") == "1" or tg["id"] not in SESS:
            SESS[tg["id"]] = sid
        elif SESS[tg["id"]] != sid:
            return web.json_response({"ok": False, "kicked": True})
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    if act in SYNC:
        res = await asyncio.to_thread(SYNC[act], tg, body)
    elif act in ASYNC:
        res = await ASYNC[act](tg, body)
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True, **res})


async def web_page(request):
    return web.Response(text=WEB_HTML.replace("__TITLE__", html.escape(TITLE)), content_type="text/html", headers={"Cache-Control": "no-cache"})


async def web_health(request):
    return web.Response(text="ok")


async def start_web():
    global WEB_RUNNER
    if not WEBAPP_URL:
        log.error("WEBAPP_URL / RAILWAY_PUBLIC_DOMAIN غير مضبوط: لن تعمل واجهة الويب")
        return
    w = web.Application(middlewares=[err_mw])
    w.add_routes([web.get("/", web_health), web.get("/app", web_page),
                  web.route("*", "/api/{act}", web_api)])
    WEB_RUNNER = web.AppRunner(w)
    await WEB_RUNNER.setup()
    await web.TCPSite(WEB_RUNNER, "0.0.0.0", PORT).start()
    log.info("Mini App on 0.0.0.0:%s (%s/app)", PORT, WEBAPP_URL)


async def block_loop():
    while True:
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(make_block)
        except Exception:
            log.exception("block loop")


async def week_loop():
    """كل دقيقة: يتحقق من انتهاء الأسبوع، فيحفظ أفضل 100 ويُبلغ الأدمن."""
    while True:
        try:
            res = await asyncio.to_thread(roll_week)
            if res and ADMIN_ID:
                sw, rows = res
                top3 = "\n".join(f"{i}. {html.escape(r['name'] or '—')} · {float(r['w']):,.0f}"
                                 for i, r in enumerate(rows[:3], 1)) or "لا أحد"
                await MAIN_APP.bot.send_message(
                    ADMIN_ID, f"🏁 <b>انتهى الأسبوع</b> وتم حفظ أفضل {len(rows)}.\n\n{top3}\n\n"
                              "لتهنئتهم: <code>/weeksend مبروك {name}! مركزك {rank} هذا الأسبوع 🎉</code>\n"
                              "لعرض الترتيب: /weekly last")
        except Exception:
            log.exception("week loop")
        await asyncio.sleep(60)


async def settle_loop():
    """كل 10 ثوانٍ: يحدّث بيانات المعدّنين النشطين الذين لا يرسل تطبيقهم طلبات (مغلق/في الخلفية)."""
    while True:
        await asyncio.sleep(10)
        try:
            await asyncio.to_thread(settle_stale)
        except Exception:
            log.exception("settle loop")


# ═══════════ 5) أوامر البوت والدفع بالنجوم ═══════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ref = None
    if context.args and context.args[0].startswith("ref_") and context.args[0][4:].isdigit():
        ref = int(context.args[0][4:])

    def reg():
        with cursor() as cur:
            ensure_user(cur, u.id, u.full_name, u.username, ref)
    await asyncio.to_thread(reg)
    text = ("👋 أهلاً بك في <b>TotalHash</b>!\n\nلعبة تعدين افتراضية: اجمع النقاط، طوّر معدّنك، "
            "وتصدّر القائمة.\nℹ️ النقاط داخل اللعبة فقط ولا تمثل عملة رقمية حقيقية ولا يمكن سحبها.")
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛏ افتح التعدين", web_app=WebAppInfo(url=APP_URL))]])
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text)


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        key, uid = q.invoice_payload.split(":")
        if key == "support":
            amount_ok = 1 <= q.total_amount <= SUPPORT_MAX
        else:
            amount_ok = key in ITEMS and q.total_amount == ITEMS[key]["stars"]
        ok = (key in ITEMS and int(uid) == q.from_user.id and q.currency == "XTR" and amount_ok
              and q.from_user.id not in BANNED)
    except Exception:
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "بيانات الدفع غير صحيحة")


async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    key = sp.invoice_payload.split(":")[0]
    ok = await asyncio.to_thread(apply_payment, update.effective_user.id, key,
                                 sp.telegram_payment_charge_id, sp.total_amount)
    if ok:
        await update.message.reply_text("✅ تم الدفع وتفعيل طلبك! عُد إلى التطبيق ⛏" if key != "support"
                                        else "❤️ شكراً لدعمك!")
        if ADMIN_ID:
            u = update.effective_user
            try:
                await context.bot.send_message(
                    ADMIN_ID, f"💳 دفعة جديدة\n👤 {html.escape(u.full_name)} (<code>{u.id}</code>)\n"
                              f"📦 {key} · ⭐ {sp.total_amount}\n"
                              f"🧾 <code>{html.escape(sp.telegram_payment_charge_id)}</code>\n\n"
                              f"للاسترجاع أرسل: <code>/refund {u.id}</code>")
            except Exception:
                pass


def _find_payment(cid):
    with cursor() as cur:
        cur.execute("SELECT * FROM m_payments WHERE charge_id=?", (cid,))
        return cur.fetchone()


def _mark_refunded(p):
    """تعليم الدفعة كمُرجعة وسحب أثرها من حساب المستخدم."""
    with cursor() as cur:
        cur.execute("UPDATE m_payments SET refunded=1 WHERE charge_id=?", (p["charge_id"],))
        try:
            u = lock_user(cur, p["user_id"])
        except Err:                      # المستخدم محذوف (بعد /resetall): لا أثر لسحبه
            return
        k = p["item"]
        if k == "permit":
            u["permit"], u["mining"] = 0, 0
        elif k == "energy_up":
            u["max_energy"] = max(1000, u["max_energy"] - 2000)
            u["regen"] = max(30, u["regen"] - 5)
            u["energy"] = min(u["energy"], xmax_of(u))
        elif k == "patron":
            u["patron"] = max(0, u["patron"] - PATRON_SECS)
        elif k.startswith("mode_"):
            mid = int(k[5:])
            s = unlocked(u)
            s.discard(mid)
            u["modes"] = ",".join(map(str, sorted(s)))
            if u["mode"] == mid:
                u["mode"] = 1
        save_user(cur, u)


R_UID, R_PICK, R_CONF = range(3)


def _user_payments(uid):
    with cursor() as cur:
        cur.execute("SELECT name FROM m_users WHERE id=?", (uid,))
        u = cur.fetchone()
        cur.execute("SELECT * FROM m_payments WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (uid,))
        return (u or {}).get("name") or "—", cur.fetchall()


async def _do_refund(bot, p):
    cur_p = await asyncio.to_thread(_find_payment, p["charge_id"])
    if not cur_p:
        return "❌ الدفعة غير موجودة في القاعدة."
    if cur_p["refunded"]:
        return "ℹ️ هذه الدفعة مُرجعة مسبقاً."
    try:
        await bot.refund_star_payment(user_id=int(p["user_id"]), telegram_payment_charge_id=p["charge_id"])
    except Exception as e:
        return f"❌ فشل الاسترجاع: {html.escape(str(e))}"
    await asyncio.to_thread(_mark_refunded, p)
    return f"💸 تم إرجاع ⭐ {p['stars']} للمستخدم <code>{p['user_id']}</code> وسحب أثر ({p['item']})."


async def _refund_show(update, context, uid):
    name, rows = await asyncio.to_thread(_user_payments, uid)
    if not rows:
        await update.message.reply_text("لا توجد دفعات لهذا المستخدم.")
        return ConversationHandler.END
    context.user_data["rf"] = rows
    lines = [f"{i}) {'↩️ مُرجعة' if r['refunded'] else '✅'} {r['item']} · ⭐ {r['stars']} · "
             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created_at'] or 0))}"
             for i, r in enumerate(rows, 1)]
    await update.message.reply_text(f"👤 {html.escape(name)} (<code>{uid}</code>)\n\n" + "\n".join(lines) +
                                    "\n\nأرسل <b>رقم</b> الدفعة التي تريد استرجاعها، أو /cancel")
    return R_PICK


async def refund_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/refund ← يسأل عن الايدي ← يعرض الدفعات مرقمة ← رقم ← تأكيد"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    context.user_data.pop("rf", None)
    context.user_data.pop("rf_sel", None)
    if context.args:
        a0 = context.args[0]
        if a0.isdigit():
            return await _refund_show(update, context, int(a0))
        p = await asyncio.to_thread(_find_payment, a0)          # /refund charge_id (مباشر)
        if not p:
            await update.message.reply_text("❌ الدفعة غير موجودة.")
            return ConversationHandler.END
        await update.message.reply_text(await _do_refund(context.bot, p))
        return ConversationHandler.END
    await update.message.reply_text("💸 أرسل <b>ايدي المستخدم</b> (أرقام فقط)، أو /cancel")
    return R_UID


async def refund_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if not t.isdigit():
        await update.message.reply_text("⚠️ الايدي أرقام فقط. أعد المحاولة أو /cancel")
        return R_UID
    return await _refund_show(update, context, int(t))


async def refund_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = context.user_data.get("rf") or []
    t = (update.message.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= len(rows):
        await update.message.reply_text(f"⚠️ أرسل رقماً من 1 إلى {len(rows)}، أو /cancel")
        return R_PICK
    p = rows[int(t) - 1]
    if p["refunded"]:
        await update.message.reply_text("ℹ️ هذه الدفعة مُرجعة مسبقاً، اختر رقماً آخر.")
        return R_PICK
    context.user_data["rf_sel"] = p
    await update.message.reply_text(
        f"⚠️ سيتم إرجاع <b>⭐ {p['stars']}</b> ({p['item']}) للمستخدم <code>{p['user_id']}</code>.\n\n"
        "أرسل كلمة <b>تأكيد</b> للتنفيذ، أو /cancel للإلغاء.")
    return R_CONF


async def refund_conf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    p = context.user_data.get("rf_sel")
    if p and t in ("تأكيد", "تاكيد", "confirm", "yes", "نعم"):
        await update.message.reply_text(await _do_refund(context.bot, p))
    else:
        await update.message.reply_text("تم الإلغاء.")
    context.user_data.pop("rf", None)
    context.user_data.pop("rf_sel", None)
    return ConversationHandler.END


async def refund_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("rf", None)
    context.user_data.pop("rf_sel", None)
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/payments [user_id]  ← آخر 15 دفعة مع charge_id"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return

    def rows():
        with cursor() as cur:
            if context.args and context.args[0].isdigit():
                cur.execute("SELECT * FROM m_payments WHERE user_id=? ORDER BY created_at DESC LIMIT 15",
                            (int(context.args[0]),))
            else:
                cur.execute("SELECT * FROM m_payments ORDER BY created_at DESC LIMIT 15")
            return cur.fetchall()
    rs = await asyncio.to_thread(rows)
    if not rs:
        await update.message.reply_text("لا توجد دفعات.")
        return
    await update.message.reply_text("\n\n".join(
        f"{'↩️' if r['refunded'] else '✅'} {r['item']} · ⭐ {r['stars']} · 👤 <code>{r['user_id']}</code>\n"
        f"<code>/refund {html.escape(r['charge_id'])}</code>" for r in rs))


async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gencode <energy|hash|permit|mode> <المقدار أو الوضع> [عدد_الاستخدامات] [كود_مخصص]"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    a = context.args
    if len(a) < 2 or a[0].lower() not in CODE_KINDS:
        modes_list = " · ".join(f"{k}={m['name']}" for k, m in MODES.items())
        await update.message.reply_text(
            "الاستعمال:\n<code>/gencode energy 10k 100</code> ← طاقة 10K لأول 100 شخص\n"
            "<code>/gencode hash 100k 50 MYCODE</code> ← نقاط بكود مخصص\n"
            "<code>/gencode permit 0 20</code> ← تصريح تعدين لأول 20 شخص\n"
            "<code>/gencode mode 2 10</code> ← فتح وضع تيربو لأول 10 أشخاص\n"
            "<code>/gencode mode turbo 10 MYCODE</code> ← بالاسم وبكود مخصص\n\n"
            f"🚀 الأوضاع: {modes_list}\n"
            "(يقبل الرقم أو الاسم: normal, turbo, super, nitro, plasma, quasar, pulsar, singularity أو بالعربي)\n\n"
            "/codes لعرض الأكواد · /delcode CODE للحذف")
        return
    kind = a[0].lower()
    try:
        if kind == "mode":
            amount = resolve_mode(a[1])
            if amount is None:
                await update.message.reply_text("❌ وضع غير معروف. أرسل /gencode لعرض الأوضاع.")
                return
        else:
            amount = parse_amt(a[1])
        max_uses = int(a[2]) if len(a) > 2 else 1
    except ValueError:
        await update.message.reply_text("قيمة غير صحيحة")
        return
    code = (a[3].upper() if len(a) > 3 else "TH-" + secrets.token_hex(4).upper())[:32]

    def add():
        with cursor() as cur:
            cur.execute("INSERT INTO m_codes(code,kind,amount,max_uses,created_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT DO NOTHING", (code, kind, amount, max_uses, int(time.time())))
            return cur.rowcount == 1
    if not await asyncio.to_thread(add):
        await update.message.reply_text("هذا الكود موجود مسبقاً")
        return
    prize = prize_text(kind, amount)
    await update.message.reply_text(
        f"✅ تم إنشاء الكود:\n<code>{html.escape(code)}</code>\n"
        f"🎁 {prize} · 👥 {max_uses} استخدام\n\nانسخ هذا للقناة:\n"
        f"🎁 كود جديد: <code>{html.escape(code)}</code>\n"
        f"أدخله في التطبيق ← المحفظة، أو أرسل <code>/redeem {html.escape(code)}</code>")


async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settitle <العنوان الجديد>   |   /settitle reset  ← يرجع الافتراضي"""
    global TITLE
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            f"✏️ العنوان الحالي: <b>{html.escape(TITLE)}</b>\n\nلتغييره:\n<code>/settitle اسم جديد</code>\n"
            f"للعودة للافتراضي ({html.escape(DEFAULT_TITLE)}):\n<code>/settitle reset</code>")
        return
    new = parts[1].strip()
    if new.lower() in ("reset", "default", "افتراضي"):
        new = DEFAULT_TITLE
    new = new[:40]
    if not new:
        return

    def save():
        with cursor() as cur:
            cur.execute("INSERT INTO m_settings(k,v) VALUES('title',?) ON CONFLICT (k) DO UPDATE SET v=excluded.v",
                        (new,))
    await asyncio.to_thread(save)
    TITLE = new
    await update.message.reply_text(f"✅ تم تغيير العنوان إلى: <b>{html.escape(new)}</b>\nيظهر في مقدمة التطبيق عند الفتح القادم.")


async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setstart 12.00 11.11.2026   |   /setstart now   |   /setstart off"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    a = context.args
    if not a:
        await update.message.reply_text(
            f"🚀 موعد بدء التعدين الحالي: <b>{fmt_dt(CHAIN['start'])}</b> (UTC{TZ_OFFSET:+g})\n\n"
            "لتغييره:\n<code>/setstart 12.00 11.11.2026</code>\nللبدء فوراً:\n<code>/setstart now</code>\n"
            "لإيقاف التعدين وإلغاء الموعد:\n<code>/setstart off</code>")
        return
    try:
        if a[0].lower() == "now":
            ts = time.time() - 1
        elif a[0].lower() in ("off", "stop", "إيقاف"):
            ts = NOT_SCHEDULED
        else:
            ts = parse_start(a)
    except (ValueError, OverflowError):
        await update.message.reply_text("❌ صيغة غير صحيحة. مثال: <code>/setstart 12.00 11.11.2026</code>")
        return
    await asyncio.to_thread(set_setting, "start_ts", ts)
    CHAIN["start"] = ts
    if ts >= NOT_SCHEDULED:
        await update.message.reply_text("⏸ تم إلغاء الموعد. التعدين متوقف حتى تحدد موعداً جديداً.")
        return
    left = ts - time.time()
    note = "التعدين يعمل الآن ✅" if left <= 0 else f"بعد {int(left // 86400)} يوم و{int(left % 86400 // 3600)} ساعة"
    await update.message.reply_text(f"✅ موعد البدء: <b>{fmt_dt(ts)}</b>\n{note}")


async def cmd_resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetall  ← يشرح ما سيُحذف   |   /resetall تأكيد  ← ينفّذ"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    if not context.args or context.args[0].lower() not in ("تأكيد", "تاكيد", "confirm"):
        await update.message.reply_text(
            "⚠️ <b>سيتم حذف كل بيانات اللعبة:</b>\nالمستخدمون وأرصدتهم، الكتل، الخوادم، المهام، "
            "الأكواد واستردادها، وتُلغى بداية التعدين.\n\n"
            "✅ لا يُحذف: سجل الدفعات (لتبقى قادراً على الاسترجاع)، العنوان، وعدد الكتل.\n\n"
            "للتنفيذ أرسل:\n<code>/resetall تأكيد</code>")
        return

    def wipe():
        with cursor() as cur:
            for t in ("m_users", "m_tasks", "m_servers", "m_redeems", "m_codes", "m_block_rewards", "m_week_top"):
                cur.execute(f"DELETE FROM {t}")
            if USE_PG:
                cur.execute("TRUNCATE m_blocks RESTART IDENTITY")
            else:
                cur.execute("DELETE FROM m_blocks")
                cur.execute("DELETE FROM sqlite_sequence WHERE name='m_blocks'")
        set_setting("start_ts", NOT_SCHEDULED)
        CHAIN["start"], CHAIN["done"] = NOT_SCHEDULED, False
        BANNED.clear()
    await asyncio.to_thread(wipe)
    await update.message.reply_text(
        "🗑 تم حذف كل شيء. اللعبة الآن في حالة «لم تبدأ».\n\nلتحديد الموعد: <code>/setstart 12.00 11.11.2026</code>\n"
        "أو للبدء فوراً: <code>/setstart now</code>")


async def cmd_setblocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setblocks 100000   (يقبل K و M)"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            f"🧱 عدد الكتل الحالي: <b>{CHAIN['blocks']:,}</b>\nالإصدار = الكتل × {BLOCK_REWARD}\n\n"
            "لتغييره: <code>/setblocks 100000</code> أو <code>/setblocks 5m</code>")
        return
    try:
        n = int(parse_amt(context.args[0]))
    except ValueError:
        await update.message.reply_text("❌ قيمة غير صحيحة")
        return
    if n < 1:
        await update.message.reply_text("❌ يجب أن يكون الرقم 1 أو أكثر")
        return

    def work():
        set_setting("blocks_total", n)
        CHAIN["blocks"] = n
        m = blocks_mined()
        CHAIN["done"] = m >= n
        return m
    m = await asyncio.to_thread(work)
    await update.message.reply_text(
        f"✅ عدد الكتل: <b>{n:,}</b>\n💎 الإصدار: {n * BLOCK_REWARD:,}\n⛏ المُعدَّن حتى الآن: {m:,}"
        + ("\n🏁 الكتل اكتملت، التعدين متوقف." if CHAIN["done"] else ""))


async def cmd_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    m = await asyncio.to_thread(blocks_mined)
    st = "🏁 اكتملت" if CHAIN["done"] else ("⏳ لم تبدأ" if time.time() < CHAIN["start"] else "✅ تعمل")
    await update.message.reply_text(
        f"⛓ <b>حالة السلسلة</b>: {st}\n🚀 البداية: {fmt_dt(CHAIN['start'])}\n"
        f"🧱 الكتل: {m:,} / {CHAIN['blocks']:,} ({m / CHAIN['blocks'] * 100:.2f}%)\n"
        f"💎 الإصدار: {CHAIN['blocks'] * BLOCK_REWARD:,}\n\n"
        "/setstart · /setblocks")


async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return

    def rows():
        with cursor() as cur:
            cur.execute("SELECT * FROM m_codes ORDER BY created_at DESC LIMIT 30")
            return cur.fetchall()
    rs = await asyncio.to_thread(rows)
    if not rs:
        await update.message.reply_text("لا توجد أكواد.")
        return
    await update.message.reply_text("\n".join(
        f"<code>{html.escape(r['code'])}</code> · "
        + (f"mode {html.escape(prize_text('mode', r['amount']))}" if r['kind'] == 'mode'
           else f"{r['kind']} {fmt_amt(r['amount'])}")
        + f" · {r['uses']}/{r['max_uses']}"
        for r in rs))


async def cmd_delcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID or not context.args:
        return
    code = context.args[0].upper()

    def rm():
        with cursor() as cur:
            cur.execute("DELETE FROM m_codes WHERE code=?", (code,))
            return cur.rowcount
    n = await asyncio.to_thread(rm)
    await update.message.reply_text("🗑 تم الحذف" if n else "الكود غير موجود")


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستعمال: <code>/redeem CODE</code>")
        return
    u = update.effective_user
    if u.id in BANNED:
        await update.message.reply_text("⛔ تم حظر حسابك.")
        return
    tg = dict(id=u.id, name=u.full_name, username=u.username or "", ref=None)
    try:
        msg = await asyncio.to_thread(redeem_code, tg, context.args[0])
        await update.message.reply_text("✅ " + msg)
    except Err as e:
        await update.message.reply_text("❌ " + html.escape(str(e)))


# ═══════════ 5.1) الحظر · البث · لوحة الإحصائيات (للأدمن) ═══════════
def _is_admin(update):
    return bool(ADMIN_ID) and update.effective_user and update.effective_user.id == ADMIN_ID


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban <id> [السبب]  ← يحظر المستخدم؛ تظهر له نافذة الحظر في التطبيق"""
    if not _is_admin(update):
        return
    a = context.args
    if not a or not a[0].isdigit():
        await update.message.reply_text("الاستعمال:\n<code>/ban 123456789 سبب الحظر</code>\n"
                                        "لإلغاء الحظر: <code>/unban 123456789</code> · القائمة: /bans")
        return
    uid = int(a[0])
    if uid == ADMIN_ID:
        await update.message.reply_text("❌ لا يمكنك حظر نفسك.")
        return
    reason = " ".join(a[1:])[:200]

    def work():
        with cursor() as cur:
            cur.execute("UPDATE m_users SET banned=1, ban_reason=?, mining=0 WHERE id=?", (reason, uid))
            if cur.rowcount != 1:
                return None
            cur.execute("SELECT name FROM m_users WHERE id=?", (uid,))
            return (cur.fetchone() or {}).get("name") or "—"
    name = await asyncio.to_thread(work)
    if name is None:
        await update.message.reply_text("❌ المستخدم غير موجود في القاعدة (لم يفتح اللعبة بعد).")
        return
    BANNED[uid] = reason
    await update.message.reply_text(f"⛔ تم حظر {html.escape(name)} (<code>{uid}</code>)"
                                    + (f"\nالسبب: {html.escape(reason)}" if reason else "")
                                    + f"\n\nلإلغاء الحظر: <code>/unban {uid}</code>")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban <id>  ← يلغي الحظر"""
    if not _is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستعمال: <code>/unban 123456789</code>")
        return
    uid = int(context.args[0])

    def work():
        with cursor() as cur:
            cur.execute("UPDATE m_users SET banned=0, ban_reason='' WHERE id=?", (uid,))
            return cur.rowcount
    n = await asyncio.to_thread(work)
    BANNED.pop(uid, None)
    await update.message.reply_text(f"✅ تم إلغاء حظر <code>{uid}</code>" if n else "❌ المستخدم غير موجود.")


async def cmd_bans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bans  ← قائمة المحظورين"""
    if not _is_admin(update):
        return

    def rows():
        with cursor() as cur:
            cur.execute("SELECT id,name,ban_reason FROM m_users WHERE banned=1 ORDER BY id LIMIT 50")
            return cur.fetchall()
    rs = await asyncio.to_thread(rows)
    if not rs:
        await update.message.reply_text("لا يوجد محظورون.")
        return
    await update.message.reply_text("⛔ <b>المحظورون</b>\n\n" + "\n".join(
        f"• {html.escape(r['name'] or '—')} · <code>{r['id']}</code>"
        + (f" · {html.escape(r['ban_reason'])}" if r["ban_reason"] else "")
        + f"\n   <code>/unban {r['id']}</code>" for r in rs))


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast <الرسالة>  ← معاينة ثم /bcconfirm للإرسال لكل اللاعبين"""
    if not _is_admin(update):
        return
    parts = (update.message.text_html or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "📢 <b>البث</b>\n\nاكتب الرسالة بعد الأمر:\n<code>/broadcast نص الرسالة هنا</code>\n\n"
            "ستظهر لك معاينة، ثم أرسل /bcconfirm للإرسال أو /bccancel للإلغاء.\n"
            "(يدعم التنسيق: عريض، مائل، روابط…)")
        return
    txt = parts[1].strip()
    context.bot_data["bc"] = txt

    def count():
        with cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM m_users WHERE banned=0")
            return cur.fetchone()["n"]
    n = await asyncio.to_thread(count)
    try:
        await update.message.reply_text(f"📢 <b>معاينة الرسالة:</b>\n\n{txt}")
    except Exception as e:
        context.bot_data.pop("bc", None)
        await update.message.reply_text(f"❌ تعذّر عرض الرسالة: {html.escape(str(e))}")
        return
    await update.message.reply_text(f"👥 عدد المستلمين: <b>{n:,}</b>\n\nللتأكيد: /bcconfirm\nللإلغاء: /bccancel")


async def cmd_bccancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    context.bot_data.pop("bc", None)
    await update.message.reply_text("تم إلغاء البث.")


async def _broadcast(bot, txt, ids, admin_chat):
    sent = failed = 0
    kb = (InlineKeyboardMarkup([[InlineKeyboardButton("⛏ افتح التعدين", web_app=WebAppInfo(url=APP_URL))]])
          if WEBAPP_URL else None)
    for uid in ids:
        for attempt in range(2):
            try:
                await bot.send_message(uid, txt, reply_markup=kb)
                sent += 1
                break
            except RetryAfter as e:
                ra = e.retry_after
                ra = ra.total_seconds() if hasattr(ra, "total_seconds") else ra
                await asyncio.sleep(float(ra) + 1)
            except Exception:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)           # ~20 رسالة/ثانية (الحد المسموح 30)
    try:
        await bot.send_message(admin_chat, f"✅ انتهى البث\n📨 وصلت: <b>{sent:,}</b>\n🚫 فشلت (حظروا البوت أو غيره): {failed:,}")
    except Exception:
        pass


async def cmd_bcconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    txt = context.bot_data.pop("bc", None)
    if not txt:
        await update.message.reply_text("لا توجد رسالة معلّقة. استخدم /broadcast أولاً.")
        return

    def ids():
        with cursor() as cur:
            cur.execute("SELECT id FROM m_users WHERE banned=0 ORDER BY id")
            return [r["id"] for r in cur.fetchall()]
    lst = await asyncio.to_thread(ids)
    await update.message.reply_text(f"🚀 بدأ الإرسال إلى {len(lst):,} مستخدم... سأبلغك عند الانتهاء.")
    BG.append(asyncio.create_task(_broadcast(context.bot, txt, lst, update.effective_chat.id)))


def build_stats():
    now = time.time()
    off = TZ_OFFSET * 3600
    today = ((now + off) // 86400) * 86400 - off
    week = now - 7 * 86400
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(balance),0) AS bal, COALESCE(SUM(total),0) AS tot,"
                    " SUM(CASE WHEN permit=1 THEN 1 ELSE 0 END) AS permits,"
                    " SUM(CASE WHEN banned=1 THEN 1 ELSE 0 END) AS bans,"
                    " SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS new_today,"
                    " SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS new_week,"
                    " SUM(CASE WHEN last_tick>=? THEN 1 ELSE 0 END) AS a1h,"
                    " SUM(CASE WHEN last_tick>=? THEN 1 ELSE 0 END) AS a24,"
                    " SUM(CASE WHEN referrer IS NOT NULL THEN 1 ELSE 0 END) AS reffed FROM m_users",
                    (today, week, now - 3600, now - 86400))
        u = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(hr),0) AS h FROM m_users WHERE " + SQL_ACTIVE, (now,))
        act = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS s, COUNT(DISTINCT user_id) AS payers "
                    "FROM m_payments WHERE refunded=0")
        pay = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(stars),0) AS s FROM m_payments WHERE refunded=0 AND created_at>=?", (today,))
        pay_today = cur.fetchone()["s"]
        cur.execute("SELECT COALESCE(SUM(stars),0) AS s FROM m_payments WHERE refunded=0 AND created_at>=?", (week,))
        pay_week = cur.fetchone()["s"]
        cur.execute("SELECT COALESCE(SUM(stars),0) AS s FROM m_payments WHERE refunded=1")
        refunded = cur.fetchone()["s"]
        cur.execute("SELECT item, COUNT(*) AS n, SUM(stars) AS s FROM m_payments WHERE refunded=0 "
                    "GROUP BY item ORDER BY s DESC LIMIT 6")
        items = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM m_servers WHERE expires=0")
        servers = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM m_codes")
        codes = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM m_redeems")
        redeems = cur.fetchone()["n"]
        cur.execute("SELECT name,level,total FROM m_users WHERE banned=0 ORDER BY total DESC LIMIT 5")
        top = cur.fetchall()
    mined = blocks_mined()
    I = lambda x: int(x or 0)
    st = "🏁 اكتملت" if CHAIN["done"] else ("⏳ لم تبدأ" if now < CHAIN["start"] else "✅ تعمل")
    lines = [
        "📊 <b>لوحة الإحصائيات</b>",
        "",
        "👥 <b>اللاعبون</b>",
        f"• الإجمالي: <b>{I(u['n']):,}</b> (لديهم تصريح: {I(u['permits']):,})",
        f"• جدد اليوم: {I(u['new_today']):,} · خلال 7 أيام: {I(u['new_week']):,}",
        f"• نشطون آخر ساعة: {I(u['a1h']):,} · آخر 24 ساعة: {I(u['a24']):,}",
        f"• معدّنون الآن: <b>{I(act['n']):,}</b> · هاش الشبكة: {float(act['h']):,.0f}",
        f"• جاؤوا بالإحالة: {I(u['reffed']):,} · محظورون: {I(u['bans']):,}",
        "",
        "⛓ <b>السلسلة</b>",
        f"• الحالة: {st} · البداية: {fmt_dt(CHAIN['start'])}",
        f"• الكتل: {mined:,} / {CHAIN['blocks']:,} ({mined / CHAIN['blocks'] * 100:.2f}%)",
        "",
        "💰 <b>الاقتصاد</b>",
        f"• أرصدة اللاعبين الآن: {float(u['bal']):,.0f}",
        f"• إجمالي ما عُدِّن: {float(u['tot']):,.0f}",
        f"• خوادم مشتراة: {servers:,} · أكواد: {codes:,} (استُرد {redeems:,})",
        "",
        "⭐ <b>الإيرادات (نجوم)</b>",
        f"• الإجمالي: <b>{I(pay['s']):,}</b> من {I(pay['n']):,} عملية ({I(pay['payers']):,} دافع)",
        f"• اليوم: {I(pay_today):,} · 7 أيام: {I(pay_week):,} · مُسترجع: {I(refunded):,}",
    ]
    if items:
        lines.append("• الأكثر مبيعاً: " + " · ".join(f"{html.escape(r['item'])} ({I(r['s']):,}⭐ ×{I(r['n'])})" for r in items))
    if top:
        lines += ["", "🏆 <b>أفضل 5</b>"]
        lines += [f"{i}. {html.escape(r['name'] or '—')} · مستوى {r['level']} · {float(r['total']):,.0f}"
                  for i, r in enumerate(top, 1)]
    lines += ["", f"🕒 {_local(now).strftime('%H:%M:%S')} · 🗄 {'PostgreSQL' if USE_PG else 'SQLite'}"]
    return "\n".join(lines)


STATS_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث", callback_data="stats")]])


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats  ← لوحة إحصائيات الأدمن"""
    if not _is_admin(update):
        return
    await update.message.reply_text(await asyncio.to_thread(build_stats), reply_markup=STATS_KB)


async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not ADMIN_ID or q.from_user.id != ADMIN_ID:
        await q.answer()
        return
    await q.answer("تم التحديث")
    try:
        await q.edit_message_text(await asyncio.to_thread(build_stats), reply_markup=STATS_KB)
    except BadRequest:
        pass


def _fmt_left(sec):
    sec = max(0, int(sec))
    return f"{sec // 86400} يوم و{sec % 86400 // 3600} ساعة"


def _last_week_items():
    with cursor() as cur:
        cur.execute("SELECT MAX(week_id) AS m FROM m_week_top")
        wid = (cur.fetchone() or {}).get("m")
        if wid is None:
            return None, []
        cur.execute("SELECT pos,user_id,name,amount FROM m_week_top WHERE week_id=? ORDER BY pos", (wid,))
        return wid, [dict(pos=r["pos"], uid=r["user_id"], name=r["name"], amount=r["amount"]) for r in cur.fetchall()]


def _live_week_items():
    return [dict(pos=i, uid=r["id"], name=r["name"], amount=r["w"]) for i, r in enumerate(week_top_rows(100), 1)]


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/weekly = ترتيب الأسبوع الحالي · /weekly last = ترتيب آخر أسبوع منتهٍ"""
    if not _is_admin(update):
        return
    last = bool(context.args and context.args[0].lower() == "last")
    if last:
        wid, items = await asyncio.to_thread(_last_week_items)
        if wid is None:
            await update.message.reply_text("لا يوجد أسبوع منتهٍ محفوظ بعد.")
            return
        st = wid * 7 * 86400 + 4 * 86400 - TZ_OFFSET * 3600
        head = f"🏁 <b>آخر أسبوع منتهٍ</b> ({fmt_d(st)} ← {fmt_d(st + 7 * 86400 - 1)})"
    else:
        items = await asyncio.to_thread(_live_week_items)
        wid, st, en = week_bounds()
        head = (f"📅 <b>الأسبوع الحالي</b> ({fmt_d(st)} ← {fmt_d(en - 1)})\n"
                f"⏳ ينتهي بعد {_fmt_left(en - time.time())}")
    lines = [head, ""]
    lines += [f"{i['pos']}. {html.escape(i['name'] or '—')} · <code>{i['uid']}</code> · {float(i['amount']):,.0f}"
              for i in items[:25]] or ["لا أحد بعد."]
    if len(items) > 25:
        lines.append(f"… و{len(items) - 25} آخرين (حتى {len(items)})")
    lines += ["", "لإرسال تهنئة لهم: /weeksend"]
    await update.message.reply_text("\n".join(lines))


WEEK_SUFFIX = " {name}! مركزك {rank} هذا الأسبوع بـ {amount} نقطة 🎉"
WEEK_DEFAULT_MSG = "مبروك {name}! مركزك {rank} هذا الأسبوع بـ {amount} نقطة 🎉"


def _pers(text, it):
    return (text.replace("{rank}", str(it["pos"])).replace("{name}", html.escape(it["name"] or ""))
            .replace("{amount}", f"{float(it['amount']):,.0f}"))


async def cmd_weeksend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/weeksend <الرسالة> ← لأفضل 100 في آخر أسبوع منتهٍ · /weeksend now <الرسالة> ← للأسبوع الجاري"""
    if not _is_admin(update):
        return
    parts = (update.message.text_html or "").split(None, 1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    live = False
    tok = rest.split(None, 1)
    if tok and tok[0].lower() in ("now", "new", "الان", "الآن"):      # للأسبوع الجاري
        live = True
        rest = tok[1].strip() if len(tok) > 1 else ""
        if not rest:
            rest = WEEK_DEFAULT_MSG
    if rest and not any(v in rest for v in ("{name}", "{rank}", "{amount}")):
        # نص بدون متغيرات: نُلحق به الاسم والمركز والنقاط تلقائياً لكل لاعب
        rest = rest + WEEK_SUFFIX
    if not rest:
        await update.message.reply_text(
            "🏆 <b>تهنئة أفضل 100 في الترتيب الأسبوعي</b>\n\n"
            "<code>/weeksend مبروك {name}! مركزك {rank} هذا الأسبوع بـ {amount} نقطة 🎉</code>\n"
            "← يُرسل لأفضل 100 في آخر أسبوع منتهٍ\n\n"
            "<code>/weeksend now مبروك</code> ← للأسبوع الجاري؛ يُضاف الاسم والمركز والنقاط تلقائياً\n"
            "<code>/weeksend now</code> ← للأسبوع الجاري بالرسالة الجاهزة\n\n"
            "المتغيرات (اختيارية): <code>{name}</code> الاسم · <code>{rank}</code> المركز · <code>{amount}</code> النقاط\n"
            "ستظهر معاينة ثم أرسل /wkconfirm للإرسال أو /wkcancel للإلغاء.")
        return
    if live:
        items, label = await asyncio.to_thread(_live_week_items), "الأسبوع الجاري"
    else:
        wid, items = await asyncio.to_thread(_last_week_items)
        label = "آخر أسبوع منتهٍ"
        if wid is None:
            await update.message.reply_text("لا يوجد أسبوع منتهٍ محفوظ بعد. للأسبوع الجاري استخدم:\n"
                                            "<code>/weeksend now نص الرسالة</code>")
            return
    if not items:
        await update.message.reply_text("لا يوجد لاعبون في هذا الترتيب.")
        return
    context.bot_data["wk"] = dict(text=rest, items=items)
    try:
        await update.message.reply_text("🏆 <b>معاينة</b> (كما ستصل للمركز الأول):\n\n" + _pers(rest, items[0]))
    except Exception as e:
        context.bot_data.pop("wk", None)
        await update.message.reply_text(f"❌ تعذّر عرض الرسالة: {html.escape(str(e))}")
        return
    await update.message.reply_text(f"👥 المستلمون: <b>{len(items)}</b> ({label})\n\n"
                                    "للتأكيد: /wkconfirm\nللإلغاء: /wkcancel")


async def cmd_wkcancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    context.bot_data.pop("wk", None)
    await update.message.reply_text("تم الإلغاء.")


async def _send_many(bot, pairs, admin_chat, label=""):
    sent = failed = 0
    kb = (InlineKeyboardMarkup([[InlineKeyboardButton("⛏ افتح التعدين", web_app=WebAppInfo(url=APP_URL))]])
          if WEBAPP_URL else None)
    for uid, txt in pairs:
        for attempt in range(2):
            try:
                await bot.send_message(uid, txt, reply_markup=kb)
                sent += 1
                break
            except RetryAfter as e:
                ra = e.retry_after
                ra = ra.total_seconds() if hasattr(ra, "total_seconds") else ra
                await asyncio.sleep(float(ra) + 1)
            except Exception:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_chat, f"✅ انتهى الإرسال{label}\n📨 وصلت: <b>{sent}</b>\n🚫 فشلت: {failed}")
    except Exception:
        pass


async def cmd_wkconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    d = context.bot_data.pop("wk", None)
    if not d:
        await update.message.reply_text("لا توجد رسالة معلّقة. استخدم /weeksend أولاً.")
        return
    pairs = [(i["uid"], _pers(d["text"], i)) for i in d["items"]]
    await update.message.reply_text(f"🚀 بدأ الإرسال إلى {len(pairs)} لاعب... سأبلغك عند الانتهاء.")
    BG.append(asyncio.create_task(_send_many(context.bot, pairs, update.effective_chat.id)))


# ═══════════ 5.2) الرسائل المجدولة (تُرسل تلقائياً كل يوم في ساعة محددة) ═══════════
DAY_NAMES_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
DAY_ALIASES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
               "الاثنين": 0, "الإثنين": 0, "الثلاثاء": 1, "الاربعاء": 2, "الأربعاء": 2,
               "الخميس": 3, "الجمعة": 4, "السبت": 5, "الأحد": 6, "الاحد": 6}
SCHED_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s+(.+)$", re.S)


def _days_txt(days):
    return "يومياً" if not days else "، ".join(DAY_NAMES_AR[int(x)] for x in days.split(",") if x != "")


def _parse_sched(chunk):
    """'20:00 نص' أو '20:00 mon,thu نص' ← (دقائق_من_منتصف_الليل, أيام, نص) أو None"""
    m = SCHED_TIME_RE.match(chunk)
    if not m:
        return None
    h, mi, rest = int(m.group(1)), int(m.group(2)), m.group(3).strip()
    if not (0 <= h < 24 and 0 <= mi < 60) or not rest:
        return None
    days = ""
    tok = rest.split(None, 1)
    first = tok[0].lower().strip(",")
    if len(tok) > 1:
        if first in ("daily", "يومي", "يومياً", "كل"):
            rest = tok[1].strip()
        else:
            ds = [x for x in first.split(",") if x]
            if ds and all(x in DAY_ALIASES for x in ds):
                days = ",".join(str(i) for i in sorted({DAY_ALIASES[x] for x in ds}))
                rest = tok[1].strip()
    if not rest:
        return None
    return h * 60 + mi, days, rest


def _plain(t, n=60):
    return html.escape(re.sub(r"<[^>]+>", "", t).replace("\n", " ")[:n])


async def cmd_addsched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addsched 20:00 نص  ← رسالة يومية · /addsched 20:00 mon,thu نص ← أيام محددة · أو عدة أسطر دفعة واحدة"""
    if not _is_admin(update):
        return
    parts = (update.message.text_html or "").split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body:
        await update.message.reply_text(
            "⏰ <b>الرسائل المجدولة</b>\n\n"
            "رسالة يومية في ساعة محددة:\n<code>/addsched 20:00 لا تنسَ مكافأتك اليومية 🎁</code>\n\n"
            "أيام محددة فقط:\n<code>/addsched 20:00 mon,thu نص الرسالة</code>\n"
            "(أو بالعربي: الاثنين,الخميس · الأيام: mon tue wed thu fri sat sun)\n\n"
            "عدة رسائل دفعة واحدة (كل سطر: الوقت ثم النص):\n"
            "<code>/addsched\n09:00 صباح الخير {name}! ⛏\n21:00 لا تنسَ مكافأتك اليومية 🎁</code>\n\n"
            f"الوقت بتوقيت UTC{TZ_OFFSET:+g}. يمكنك استعمال <code>{{name}}</code> و<code>{{level}}</code> في النص.\n\n"
            "/schedules لعرضها · /delsched ID للحذف · /togglesched ID للإيقاف/التشغيل · /testsched ID لتجربتها عندك")
        return
    lines = [l for l in body.split("\n") if l.strip()]
    chunks = lines if (len(lines) > 1 and all(SCHED_TIME_RE.match(l) for l in lines)) else [body]
    items, bad = [], 0
    for ch in chunks:
        it = _parse_sched(ch)
        if not it:
            bad += 1
            continue
        try:                                     # معاينة (وتتحقق أن التنسيق صالح)
            await update.message.reply_text(
                f"👁 معاينة ({it[0] // 60:02d}:{it[0] % 60:02d}):\n\n"
                + it[2].replace("{name}", html.escape(update.effective_user.full_name))
                .replace("{level}", "1"))
        except Exception as e:
            await update.message.reply_text(f"❌ تعذّر قبول رسالة ({html.escape(str(e))})")
            bad += 1
            continue
        items.append(it)
    if not items:
        await update.message.reply_text("❌ صيغة غير صحيحة. مثال:\n<code>/addsched 20:00 نص الرسالة</code>")
        return

    def save():
        now = time.time()
        lt = _local(now)
        today, cm = lt.strftime("%Y-%m-%d"), lt.hour * 60 + lt.minute
        ids = []
        with cursor() as cur:
            for hhmm, days, text in items:
                # لو كان الوقت قد مرّ اليوم، يبدأ الإرسال من الغد
                cur.execute("INSERT INTO m_sched(hhmm,days,text,active,last_run,created_at) VALUES(?,?,?,?,?,?)",
                            (hhmm, days, text, 1, today if cm >= hhmm else "", int(now)))
                cur.execute("SELECT MAX(id) AS m FROM m_sched")
                ids.append(cur.fetchone()["m"])
        return ids
    ids = await asyncio.to_thread(save)
    out = [f"✅ تمت جدولة {len(ids)} رسالة:"]
    out += [f"#{i} · {h // 60:02d}:{h % 60:02d} · {_days_txt(d)}" for i, (h, d, _) in zip(ids, items)]
    if bad:
        out.append(f"\n⚠️ تم تجاهل {bad} سطر بصيغة غير صحيحة.")
    out.append("\nستُرسل لكل اللاعبين تلقائياً. /schedules لعرضها.")
    await update.message.reply_text("\n".join(out))


def _all_sched():
    with cursor() as cur:
        cur.execute("SELECT * FROM m_sched ORDER BY hhmm, id")
        return cur.fetchall()


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    rs = await asyncio.to_thread(_all_sched)
    if not rs:
        await update.message.reply_text("لا توجد رسائل مجدولة.\nأضف واحدة: <code>/addsched 20:00 نص الرسالة</code>")
        return
    lt = _local(time.time())
    out = [f"⏰ <b>الرسائل المجدولة</b> (الآن {lt.strftime('%H:%M')} UTC{TZ_OFFSET:+g})", ""]
    for r in rs:
        h = r["hhmm"]
        out.append(f"{'✅' if r['active'] else '⏸'} <b>#{r['id']}</b> · {h // 60:02d}:{h % 60:02d} · {_days_txt(r['days'])}"
                   f"{' · آخر إرسال ' + r['last_run'] if r['last_run'] else ''}\n"
                   f"   {_plain(r['text'])}\n"
                   f"   /delsched {r['id']} · /togglesched {r['id']} · /testsched {r['id']}")
    await update.message.reply_text("\n".join(out))


async def cmd_delsched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستعمال: <code>/delsched 1</code>")
        return
    sid = int(context.args[0])

    def rm():
        with cursor() as cur:
            cur.execute("DELETE FROM m_sched WHERE id=?", (sid,))
            return cur.rowcount
    await update.message.reply_text("🗑 تم الحذف" if await asyncio.to_thread(rm) else "غير موجودة.")


async def cmd_togglesched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستعمال: <code>/togglesched 1</code>")
        return
    sid = int(context.args[0])

    def work():
        with cursor() as cur:
            cur.execute("UPDATE m_sched SET active=1-active WHERE id=?", (sid,))
            if cur.rowcount != 1:
                return None
            cur.execute("SELECT active FROM m_sched WHERE id=?", (sid,))
            return cur.fetchone()["active"]
    a = await asyncio.to_thread(work)
    await update.message.reply_text("غير موجودة." if a is None else ("▶️ تم التشغيل" if a else "⏸ تم الإيقاف"))


async def cmd_testsched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testsched ID ← يرسلها لك أنت فقط لتتأكد من شكلها"""
    if not _is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستعمال: <code>/testsched 1</code>")
        return
    sid = int(context.args[0])

    def get():
        with cursor() as cur:
            cur.execute("SELECT text FROM m_sched WHERE id=?", (sid,))
            return cur.fetchone()
    r = await asyncio.to_thread(get)
    if not r:
        await update.message.reply_text("غير موجودة.")
        return
    txt = r["text"].replace("{name}", html.escape(update.effective_user.full_name)).replace("{level}", "1")
    kb = (InlineKeyboardMarkup([[InlineKeyboardButton("⛏ افتح التعدين", web_app=WebAppInfo(url=APP_URL))]])
          if WEBAPP_URL else None)
    await update.message.reply_text(txt, reply_markup=kb)


def sched_due():
    now = time.time()
    lt = _local(now)
    today, cm, wd = lt.strftime("%Y-%m-%d"), lt.hour * 60 + lt.minute, lt.weekday()
    due = []
    with cursor() as cur:
        cur.execute("SELECT * FROM m_sched WHERE active=1")
        for r in cur.fetchall():
            if r["last_run"] == today:
                continue
            if r["days"] and str(wd) not in r["days"].split(","):
                continue
            if 0 <= cm - r["hhmm"] <= 10:       # نافذة 10 دقائق (إن كان البوت متوقفاً أطول من ذلك تُتخطى)
                cur.execute("UPDATE m_sched SET last_run=? WHERE id=?", (today, r["id"]))
                due.append(r)
    return due


def _sched_pairs(text):
    with cursor() as cur:
        cur.execute("SELECT id,name,level FROM m_users WHERE banned=0 ORDER BY id")
        return [(r["id"], text.replace("{name}", html.escape(r["name"] or "")).replace("{level}", str(r["level"])))
                for r in cur.fetchall()]


async def _run_sched(bot, r):
    pairs = await asyncio.to_thread(_sched_pairs, r["text"])
    await _send_many(bot, pairs, ADMIN_ID, label=f" (مجدولة #{r['id']})")


async def sched_loop():
    while True:
        await asyncio.sleep(20)
        try:
            BG[:] = [t for t in BG if not t.done()]
            for r in await asyncio.to_thread(sched_due):
                BG.append(asyncio.create_task(_run_sched(MAIN_APP.bot, r)))
        except Exception:
            log.exception("sched loop")


# ═══════════ 5.3) نسخ احتياطي · استرجاع · مشبوهون · تقرير يومي ═══════════
BACKUP_TABLES = ["m_users", "m_payments", "m_tasks", "m_blocks", "m_codes", "m_redeems",
                 "m_servers", "m_settings", "m_week_top", "m_sched"]
SERIAL_TABLES = ["m_blocks", "m_servers", "m_sched"]


def make_backup():
    data = dict(v=1, ts=time.time(), tables={})
    with cursor() as cur:
        for t in BACKUP_TABLES:
            cur.execute(f"SELECT * FROM {t}")
            data["tables"][t] = cur.fetchall()
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8")), len(data["tables"]["m_users"])


def _table_cols(cur, t):
    if USE_PG:
        cur.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_name=?", (t,))
    else:
        cur.execute(f"PRAGMA table_info({t})")
    return {r["name"] for r in cur.fetchall()}


def reload_state():
    """يعيد قراءة الإعدادات المخزّنة في الذاكرة (بعد الاسترجاع)."""
    global TITLE
    with cursor() as cur:
        cur.execute("SELECT v FROM m_settings WHERE k='title'")
        r = cur.fetchone()
        TITLE = r["v"] if r and r["v"] else DEFAULT_TITLE
    try:
        CHAIN["start"] = float(get_setting("start_ts", NOT_SCHEDULED))
        CHAIN["blocks"] = max(1, int(float(get_setting("blocks_total", DEFAULT_BLOCKS))))
    except ValueError:
        pass
    CHAIN["done"] = blocks_mined() >= CHAIN["blocks"]
    with cursor() as cur:
        cur.execute("SELECT id, ban_reason FROM m_users WHERE banned=1")
        BANNED.clear()
        BANNED.update({r["id"]: r["ban_reason"] or "" for r in cur.fetchall()})
    WEEK_CACHE[1] = None
    SESS.clear()


def restore_data(data):
    tables = data.get("tables") or {}
    if "m_users" not in tables:
        raise ValueError("الملف لا يحتوي على جدول المستخدمين")
    counts = {}
    with cursor() as cur:
        for t in BACKUP_TABLES:
            if t not in tables:
                continue
            cols = _table_cols(cur, t)
            cur.execute(f"DELETE FROM {t}")
            rows = tables[t]
            if t == "m_blocks" and USE_PG:
                pass
            if rows:
                keys = [k for k in rows[0].keys() if k in cols]
                tuples = [tuple(r.get(k) for k in keys) for r in rows]
                if USE_PG:
                    from psycopg2.extras import execute_values
                    execute_values(cur.cur, f"INSERT INTO {t} ({','.join(keys)}) VALUES %s", tuples, page_size=1000)
                else:
                    cur.cur.executemany(f"INSERT INTO {t} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                                        tuples)
            counts[t] = len(rows)
        if USE_PG:
            for t in SERIAL_TABLES:
                cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                            f"GREATEST(COALESCE((SELECT MAX(id) FROM {t}),0),1))")
    reload_state()
    return counts


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backup ← يرسل لك نسخة احتياطية مضغوطة بكل البيانات"""
    if not _is_admin(update):
        return
    await update.message.reply_text("⏳ جاري تجهيز النسخة الاحتياطية...")
    data, n = await asyncio.to_thread(make_backup)
    if len(data) > 49 * 1024 * 1024:
        await update.message.reply_text("⚠️ حجم النسخة أكبر من حد تيليجرام (50MB). تواصل معي لنقسّمها.")
        return
    fname = f"backup_{_local(time.time()).strftime('%Y%m%d_%H%M')}.json.gz"
    await context.bot.send_document(update.effective_chat.id, io.BytesIO(data), filename=fname,
                                    caption=f"💾 نسخة احتياطية · {n:,} لاعب · {len(data) / 1024:.0f}KB\n"
                                            "للاسترجاع: أرسل الملف نفسه مع التعليق /restore تأكيد")


async def cmd_restore_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text(
        "♻️ <b>الاسترجاع</b>\n\nأرسل ملف النسخة (.json.gz) وفي التعليق اكتب:\n<code>/restore تأكيد</code>\n\n"
        "⚠️ سيُحذف كل شيء حالي ويُستبدل بمحتوى النسخة. الأفضل تنفيذه في وقت هادئ، وخذ /backup قبله.\n"
        "(حد تيليجرام لتنزيل الملف 20MB)")


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    cap = (update.message.caption or "").lower()
    if "تأكيد" not in cap and "تاكيد" not in cap and "confirm" not in cap:
        await cmd_restore_help(update, context)
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ الملف أكبر من 20MB (حد تنزيل البوتات). تواصل معي لحل بديل.")
        return
    await update.message.reply_text("⏳ جاري الاسترجاع...")
    try:
        f = await doc.get_file()
        raw = bytes(await f.download_as_bytearray())
        try:
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        except OSError:
            data = json.loads(raw.decode("utf-8"))
        counts = await asyncio.to_thread(restore_data, data)
    except Exception as e:
        log.exception("restore")
        await update.message.reply_text(f"❌ فشل الاسترجاع (لم يتغير شيء): {html.escape(str(e))[:300]}")
        return
    await update.message.reply_text("✅ تم الاسترجاع:\n" + "\n".join(f"• {k}: {v:,}" for k, v in counts.items()))


def find_suspects():
    now = time.time()
    with cursor() as cur:
        cur.execute("SELECT referrer AS rid, COUNT(*) AS n, SUM(CASE WHEN mined_secs>=? THEN 1 ELSE 0 END) AS act, "
                    "SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS recent FROM m_users "
                    "WHERE referrer IS NOT NULL GROUP BY referrer HAVING COUNT(*)>=10 ORDER BY n DESC LIMIT 200",
                    (REF_SECS, now - 86400))
        rows = [dict(rid=r["rid"], n=int(r["n"]), act=int(r["act"] or 0), recent=int(r["recent"] or 0))
                for r in cur.fetchall()]
        rows = [r for r in rows if r["recent"] >= 15 or r["act"] * 5 < r["n"]][:15]
        if rows:
            cur.execute("SELECT id,name,banned FROM m_users WHERE id IN (" + ",".join("?" * len(rows)) + ")",
                        tuple(r["rid"] for r in rows))
            nm = {r["id"]: r for r in cur.fetchall()}
            for r in rows:
                r["name"] = (nm.get(r["rid"]) or {}).get("name") or "—"
                r["banned"] = bool((nm.get(r["rid"]) or {}).get("banned"))
    return rows


async def cmd_suspects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/suspects ← مُحيلون مشتبه بهم (إحالات كثيرة جديدة، أو معظم مُحاليهم لم يعدّنوا)"""
    if not _is_admin(update):
        return
    rows = await asyncio.to_thread(find_suspects)
    if not rows:
        await update.message.reply_text("✅ لا يوجد مشتبه بهم حالياً.")
        return
    await update.message.reply_text("⚠️ <b>مُحيلون مشتبه بهم</b>\n(10+ إحالات، وأقل من 20% منهم عدّن ساعة، أو 15+ جديدة خلال 24 ساعة)\n\n"
        + "\n\n".join(f"👤 {html.escape(r['name'])} · <code>{r['rid']}</code>{' · ⛔ محظور' if r['banned'] else ''}\n"
                      f"   مُحالون: {r['n']} · نشطون فعلاً: {r['act']} · جدد 24س: {r['recent']}\n"
                      + ("" if r["banned"] else f"   <code>/ban {r['rid']} إحالات وهمية</code>") for r in rows))


def _delta(a, b):
    d = a - b
    return "—" if d == 0 else (f"▲ +{d:,}" if d > 0 else f"▼ {d:,}")


def build_daily():
    now = time.time()
    off = TZ_OFFSET * 3600
    t0 = ((now + off) // 86400) * 86400 - off          # بداية اليوم الحالي
    y0, p0 = t0 - 86400, t0 - 2 * 86400                # أمس · أول أمس
    with cursor() as cur:
        def one(sql, p=()):
            cur.execute(sql, p)
            return cur.fetchone()
        new_y = one("SELECT COUNT(*) AS n FROM m_users WHERE created_at>=? AND created_at<?", (y0, t0))["n"]
        new_p = one("SELECT COUNT(*) AS n FROM m_users WHERE created_at>=? AND created_at<?", (p0, y0))["n"]
        pay_y = one("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS s FROM m_payments WHERE refunded=0 AND "
                    "created_at>=? AND created_at<?", (y0, t0))
        pay_p = one("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS s FROM m_payments WHERE refunded=0 AND "
                    "created_at>=? AND created_at<?", (p0, y0))
        cur.execute("SELECT item, COUNT(*) AS n, SUM(stars) AS s FROM m_payments WHERE refunded=0 AND created_at>=? "
                    "AND created_at<? GROUP BY item ORDER BY s DESC LIMIT 5", (y0, t0))
        items = cur.fetchall()
        mining = one("SELECT COUNT(*) AS n, COALESCE(SUM(hr),0) AS h FROM m_users WHERE " + SQL_ACTIVE, (now,))
        a24 = one("SELECT COUNT(*) AS n FROM m_users WHERE last_tick>=?", (now - 86400,))["n"]
        tot = one("SELECT COUNT(*) AS n, SUM(CASE WHEN banned=1 THEN 1 ELSE 0 END) AS b FROM m_users")
        payers = one("SELECT COUNT(DISTINCT user_id) AS n FROM m_payments WHERE refunded=0")["n"]
        cur.execute("SELECT name,total-week_base AS w FROM m_users WHERE banned=0 AND total-week_base>0 "
                    "ORDER BY w DESC LIMIT 3")
        top = cur.fetchall()
    I = lambda x: int(x or 0)
    sus = len(find_suspects())
    day = _local(y0 + 3600).strftime("%d.%m.%Y")
    L = [f"🌅 <b>تقرير يوم {day}</b>", "",
         f"👥 لاعبون جدد: <b>{I(new_y):,}</b>  ({_delta(I(new_y), I(new_p))} عن اليوم السابق)",
         f"⭐ إيراد النجوم: <b>{I(pay_y['s']):,}</b> من {I(pay_y['n'])} عملية  ({_delta(I(pay_y['s']), I(pay_p['s']))})"]
    if items:
        L.append("   " + " · ".join(f"{html.escape(r['item'])} {I(r['s']):,}⭐" for r in items))
    L += ["", f"⛏ معدّنون نشطون الآن: <b>{I(mining['n']):,}</b> · هاش الشبكة {float(mining['h']):,.0f}",
          f"🟢 نشطون آخر 24 ساعة: {I(a24):,}",
          f"📊 الإجمالي: {I(tot['n']):,} لاعب · دافعون {I(payers):,} · محظورون {I(tot['b']):,}"]
    if sus:
        L.append(f"⚠️ مُحيلون مشتبه بهم: <b>{sus}</b> ← /suspects")
    if top:
        L += ["", "🏆 صدارة الأسبوع: " + " · ".join(f"{html.escape(r['name'] or '—')} ({float(r['w']):,.0f})" for r in top)]
    return "\n".join(L)


async def _send_daily(bot, with_backup=True):
    await bot.send_message(ADMIN_ID, await asyncio.to_thread(build_daily))
    if with_backup and DAILY_BACKUP:
        data, n = await asyncio.to_thread(make_backup)
        if len(data) <= 49 * 1024 * 1024:
            await bot.send_document(ADMIN_ID, io.BytesIO(data),
                                    filename=f"backup_{_local(time.time()).strftime('%Y%m%d')}.json.gz",
                                    caption=f"💾 نسخة احتياطية يومية · {n:,} لاعب")
        else:
            await bot.send_message(ADMIN_ID, "⚠️ النسخة الاحتياطية أكبر من 50MB ولم تُرسل.")


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/daily ← يعرض تقرير أمس الآن (بدون نسخة احتياطية)"""
    if not _is_admin(update):
        return
    await update.message.reply_text(await asyncio.to_thread(build_daily))


async def daily_loop():
    """كل يوم في الساعة DAILY_REPORT_TIME: تقرير + نسخة احتياطية للأدمن."""
    while True:
        await asyncio.sleep(30)
        try:
            if not ADMIN_ID:
                continue
            lt = _local(time.time())
            today, cm = lt.strftime("%Y-%m-%d"), lt.hour * 60 + lt.minute
            if 0 <= cm - DAILY_MIN <= 120:
                if await asyncio.to_thread(get_setting, "daily_last") != today:
                    await asyncio.to_thread(set_setting, "daily_last", today)
                    await _send_daily(MAIN_APP.bot)
        except Exception:
            log.exception("daily loop")


async def post_init(app: Application):
    global MAIN_APP
    MAIN_APP = app
    await start_web()
    try:
        user_cmds = [BotCommand("start", "🏠 الصفحة الرئيسية"),
                     BotCommand("redeem", "🎁 استرداد كود")]
        await app.bot.set_my_commands(user_cmds)
        if ADMIN_ID:
            admin_cmds = user_cmds + [BotCommand("gencode", "➕ إنشاء كود جديد"),
                                      BotCommand("codes", "📋 عرض الأكواد"),
                                      BotCommand("delcode", "🗑 حذف كود"),
                                      BotCommand("payments", "🧾 عرض الدفعات"),
                                      BotCommand("settitle", "✏️ تغيير عنوان البوت"),
                                      BotCommand("setstart", "🚀 موعد بدء التعدين"),
                                      BotCommand("resetall", "🗑 حذف كل البيانات"),
                                      BotCommand("setblocks", "🧱 عدد الكتل"),
                                      BotCommand("chain", "⛓ حالة السلسلة"),
                                      BotCommand("refund", "💸 استرجاع دفعة"),
                                      BotCommand("stats", "📊 لوحة الإحصائيات"),
                                      BotCommand("broadcast", "📢 بث رسالة للجميع"),
                                      BotCommand("ban", "⛔ حظر مستخدم"),
                                      BotCommand("unban", "✅ إلغاء حظر"),
                                      BotCommand("bans", "📋 قائمة المحظورين"),
                                      BotCommand("weekly", "🏆 الترتيب الأسبوعي"),
                                      BotCommand("weeksend", "🎉 تهنئة أفضل 100 أسبوعياً"),
                                      BotCommand("addsched", "⏰ جدولة رسالة يومية"),
                                      BotCommand("schedules", "📋 الرسائل المجدولة"),
                                      BotCommand("daily", "🌅 تقرير أمس"),
                                      BotCommand("backup", "💾 نسخة احتياطية"),
                                      BotCommand("restore", "♻️ استرجاع نسخة"),
                                      BotCommand("suspects", "⚠️ مُحيلون مشتبه بهم")]
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        if WEBAPP_URL:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="⛏ تعدين", web_app=WebAppInfo(url=APP_URL)))
    except Exception as e:
        log.warning("setup failed: %s", e)
    BG.append(asyncio.create_task(block_loop()))
    BG.append(asyncio.create_task(settle_loop()))
    BG.append(asyncio.create_task(week_loop()))
    BG.append(asyncio.create_task(sched_loop()))
    BG.append(asyncio.create_task(daily_loop()))
    if ADMIN_ID:
        try:
            await app.bot.send_message(ADMIN_ID, f"✅ البوت يعمل.\n🗄 {'PostgreSQL' if USE_PG else 'SQLite'}\n"
                                       f"🌐 {WEBAPP_URL + '/app' if WEBAPP_URL else 'WEBAPP_URL غير مضبوط'}")
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
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{--mono:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;--disp:"Space Grotesk",system-ui,-apple-system,"Segoe UI","Noto Sans Arabic",Tahoma,sans-serif;--bg:#1a1c1e;--card:#282b2e;--line:#33363a;--ac:#ffa45b;--tx:#eceef0;--mut:#8b9098;--red:#d94848;--grn:#3ecf8e;--blue:#38b6ff;--pur:#9b7bff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic",Tahoma,sans-serif;padding-bottom:calc(84px + env(safe-area-inset-bottom))}
.w{max-width:560px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px;margin-bottom:12px}
h2{margin:0 0 6px;font-size:20px}h3{margin:0 0 10px;font-size:15px;color:var(--mut)}
.h{color:var(--mut);font-size:13px;margin:0}
.big{font-size:44px;font-weight:800;line-height:1.1;direction:ltr;text-align:right}
.big small{font-size:22px;color:var(--mut)}
.bar{height:9px;background:var(--bg);border-radius:9px;overflow:hidden;margin:6px 0 12px}
.bar i{display:block;height:100%;border-radius:9px;transition:width .3s}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13px;color:var(--mut)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.cell{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:11px}
.cell b{display:block;font-size:19px;color:var(--tx);direction:ltr;text-align:right}
.mini{margin:0;cursor:pointer;padding:13px}
.mini b{font-size:17px;display:block;direction:ltr;text-align:right}
button{font:inherit;font-weight:700;border:0;border-radius:14px;padding:14px;width:100%;margin-top:10px;cursor:pointer;color:var(--tx);background:var(--bg);border:1px solid var(--line)}
button:active{transform:scale(.98)}button:disabled{opacity:.45}
button.pri{background:var(--ac);color:#1e1307;border:0}button.stop{background:var(--red);color:#fff;border:0}
button.sm{width:auto;margin:0;padding:8px 14px}
.blk{padding:10px 0;border-bottom:1px solid var(--line);font-size:13px}
.blk:last-child{border:0}.blk b{font-size:14px}
.tag{color:var(--ac);font-weight:700;direction:ltr;display:inline-block}
.days{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.day{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:9px;text-align:center;font-size:13px;color:var(--mut)}
.day.on{border-color:var(--ac);color:var(--ac)}.day b{display:block;color:var(--tx);font-size:15px}
.day.wide{grid-column:1/-1}
.kv{background:var(--bg);border:1px dashed var(--line);border-radius:12px;padding:10px;word-break:break-all;direction:ltr;text-align:left;font-size:12px;font-family:ui-monospace,Menlo,monospace}
.nav{position:fixed;inset:auto 0 0 0;display:flex;background:radial-gradient(#ffffff0b 1px,transparent 1.2px) 0 0/12px 12px,#282b2e;border-top:1px solid #33363a;padding:9px 6px calc(8px + env(safe-area-inset-bottom));z-index:5}
.nav div{position:relative;flex:1;text-align:center;padding:5px 2px 3px;color:#8b9098;font:600 10px var(--mono);letter-spacing:.03em;cursor:pointer}
.nav div.on{color:var(--ac)}
.nav div.on::before{content:"";position:absolute;top:-10px;left:50%;width:36px;margin-left:-18px;height:2px;border-radius:2px;background:var(--ac)}
.nav span{display:block;height:22px;margin-bottom:3px}.nav svg{width:20px;height:20px}
.earn{position:absolute;top:-15px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:3px;font:700 9px var(--mono);font-style:normal;letter-spacing:.06em;color:#3ecf8e;background:#1e3a30;border:1px solid #2c5a47;border-radius:10px;padding:2px 5px 2px 7px;z-index:2}
.earn b{width:11px;height:11px;border-radius:50%;background:#38b6ff;margin-left:-3px;border:1px solid #1e3a30}.earn b+b{background:#3ecf8e}
.rk{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.rk:last-child{border:0}
.rk i{width:30px;height:30px;border-radius:9px;background:var(--bg);border:1px solid var(--line);display:grid;place-items:center;font-style:normal;font-size:13px;font-weight:800}
.rk .n{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:700}
.rk.me{color:var(--ac)}
.inp{width:100%;padding:13px;border-radius:14px;border:1px solid var(--line);background:var(--bg);color:var(--tx);font:inherit;direction:ltr;text-align:center}
.av{width:30px;height:30px;border-radius:50%;display:inline-grid;place-items:center;font-weight:800;font-size:12px;color:#fff;vertical-align:middle}
.bdg{font-size:11px;border:1px solid;border-radius:8px;padding:0 7px;margin-inline:4px;font-weight:700}
.turbo{color:var(--blue)}.nitro{color:var(--grn)}.super{color:var(--ac)}
.live{color:var(--grn);border:1px solid var(--grn);border-radius:9px;padding:1px 9px;font-size:12px;font-weight:700}
.off{color:var(--mut);border:1px solid var(--line);border-radius:9px;padding:1px 9px;font-size:12px}
.up{color:var(--grn)}.dn{color:#ff6b81}
.seg{display:flex;gap:8px}.seg button{margin:0}.seg button.on{background:var(--ac);color:#1e1307;border:0}
.ov{position:fixed;inset:0;background:#000b;z-index:30;display:flex;align-items:flex-end;justify-content:center}
.sh{width:100%;max-width:560px;background:var(--card);border:1px solid var(--line);border-radius:22px 22px 0 0;padding:16px;max-height:88vh;overflow:auto;padding-bottom:calc(16px + env(safe-area-inset-bottom))}
.cap{font:700 11px ui-monospace,Menlo,monospace;letter-spacing:.15em;color:var(--mut);direction:ltr;text-align:right}
.stc{position:relative;background:radial-gradient(#ffffff0b 1px,transparent 1.2px) 0 0/12px 12px,#282b2e;border:1px solid #33363a;border-radius:14px;margin-bottom:12px;overflow:hidden}
.stc::before,.nav::before{content:"";position:absolute;top:0;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,#ffa45bb8,transparent);z-index:1}
.sth{display:flex;align-items:center;gap:8px;padding:9px 12px;min-height:43px;border-bottom:1px solid #303337;font:700 13px var(--disp);letter-spacing:-.02em;color:#f0f0f0}
.stic{width:24px;height:24px;border:1px solid #3a3d41;border-radius:7px;display:grid;place-items:center;color:#8b9098;background:#ffffff05;flex:none}
.stic.or{border-color:#6e4c32;background:#3e3833;color:#ffa861}
.tmb{padding:16px 16px 15px}.tr{display:flex;justify-content:space-between;align-items:center}
.mlb{font:400 11px var(--mono);letter-spacing:.04em;color:#8b9098}
.pc{font:700 20px var(--mono);color:#ffa861;direction:ltr}.pc small{font-size:13px;color:#8b9098;font-weight:400}
.pb{height:6px;background:#483e35;border-radius:6px;overflow:hidden;margin-top:13px}.pb i{display:block;height:100%;background:#ffa861;border-radius:6px}
.st3{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid #303337}
.st3>div{padding:11px 12px 10px;border-inline-start:1px solid #303337;min-width:0}.st3>div:first-child{border:0}
.fl{display:flex;align-items:center;gap:5px;font:600 11px var(--disp);color:#8b9098}
.fv{font:700 13px var(--mono);color:#f4f4f4;direction:ltr;text-align:right;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rb{margin-inline-start:auto;font:700 12px var(--mono);color:#ffa861;background:#3e3833;border:1px solid #7a5535;border-radius:7px;padding:4px 10px;direction:ltr}
.mb{display:flex;align-items:center;gap:12px;padding:13px 14px;background:#333231}
.avs{width:32px;height:32px;border-radius:7px;display:grid;place-items:center;font:700 13px var(--disp);color:#fff;text-shadow:0 1px 2px #0005;flex:none}
.lr{display:flex;align-items:center;gap:10px;padding:10px 12px;min-height:60px;border-bottom:1px solid #333536}.lr:last-child{border:0}
.lr .avs{width:28px;height:28px;font-size:12px;border-radius:6px}
.lr.me{background:#333231}
.lrk{width:28px;height:28px;border:1px solid #3d4044;border-radius:7px;display:grid;place-items:center;font:700 12px var(--mono);color:#8b9098;flex:none}
.lrk.tp{border-color:#6e4c32;background:#3a342f;color:#ffa861}
.nm{min-width:0;flex:1}.nl{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.nl b{font:700 15px var(--disp);letter-spacing:-.01em;color:#f4f4f4}
.lv{font:400 11px var(--mono);color:#8b9098}
.am{display:flex;align-items:center;gap:5px;margin-top:6px;direction:ltr;justify-content:flex-end}
.cn{width:10px;height:10px;border-radius:3px;background:linear-gradient(#ffb674,#ff9a4a);color:#fff;font:700 7px var(--mono);display:grid;place-items:center;font-style:normal;flex:none}
.amt{font:700 15px var(--mono);color:#f4f4f4}.amt small{font-size:10px;color:#8b9098;font-weight:400}
.lr .amt{font-size:13px;font-weight:400;color:#c3c6c9}
.bdg{font:700 10px var(--mono);letter-spacing:.06em;border:1px solid;border-radius:5px;padding:1px 6px;margin:0;line-height:1.5}
.turbo{color:#38b6ff;background:#38b6ff1f;border-color:#38b6ffb0}.nitro{color:#3ecf8e;background:#3ecf8e1f;border-color:#3ecf8eb0}.super{color:#ffa45b;background:#ffa45b1f;border-color:#ffa45bb0}
.rc{padding:12px}
.rtop{display:flex;justify-content:space-between;align-items:center;margin:4px 2px 13px}
.cap2{display:flex;align-items:center;gap:6px}.dot{width:5px;height:5px;border-radius:50%;background:#ffa861;flex:none}
.rcount{display:flex;align-items:center;gap:6px;color:#8b9098}.rcount b{font:700 20px var(--mono);color:#f4f4f4;line-height:1}
.lk{display:flex;align-items:center;gap:8px;border:1px solid #33363a;background:#25282b;border-radius:10px;height:39px;padding:0 10px 0 14px;margin-bottom:11px}
.lk span{flex:1;min-width:0;font:500 13px var(--mono);color:#f0f0f0;direction:ltr;text-align:left;overflow:hidden;white-space:nowrap}
.lk .cp{width:auto;margin:0;padding:4px;background:none;border:0;color:#9aa0a6;display:grid;place-items:center}
.inv{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;height:44px;margin:0;padding:0;border:0;border-radius:9px;background:#ffa861;color:#1e1307;font:600 15px var(--disp);letter-spacing:.03em}
.bn{padding:12px}.bnr{display:flex;gap:12px;align-items:center}
.stic.big{width:32px;height:32px;border-radius:8px}
.bt{font:700 14px var(--disp);letter-spacing:-.01em;margin-top:4px;color:#f4f4f4}
.bd{font-size:12.5px;line-height:1.6;color:#8b9098;margin:11px 0 0}
.lbh{justify-content:space-between;padding:11px 14px}
.lbt{font:700 13px var(--disp);letter-spacing:-.02em;color:#f0f0f0}.lbs{font:400 10px var(--mono);letter-spacing:.04em;color:#8b9098;margin-top:3px}
.lbn{font:400 12px var(--mono);color:#8b9098}
.em{display:flex;flex-direction:column;align-items:center;gap:15px;padding:38px 26px 42px;text-align:center;color:#8b9098;font-size:13px;line-height:1.65}
.emi{width:38px;height:38px;border:1px solid #3a3d41;border-radius:9px;display:grid;place-items:center;background:#25282b;color:#9aa0a6}
.rcn{color:#8b9098;display:grid;place-items:center}
/* ── أوضاع التعدين ── */
.grid.ctl{grid-template-columns:1.12fr 1fr;margin:0;gap:10px}
.sbtn{display:flex;align-items:center;justify-content:center;gap:10px;margin:0;padding:8px 12px;min-height:52px}
.pbx{width:34px;height:34px;border-radius:9px;background:#3b2a1a;color:#ffa861;display:grid;place-items:center;flex:none}
.mdb{display:flex;align-items:center;gap:9px;margin:0;padding:8px 12px;min-height:52px;background:#2c2f32;border:1px solid #44484c;text-align:start}
.mdi{width:34px;height:34px;border:1px solid #4a4e53;border-radius:9px;display:grid;place-items:center;color:var(--mut);flex:none}
.mdt{flex:1;min-width:0;text-align:start}
.mdt small{display:block;font:400 11px var(--mono);color:var(--mut);line-height:1.2}
.mdt b{display:block;font:700 15px var(--disp);color:var(--tx);line-height:1.3}
.mdx{font:700 11px var(--mono);color:var(--mut);align-self:flex-start;direction:ltr}
.mdb>svg{color:var(--mut);flex:none}
.runb{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:12px;padding:11px 14px;border-radius:9px;font-size:13px;background:#4a3320;color:#ffd9b8;border:1px solid #5d4128}
.ov.mid{align-items:center;padding:14px}
.shw{position:relative;width:100%;max-width:560px}
.ov.mid .sh{border-radius:16px;max-height:86vh;padding:22px 20px 18px;background:radial-gradient(#ffffff0b 1px,transparent 1.2px) 0 0/12px 12px,#282b2e;scrollbar-width:thin;scrollbar-color:#4a4e53 transparent}
.sh.lt{direction:ltr;text-align:left}
.sh.lt .cap{text-align:left}
.sh.lt h2{margin:8px 0 6px;font:700 20px var(--disp)}
.sh.lt .h{font-size:12.5px;line-height:1.6}
.xb{position:absolute;top:8px;right:12px;z-index:3;width:24px;height:24px;margin:0;padding:0;display:grid;place-items:center;border:1px solid #3a3d41;border-radius:6px;background:#25282b;color:#9aa0a6}
.mc{position:relative;display:flex;align-items:center;gap:12px;padding:12px;margin-top:8px;border:1px solid #33363a;border-radius:10px;background:#25282b;overflow:hidden;cursor:pointer}
.sh.lt .h+.mc{margin-top:14px}
.mc.act{border-color:#b9702f;background:#3a342f}
.mi{width:36px;height:36px;border:1px solid var(--mc);border-radius:8px;display:grid;place-items:center;color:var(--mc);background:color-mix(in srgb,var(--mc) 14%,transparent);flex:none}
.mt{flex:1;min-width:0}.mt b{font:700 14px var(--disp);color:#f4f4f4;line-height:1.3}
.mt em{font:700 10px var(--mono);color:var(--mc);font-style:normal;margin-inline-start:6px;direction:ltr;display:inline-block}
.ms{font:400 11px var(--mono);color:var(--mut);margin-top:3px;line-height:1.2}
.ms .lt{direction:ltr;display:inline-block}
.mbtn{display:flex;align-items:center;gap:4px;flex:none;padding:4px 9px;border-radius:6px;font:700 11px var(--mono);direction:ltr;line-height:1.5}
.mbtn.pr{background:#ffa861;color:#1e1307}
.mbtn.on{border:1px solid #4a4e53;color:#c3c6c9}
.mbtn.ok{border:1px solid #3a3d41;color:var(--tx)}
.mbar{position:absolute;left:0;bottom:0;height:3px;width:0;background:var(--mc);border-radius:0 3px 3px 0;transition:width .25s linear}
.mnote{font-size:11px;line-height:1.6;color:var(--mut);margin:12px 2px 0}
/* ── بطاقات الكتل ── */
.bkc{background:#25282b;border:1px solid #33363a;border-radius:12px;padding:13px 14px 12px;margin-bottom:12px;cursor:pointer}
.bkc:active{background:#2a2d30}
.bkh{display:flex;justify-content:space-between;align-items:center;gap:8px}
.bkn{font:400 11px var(--mono);letter-spacing:.12em;color:#8b9098;direction:ltr;white-space:nowrap}
.bkn b{font:700 15px var(--mono);letter-spacing:0;color:#ffa861;margin-inline-start:8px}
.bkr{display:flex;align-items:center;gap:8px;color:#8b9098}
.rwp{display:flex;align-items:center;gap:6px;border:1px solid #3a3d41;border-radius:8px;background:#2b2e31;padding:3px 9px;font:400 10.5px var(--mono);color:#8b9098;white-space:nowrap}
.rwp .rv{direction:ltr;font:700 10.5px var(--mono);color:#c3c6c9}.rwp .rv small{font-size:8.5px;color:#6b7178;font-weight:400}
.cx{display:inline-grid;place-items:center;width:var(--s,12px);height:var(--s,12px);border-radius:calc(var(--s,12px)*.28);background:linear-gradient(#ffb674,#ff9a4a);color:#fff;font:800 calc(var(--s,12px)*.7) var(--mono);font-style:normal;flex:none;line-height:1}
.bkm{display:flex;align-items:center;gap:8px;margin-top:11px;min-width:0}
.bkl{color:#8b9098;font-size:13px;flex:none}
.bkm .avs{width:22px;height:22px;font-size:10px;border-radius:6px}
.bkm b{font:700 14px var(--disp);color:#f4f4f4;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lvp{font:400 11px var(--mono);color:#8b9098;border:1px solid #3a3d41;border-radius:6px;padding:0 7px;background:#2b2e31;white-space:nowrap;direction:ltr;flex:none}
.bkf{display:flex;justify-content:space-between;align-items:flex-end;gap:8px;margin-top:9px}
.bkd{font:400 11px var(--mono);color:#8b9098;direction:ltr}
.rar{font:700 10px var(--mono);letter-spacing:.08em;border:1px solid #4a4e53;border-radius:5px;padding:2px 10px;color:#9aa0a6;background:#ffffff08;white-space:nowrap}
.rar.gr{color:#3ecf8e;border-color:#3ecf8eb0;background:#3ecf8e1f}
/* ── نافذة الحظر ── */
.bnx{position:fixed;inset:0;z-index:200;background:#0c0d0ee6;display:flex;align-items:center;justify-content:center;padding:18px}
.bnc{position:relative;width:100%;max-width:520px;border:1px solid #33363a;border-radius:14px;overflow:hidden;background:radial-gradient(#ffffff0b 1px,transparent 1.2px) 0 0/12px 12px,#25282b}
.bnc::before{content:"";position:absolute;top:0;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,#ffa45bb8,transparent)}
.bnc>i{position:absolute;width:8px;height:8px;border:0 solid #5a5f66}
.bnc>i.bk1{top:8px;left:8px;border-top-width:1px;border-left-width:1px}
.bnc>i.bk2{top:8px;right:8px;border-top-width:1px;border-right-width:1px}
.bnc>i.bk3{bottom:8px;left:8px;border-bottom-width:1px;border-left-width:1px}
.bnc>i.bk4{bottom:8px;right:8px;border-bottom-width:1px;border-right-width:1px}
.bnt{display:flex;justify-content:space-between;align-items:center;padding:16px 20px 0;font:400 10px var(--mono);letter-spacing:.14em;color:#8b9098;direction:ltr}
.bnd{display:inline-block;width:5px;height:5px;border-radius:50%;background:#ff5b5b;margin-right:7px;vertical-align:middle}
.bnf{color:#ff5b5b;letter-spacing:.05em}
.bnb{padding:22px 22px 20px;text-align:center}
.bni{width:54px;height:54px;margin:0 auto 18px;border-radius:13px;display:grid;place-items:center;background:#3a2426;border:1px solid #6b3a3d;color:#ff5b5b}
.bnb h2{margin:0 0 10px;font:700 19px var(--disp);color:#f4f4f4}
.bnb p{margin:0;font-size:12.5px;line-height:1.75;color:#8b9098}
.bnb p b{color:#c3c6c9}
.bnbtn{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:20px;font-size:13px;padding:13px}
.bnl{border-top:1px solid #303337;padding:12px 20px 14px}
/* ── بطاقة التصنيف الأسبوعي ── */
.bm.flat{max-height:none;overflow:hidden;margin-bottom:12px}
.wkc{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 16px 14px}
.wkc .bmd{direction:rtl;justify-content:flex-start}
.wkc .bmd small:first-child{font:600 10px var(--disp);color:#8b9098}
.wkc .bmd b,.wkc .bmd small:last-child{direction:ltr;unicode-bidi:isolate;text-align:right}
.cdw{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:12px;direction:ltr}
.cdb{min-width:58px;text-align:center;background:#25282b;border:1px solid #33363a;border-radius:9px;padding:8px 6px}
.cdb b{display:block;font:700 24px var(--mono);color:#f4f4f4;line-height:1.15}
.cdb small{font:400 10px var(--disp);color:#8b9098}
.cds{font:700 20px var(--mono);color:#6b7178;margin-bottom:14px}
.bmg.g3 .bmc2:nth-child(-n+4){border-bottom:1px solid #33363a}
.bmcv .sm{font:400 11px var(--mono);color:#6b7178}
.bmcv .ar{font:700 15px var(--disp)}
/* ── نافذة تفاصيل الكتلة ── */
.bm{position:relative;border:1px solid #3a3d41;border-radius:16px;overflow:hidden;background:radial-gradient(#ffffff0b 1px,transparent 1.2px) 0 0/12px 12px,#282b2e;max-height:86vh;overflow-y:auto}
.bm::before{content:"";position:absolute;top:0;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,#ffa45bb8,transparent)}
.bmh{display:flex;justify-content:space-between;align-items:flex-start;padding:16px 16px 12px;gap:10px}
.bml{font:400 11px var(--mono);letter-spacing:.1em;color:#8b9098}
.bmn{font:700 24px var(--disp);color:#f4f4f4;direction:ltr;text-align:start;margin-top:4px}
.bmr{display:flex;align-items:center;gap:8px}
.bmd{display:flex;align-items:center;gap:8px;border:1px solid #3a3d41;border-radius:9px;background:#25282b;padding:6px 10px;color:#8b9098;direction:ltr}
.bmd b{display:block;font:700 12px var(--mono);color:#f4f4f4;line-height:1.3}.bmd small{display:block;font:400 11px var(--mono);color:#8b9098;line-height:1.3}
.bmx{width:26px;height:26px;margin:0;padding:0;display:grid;place-items:center;border:1px solid #3a3d41;border-radius:7px;background:#25282b;color:#9aa0a6}
.bmy{margin:0 16px 14px;border:1px solid #33363a;border-radius:11px;background:#1f2225;padding:14px 16px}
.bmv{display:flex;align-items:center;gap:10px;margin-top:10px;direction:ltr;justify-content:flex-start}
.bmv span{font:700 34px var(--mono);color:#f4f4f4;line-height:1}.bmv span small{font-size:20px;color:#6b7178;font-weight:700}
.bmv span.z{color:#8b9098}
.bmg{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid #33363a;border-bottom:1px solid #33363a}
.bmc2{padding:14px 16px;min-width:0}
.bmc2:nth-child(1),.bmc2:nth-child(2){border-bottom:1px solid #33363a}
.bmc2:nth-child(odd){border-inline-end:1px solid #33363a}
.bmcl{display:flex;align-items:center;gap:8px;font:600 11px var(--disp);color:#8b9098}
.bmi{width:26px;height:26px;border:1px solid;border-radius:7px;display:grid;place-items:center;flex:none}
.bmi.o{color:#ffa861;background:#3e3833;border-color:#6e4c32}
.bmi.t{color:#38b6ff;background:#38b6ff1f;border-color:#38b6ff66}
.bmi.g{color:#3ecf8e;background:#3ecf8e1f;border-color:#3ecf8e66}
.bmi.y{color:#ffc23f;background:#ffc23f1f;border-color:#ffc23f66}
.bmcv{margin-top:10px;font:700 17px var(--mono);color:#f4f4f4;direction:ltr;text-align:start;display:flex;align-items:center;gap:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bmcv small{font-size:12px;color:#6b7178;font-weight:400}
.bmf{padding:14px 16px 16px}
.bmfr{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}
.bmc{display:flex;align-items:center;gap:6px;width:auto;margin:0;padding:6px 12px;border:1px solid #4a4e53;border-radius:8px;background:#25282b;font:600 12px var(--disp);color:#e6e8ea}
.bmhash{font:400 12px/1.6 var(--mono);color:#e6e8ea;background:#1f2225;border:1px solid #33363a;border-radius:9px;padding:12px;word-break:break-all;direction:ltr;text-align:left}
#introw{--u:.61px;position:fixed;inset:0;z-index:100;background:#17191c;transition:opacity .6s ease;pointer-events:none;overflow:hidden}
#intro{position:absolute;inset:0;width:100%;height:100%}
.it{position:absolute;top:10.7%;left:0;right:0;transform:translateY(-50%);text-align:center;font-weight:800;font-size:calc(var(--u)*35);line-height:1.2;letter-spacing:calc(var(--u)*-.6);color:#fff;padding:0 14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.it i{color:var(--ac);font-style:normal}
.il{position:absolute;left:calc(var(--u)*75);right:calc(var(--u)*73);top:57.4%;height:0;direction:ltr}
.ir{position:absolute;left:0;right:0;top:0;display:flex;justify-content:space-between;align-items:center;font-size:calc(var(--u)*17);color:#8b9098;opacity:0;transition:opacity .35s,transform .35s,color .35s}
.ir.cur{color:#e6e8ea}
.ist{font-size:calc(var(--u)*13);color:var(--ac);direction:ltr;white-space:nowrap}
.ist b{font-weight:400;margin-right:calc(var(--u)*6)}
.ist em{display:inline-block;width:calc(var(--u)*4);height:calc(var(--u)*4);border-radius:50%;background:#6b7178;margin:0 calc(var(--u)*3);animation:bl 1s infinite}
.ist em:nth-child(2){animation-delay:.2s}.ist em:nth-child(3){animation-delay:.4s}
.ib{position:absolute;left:calc(var(--u)*69);width:calc(var(--u)*452);top:74.9%;height:calc(var(--u)*3);background:#ffffff12;border-radius:3px;overflow:hidden}
.ib i{display:block;height:100%;width:0;background:var(--ac)}
@keyframes bl{50%{opacity:.2}}
#toast{position:fixed;left:50%;bottom:calc(96px + env(safe-area-inset-bottom));transform:translateX(-50%);background:#000c;color:#fff;padding:9px 16px;border-radius:12px;font-size:14px;opacity:0;pointer-events:none;transition:opacity .25s;z-index:40}
#toast.on{opacity:1}
</style></head><body>
<div id="introw"><canvas id="intro"></canvas>
<div class="it">__TITLE__<i>.</i></div><div class="il" id="il"></div><div class="ib"><i id="ibf"></i></div></div>
<script>
/* مقدمة التطبيق (4 ثوانٍ). كل الأبعاد مقاسة من الفيديو (عرض 590) وتتناسب مع عرض شاشة الجهاز */
(function(){
  const wrap=document.getElementById('introw'),cv=document.getElementById('intro'),il=document.getElementById('il'),bf=document.getElementById('ibf');
  try{
    let U=innerWidth/590;wrap.style.setProperty('--u',U+'px');
    const STEPS=['تفعيل المحرك','تنزيل السلسلة','الانضمام إلى البركة','المزامنة مع الشبكة','تجهيز أول كتلة'];
    const AT=[200,900,1600,2300,3000],DONE=3500,OP=[1,.6,.35,.2],GAP=37.5;
    const ty=a=>'translateY(calc(-50% + '+a+'px))';
    il.innerHTML=STEPS.map(s=>`<div class="ir"><span>${s}</span><span class="ist"></span></div>`).join('');
    const rows=il.children;let shown=-1;
    function upd(t){
      let idx=0;AT.forEach((a,i)=>{if(t>=a)idx=i+1});
      const fin=t>=DONE,key=idx+(fin?100:0);
      if(key===shown)return;shown=key;
      for(let k=0;k<rows.length;k++){
        const r=rows[k],st=r.lastChild;
        if(k>=idx){r.style.opacity=0;r.style.transform=ty(-14*U);st.innerHTML='';continue}
        const age=idx-1-k,done=k<idx-1||fin;
        r.style.opacity=OP[age]!==undefined?OP[age]:0;
        r.style.transform=ty(age*GAP*U);
        r.classList.toggle('cur',age===0&&!done);
        st.innerHTML=done?'<b>✓</b> تم':'<em></em><em></em><em></em>'}}
    const cx=cv.getContext('2d'),DUR=4000,START=.46,RAMP=.18,dpr=Math.min(2,window.devicePixelRatio||1);
    let W=0,H=0,cells=[],CELL=16,SZ=11,RAD=2.5;
    function init(){
      const r=cv.getBoundingClientRect();W=Math.max(1,Math.round(r.width));H=Math.max(1,Math.round(r.height));
      U=W/590;wrap.style.setProperty('--u',U+'px');shown=-1;
      CELL=16*U;SZ=11*U;RAD=2.5*U;
      cv.width=W*dpr;cv.height=H*dpr;cx.setTransform(dpr,0,0,dpr,0,0);cells=[];
      const cols=Math.ceil(W/CELL)+1,rows2=Math.ceil(H/CELL)+1;
      for(let r2=0;r2<rows2;r2++){
        const yf=r2*CELL/H;if(yf<START)continue;
        const ramp=Math.min(1,(yf-START)/RAMP);
        for(let c=0;c<cols;c++)cells.push({x:c*CELL+(CELL-SZ)/2,y:r2*CELL+(CELL-SZ)/2,ramp:ramp,
          o:Math.random()<.2,p:Math.random()*6.28,s:1.2+Math.random()*2.6,pw:4+Math.random()*3})}}
    init();
    function sq(x,y){if(cx.roundRect){cx.beginPath();cx.roundRect(x,y,SZ,SZ,RAD);cx.fill()}else cx.fillRect(x,y,SZ,SZ)}
    let t=0,last=0,fading=false;
    function frame(now){
      const dt=last?Math.min(50,now-last):16;last=now;t+=dt;   /* لا يحسب وقت التوقف أو التحميل */
      if(t>=DUR){wrap.remove();return}
      const r=cv.getBoundingClientRect();
      if(Math.round(r.width)!==W||Math.round(r.height)!==H)init();
      if(!fading&&t>DUR-600){fading=true;wrap.style.opacity='0'}
      upd(t);bf.style.width=Math.min(100,t/DONE*100)+'%';
      cx.clearRect(0,0,W,H);
      const g=Math.min(1,t/300);
      for(const c of cells){
        const w=.5+.5*Math.sin(t/1000*c.s+c.p);
        const a=c.ramp*g*(.1+.9*Math.pow(w,c.pw));
        cx.fillStyle=c.o?`rgba(215,138,78,${(a*.85).toFixed(3)})`:`rgba(105,96,80,${(a*.6).toFixed(3)})`;
        sq(c.x,c.y)}
      requestAnimationFrame(frame)}
    requestAnimationFrame(frame);
    setTimeout(()=>{if(wrap.parentNode)wrap.remove()},DUR+6000);   /* أمان: لا يعلق أبداً */
  }catch(e){wrap.remove()}
})();
</script>
<div class="w" id="app"><p class="h" style="text-align:center;margin-top:40vh">…</p></div>
<div class="nav" id="nav"></div><div id="sh"></div><div id="toast"></div>
<script>
const tg=window.Telegram.WebApp;try{tg.ready();tg.expand()}catch(e){}
const SID=(()=>{try{const a=new Uint8Array(8);crypto.getRandomValues(a);return Array.from(a,x=>x.toString(16).padStart(2,'0')).join('')}catch(e){return String(Math.random()).slice(2)+Date.now()}})();let SESSNEW=true;
try{tg.BackButton.onClick(()=>{if(sheet)closeSheet()})}catch(e){}
function bb(){try{if(sheet)tg.BackButton.show();else tg.BackButton.hide()}catch(e){}}
let topView='all',BANNED=false,S=null,BK=null,B=[],TOP=null,REFS=null,tab='mine',sheet=null,dxSide='buy',dxVal='';
const TABS=[['mine','⛏','تعدين'],['refs','👥','إحالات'],['tasks','✅','مهام'],['top','🏆','إحصائيات'],['wallet','👛','محفظة']];
const TL={turbo:'تيربو',nitro:'نيترو',super:'سوبر'};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f=(n,d=2)=>Number(n).toLocaleString('en',{minimumFractionDigits:d,maximumFractionDigits:d});
const fi=n=>Number(n).toLocaleString('en',{maximumFractionDigits:0});
const fk=n=>{n=Number(n);return n>=1e9?f(n/1e9)+'B':n>=1e6?f(n/1e6)+'M':n>=1e3?f(n/1e3)+'K':fi(n)};
const bal=n=>{const s=f(n,4);return s.slice(0,-3)+'<small>'+s.slice(-3)+'</small>'};
/* الشارة = اسم وضع التعدين المفتوح والفعّال للمستخدم (t = رقم الوضع، 0 = عادي بدون شارة) */
const badge=t=>{if(!t||!S||!S.modes||!S.modes[t])return '';const c=(MC[t]||MC[1])[1];
  return `<span class="bdg" style="color:${c};background:${c}1f;border-color:${c}b0">${esc(S.modes[t].name)}</span>`};
const av=n=>{let h=0;for(const c of String(n))h=(h*31+c.charCodeAt(0))%360;return `<span class="av" style="background:hsl(${h},55%,48%)">${esc(String(n).trim().slice(0,2).toUpperCase())}</span>`};
function toast(m){if(!m)return;const e=$('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),2200)}
async function api(p,b){
  if(BANNED)throw new Error('');
  const r=await fetch('/api/'+p,{method:b===undefined?'GET':'POST',headers:{'X-Init-Data':tg.initData,'X-Session':SID,'X-Sess-New':SESSNEW?'1':'0','Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)});
  if(!r.ok)throw new Error('HTTP '+r.status);
  const j=await r.json();if(j.banned){showBan(j.reason||'');throw new Error('')}
  if(j.kicked){showLock('kicked');throw new Error('')}
  SESSNEW=false;
  if(!j.ok)throw new Error(j.error||'خطأ');return j}
async function load(){
  const jobs=[api('me')];
  if(tab==='mine')jobs.push(api('blocks'));
  if(tab==='top')jobs.push(api('top',{week:topView==='week'?1:0}));
  if(tab==='refs')jobs.push(api('refs'));
  const r=await Promise.all(jobs);
  S=r[0];if(tab==='mine')B=r[1].blocks;if(tab==='top')TOP=r[1];if(tab==='refs')REFS=r[1].reftop;
  draw()}
function go(t){stopBench();tab=t;sheet=null;bb();load().catch(e=>toast(e.message))}
function openSheet(s){sheet=s;draw();bb()}
function closeSheet(){stopBench();sheet=null;draw();bb()}
function buy(item){api('invoice',{item}).then(r=>tg.openInvoice(r.link,s=>{if(s==='paid')setTimeout(()=>load(),1500)})).catch(e=>toast(e.message))}
function redeem(){const c=$('cd').value.trim();if(!c)return;
  api('redeem',{code:c}).then(r=>{toast(r.msg);load()}).catch(e=>toast(e.message))}
async function act(p,b){try{const r=await api(p,b||{});if(r.msg)toast(r.msg);await load()}catch(e){toast(e.message)}}
function dxPrev(){const v=parseFloat(dxVal)||0,p=S.dex.price;
  return dxSide==='buy'?'≈ '+fi(v/p)+' ⚡':'≈ '+f(v*p*(1-S.dex.fee),4)+' HASH'}
function dxGo(){api('dex',{side:dxSide,amount:parseFloat(dxVal)||0}).then(r=>{toast(r.msg);dxVal='';load()}).catch(e=>toast(e.message))}
function donate(){const n=parseInt($('sp').value);if(!n||n<1)return toast('أدخل عدد النجوم');
  api('invoice',{item:'support',stars:n}).then(r=>tg.openInvoice(r.link,s=>{if(s==='paid')toast('❤️ شكراً لدعمك!')})).catch(e=>toast(e.message))}
function fmtDate(t){return new Date(t*1000).toLocaleString('en-GB',{hour12:false}).replace(',','')}
function fmtDays(s){s=Math.max(0,s);return Math.floor(s/86400)+'ي '+Math.floor(s%86400/3600)+'س '+Math.floor(s%3600/60)+'د'}
function setView(v){topView=v;load().catch(e=>toast(e.message))}
function fmtWait(s){return Math.floor(s/3600)+'س '+Math.floor(s%3600/60)+'د'}
function fmtLeft(s){return Math.floor(s/60)+'د '+(s%60)+'ث'}

/* ═══ أوضاع التعدين + اختبار السرعة ═══ */
const TEST_SECS=10;   /* مدة اختبار كل وضع بالثواني */
const MC={1:['gauge','#8b9098'],2:['rocket','#38b6ff'],3:['zap','#ffb02e'],4:['flame','#3ecf8e'],5:['atom','#9b7bff'],6:['orbit','#ff4fa3'],7:['radiation','#ff5252'],8:['spark','#ffd23f']};
let TEST={run:false,stop:false,cur:0,rate:0,el:0,res:{}};
function saveRes(){}   /* النتائج لا تُحفظ: تبدأ فارغة في كل مرة يُفتح فيها التطبيق */
/* الاختبار يعمل داخل Web Worker (خيط منفصل) حتى لا تعلق الواجهة */
const WSRC=`onmessage=async e=>{const par=e.data.par,ms=e.data.ms,buf=new Uint8Array(32);crypto.getRandomValues(buf);const t0=performance.now();let n=0,last=0;
while(true){const el=performance.now()-t0;if(el>=ms)break;
if(par===1){buf[0]=n&255;buf[1]=(n>>8)&255;await crypto.subtle.digest('SHA-256',buf);n++}
else{const j=new Array(par);for(let i=0;i<par;i++){buf[0]=(n+i)&255;buf[1]=((n+i)>>8)&255;j[i]=crypto.subtle.digest('SHA-256',buf)}await Promise.all(j);n+=par}
if(el-last>200){last=el;postMessage({n:n})}}
postMessage({n:n,done:1})}`;
/* لكل وضع: [عدد الخيوط، حجم الدفعة المتوازية] — كل وضع أعلى = توازي أكبر */
const BENCH={1:[1,1],2:[1,2],3:[1,8],4:[2,8],5:[2,32],6:[3,32],7:[4,64],8:[8,64]};
const MAXW=Math.max(1,(navigator.hardwareConcurrency||4)-1);   /* نترك خيطاً حراً للواجهة */
function stopBench(){TEST.stop=true;if(TEST.wres)TEST.wres({stop:1})}
function benchOne(cfg){
  return new Promise(res=>{
    const url=URL.createObjectURL(new Blob([WSRC],{type:'text/javascript'}));
    const ws=[],ns=new Array(cfg.w).fill(0),t0=performance.now();let left=cfg.w,dead=false;
    const finish=v=>{if(dead)return;dead=true;ws.forEach(w=>{try{w.terminate()}catch(e){}});try{URL.revokeObjectURL(url)}catch(e){}TEST.wres=null;res(v)};
    TEST.wres=finish;
    try{for(let i=0;i<cfg.w;i++){
      const w=new Worker(url);ws.push(w);
      w.onmessage=ev=>{const d=ev.data;ns[i]=d.n;const tot=ns.reduce((a,b)=>a+b,0);
        TEST.el=performance.now()-t0;TEST.rate=tot;uiModes();
        if(d.done&&--left===0)finish({n:tot,el:performance.now()-t0})};
      w.onerror=()=>finish(null);
      w.postMessage({par:cfg.b,ms:TEST_SECS*1000})}}
    catch(e){finish(null)}})}
function mSub(k){
  if(TEST.run&&TEST.cur==k)return `<span class="lt">${fk(TEST.rate)} H</span> · ${Math.max(0,Math.ceil(TEST_SECS-TEST.el/1000))}ث`;
  if(TEST.res[k]!=null)return `<span class="lt">${fk(TEST.res[k])} H</span>`;
  return 'شغّل الاختبار'}
function mBar(k){
  if(TEST.run&&TEST.cur==k)return Math.min(100,TEST.el/(TEST_SECS*1000)*100);   /* أثناء اختبار هذا الوضع: تقدّم الوقت */
  if(TEST.res[k]==null)return 0;
  const mx=Math.max(...Object.values(TEST.res).map(Number));                      /* بعد الانتهاء: نسبة النتيجة إلى أعلى نتيجة */
  return mx>0?TEST.res[k]/mx*100:0}
function runLbl(){return TEST.run?ic('stop',14)+'إيقاف الاختبار':ic('play',14)+'اختبار السرعة'}
function uiModes(){
  if(sheet!=='modes'||!S)return;
  Object.keys(S.modes).forEach(k=>{const s=$('ms'+k),b=$('mb'+k);if(s)s.innerHTML=mSub(k);if(b)b.style.width=mBar(k)+'%'});
  const r=$('rb');if(r)r.innerHTML=runLbl()}
async function runBench(){
  if(TEST.run){stopBench();return}
  if(!window.crypto||!crypto.subtle)return toast('المتصفح لا يدعم WebCrypto');
  TEST.run=true;TEST.stop=false;TEST.res={};   /* كل اختبار جديد يبدأ بنتائج فارغة */
  const ks=Object.keys(S.modes).map(Number).sort((a,b)=>a-b);
  try{
    for(const k of ks){
      if(TEST.stop)break;
      TEST.cur=k;TEST.rate=0;TEST.el=0;
      const bc=BENCH[k]||[1,1];
      const r=await benchOne({w:Math.min(bc[0],MAXW),b:bc[1]});
      if(TEST.stop||(r&&r.stop))break;
      if(!r){toast('المتصفح لا يدعم الاختبار');break}
      TEST.res[k]=r.n;saveRes()
      TEST.el=0;uiModes()}
  }catch(e){toast('تعذّر تنفيذ الاختبار')}
  TEST.run=false;TEST.cur=0;uiModes()}
function modeClick(k){
  if(S.unlocked.includes(+k)){if(S.mode!=k)act('mode',{mode:+k})}
  else buy('mode_'+k)}
function modeCard(k){
  const m=S.modes[k],c=MC[k]||MC[1],un=S.unlocked.includes(+k),on=S.mode==k;
  const btn=on?`<span class="mbtn on">${ic('check',12)}نشط</span>`:
    un?`<span class="mbtn ok">اختر</span>`:
    `<span class="mbtn pr">${ic('lock',12)}${m.stars}${ic('star',12)}</span>`;
  return `<div class="mc ${on?'act':''}" style="--mc:${c[1]}" onclick="modeClick(${k})">
    <span class="mi">${ic(c[0],18)}</span>
    <div class="mt"><b>${esc(m.name)}</b><em>×${m.mul}</em><div class="ms" id="ms${k}">${mSub(k)}</div></div>${btn}
    <i class="mbar" id="mb${k}" style="width:${mBar(k)}%"></i></div>`}

function walletCard(){return `<div class="card"><div class="cap">WALLET · MAIN VAULT</div>
  <div class="row" style="margin-top:6px"><span>إجمالي الرصيد · HASH</span>${badge(S.tier)}</div>
  <div class="big" id="bal">${bal(S.balance)}</div>
  <div class="row" style="margin-top:12px"><span>XP · مستوى ${S.level}</span><span>${S.xp} / ${S.xp_need}</span></div>
  <div class="bar"><i style="width:${Math.min(100,S.xp/S.xp_need*100)}%;background:var(--pur)"></i></div>
  <div class="row"><span>ENERGY · +${S.regen}/د</span><span id="enx">${fi(S.energy)} / ${fi(S.max_energy)}</span></div>
  <div class="bar"><i id="enb" style="width:${Math.min(100,S.energy/S.max_energy*100)}%;background:var(--blue)"></i></div>
  <button class="pri" onclick="openSheet('energy')">⚡ احصل على طاقة</button></div>`}

const LOCKS={ban:{tag:'ACCOUNT · BANNED',foot:'banned',title:'تم حظر حسابك',
  desc:'تم إيقاف هذا الحساب بسبب مخالفة قواعد اللعبة، ولم يعد بإمكانك استخدام التطبيق.'},
 kicked:{tag:'SESSION · KICKED',foot:'kicked',title:'تم اكتشاف جلسات متعددة',
  desc:'سجّل الدخول إلى التطبيق من جهاز واحد فقط. تم فصل هذه الجلسة لأن جهازاً آخر فتح التطبيق.'}};
function showLock(kind,reason){if(BANNED)return;BANNED=true;sheet=null;try{tg.BackButton.hide()}catch(e){}
  const K=LOCKS[kind]||LOCKS.ban;
  const d=document.createElement('div');d.className='bnx';d.id='bn';
  d.innerHTML=`<div class="bnc"><i class="bk1"></i><i class="bk2"></i><i class="bk3"></i><i class="bk4"></i>
  <div class="bnt"><span><i class="bnd"></i>${K.tag}</span><span class="bnf">fail</span></div>
  <div class="bnb"><span class="bni">${ic('logout',24)}</span><h2>${K.title}</h2>
  <p>${K.desc}${reason?'<br><b>السبب:</b> '+esc(reason):''}</p>
  <button class="pri bnbtn" onclick="try{tg.close()}catch(e){}">إغلاق هذه النافذة ${ic('x',13)}</button></div>
  <div class="bnt bnl"><span>TRACE</span><span>${K.foot}</span></div></div>`;
  document.body.appendChild(d)}
function showBan(reason){showLock('ban',reason)}
function dparts(t){const d=new Date(t*1000),p=n=>String(n).padStart(2,'0');
  return [p(d.getDate())+'.'+p(d.getMonth()+1)+'.'+d.getFullYear(),p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())]}
function openBlk(i){BK=B[i];openSheet('blk')}
function cpHash(){if(BK&&BK.prev)navigator.clipboard.writeText(BK.prev).then(()=>toast('تم النسخ')).catch(()=>toast('تعذّر النسخ'))}
function blkCard(b,i){const d=dparts(b.t);
  return `<div class="bkc" onclick="openBlk(${i})">
  <div class="bkh"><span class="bkn">BLOCK<b>#${fi(b.id)}</b></span>
   <span class="bkr"><span class="rwp">مكافأتك<i class="cx" style="--s:11px">#</i><span class="rv">${bal(b.my)}</span></span>${ic('chev',14)}</span></div>
  <div class="bkm"><span class="bkl">المعدّن:</span>${avs(b.miner)}<b>${esc(b.miner)}</b>${badge(b.tier)}<span class="lvp">Lv ${b.level}</span></div>
  <div class="bkf"><span class="bkd">أُنشئت: ${d[0]}, ${d[1]}</span><span class="rar ${b.rare?'gr':''}">${b.rare?'غير عادي':'عادي'}</span></div></div>`}
function blkModal(){const b=BK;if(!b)return '';const d=dparts(b.t);
  const cell=(c,icn,l,v)=>`<div class="bmc2"><div class="bmcl"><span class="bmi ${c}">${ic(icn,13)}</span><span>${l}</span></div><div class="bmcv">${v}</div></div>`;
  return `<div class="ov mid" onclick="if(event.target===this)closeSheet()"><div class="shw"><div class="bm">
  <div class="bmh"><div><div class="bml">الكتلة</div><div class="bmn">#${fi(b.id)}</div></div>
   <div class="bmr"><div class="bmd">${ic('cal',14)}<div><b>${d[0]}</b><small>${d[1]}</small></div></div><button class="bmx" onclick="closeSheet()">${ic('x',14)}</button></div></div>
  <div class="bmy"><div class="bml">مكافأتك</div><div class="bmv"><i class="cx" style="--s:26px">#</i><span class="${b.my>0?'':'z'}">${bal(b.my)}</span></div></div>
  <div class="bmg">`+
   cell('o','user','المعدّن',esc(b.miner))+
   cell('t','users','عدد المعدّنين',fi(b.miners))+
   cell('g','link','مكافأة المعدّن','<i class="cx" style="--s:16px">#</i>'+bal(b.reward))+
   cell('y','share','المكافأة المشتركة','<i class="cx" style="--s:16px">#</i>'+bal(b.shared))+
  `</div><div class="bmf"><div class="bmfr"><span class="bml">هاش الكتلة السابقة</span><button class="bmc" onclick="cpHash()">${ic('copy',13)}نسخ</button></div>
  <div class="bmhash">${esc(b.prev||'—')}</div></div></div></div></div>`}

function vMine(){
  let h=walletCard();
  if(!S.chain.started)h+=S.chain.scheduled?`<div class="card"><div class="cap">LAUNCH</div><h2>⏳ التعدين لم يبدأ بعد</h2><p class="h">ينطلق في <b>${esc(S.chain.start_txt)}</b></p><div class="big" id="cdn" style="font-size:30px;text-align:center;margin-top:10px">--:--:--</div></div>`:
    `<div class="card"><div class="cap">LAUNCH</div><h2>⏳ التعدين لم يبدأ بعد</h2><p class="h">سيُعلن عن موعد الانطلاق قريباً.</p></div>`;
  else if(S.chain.done)h+=`<div class="card"><h2>🏁 اكتمل التعدين</h2><p class="h">تم تعدين جميع الكتل (${fi(S.chain.blocks)}).</p></div>`;
  const D=S.dex;
  h+=`<div class="grid"><div class="card mini" onclick="openSheet('dex')"><b>DEX β</b>
    <span class="h">1 ⚡ ≈ ${f(D.price,5)} #</span><br><span class="${D.change>=0?'up':'dn'}" style="direction:ltr;display:inline-block">${D.change>=0?'▲':'▼'} ${f(Math.abs(D.change))}%</span><br><span class="h">اشترِ، بِع ← فتح</span></div>
    <div class="card mini" onclick="openSheet('servers')"><b>🖥 الخوادم</b><span class="h">+${fi(S.srv)} H/s</span><br><span class="h">مركز إدارة الخوادم ← فتح</span></div></div>`;
  h+=`<div class="card"><div class="row"><div><b style="color:var(--tx);font-size:16px">👑 كن راعي التعدين</b><br><span class="h">×2 مكافأة كل كتلة · 24h+</span></div>
    <button class="sm pri" onclick="buy('patron')">⭐ ${S.prices.patron}</button></div>
    ${S.patron_left>0?`<p class="h" style="margin-top:8px;color:var(--grn)">فعّال · المتبقي ${fmtWait(S.patron_left)}</p>`:''}</div>`;
  h+=`<div class="card"><div class="row"><b style="color:var(--tx);font-size:16px">معلومات الكتلة <span class="tag">#${fi(S.next_block)}</span></b><span class="off">عادي</span></div>
    <div class="grid" style="margin:12px 0 0"><div class="cell"><span class="h">الصعوبة</span><b>${fk(S.difficulty)}</b></div>
    <div class="cell"><span class="h">إجمالي معدل الهاش</span><b>${fk(S.total_hr)}</b></div>
    <div class="cell"><span class="h">المكافأة</span><b>${f(S.block_reward,1)} #</b></div>
    <div class="cell"><span class="h">متصل</span><b>${fi(S.online)}</b></div></div></div>`;
  if(!S.permit){
    h+=`<div class="card"><h2>🔑 تصريح التعدين</h2><p class="h">لبدء التعدين تحتاج تصريحاً بدفعة واحدة.<br>النقاط افتراضية داخل اللعبة.</p>
    <button class="pri" onclick="buy('permit')">⭐ ادفع ${S.prices.permit} نجمة</button></div>`;
  }else{
    const m=S.modes[S.mode],mc=MC[S.mode]||MC[1];
    h+=`<div class="card"><div class="row"><b style="color:var(--tx);font-size:16px">⛏ تعدين</b>${S.mining?'<span class="live">قيد التشغيل</span>':'<span class="off">متوقف</span>'}</div>
    <div class="grid" style="margin:12px 0 0"><div class="cell"><span class="h">معدل الهاش</span><b>${fk(S.hashrate)} H/s</b></div>
    <div class="cell"><span class="h">هاش</span><b id="hs">${fk(S.hashes)}</b></div>
    <div class="cell"><span class="h">أسهم</span><b>${fi(S.wins)}</b></div>
    <div class="cell"><span class="h">الدخل</span><b>${f(S.income,1)} #</b></div></div>
    <div class="grid ctl" style="margin-top:0">${S.mining?'<button class="stop sbtn" onclick="act(\'mining\',{on:false})">⏹ إيقاف التعدين</button>':
      '<button class="pri sbtn" '+((S.chain.started&&!S.chain.done)?'':'disabled ')+'onclick="act(\'mining\',{on:true})"><span class="pbx">'+ic('play',16)+'</span>بدء التعدين</button>'}
    <button class="mdb" onclick="openSheet('modes')"><span class="mdi">${ic(mc[0],18)}</span><span class="mdt"><small>الوضع</small><b>${esc(m.name)}</b></span><span class="mdx">×${m.mul}</span>${ic('sliders',17)}</button></div>
    <button class="runb" onclick="openSheet('servers')">${ic('server',16)}مركز إدارة الخوادم</button></div>`;
  }
  h+=`<h3 style="margin:4px 2px 10px">آخر 25 كتلة</h3>`+(B.length?B.map(blkCard).join(''):'<div class="card"><p class="h">لا توجد كتل بعد.</p></div>');
  return h}

function vRefs(){
  const L=REFS||[],link=S.ref_link.replace('https://','');
  const rows=L.length?L.map((r,i)=>`<div class="lr ${r.me?'me':''}"><span class="lrk ${i<3?'tp':''}">${String(i+1).padStart(2,'0')}</span>${avs(r.name)}
    <div class="nm"><div class="nl"><b>${esc(r.name)}</b>${badge(r.tier)}<span class="lv">مستوى ${r.level}</span></div>
    <div class="am"><span class="rcn">${ic('users',12)}</span><span class="amt">${fi(r.count)}<small> · نشط ${fi(r.active)}</small></span></div></div></div>`).join('')
    :`<div class="em"><span class="emi">${ic('users',20)}</span><div>لا توجد إحالات حتى الآن. شارك الرابط لدعوة أصدقائك.</div></div>`;
  return `<div class="stc rc"><div class="rtop"><span class="mlb cap2"><i class="dot"></i>رابط الدعوة</span>
    <span class="rcount">${ic('users',14)}<b>${fi(S.refs)}</b><span class="mlb">مُحالين</span></span></div>
    <div class="lk"><span>${esc(link)}</span><button class="cp" onclick="navigator.clipboard.writeText(S.ref_link).then(()=>toast('تم النسخ'))">${ic('copy',17)}</button></div>
    <button class="inv" onclick="tg.openTelegramLink('https://t.me/share/url?url='+encodeURIComponent(S.ref_link)+'&text='+encodeURIComponent('⛏ تعال نعدّن معاً!'))">${ic('send',16)}دعوة</button></div>
  <div class="stc bn"><div class="bnr"><span class="stic or big">${ic('spark',18)}</span><div><span class="mlb cap2"><i class="dot"></i>قاعدة المكافأة</span><div class="bt">احصل على +${fi(S.ref_energy)} طاقة قصوى</div></div></div>
    <p class="bd">تحصل على +${fi(S.ref_energy)} طاقة قصوى عن كل مُحال يعدّن ${fi(S.ref_hours)} ساعة فعلية على الأقل (حتى ${fi(S.ref_max)} مُحال). المُحالون النشطون حالياً: <b style="color:var(--tx)">${fi(S.ref_active)}</b> / ${fi(S.ref_max)}. الإحالات الوهمية لا تُحتسب.</p></div>
  <div class="stc"><div class="sth lbh"><div><div class="lbt">أفضل 100 إحالة</div><div class="lbs">لوحة الصدارة</div></div><span class="lbn">${String(L.length).padStart(3,'0')}</span></div>${rows}</div>`}
function vTasks(){
  const lab=['+1,000','+2,000','+3,000','+5,000','+7,500','+12,000'];
  const ready=S.daily_wait<=0;
  let h=`<div class="card"><div class="cap">DAILY · STREAK</div><h2>🎁 مكافآت يومية</h2><p class="h">عُد كل يوم — تزداد المكافآت (الطاقة تتجاوز الحد الأقصى).</p><div class="days" style="margin-top:12px">`+
  lab.map((l,i)=>`<div class="day ${i<S.day_idx?'on':''}"><span>اليوم ${i+1}</span><b>⚡ ${l}</b>${i<S.day_idx?'✓':'🔒'}</div>`).join('')+
  `<div class="day wide ${S.day_idx>6?'on':''}"><span>اليوم 7</span><b style="color:var(--blue)">SLIM</b><span>خادم تعدين SLIM · 30 دقيقة</span></div></div>
  <button class="pri" ${ready?'':'disabled'} onclick="act('daily')">${ready?'🎁 استلم مكافأة اليوم':'التالية بعد '+fmtWait(S.daily_wait)}</button></div>`;
  if(S.tasks.length)h+=`<div class="card"><h3>المهام</h3>`+S.tasks.map(t=>`<div class="rk"><span class="n">${esc(t.title)}</span><span class="tag">+${t.reward}</span>
  ${t.done?'<span>✅</span>':`<button class="sm pri" onclick="tg.openTelegramLink('https://t.me/${esc(S.channel.replace('@',''))}');setTimeout(()=>act('task',{task:'${t.id}'}),4000)">انضم</button>`}</div>`).join('')+'</div>';
  return h}

const ICO={trend:'<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
 trophy:'<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
 users:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
 rocket:'<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
 cal:'<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
 pick:'<path d="M14.531 12.469 6.619 20.38a1 1 0 1 1-3-3l7.912-7.912"/><path d="M15.686 4.314A12.5 12.5 0 0 0 5.461 2.958 1 1 0 0 0 5.58 4.71a22 22 0 0 1 6.318 3.393"/><path d="M17.7 3.7a1 1 0 0 0-1.4 0l-4.6 4.6a1 1 0 0 0 0 1.4l2.6 2.6a1 1 0 0 0 1.4 0l4.6-4.6a1 1 0 0 0 0-1.4z"/><path d="M19.686 8.314a12.501 12.501 0 0 1 1.356 10.225 1 1 0 0 1-1.751-.119 22 22 0 0 0-3.393-6.319"/>',
 list:'<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
 bars:'<path d="M3 21h18"/><path d="M7 21V12"/><path d="M12 21V5"/><path d="M17 21v-9"/>',
 wallet:'<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>'};
Object.assign(ICO,{copy:'<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
 send:'<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
 spark:'<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>'});
Object.assign(ICO,{
 gauge:'<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
 zap:'<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
 flame:'<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
 atom:'<circle cx="12" cy="12" r="1"/><path d="M20.2 20.2c2.04-2.03.02-7.36-4.5-11.9-4.54-4.52-9.87-6.54-11.9-4.5-2.04 2.03-.02 7.36 4.5 11.9 4.54 4.52 9.87 6.54 11.9 4.5Z"/><path d="M15.7 15.7c4.52-4.54 6.54-9.87 4.5-11.9-2.03-2.04-7.36-.02-11.9 4.5-4.52 4.54-6.54 9.87-4.5 11.9 2.03 2.04 7.36.02 11.9-4.5Z"/>',
 orbit:'<circle cx="12" cy="12" r="3"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><path d="M10.4 21.9a10 10 0 0 0 9.941-15.416"/><path d="M13.5 2.1a10 10 0 0 0-9.841 15.416"/>',
 radiation:'<path d="M12 12h.01"/><path d="M7.5 4.2c-.3-.5-.9-.7-1.3-.4C3.9 5.5 2.3 8.1 2 11c-.1.5.4 1 1 1h5c0-1.5.8-2.8 2-3.4-1.1-1.9-2-3.5-2.5-4.4z"/><path d="M21 12.1c.6 0 1.1-.5 1-1-.3-2.9-1.8-5.5-4.1-7.1-.4-.3-1.1-.2-1.3.3-.6.9-1.5 2.5-2.6 4.3 1.2.7 2 2 2 3.5h5z"/><path d="M7.5 19.8c-.3.5-.1 1.1.4 1.3 2.6 1.2 5.6 1.2 8.2 0 .5-.2.7-.8.4-1.3-.5-.9-1.4-2.5-2.5-4.3-1.2.7-2.8.7-4 0-1.1 1.8-2 3.4-2.5 4.3z"/>',
 lock:'<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
 star:'<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z" fill="currentColor"/>',
 check:'<path d="M20 6 9 17l-5-5"/>',
 play:'<polygon points="6 3 20 12 6 21 6 3"/>',
 stop:'<rect width="14" height="14" x="5" y="5" rx="2"/>',
 x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 sliders:'<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
 server:'<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><path d="M6 6h.01M6 18h.01"/>'});
Object.assign(ICO,{logout:'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',share:'<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.59 13.51 6.83 3.98"/><path d="m15.41 6.51-6.82 3.98"/>',chev:'<path d="m15 18-6-6 6-6"/>',
 user:'<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
 link:'<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'});
const ic=(n,z)=>`<svg viewBox="0 0 24 24" width="${z||20}" height="${z||20}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICO[n]}</svg>`;
const NI={mine:'pick',refs:'users',tasks:'list',top:'bars',wallet:'wallet'};
function avs(n){let h=0;for(const c of String(n))h=(h*31+c.charCodeAt(0))%360;return `<span class="avs" style="background:linear-gradient(160deg,hsl(${h},68%,66%),hsl(${h},58%,48%))">${esc(String(n).trim().slice(0,2).toUpperCase())}</span>`}
function amt(n){const s=f(n,4);return '<i class="cn">#</i><span class="amt">'+s.slice(0,-3)+'<small>'+s.slice(-3)+'</small></span>'}
function who(name,lv,t,tot){return `<div class="nm"><div class="nl"><b>${esc(name)}</b>${badge(t)}<span class="lv">مستوى ${lv}</span></div><div class="am">${amt(tot)}</div></div>`}
function ago(t){const x=Math.max(0,Date.now()/1000-t);if(x<60)return 'الآن';if(x<3600)return 'منذ '+Math.floor(x/60)+' د';if(x<86400)return 'منذ '+Math.floor(x/3600)+' س';return 'منذ '+Math.floor(x/86400)+' ي'}
function cdParts(l){l=Math.max(0,Math.floor(l));return [Math.floor(l/86400),Math.floor(l%86400/3600),Math.floor(l%3600/60),l%60]}
function wkTick(){if(!TOP||!TOP.week||tab!=='top'||topView!=='week')return;
  const W=TOP.week,now=Date.now()/1000,p=cdParts(W.end_ts-now);
  ['d','h','m','s'].forEach((k,i)=>{const e=$('wk'+k);if(e)e.textContent=String(p[i]).padStart(2,'0')});
  const b=$('wkp');if(b)b.style.width=Math.min(100,Math.max(0,(now-W.start_ts)/(W.end_ts-W.start_ts)*100))+'%'}
function vTopWeek(){
  const W=TOP&&TOP.week;if(!W)return '<p class="h">…</p>';
  const L=W.top,M=W.mine,X=W.me,m=S.modes[X.mode]||S.modes[1],mc=MC[X.mode]||MC[1],c=mc[1];
  const p=cdParts(W.end_ts-Date.now()/1000),pr=Math.min(100,Math.max(0,(Date.now()/1000-W.start_ts)/(W.end_ts-W.start_ts)*100));
  const cell=(st,icn,l,v)=>`<div class="bmc2"><div class="bmcl"><span class="bmi" style="${st}">${ic(icn,13)}</span><span>${l}</span></div><div class="bmcv">${v}</div></div>`;
  const col=x=>`color:${x};background:${x}1f;border-color:${x}66`;
  const gapTxt=W.rank===1?'👑 الأول':(W.gap==null?'—':'+'+f(W.gap,2));
  const gapLbl=W.rank===1?'مركزك':(W.gap_kind==='top100'?'للدخول في أفضل 100':'للمركز الأعلى');
  const card=`<div class="bm flat">
  <div class="bmh"><div><div class="bml">تصنيفي الأسبوعي</div><div class="bmn">#${fi(W.rank)}</div></div>
   <div class="bmr"><div class="bmd">${ic('users',14)}<div><b>${fi(W.players)}</b><small>منافس</small></div></div></div></div>
  <div class="wkc"><div class="bmd">${ic('cal',14)}<div><small>بداية الأسبوع</small><b>${esc(W.start)}</b><small>00:00</small></div></div>
   <div class="bmd">${ic('cal',14)}<div><small>نهاية الأسبوع</small><b>${esc(W.end)}</b><small>23:59</small></div></div></div>
  <div class="bmy"><div class="bml">المتبقي لنهاية الأسبوع</div>
   <div class="cdw"><div class="cdb"><b id="wkd">${String(p[0]).padStart(2,'0')}</b><small>يوم</small></div><span class="cds">:</span>
   <div class="cdb"><b id="wkh">${String(p[1]).padStart(2,'0')}</b><small>ساعة</small></div><span class="cds">:</span>
   <div class="cdb"><b id="wkm">${String(p[2]).padStart(2,'0')}</b><small>دقيقة</small></div><span class="cds">:</span>
   <div class="cdb"><b id="wks">${String(p[3]).padStart(2,'0')}</b><small>ثانية</small></div></div>
   <div class="pb"><i id="wkp" style="width:${pr}%"></i></div></div>
  <div class="bmy"><div class="bml">أرباحي هذا الأسبوع</div><div class="bmv"><i class="cx" style="--s:26px">#</i><span class="${M.total>0?'':'z'}">${bal(M.total)}</span></div></div>
  <div class="bmg g3">`+
   cell(col(c),mc[0],'وضعك',`<span class="ar">${esc(m.name)}</span> <span class="sm">×${m.mul}</span>`)+
   cell(col('#ffa861'),'bars','المستوى',`Lv ${X.level} <span class="sm">${fk(X.hr)} H/s</span>`)+
   cell(col('#3ecf8e'),'pick','آخر كتلة عدّنتها',X.last_win?`#${fi(X.last_win)} <span class="sm">${ago(X.last_win_at)}</span>`:'<span class="sm">لم تفز بعد</span>')+
   cell(col('#ffc23f'),'trophy','كتل فزت بها',fi(X.wins)+' <span class="sm">هذا الأسبوع</span>')+
   cell(col('#38b6ff'),'trend',gapLbl,gapTxt)+
   cell(col(S.mining?'#3ecf8e':'#8b9098'),'server','حالة التعدين',`<span class="ar">${S.mining?'قيد التشغيل':'متوقف'}</span>`)+
  `</div><div class="bmf"><div class="mnote" style="margin:0">يُصفَّر الترتيب كل اثنين 00:00، وأفضل 100 يصلهم تهنئة من الإدارة.</div></div></div>`;
  return card+`<div class="stc"><div class="sth"><span class="stic">${ic('trophy',14)}</span>أفضل 100 هذا الأسبوع</div>`+
  (L.length?L.map((r,i)=>`<div class="lr ${r.me?'me':''}"><span class="lrk ${i<3?'tp':''}">${String(i+1).padStart(2,'0')}</span>${avs(r.name)}${who(r.name,r.level,r.tier,r.total)}</div>`).join(''):'<p class="h" style="padding:14px">لا أحد بعد هذا الأسبوع.</p>')+'</div>'}
function vTop(){
  const seg=`<div class="seg" style="margin-bottom:12px"><button class="${topView==='all'?'on':''}" onclick="setView('all')">الإجمالي</button><button class="${topView==='week'?'on':''}" onclick="setView('week')">الأسبوعي</button></div>`;
  return seg+(topView==='week'?vTopWeek():vTopAll())}
function vTopAll(){
  if(!TOP)return '<p class="h">…</p>';
  const C=TOP.chain,M=TOP.mine;
  return `<div class="stc"><div class="sth"><span class="stic">${ic('trend',14)}</span>إحصائيات البلوكشين</div>
  <div class="tmb"><div class="tr"><span class="mlb">إجمالي المُعدَّن</span><span class="pc">${f(C.pct)}<small>%</small></span></div>
  <div class="pb"><i style="width:${Math.min(100,C.pct)}%"></i></div></div>
  <div class="st3"><div><div class="fl">${ic('users',12)}الحائزون</div><div class="fv">${fi(C.holders)}</div></div>
  <div><div class="fl">${ic('rocket',12)}الإصدار</div><div class="fv">${fi(C.emission)}</div></div>
  <div><div class="fl">${ic('cal',12)}البداية</div><div class="fv">${esc(C.start_date)}</div></div></div></div>
  <div class="stc"><div class="sth"><span class="stic or">${ic('trophy',14)}</span>ترتيبي<span class="rb">#${fi(TOP.rank)}</span></div>
  <div class="mb">${avs(M.name)}${who(M.name,M.level,M.tier,M.total)}</div></div>
  <div class="stc"><div class="sth"><span class="stic">${ic('trophy',14)}</span>أفضل 100 معدّن</div>`+
  (TOP.top.length?TOP.top.map((r,i)=>`<div class="lr ${r.me?'me':''}"><span class="lrk ${i<3?'tp':''}">${String(i+1).padStart(2,'0')}</span>${avs(r.name)}${who(r.name,r.level,r.tier,r.total)}</div>`).join(''):'<p class="h" style="padding:14px">لا أحد بعد.</p>')+'</div>'}
function vWallet(){return `<div class="card"><div class="row"><span>الرصيد الإجمالي</span><span>HASH</span></div><div class="big">${bal(S.balance)}</div></div>
  <div class="card"><h3>🎁 لديك كود؟</h3>
  <input id="cd" class="inp" placeholder="TH-XXXXXXXX" autocapitalize="characters" autocomplete="off">
  <button class="pri" onclick="redeem()">استرداد</button></div>
  <div class="card"><h3>المتجر (نجوم تيليجرام)</h3>
  ${S.permit?'<p class="h">✅ تصريح التعدين مفعّل.</p>':`<button class="pri" onclick="buy('permit')">🔑 تصريح التعدين — ⭐ ${S.prices.permit}</button>`}
  <button onclick="buy('energy_up')">🚀 زيادة الطاقة القصوى (+2000، +5/د) — ⭐ ${S.prices.energy_up}</button>
  <button onclick="buy('restore')">🔋 استعادة الطاقة — ⭐ ${S.prices.restore}</button>
  <button onclick="buy('patron')">👑 راعي التعدين ×2 (24h) — ⭐ ${S.prices.patron}</button></div>
  <div class="card"><h3>❤️ ادعم المطوّر</h3><p class="h">اختر المبلغ الذي تريده من النجوم.</p>
  <div class="seg" style="margin-top:10px"><button onclick="$('sp').value=10">10</button><button onclick="$('sp').value=50">50</button><button onclick="$('sp').value=100">100</button></div>
  <input id="sp" class="inp" type="number" inputmode="numeric" min="1" placeholder="عدد النجوم" style="margin-top:10px">
  <button class="pri" onclick="donate()">⭐ ادعم الآن</button></div>
  <div class="card"><p class="h">ℹ️ النقاط افتراضية داخل اللعبة فقط، ولا تمثل عملة رقمية ولا يمكن سحبها أو تحويلها لمال.</p></div>`}

function sheetHTML(){
  if(!sheet)return '';
  if(sheet==='blk')return blkModal();
  let b='';
  if(sheet==='energy'){
    b=`<div class="cap">ENERGY · UPGRADE</div><h2>زيادة الطاقة القصوى</h2>
    <p class="h">ارفع احتياطي الطاقة القصوى باستخدام Telegram Stars. ستعود طاقتك الحالية إلى الحد الأقصى بعد الاستعادة.</p>
    <div class="grid" style="margin-top:12px"><div class="cell"><span class="h">الطاقة القصوى</span><b>+2,000</b><span class="h">← ${fi(S.max_energy+2000)}</span></div>
    <div class="cell"><span class="h">في الدقيقة</span><b>+5</b><span class="h">← ${S.regen+5}</span></div></div>
    <button class="pri" onclick="buy('energy_up')">⭐ ادفع ${S.prices.energy_up} STARS</button>
    <p class="h" style="text-align:center;margin:14px 0 0">— أو —</p>
    <h3 style="margin-top:8px">استعادة الطاقة</h3><p class="h">استعد طاقتك الحالية إلى الحد الأقصى مقابل رسوم صغيرة.</p>
    <button onclick="buy('restore')">⭐ ادفع ${S.prices.restore} STARS</button>`;
  }else if(sheet==='servers'){
    b=`<div class="cap">SERVER · CONTROL CENTER</div><h2>🖥 مركز إدارة الخوادم</h2>
    <p class="h">كل خادم يزيد معدل الهاش دائماً. السعر يرتفع 25% مع كل نسخة. زيادتك الحالية: <b>+${fi(S.srv)} H/s</b></p>`+
    S.temps.map(t=>`<div class="rk"><span class="n">${esc(t.title)} <span class="h">مؤقت</span></span><span class="tag">+${fi(t.hr)} H/s</span><span class="h">${fmtLeft(t.left)}</span></div>`).join('')+
    S.servers.map(s=>`<div class="rk"><span class="n">${esc(s.title)} <span class="h">×${s.owned}/${s.max}</span><br><span class="h">+${fi(s.hr)} H/s</span></span>
    <button class="sm pri" ${s.owned>=s.max?'disabled':''} onclick="act('server',{key:'${s.key}'})">${fi(s.cost)} #</button></div>`).join('');
  }else if(sheet==='dex'){
    const D=S.dex;
    b=`<div class="cap">DEX · β</div><h2>تبادل HASH ⇄ طاقة</h2>
    <p class="h">1 ⚡ ≈ <b style="color:var(--tx)">${f(D.price,5)}</b> # · <span class="${D.change>=0?'up':'dn'}" style="direction:ltr;display:inline-block">${D.change>=0?'▲':'▼'} ${f(Math.abs(D.change))}%</span> · عمولة البيع ${Math.round(D.fee*100)}%</p>
    <div class="seg" style="margin:12px 0"><button class="${dxSide==='buy'?'on':''}" onclick="dxSide='buy';dxVal='';draw()">شراء طاقة</button>
    <button class="${dxSide==='sell'?'on':''}" onclick="dxSide='sell';dxVal='';draw()">بيع طاقة</button></div>
    <p class="h" style="margin-bottom:6px">${dxSide==='buy'?'المبلغ بـ HASH (رصيدك '+f(S.balance,2)+')':'كمية الطاقة (لديك '+fi(S.energy)+')'}</p>
    <input id="dxi" class="inp" type="number" inputmode="decimal" placeholder="0" value="${esc(dxVal)}" oninput="dxVal=this.value;$('dxp').textContent=dxPrev()">
    <p class="h" id="dxp" style="text-align:center;margin:8px 0 0">${dxPrev()}</p>
    <button class="pri" onclick="dxGo()">${dxSide==='buy'?'شراء':'بيع'}</button>`;
  }else if(sheet==='modes'){
    const ks=Object.keys(S.modes).map(Number).sort((a,b)=>a-b),tot=ks.length*TEST_SECS;
    b=`<div class="cap cap2"><i class="dot"></i>MINING · MODES</div><h2>أوضاع التعدين</h2>
    <p class="h">كل مستوى يستخدم طريقة بحث هاش مختلفة — درجات متفاوتة من توازي WebCrypto. أداء موحد على الجوال والكمبيوتر.</p>`+
    ks.map(k=>modeCard(k)).join('')+
    `<button class="runb" id="rb" onclick="runBench()">${runLbl()}</button>
    <p class="mnote">يستغرق الاختبار نحو ${tot} ثانية (${TEST_SECS} ث لكل وضع). لا تستخدم الجهاز خلال الاختبار للحصول على أدق نتيجة.</p>`;
    return `<div class="ov mid" onclick="if(event.target===this)closeSheet()"><div class="shw"><button class="xb" onclick="closeSheet()">${ic('x',16)}</button><div class="sh lt">${b}</div></div></div>`}
  return `<div class="ov" onclick="if(event.target===this)closeSheet()"><div class="sh"><button class="sm" style="float:left;margin:0" onclick="closeSheet()">✕</button>${b}</div></div>`}

const V={mine:vMine,refs:vRefs,tasks:vTasks,top:vTop,wallet:vWallet};
function draw(){
  $('nav').innerHTML=TABS.map(t=>`<div class="${t[0]===tab?'on':''}" onclick="go('${t[0]}')">${t[0]==='tasks'?'<i class="earn">EARN<b></b><b></b></i>':''}<span>${ic(NI[t[0]])}</span>${t[2]}</div>`).join('');
  $('app').innerHTML='<div class="w" style="padding:0">'+V[tab]()+'</div>';
  $('sh').innerHTML=sheetHTML()}
setInterval(()=>{ // تحديث محلي سلس بين طلبات السيرفر
  if(!S||!S.mining||tab!=='mine')return;
  const dt=.25,run=Math.min(dt,S.energy/S.cost);
  S.balance+=run*S.rate;S.energy-=run*S.cost;S.hashes+=run*S.hashrate;
  if($('bal'))$('bal').innerHTML=bal(S.balance);
  if($('hs'))$('hs').textContent=fk(S.hashes);
  if($('enx'))$('enx').textContent=fi(S.energy)+' / '+fi(S.max_energy);
  if($('enb'))$('enb').style.width=Math.min(100,S.energy/S.max_energy*100)+'%';
  if(S.energy<=0)load().catch(()=>{})},250);
setInterval(()=>{if(S&&!document.hidden&&sheet!=='dex'&&sheet!=='modes'&&sheet!=='blk'&&document.activeElement.tagName!=='INPUT')load().catch(()=>{})},6000);
setInterval(()=>{if(!S||!S.chain||S.chain.started||!S.chain.scheduled)return;
  let l=Math.floor(S.chain.start-Date.now()/1000);if(l<=0){load().catch(()=>{});return}
  const e=$('cdn'),d=Math.floor(l/86400),h=Math.floor(l%86400/3600),m=Math.floor(l%3600/60),s=l%60,p=n=>String(n).padStart(2,'0');
  if(e)e.textContent=(d?d+'ي ':'')+p(h)+':'+p(m)+':'+p(s)},1000);
setInterval(wkTick,1000);
if(!tg.initData){$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">افتح التطبيق من داخل تيليجرام.</p>'}
else load().catch(e=>{if(BANNED)return;$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">'+esc(e.message)+'<br><br><button class="pri" style="width:auto;padding:10px 24px" onclick="location.reload()">إعادة المحاولة</button></p>'});
</script></body></html>
"""


# ═══════════ 7) التشغيل ═══════════
def main():
    init_db()
    app = (Application.builder().token(BOT_TOKEN).defaults(Defaults(parse_mode=ParseMode.HTML))
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    txt = filters.TEXT & ~filters.COMMAND
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("refund", refund_start)],
        states={R_UID: [MessageHandler(txt, refund_uid)],
                R_PICK: [MessageHandler(txt, refund_pick)],
                R_CONF: [MessageHandler(txt, refund_conf)]},
        fallbacks=[CommandHandler("cancel", refund_cancel)], allow_reentry=True))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("settitle", cmd_settitle))
    app.add_handler(CommandHandler("setstart", cmd_setstart))
    app.add_handler(CommandHandler("resetall", cmd_resetall))
    app.add_handler(CommandHandler("setblocks", cmd_setblocks))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("gencode", cmd_gencode))
    app.add_handler(CommandHandler("codes", cmd_codes))
    app.add_handler(CommandHandler("delcode", cmd_delcode))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(cb_stats, pattern="^stats$"))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("bcconfirm", cmd_bcconfirm))
    app.add_handler(CommandHandler("bccancel", cmd_bccancel))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("bans", cmd_bans))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("restore", cmd_restore_help))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/restore"), cmd_restore))
    app.add_handler(CommandHandler("suspects", cmd_suspects))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("addsched", cmd_addsched))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("delsched", cmd_delsched))
    app.add_handler(CommandHandler("togglesched", cmd_togglesched))
    app.add_handler(CommandHandler("testsched", cmd_testsched))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("weeksend", cmd_weeksend))
    app.add_handler(CommandHandler("wkconfirm", cmd_wkconfirm))
    app.add_handler(CommandHandler("wkcancel", cmd_wkcancel))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    log.info("Bot started (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
