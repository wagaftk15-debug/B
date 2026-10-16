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

import os, sys, json, hmac, hashlib, time, asyncio, sqlite3, logging, threading, html, secrets, math, random
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
                          ContextTypes, Defaults, filters)

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

# اللعبة (كلها نقاط افتراضية داخل البوت)
ITEMS = {
    "permit": dict(stars=500, title="تصريح التعدين", desc="يفتح لك التعدين في اللعبة (نقاط افتراضية)."),
    "energy_up": dict(stars=59, title="زيادة الطاقة القصوى", desc="+2000 طاقة قصوى و +5 طاقة/دقيقة."),
    "restore": dict(stars=32, title="استعادة الطاقة", desc="يملأ طاقتك للحد الأقصى فوراً."),
    "patron": dict(stars=99, title="راعي التعدين", desc="مكافأة الكتلة ×2 لمدة 24 ساعة (تتراكم)."),
}
BASE_HR = 1000          # معدل الهاش الأساسي (H/s)
COST = 1.0              # طاقة/ثانية أثناء التعدين
BLOCK_REWARD = 50       # مكافأة الكتلة (كل 5 ثوانٍ، تُسحب بالقرعة حسب قوة الهاش)
REF_BONUS = 500         # مكافأة المُحيل عند شراء المُحال للتصريح
DAILY = [1000, 2000, 3000, 5000, 7500, 12000]   # اليوم 7: خادم SLIM لمدة 30 دقيقة
PATRON_SECS = 86400
MAX_SRV = 10            # أقصى عدد من كل نوع خادم دائم

# أوضاع التعدين: مضاعف الهاش، مضاعف استهلاك الطاقة
MODES = {1: dict(name="عادي", mul=1.0, cost=1.0),
         2: dict(name="تيربو", mul=2.0, cost=2.5),
         3: dict(name="نيترو", mul=4.0, cost=6.0)}

# الخوادم: hr = زيادة الهاش، cost بنقاط HASH (0 = مكافأة فقط)، mins = مدة مؤقتة
SERVERS = {
    "slim": dict(title="SLIM", hr=500, cost=0, mins=30),
    "nano": dict(title="NANO", hr=800, cost=1000, mins=0),
    "core": dict(title="CORE", hr=3000, cost=5000, mins=0),
    "rack": dict(title="RACK", hr=12000, cost=20000, mins=0),
    "farm": dict(title="FARM", hr=50000, cost=80000, mins=0),
}
DEX_BASE = 0.012        # سعر طاقة واحدة بنقاط HASH
DEX_FEE = 0.03          # عمولة البيع
TASKS = {"channel": {"title": "اشترك في القناة", "reward": 300}} if TASK_CHANNEL else {}
SQL_ACTIVE = "mining=1 AND energy-(?-last_tick)>1"


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
    add_cols("m_users", [("mode", "INT NOT NULL DEFAULT 1"),
                         ("hr", "DOUBLE PRECISION NOT NULL DEFAULT 1000"),
                         ("hashes", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("wins", "INT NOT NULL DEFAULT 0"),
                         ("income", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                         ("patron", "DOUBLE PRECISION NOT NULL DEFAULT 0")])
    add_cols("m_blocks", [("thr", "DOUBLE PRECISION NOT NULL DEFAULT 0")])
    for ddl in ("CREATE INDEX IF NOT EXISTS ix_m_total ON m_users (total DESC)",
                "CREATE INDEX IF NOT EXISTS ix_m_ref ON m_users (referrer)",
                "CREATE INDEX IF NOT EXISTS ix_m_srv ON m_servers (user_id)"):
        with cursor() as cur:
            cur.execute(ddl)
    log.info("DB ready (%s)", "PostgreSQL" if USE_PG else "SQLite")


# ═══════════ 3) منطق التعدين (يُحسب كسولاً عند كل طلب) ═══════════
def mode_of(u):
    return MODES.get(u.get("mode") or 1, MODES[1])


def hashrate(u):
    return (BASE_HR * (1 + 0.05 * (u["level"] - 1)) + u.get("srv", 0)) * mode_of(u)["mul"]


def tier(hr):
    if hr >= 20000:
        return "super"
    if hr >= 5000:
        return "nitro"
    if hr >= 1500:
        return "turbo"
    return ""


def xp_need(level):
    return 150 + 144 * level


def settle(u):
    now = time.time()
    dt = max(0.0, now - u["last_tick"])
    if u["mining"]:
        cost = COST * mode_of(u)["cost"]
        hr = hashrate(u)
        run = min(dt, u["energy"] / cost)
        earned = run * hr / 1e5
        u["energy"] = max(0.0, u["energy"] - run * cost)
        u["balance"] += earned
        u["total"] += earned
        u["hashes"] += run * hr
        u["xp"] += earned * 5
        while u["xp"] >= xp_need(u["level"]):
            u["xp"] -= xp_need(u["level"])
            u["level"] += 1
        if u["energy"] <= 0.01 or run < dt:
            u["mining"] = 0
    elif u["energy"] < u["max_energy"]:
        u["energy"] = min(u["max_energy"], u["energy"] + u["regen"] / 60 * dt)
    u["last_tick"] = now


class Err(Exception):
    pass


def ensure_user(cur, uid, name, username, ref=None):
    ref_ok = None
    if ref and ref != uid:
        cur.execute("SELECT id FROM m_users WHERE id=?", (ref,))
        if cur.fetchone():
            ref_ok = ref
    cur.execute("""INSERT INTO m_users(id,name,username,referrer,last_tick,created_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET name=excluded.name, username=excluded.username""",
                (uid, name, username, ref_ok, time.time(), int(time.time())))


def lock_user(cur, uid):
    cur.execute("SELECT * FROM m_users WHERE id=?" + (" FOR UPDATE" if USE_PG else ""), (uid,))
    u = cur.fetchone()
    if not u:
        raise Err("user")
    cur.execute("SELECT kind FROM m_servers WHERE user_id=? AND (expires=0 OR expires>?)", (uid, time.time()))
    u["srv"] = sum(SERVERS[r["kind"]]["hr"] for r in cur.fetchall() if r["kind"] in SERVERS)
    settle(u)
    return u


def save_user(cur, u):
    cur.execute("""UPDATE m_users SET permit=?,level=?,xp=?,balance=?,total=?,energy=?,max_energy=?,
        regen=?,mining=?,last_tick=?,streak=?,last_daily=?,mode=?,hr=?,hashes=?,wins=?,income=?,patron=?
        WHERE id=?""",
                (u["permit"], u["level"], u["xp"], u["balance"], u["total"], u["energy"], u["max_energy"],
                 u["regen"], u["mining"], u["last_tick"], u["streak"], u["last_daily"], u["mode"],
                 hashrate(u), u["hashes"], u["wins"], u["income"], u["patron"], u["id"]))


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
                balance=u["balance"], energy=u["energy"], max_energy=u["max_energy"], regen=u["regen"],
                mining=bool(u["mining"]), hashrate=hr, rate=hr / 1e5, cost=COST * m["cost"],
                mode=u["mode"], hashes=u["hashes"], wins=u["wins"], income=u["income"],
                patron_left=max(0, int(u["patron"] - time.time())), srv=u["srv"], tier=tier(hr),
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
            cur.execute("SELECT referrer FROM m_users WHERE id=?", (uid,))
            ref = (cur.fetchone() or {}).get("referrer")
            if ref:
                add_points(cur, ref, REF_BONUS)
        elif key == "energy_up":
            u["max_energy"] += 2000
            u["regen"] += 5
        elif key == "restore":
            u["energy"] = max(u["energy"], u["max_energy"])
        elif key == "patron":
            u["patron"] = max(time.time(), u["patron"]) + PATRON_SECS
        save_user(cur, u)
    return True


def make_block():
    """كتلة كل 5 ثوانٍ؛ الفائز بالقرعة موزونة بقوة الهاش بين المعدّنين النشطين فعلاً."""
    now = time.time()
    with cursor() as cur:
        cur.execute("DELETE FROM m_servers WHERE expires>0 AND expires<?", (now,))
        cur.execute("SELECT id,name,level,hr,patron FROM m_users WHERE " + SQL_ACTIVE + " LIMIT 3000", (now,))
        ms = cur.fetchall()
        if not ms:
            return
        total = sum(m["hr"] for m in ms)
        w = random.choices(ms, weights=[max(m["hr"], 1.0) for m in ms])[0]
        rew = BLOCK_REWARD * (2 if w["patron"] > now else 1)
        cur.execute("UPDATE m_users SET balance=balance+?,total=total+?,wins=wins+1,income=income+? WHERE id=?",
                    (rew, rew, rew, w["id"]))
        cur.execute("INSERT INTO m_blocks(miner_id,miner_name,miner_level,reward,miners,thr,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (w["id"], w["name"], w["level"], rew, len(ms), total, int(now)))
        cur.execute("DELETE FROM m_blocks WHERE id < (SELECT MAX(id)-200 FROM m_blocks)")


# ═══════════ 3.1) DEX: تبادل HASH ↔ طاقة ═══════════
def dex_price(t):
    return DEX_BASE * (1 + 0.06 * math.sin(t / 1800) + 0.03 * math.sin(t / 421 + 1))


def dex_info():
    now = time.time()
    p, old = dex_price(now), dex_price(now - 3600)
    return dict(price=p, change=(p / old - 1) * 100, fee=DEX_FEE)


# ═══════════ 3.2) أكواد الاسترداد ═══════════
CODE_KINDS = {"energy": "طاقة", "hash": "نقاط HASH", "permit": "تصريح التعدين"}


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
                ref_bonus=REF_BONUS, daily=DAILY, dex=dex_info(), servers=servers, temps=temps,
                modes={k: dict(name=v["name"], mul=v["mul"], cost=v["cost"]) for k, v in MODES.items()},
                ref_link=f"https://t.me/{MAIN_APP.bot.username}?start=ref_{u['id']}",
                tasks=[dict(id=k, title=v["title"], reward=v["reward"], done=k in done) for k, v in TASKS.items()],
                channel=TASK_CHANNEL, prices={k: v["stars"] for k, v in ITEMS.items()})


def act_mining(tg, b):
    with cursor() as cur:
        u = touch(cur, tg)
        if b.get("on"):
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
            if u["energy"] + gain > u["max_energy"] + 0.5:
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


def act_blocks(tg, b):
    with cursor() as cur:
        cur.execute("SELECT * FROM m_blocks ORDER BY id DESC LIMIT 25")
        rows = cur.fetchall()
    return {"blocks": [dict(id=r["id"], miner=r["miner_name"], level=r["miner_level"], reward=r["reward"],
                            t=r["created_at"], miners=r["miners"]) for r in rows]}


def act_top(tg, b):
    with cursor() as cur:
        cur.execute("SELECT id,name,level,total,hr FROM m_users WHERE total>0 ORDER BY total DESC LIMIT 100")
        rows = cur.fetchall()
        cur.execute("SELECT total FROM m_users WHERE id=?", (tg["id"],))
        mine = (cur.fetchone() or {}).get("total", 0)
        cur.execute("SELECT COUNT(*)+1 AS r FROM m_users WHERE total>?", (mine,))
        rank = cur.fetchone()["r"]
    return {"top": [dict(name=r["name"], level=r["level"], total=r["total"], tier=tier(r["hr"]),
                         me=r["id"] == tg["id"]) for r in rows],
            "rank": rank, "mytotal": mine}


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
    if key == "permit":
        def has():
            with cursor() as cur:
                ensure_user(cur, tg["id"], tg["name"], tg["username"], tg["ref"])
                cur.execute("SELECT permit FROM m_users WHERE id=?", (tg["id"],))
                return cur.fetchone()["permit"]
        if await asyncio.to_thread(has):
            raise Err("لديك تصريح بالفعل")
    link = await MAIN_APP.bot.create_invoice_link(
        title=it["title"], description=it["desc"], payload=f"{key}:{tg['id']}", provider_token="",
        currency="XTR", prices=[LabeledPrice(it["title"], it["stars"])])
    return {"link": link}


SYNC = {"me": act_me, "mining": act_mining, "mode": act_mode, "daily": act_daily, "server": act_server,
        "dex": act_dex, "blocks": act_blocks, "top": act_top, "redeem": act_redeem}
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
    return web.Response(text=WEB_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})


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
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛏ افتح التعدين", web_app=WebAppInfo(url=WEBAPP_URL + "/app"))]])
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text)


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        key, uid = q.invoice_payload.split(":")
        ok = key in ITEMS and int(uid) == q.from_user.id and q.currency == "XTR" \
            and q.total_amount == ITEMS[key]["stars"]
    except Exception:
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "بيانات الدفع غير صحيحة")


async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    key = sp.invoice_payload.split(":")[0]
    ok = await asyncio.to_thread(apply_payment, update.effective_user.id, key,
                                 sp.telegram_payment_charge_id, sp.total_amount)
    if ok:
        await update.message.reply_text("✅ تم الدفع وتفعيل طلبك! عُد إلى التطبيق ⛏")


async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    a = context.args
    if len(a) != 2 or not a[0].isdigit():
        await update.message.reply_text("الاستعمال: <code>/refund user_id charge_id</code>")
        return
    try:
        await context.bot.refund_star_payment(user_id=int(a[0]), telegram_payment_charge_id=a[1])
    except Exception as e:
        await update.message.reply_text(f"فشل الاسترجاع: {html.escape(str(e))}")
        return

    def mark():
        with cursor() as cur:
            cur.execute("SELECT item FROM m_payments WHERE charge_id=?", (a[1],))
            r = cur.fetchone()
            cur.execute("UPDATE m_payments SET refunded=1 WHERE charge_id=?", (a[1],))
            if r and r["item"] == "permit":
                cur.execute("UPDATE m_users SET permit=0, mining=0 WHERE id=?", (int(a[0]),))
    await asyncio.to_thread(mark)
    await update.message.reply_text("💸 تم الاسترجاع وإلغاء التصريح.")


async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gencode <energy|hash|permit> <المقدار> [عدد_الاستخدامات] [كود_مخصص]"""
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    a = context.args
    if len(a) < 2 or a[0].lower() not in CODE_KINDS:
        await update.message.reply_text(
            "الاستعمال:\n<code>/gencode energy 10k 100</code> ← طاقة 10K لأول 100 شخص\n"
            "<code>/gencode hash 100k 50 MYCODE</code> ← نقاط بكود مخصص\n"
            "<code>/gencode permit 0 20</code> ← تصريح تعدين لأول 20 شخص\n\n"
            "/codes لعرض الأكواد · /delcode CODE للحذف")
        return
    kind = a[0].lower()
    try:
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
    prize = CODE_KINDS[kind] + ("" if kind == "permit" else f" {fmt_amt(amount)}")
    await update.message.reply_text(
        f"✅ تم إنشاء الكود:\n<code>{html.escape(code)}</code>\n"
        f"🎁 {prize} · 👥 {max_uses} استخدام\n\nانسخ هذا للقناة:\n"
        f"🎁 كود جديد: <code>{html.escape(code)}</code>\n"
        f"أدخله في التطبيق ← المحفظة، أو أرسل <code>/redeem {html.escape(code)}</code>")


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
        f"<code>{html.escape(r['code'])}</code> · {r['kind']} {fmt_amt(r['amount'])} · {r['uses']}/{r['max_uses']}"
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
    tg = dict(id=u.id, name=u.full_name, username=u.username or "", ref=None)
    try:
        msg = await asyncio.to_thread(redeem_code, tg, context.args[0])
        await update.message.reply_text("✅ " + msg)
    except Err as e:
        await update.message.reply_text("❌ " + html.escape(str(e)))


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
                                      BotCommand("refund", "💸 استرجاع دفعة")]
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        if WEBAPP_URL:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="⛏ تعدين", web_app=WebAppInfo(url=WEBAPP_URL + "/app")))
    except Exception as e:
        log.warning("setup failed: %s", e)
    BG.append(asyncio.create_task(block_loop()))
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
<title>TotalHash</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#17191c;--card:#202327;--line:#31353b;--ac:#ffa45b;--tx:#eceef0;--mut:#8b9098;--red:#d94848;--grn:#3ecf8e;--blue:#38b6ff;--pur:#9b7bff}
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
.nav{position:fixed;inset:auto 0 0 0;display:flex;background:var(--bg);border-top:1px solid var(--line);padding:6px 8px calc(6px + env(safe-area-inset-bottom));z-index:5}
.nav div{flex:1;text-align:center;padding:6px 2px;color:var(--mut);font-size:12px;font-weight:700;cursor:pointer}
.nav div.on{color:var(--ac)}.nav span{display:block;font-size:22px}
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
#toast{position:fixed;left:50%;bottom:calc(96px + env(safe-area-inset-bottom));transform:translateX(-50%);background:#000c;color:#fff;padding:9px 16px;border-radius:12px;font-size:14px;opacity:0;pointer-events:none;transition:opacity .25s;z-index:40}
#toast.on{opacity:1}
</style></head><body>
<div class="w" id="app"><p class="h" style="text-align:center;margin-top:40vh">…</p></div>
<div class="nav" id="nav"></div><div id="sh"></div><div id="toast"></div>
<script>
const tg=window.Telegram.WebApp;try{tg.ready();tg.expand()}catch(e){}
let S=null,B=[],TOP=null,tab='mine',sheet=null,dxSide='buy',dxVal='';
const TABS=[['mine','⛏','تعدين'],['refs','👥','إحالات'],['tasks','✅','مهام'],['top','🏆','إحصائيات'],['wallet','👛','محفظة']];
const TL={turbo:'تيربو',nitro:'نيترو',super:'سوبر'};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f=(n,d=2)=>Number(n).toLocaleString('en',{minimumFractionDigits:d,maximumFractionDigits:d});
const fi=n=>Number(n).toLocaleString('en',{maximumFractionDigits:0});
const fk=n=>{n=Number(n);return n>=1e9?f(n/1e9)+'B':n>=1e6?f(n/1e6)+'M':n>=1e3?f(n/1e3)+'K':fi(n)};
const bal=n=>{const s=f(n,4);return s.slice(0,-3)+'<small>'+s.slice(-3)+'</small>'};
const badge=t=>t?`<span class="bdg ${t}">${TL[t]}</span>`:'';
const av=n=>{let h=0;for(const c of String(n))h=(h*31+c.charCodeAt(0))%360;return `<span class="av" style="background:hsl(${h},55%,48%)">${esc(String(n).trim().slice(0,2).toUpperCase())}</span>`};
function toast(m){const e=$('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),2200)}
async function api(p,b){
  const r=await fetch('/api/'+p,{method:b===undefined?'GET':'POST',headers:{'X-Init-Data':tg.initData,'Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)});
  if(!r.ok)throw new Error('HTTP '+r.status);
  const j=await r.json();if(!j.ok)throw new Error(j.error||'خطأ');return j}
async function load(){
  const jobs=[api('me')];
  if(tab==='mine')jobs.push(api('blocks'));
  if(tab==='top')jobs.push(api('top'));
  const r=await Promise.all(jobs);
  S=r[0];if(tab==='mine')B=r[1].blocks;if(tab==='top')TOP=r[1];
  draw()}
function go(t){tab=t;sheet=null;load().catch(e=>toast(e.message))}
function openSheet(s){sheet=s;draw()}
function closeSheet(){sheet=null;draw()}
function buy(item){api('invoice',{item}).then(r=>tg.openInvoice(r.link,s=>{if(s==='paid')setTimeout(()=>load(),1500)})).catch(e=>toast(e.message))}
function redeem(){const c=$('cd').value.trim();if(!c)return;
  api('redeem',{code:c}).then(r=>{toast(r.msg);load()}).catch(e=>toast(e.message))}
async function act(p,b){try{const r=await api(p,b||{});if(r.msg)toast(r.msg);await load()}catch(e){toast(e.message)}}
function dxPrev(){const v=parseFloat(dxVal)||0,p=S.dex.price;
  return dxSide==='buy'?'≈ '+fi(v/p)+' ⚡':'≈ '+f(v*p*(1-S.dex.fee),4)+' HASH'}
function dxGo(){api('dex',{side:dxSide,amount:parseFloat(dxVal)||0}).then(r=>{toast(r.msg);dxVal='';load()}).catch(e=>toast(e.message))}
function fmtDate(t){return new Date(t*1000).toLocaleString('en-GB',{hour12:false}).replace(',','')}
function fmtWait(s){return Math.floor(s/3600)+'س '+Math.floor(s%3600/60)+'د'}
function fmtLeft(s){return Math.floor(s/60)+'د '+(s%60)+'ث'}

function walletCard(){return `<div class="card"><div class="cap">WALLET · MAIN VAULT</div>
  <div class="row" style="margin-top:6px"><span>إجمالي الرصيد · HASH</span>${badge(S.tier)}</div>
  <div class="big" id="bal">${bal(S.balance)}</div>
  <div class="row" style="margin-top:12px"><span>XP · مستوى ${S.level}</span><span>${S.xp} / ${S.xp_need}</span></div>
  <div class="bar"><i style="width:${Math.min(100,S.xp/S.xp_need*100)}%;background:var(--pur)"></i></div>
  <div class="row"><span>ENERGY · +${S.regen}/د</span><span id="enx">${fi(S.energy)} / ${fi(S.max_energy)}</span></div>
  <div class="bar"><i id="enb" style="width:${Math.min(100,S.energy/S.max_energy*100)}%;background:var(--blue)"></i></div>
  <button class="pri" onclick="openSheet('energy')">⚡ احصل على طاقة</button></div>`}

function vMine(){
  let h=walletCard();
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
    const m=S.modes[S.mode],nx=S.mode%3+1;
    h+=`<div class="card"><div class="row"><b style="color:var(--tx);font-size:16px">⛏ تعدين</b>${S.mining?'<span class="live">قيد التشغيل</span>':'<span class="off">متوقف</span>'}</div>
    <div class="grid" style="margin:12px 0 0"><div class="cell"><span class="h">معدل الهاش</span><b>${fk(S.hashrate)} H/s</b></div>
    <div class="cell"><span class="h">هاش</span><b id="hs">${fk(S.hashes)}</b></div>
    <div class="cell"><span class="h">أسهم</span><b>${fi(S.wins)}</b></div>
    <div class="cell"><span class="h">الدخل</span><b>${f(S.income,1)} #</b></div></div>
    <div class="grid" style="margin:0">${S.mining?'<button class="stop" onclick="act(\'mining\',{on:false})">⏹ إيقاف التعدين</button>':
      '<button class="pri" onclick="act(\'mining\',{on:true})">▶ بدء التعدين</button>'}
    <button onclick="act('mode',{mode:${nx}})">الوضع: ${m.name} ×${m.mul}<br><small class="h">طاقة ×${m.cost}/ث</small></button></div>
    <button onclick="openSheet('servers')">🖥 مركز إدارة الخوادم</button></div>`;
  }
  h+=`<div class="card"><h3>آخر 25 كتلة</h3>`+(B.length?B.map(b=>`<div class="blk"><div class="row" style="color:var(--tx)"><b>كتلة <span class="tag">#${fi(b.id)}</span></b><span class="tag">+${f(b.reward,1)} #</span></div>
  <div style="margin-top:4px">${av(b.miner)} <b>${esc(b.miner)}</b> <span class="h">مستوى ${b.level} · ${fi(b.miners)} معدّن</span></div>
  <div class="h" style="direction:ltr;text-align:right">${fmtDate(b.t)}</div></div>`).join(''):'<p class="h">لا توجد كتل بعد.</p>')+'</div>';
  return h}

function vRefs(){return `<div class="card"><h2>👥 إحالاتك</h2><p class="h">ادعُ أصدقاءك: تحصل على <b>${S.ref_bonus}</b> نقطة عن كل صديق يشتري تصريح التعدين.</p>
  <div class="grid" style="margin-top:12px"><div class="cell"><span class="h">عدد المُحالين</span><b>${S.refs}</b></div></div>
  <div class="kv">${esc(S.ref_link)}</div>
  <button class="pri" onclick="tg.openTelegramLink('https://t.me/share/url?url='+encodeURIComponent(S.ref_link)+'&text='+encodeURIComponent('⛏ تعال نعدّن معاً!'))">📤 مشاركة الرابط</button>
  <button onclick="navigator.clipboard.writeText(S.ref_link).then(()=>toast('تم النسخ'))">📋 نسخ الرابط</button></div>`}

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

function vTop(){
  if(!TOP)return '<p class="h">…</p>';
  return `<div class="card"><h2>🏆 أفضل 100 معدّن</h2><p class="h">ترتيبك: #${TOP.rank} · ${f(TOP.mytotal,2)}</p></div><div class="card">`+
  (TOP.top.length?TOP.top.map((r,i)=>`<div class="rk ${r.me?'me':''}"><i>${i+1}</i>${av(r.name)}<span class="n">${esc(r.name)}${badge(r.tier)}<span class="h">Lv${r.level}</span></span><span class="tag">${f(r.total,1)}</span></div>`).join(''):'<p class="h">لا أحد بعد.</p>')+'</div>'}

function vWallet(){return `<div class="card"><div class="row"><span>الرصيد الإجمالي</span><span>HASH</span></div><div class="big">${bal(S.balance)}</div></div>
  <div class="card"><h3>🎁 لديك كود؟</h3>
  <input id="cd" class="inp" placeholder="TH-XXXXXXXX" autocapitalize="characters" autocomplete="off">
  <button class="pri" onclick="redeem()">استرداد</button></div>
  <div class="card"><h3>المتجر (نجوم تيليجرام)</h3>
  ${S.permit?'<p class="h">✅ تصريح التعدين مفعّل.</p>':`<button class="pri" onclick="buy('permit')">🔑 تصريح التعدين — ⭐ ${S.prices.permit}</button>`}
  <button onclick="buy('energy_up')">🚀 زيادة الطاقة القصوى (+2000، +5/د) — ⭐ ${S.prices.energy_up}</button>
  <button onclick="buy('restore')">🔋 استعادة الطاقة — ⭐ ${S.prices.restore}</button>
  <button onclick="buy('patron')">👑 راعي التعدين ×2 (24h) — ⭐ ${S.prices.patron}</button></div>
  <div class="card"><p class="h">ℹ️ النقاط افتراضية داخل اللعبة فقط، ولا تمثل عملة رقمية ولا يمكن سحبها أو تحويلها لمال.</p></div>`}

function sheetHTML(){
  if(!sheet)return '';
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
  }
  return `<div class="ov" onclick="if(event.target===this)closeSheet()"><div class="sh"><button class="sm" style="float:left;margin:0" onclick="closeSheet()">✕</button>${b}</div></div>`}

const V={mine:vMine,refs:vRefs,tasks:vTasks,top:vTop,wallet:vWallet};
function draw(){
  $('nav').innerHTML=TABS.map(t=>`<div class="${t[0]===tab?'on':''}" onclick="go('${t[0]}')"><span>${t[1]}</span>${t[2]}</div>`).join('');
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
setInterval(()=>{if(S&&!document.hidden&&sheet!=='dex'&&document.activeElement.tagName!=='INPUT')load().catch(()=>{})},6000);
if(!tg.initData){$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">افتح التطبيق من داخل تيليجرام.</p>'}
else load().catch(e=>{$('app').innerHTML='<p class="h" style="text-align:center;margin-top:35vh">'+esc(e.message)+'<br><br><button class="pri" style="width:auto;padding:10px 24px" onclick="location.reload()">إعادة المحاولة</button></p>'});
</script></body></html>
"""


# ═══════════ 7) التشغيل ═══════════
def main():
    init_db()
    app = (Application.builder().token(BOT_TOKEN).defaults(Defaults(parse_mode=ParseMode.HTML))
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("gencode", cmd_gencode))
    app.add_handler(CommandHandler("codes", cmd_codes))
    app.add_handler(CommandHandler("delcode", cmd_delcode))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    log.info("Bot started (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
