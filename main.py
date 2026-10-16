#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HashChain - سلسلة بلوكات حقيقية (Proof of Work) + موقع ويب + بوت تيليجرام
ملف واحد، بدون أي مكتبات خارجية (مكتبة بايثون القياسية فقط).

التشغيل محلياً:      python main.py
على Railway: المتغير الوحيد المطلوب هو BOT_TOKEN. الباقي يُضبط تلقائياً:
    BOT_TOKEN     توكن البوت من @BotFather (بدونه يعمل الموقع بوضع الضيف فقط)
    DB_PATH       اختياري. إن أضفت Volume يُستخدم تلقائياً (RAILWAY_VOLUME_MOUNT_PATH أو /data)
    WEB_APP_URL   اختياري. يُكتشف تلقائياً من RAILWAY_PUBLIC_DOMAIN
    ALLOW_GUEST   1 للسماح بالدخول من المتصفح بدون تيليجرام (الافتراضي 1)
    ADMIN_IDS     معرّفات تيليجرام للأدمن مفصولة بفاصلة (احصل على معرّفك من @userinfobot)
    UNLOCK_STARS  سعر فتح التعدين بنجوم تيليجرام (الافتراضي 1000، والأدمن يغيّره من اللوحة؛ 0 = مجاني)
    SUPPORT_CONTACT نص دعم الدفع الذي يظهر عند /paysupport (يشترطه تيليجرام)
Start Command:       python main.py

الحماية من 20 خطأ محتمل (ابحث عن [FIX-n] في الكود):
 1 قفل/تلف SQLite (WAL + busy_timeout)        11 إساءة نظام الإحالات
 2 فحص سلامة القاعدة واستعادتها عند التلف     12 ساعة النظام/NaN/دقة الأرقام
 3 فهارس + ترقية إصدار القاعدة                13 المعدّن يحجب الـ API (ميزانية زمنية)
 4 قراءة آمنة لمتغيرات البيئة                 14 تعارض إدخال البلوكات + إعادة المزامنة
 5 أي استثناء لا يُسقط الخادم + ترويسات أمان  15 إعادة تشغيل الخيوط تلقائياً عند الانهيار
 6 حجم/صيغة الطلب وقيم غير صالحة              16 أخطاء تيليجرام (401/409/429/403)
 7 تحديد معدل الطلبات                         17 أوامر البوت: @bot، تحديثات تالفة
 8 تحديد إنشاء حسابات الضيوف لكل IP           18 إغلاق نظيف عند SIGTERM + مهلة للاتصالات
 9 XSS: تنظيف الأسماء (خادم + واجهة)          19 كاش + إزالة استعلامات N+1
10 تحقق صارم من توقيع تيليجرام                20 مرونة الواجهة (شبكة/502/تكرار/تخزين)
"""
import hashlib, hmac, json, os, random, re, signal, sqlite3, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# حاويات البناء/التشغيل قد تعمل بترميز ASCII (LANG=C): اجعل الطباعة UTF-8 حتى لا ينهار print للعربية
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ───────────────────────── الإعدادات ─────────────────────────
def _env_int(name, default):
    """[FIX-4] قراءة رقم من البيئة بدون انهيار إن كانت القيمة تالفة."""
    try:
        v = int(str(os.environ.get(name, default)).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _pick_db_path():
    """اختيار مكان قاعدة البيانات تلقائياً: DB_PATH ثم Volume في Railway ثم /data ثم محلي."""
    p = os.environ.get("DB_PATH", "").strip()
    if not p:
        vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if vol:
            p = os.path.join(vol, "hash.db")
        elif os.path.isdir("/data") and os.access("/data", os.W_OK):
            p = "/data/hash.db"
        else:
            p = "hash.db"
    d = os.path.dirname(os.path.abspath(p))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as ex:
        print("cannot create DB dir:", ex)
    return p


PORT = _env_int("PORT", 8080)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip('"').strip("'").strip()
DB_PATH = _pick_db_path()
ALLOW_GUEST = os.environ.get("ALLOW_GUEST", "1").strip() == "1"
_dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "") or os.environ.get("RAILWAY_STATIC_URL", "").replace("https://", "")
WEB_APP_URL = (os.environ.get("WEB_APP_URL") or (f"https://{_dom}" if _dom else "")).strip().rstrip("/")
ADMIN_IDS = {x for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.isdigit()}
UNLOCK_STARS_DEFAULT = _env_int("UNLOCK_STARS", 1000)
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "").strip() or "لأي مشكلة في الدفع تواصل مع مالك البوت وأرسل له معرّف الدفعة."

COIN = "HASH"
TARGET_BLOCK_TIME = 10          # ثواني لكل بلوك (تعديل الصعوبة تلقائي)
MIN_BITS, MAX_BITS = 8, 44
START_BITS = 14
BASE_HASHRATE = 4000            # هاش/ثانية لكل معدّن بالوضع x1
MAX_ENERGY = 1000
ENERGY_REGEN = 0.5              # طاقة/ثانية (30 في الدقيقة)
ENERGY_DRAIN = 1.0              # طاقة/ثانية × وضع التعدين
REFILL_AMOUNT, REFILL_COOLDOWN = 500, 6 * 3600
HALVING_EVERY = 100000
BASE_REWARD = 10.0
REF_BONUS = 5.0
MAX_REFS = 200                  # [FIX-11] أقصى عدد إحالات مكافأة لكل مستخدم
TICK_BUDGET = 0.30              # [FIX-13] أقصى زمن (ثانية) يقضيه المعدّن في كل دورة
MAX_USER_TICK = 60000           # سقف الهاشات الاسمية لكل مستخدم في الدورة
MAX_BODY = 4096                 # [FIX-6]
INITDATA_MAX_AGE = 3 * 86400    # [FIX-10]
SCHEMA_VERSION = 3
GENESIS_PREV = "0" * 64

STOP = threading.Event()
LOCK = threading.RLock()


# ───────────────────────── قاعدة البيانات ─────────────────────────
def open_db(path):
    """[FIX-1][FIX-2] فتح القاعدة بوضع WAL، وفحص سلامتها، ونقل التالفة جانباً."""
    def connect():
        c = sqlite3.connect(path, check_same_thread=False, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    c = connect()
    try:
        ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        try:
            c.close()
        except Exception:
            pass
        bak = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bak)
            print("!! قاعدة البيانات تالفة، نُقلت إلى:", bak)
        except OSError as ex:
            print("!! تعذّر نقل القاعدة التالفة:", ex)
        for ext in ("-wal", "-shm"):
            try:
                os.remove(path + ext)
            except OSError:
                pass
        c = connect()
    for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=15000"):
        try:
            c.execute("PRAGMA " + pragma)
        except sqlite3.DatabaseError as ex:
            print("pragma", pragma, "failed:", ex)
    return c


DB = open_db(DB_PATH)


def init_db():
    """[FIX-3] إنشاء الجداول والفهارس وترقية الإصدار."""
    with LOCK:
        DB.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY, name TEXT, balance REAL DEFAULT 0, xp REAL DEFAULT 0,
            energy REAL DEFAULT 1000, last_ts REAL, mining INTEGER DEFAULT 0, mode INTEGER DEFAULT 1,
            hashes INTEGER DEFAULT 0, blocks_found INTEGER DEFAULT 0, referrer TEXT,
            last_refill REAL DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS blocks(
            idx INTEGER PRIMARY KEY, prev TEXT, ts INTEGER, miner_id TEXT, miner_name TEXT,
            reward REAL, bits INTEGER, nonce INTEGER, hash TEXT, found_at REAL);
        CREATE INDEX IF NOT EXISTS idx_users_mining  ON users(mining);
        CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
        CREATE INDEX IF NOT EXISTS idx_users_ref     ON users(referrer);
        CREATE INDEX IF NOT EXISTS idx_blocks_miner  ON blocks(miner_id);
        """)
        # ترحيلات مستقبلية: if ver < N: ALTER TABLE ...
        DB.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        DB.commit()


# ───────────────────────── أدوات مساعدة ─────────────────────────
class ApiError(Exception):
    def __init__(self, code, key):
        super().__init__(key)
        self.code, self.key = code, key


_BAD_CHARS = re.compile(r'''[\x00-\x1f\x7f<>&"'`‪-‮⁦-⁩]''')
REF_RE = re.compile(r"^[0-9]{1,20}$")
GUEST_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


def clean_name(n, fallback="User"):
    """[FIX-9] تنظيف الأسماء: إزالة وسوم HTML والرموز التحكمية وأحرف عكس الاتجاه."""
    n = _BAD_CHARS.sub("", str(n or "")).strip()[:32]
    return n or fallback


class RateLimiter:
    """[FIX-7] دلو رموز بسيط (token bucket) لكل مفتاح."""

    def __init__(self, rate, burst):
        self.rate, self.burst = float(rate), float(burst)
        self.b, self.lk, self.gc = {}, threading.Lock(), time.time()

    def allow(self, key, cost=1.0):
        now = time.time()
        with self.lk:
            if now - self.gc > 300:
                self.b = {k: v for k, v in self.b.items() if now - v[1] < 300}
                self.gc = now
            tok, ts = self.b.get(key, (self.burst, now))
            tok = min(self.burst, tok + max(0.0, now - ts) * self.rate)
            if tok < cost:
                self.b[key] = (tok, now)
                return False
            self.b[key] = (tok - cost, now)
            return True


RL_API = RateLimiter(rate=8, burst=40)              # طلبات API لكل IP
RL_ACT = RateLimiter(rate=1, burst=10)              # أفعال (بدء/إيقاف/وضع/طاقة) لكل مستخدم
RL_GUEST_NEW = RateLimiter(rate=20 / 3600, burst=20)  # [FIX-8] ضيوف جدد لكل IP
RL_BOT = RateLimiter(rate=0.5, burst=5)             # أوامر البوت لكل مستخدم

_CACHE, _CLK = {}, threading.Lock()


def cached(key, ttl, fn):
    """[FIX-19] كاش زمني قصير لتخفيف الضغط على القاعدة."""
    now = time.time()
    with _CLK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CLK:
        _CACHE[key] = (now, val)
    return val


def supervise(name, fn):
    """[FIX-15] تشغيل خيط وإعادة تشغيله تلقائياً إن انهار."""
    def run():
        while not STOP.is_set():
            try:
                fn()
                return
            except Exception as ex:
                print(f"[{name}] crashed: {ex!r} — restart in 3s")
                STOP.wait(3)
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


# ───────────────────────── البلوك تشين ─────────────────────────
def block_prefix(idx, prev, ts, miner, reward, bits):
    return f"{idx}|{prev}|{ts}|{miner}|{reward:.8f}|{bits}|"


def block_hash(prefix, nonce):
    return hashlib.sha256(hashlib.sha256((prefix + str(nonce)).encode()).digest()).hexdigest()


def target_for(bits):
    return 1 << (256 - bits)


def reward_for(idx):
    return BASE_REWARD / (2 ** (idx // HALVING_EVERY))


class Chain:
    def __init__(self):
        self.height = 0
        self.prev = GENESIS_PREV
        self.bits = START_BITS
        self.tpl_ts = int(time.time())
        self.last_found = time.time()
        self.last_interval = 0.0
        self.load()

    def load(self):
        """تحميل حالة السلسلة من القاعدة (يُستخدم أيضاً لإعادة المزامنة بعد خطأ)."""
        with LOCK:
            r = DB.execute("SELECT * FROM blocks ORDER BY idx DESC LIMIT 1").fetchone()
            if r is None:
                pre = block_prefix(0, GENESIS_PREV, 1760000000, "genesis", 0.0, 1)
                nonce = 0
                while int(block_hash(pre, nonce), 16) >= target_for(1):
                    nonce += 1
                DB.execute("INSERT OR IGNORE INTO blocks VALUES(0,?,?,?,?,?,?,?,?,?)",
                           (GENESIS_PREV, 1760000000, "genesis", "Genesis", 0.0, 1, nonce,
                            block_hash(pre, nonce), time.time()))
                DB.commit()
                r = DB.execute("SELECT * FROM blocks ORDER BY idx DESC LIMIT 1").fetchone()
            self.height = r["idx"]
            self.prev = r["hash"]
            self.bits = r["bits"] if r["idx"] > 0 else START_BITS
            self.tpl_ts = int(r["found_at"])
            self.last_found = r["found_at"]

    def next_idx(self):
        return self.height + 1

    def template(self, miner_id):
        i = self.next_idx()
        return block_prefix(i, self.prev, self.tpl_ts, miner_id, reward_for(i), self.bits)

    def accept(self, miner_id, miner_name, nonce):
        """تحقق من الحل ثم أضف البلوك. [FIX-14] أي خطأ قاعدة → تراجع وإعادة مزامنة."""
        i = self.next_idx()
        rw = reward_for(i)
        pre = block_prefix(i, self.prev, self.tpl_ts, miner_id, rw, self.bits)
        h = block_hash(pre, nonce)
        if int(h, 16) >= target_for(self.bits):
            return False
        now = time.time()
        try:
            DB.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (i, self.prev, self.tpl_ts, miner_id, miner_name, rw, self.bits, nonce, h, now))
            DB.execute("UPDATE users SET balance=ROUND(balance+?,8), xp=xp+50, blocks_found=blocks_found+1 WHERE id=?",
                       (rw, miner_id))
            DB.commit()
        except sqlite3.Error as ex:
            print("accept failed, resync:", ex)
            try:
                DB.rollback()
            except sqlite3.Error:
                pass
            self.load()
            return False
        interval = max(0.0, now - self.last_found)
        self.last_interval = interval
        if interval < TARGET_BLOCK_TIME / 2:
            self.bits = min(MAX_BITS, self.bits + 1)
        elif interval > TARGET_BLOCK_TIME * 2:
            self.bits = max(MIN_BITS, self.bits - 1)
        self.height, self.prev, self.last_found, self.tpl_ts = i, h, now, int(now)
        return True

    def verify(self):
        """التحقق من كل السلسلة على دفعات (لا يحجب القاعدة طويلاً)."""
        prev, last, count = GENESIS_PREV, -1, 0
        while True:
            with LOCK:
                rows = DB.execute("SELECT * FROM blocks WHERE idx>? ORDER BY idx LIMIT 2000", (last,)).fetchall()
            if not rows:
                break
            for r in rows:
                if r["idx"] != last + 1 or r["prev"] != prev:
                    return False, r["idx"]
                pre = block_prefix(r["idx"], r["prev"], r["ts"], r["miner_id"], r["reward"], r["bits"])
                h = block_hash(pre, r["nonce"])
                if h != r["hash"] or int(h, 16) >= target_for(r["bits"]):
                    return False, r["idx"]
                prev, last, count = r["hash"], r["idx"], count + 1
        return True, count


CHAIN = None


# ───────────────────────── المستخدمون ─────────────────────────
def xp_need(level):
    return int(100 * level ** 1.5 + 64)


def level_of(xp):
    try:
        xp = float(xp)
    except (TypeError, ValueError):
        xp = 0.0
    if xp != xp or xp < 0:
        xp = 0.0
    l = 1
    while xp >= xp_need(l):
        xp -= xp_need(l)
        l += 1
    return l, xp


def hashrate_of(xp, mode):
    lvl, _ = level_of(xp)
    return int(BASE_HASHRATE * mode * (1 + 0.05 * (lvl - 1)))


def user_hashrate(u):
    return hashrate_of(u["xp"], u["mode"])


def get_user(uid, name=None, referrer=None):
    name = clean_name(name, f"User{uid[-4:]}") if name else None
    with LOCK:
        u = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            now = time.time()
            DB.execute("INSERT OR IGNORE INTO users(id,name,energy,last_ts,created) VALUES(?,?,?,?,?)",
                       (uid, name or f"User{uid[-4:]}", MAX_ENERGY, now, now))
            # [FIX-11] الإحالة: مستخدمو تيليجرام فقط، لا إحالة ذاتية، سقف لكل مُحيل
            ref = str(referrer or "")
            if uid.isdigit() and REF_RE.match(ref) and ref != uid:
                r = DB.execute("SELECT id FROM users WHERE id=?", (ref,)).fetchone()
                n = DB.execute("SELECT COUNT(*) c FROM users WHERE referrer=?", (ref,)).fetchone()["c"]
                if r and n < MAX_REFS:
                    DB.execute("UPDATE users SET referrer=?, balance=ROUND(balance+?,8) WHERE id=? AND referrer IS NULL",
                               (ref, REF_BONUS, uid))
                    DB.execute("UPDATE users SET balance=ROUND(balance+?,8) WHERE id=?", (REF_BONUS, ref))
            DB.commit()
        elif name and u["name"] != name:
            DB.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
            DB.commit()
        return settle(uid)


def settle(uid, now=None):
    """تحديث الطاقة حسب الزمن. [FIX-12] حماية من رجوع الساعة للخلف وقيم NaN."""
    now = now or time.time()
    with LOCK:
        u = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u is None:
            return None
        last = u["last_ts"] or now
        dt = now - last
        if dt != dt or dt < 0:
            dt = 0.0
        dt = min(dt, 86400.0)
        e, mining = u["energy"], u["mining"]
        if e is None or e != e:
            e = 0.0
        e += ENERGY_REGEN * dt
        if mining:
            e -= ENERGY_DRAIN * u["mode"] * dt
        e = max(0.0, min(float(MAX_ENERGY), e))
        if mining and e <= 0:
            mining = 0
        DB.execute("UPDATE users SET energy=?, mining=?, last_ts=? WHERE id=?", (e, mining, now, uid))
        DB.commit()
        return DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


# ───────────────────────── المعدّن (خيط خلفي) ─────────────────────────
def miner_tick():
    """[FIX-13] دورة تعدين بميزانية زمنية حتى لا تحجب الـ API."""
    deadline = time.time() + TICK_BUDGET
    with LOCK:
        rows = DB.execute("SELECT id FROM users WHERE mining=1").fetchall()
        users = [x for x in (settle(r["id"]) for r in rows) if x and x["mining"]]
        if not users:
            return
        random.shuffle(users)
        tgt = target_for(CHAIN.bits)
        sha = hashlib.sha256
        found = False
        for i, u in enumerate(users):
            now = time.time()
            slice_end = now + max(0.0, deadline - now) / (len(users) - i)
            n = max(1, min(user_hashrate(u), MAX_USER_TICK))
            pre = CHAIN.template(u["id"])
            start = random.getrandbits(48)
            done = 0
            for k in range(n):
                if (k & 2047) == 2047 and time.time() > slice_end:
                    break
                done = k + 1
                nonce = start + k
                h = sha(sha((pre + str(nonce)).encode()).digest()).digest()
                if int.from_bytes(h, "big") < tgt:
                    if CHAIN.accept(u["id"], u["name"], nonce):
                        found = True
                    break
            DB.execute("UPDATE users SET hashes=hashes+?, xp=xp+? WHERE id=?", (done, done / 800.0, u["id"]))
            if found:
                break
        DB.commit()


def miner_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            miner_tick()
        except Exception as ex:
            print("miner error:", repr(ex))
            try:
                with LOCK:
                    DB.rollback()
            except Exception:
                pass
        STOP.wait(max(0.0, 1.0 - (time.time() - t0)))


# ───────────────────────── واجهة API ─────────────────────────
def check_init_data(init_data):
    """[FIX-10] التحقق الصارم من توقيع تيليجرام Mini App."""
    if not BOT_TOKEN or not init_data or len(init_data) > 4096:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        if not got or "user" not in pairs:
            return None
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        auth_date = int(pairs.get("auth_date", 0))
        now = time.time()
        if auth_date <= 0 or now - auth_date > INITDATA_MAX_AGE or auth_date - now > 300:
            return None
        usr = json.loads(pairs["user"])
        uid = int(usr["id"])
        name = ("@" + usr["username"]) if usr.get("username") else (usr.get("first_name") or "User")
        return str(uid), clean_name(name)
    except (ValueError, KeyError, TypeError):
        return None


BOT_USERNAME = ""


def state_for(u):
    lvl, xp_in = level_of(u["xp"])
    with LOCK:
        online = DB.execute("SELECT xp, mode FROM users WHERE mining=1").fetchall()   # [FIX-19] بدون N+1
        net = sum(hashrate_of(r["xp"], r["mode"]) for r in online)
        refs = DB.execute("SELECT COUNT(*) c FROM users WHERE referrer=?", (u["id"],)).fetchone()["c"]
        supply = DB.execute("SELECT COALESCE(SUM(reward),0) s FROM blocks").fetchone()["s"]
        users_total = DB.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        rank = DB.execute("SELECT COUNT(*)+1 c FROM users WHERE balance>?", (u["balance"],)).fetchone()["c"]
    nxt = CHAIN.next_idx()
    return {
        "id": u["id"], "name": u["name"], "coin": COIN,
        "balance": u["balance"], "level": lvl, "xp": xp_in, "xp_need": xp_need(lvl),
        "energy": u["energy"], "max_energy": MAX_ENERGY, "regen": int(ENERGY_REGEN * 60),
        "mining": bool(u["mining"]), "mode": u["mode"], "hashrate": user_hashrate(u),
        "hashes": u["hashes"], "blocks_found": u["blocks_found"], "refs": refs, "rank": rank,
        "ref_link": (f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}" if BOT_USERNAME and u["id"].isdigit() else ""),
        "refill_in": max(0, int(u["last_refill"] + REFILL_COOLDOWN - time.time())),
        "net": {"height": CHAIN.height, "bits": CHAIN.bits, "difficulty": 2 ** CHAIN.bits,
                "hashrate": net, "miners": len(online), "reward": reward_for(nxt),
                "supply": supply, "users": users_total, "last_interval": CHAIN.last_interval,
                "target_time": TARGET_BLOCK_TIME, "next_block": nxt},
    }


def recent_blocks(limit=25):
    with LOCK:
        rows = DB.execute("SELECT b.*, COALESCE(u.xp,0) xp FROM blocks b LEFT JOIN users u ON u.id=b.miner_id "
                          "ORDER BY idx DESC LIMIT ?", (limit,)).fetchall()
    return [{"idx": r["idx"], "hash": r["hash"], "miner": clean_name(r["miner_name"]), "reward": r["reward"],
             "level": level_of(r["xp"])[0],
             "time": time.strftime("%d.%m.%Y, %H:%M:%S", time.localtime(r["found_at"])),
             "nonce": r["nonce"], "bits": r["bits"]} for r in rows]


def leaderboard(limit=100):
    with LOCK:
        rows = DB.execute("SELECT name,balance,blocks_found,xp,mode FROM users ORDER BY balance DESC LIMIT ?",
                          (limit,)).fetchall()
    return [{"name": clean_name(r["name"]), "balance": r["balance"], "blocks": r["blocks_found"],
             "level": level_of(r["xp"])[0], "mode": r["mode"]} for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "HashChain/1.1"
    timeout = 15            # [FIX-18] مهلة للاتصال (يحمي من الاتصالات البطيئة/المعلّقة)

    def log_message(self, *a):
        pass

    # ---- مساعدات ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", head=False):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")          # [FIX-5]
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _ip(self):
        xri = self.headers.get("X-Real-Ip")
        if xri:
            return xri.strip()[:64]
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[-1].strip()[:64]
        return self.client_address[0]

    def _limit(self):
        if not RL_API.allow(self._ip()):
            raise ApiError(429, "rate")

    def _auth(self):
        t = check_init_data(self.headers.get("X-Init-Data", ""))
        if t:
            return t
        if ALLOW_GUEST:
            g = self.headers.get("X-Guest-Id", "")
            if GUEST_RE.match(g):
                uid = "g" + g
                with LOCK:
                    known = DB.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone()
                if not known and not RL_GUEST_NEW.allow(self._ip()):      # [FIX-8]
                    raise ApiError(429, "rate")
                return uid, "Guest" + g[:4]
        raise ApiError(401, "auth")

    def _read_body(self):
        """[FIX-6] حد أقصى للحجم + JSON آمن."""
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request")
        if ln < 0 or ln > MAX_BODY:
            raise ApiError(413, "too_large")
        if ln == 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json")
        return obj if isinstance(obj, dict) else {}

    def _dispatch(self, fn):
        """[FIX-5] أي استثناء يتحول إلى استجابة خطأ ولا يُسقط الخادم."""
        try:
            fn()
        except ApiError as e:
            try:
                self._json({"error": e.key}, e.code)
            except OSError:
                pass
        except OSError:
            pass                      # العميل قطع الاتصال
        except Exception as ex:
            print("handler error:", repr(ex))
            try:
                self._json({"error": "server"}, 500)
            except Exception:
                pass

    # ---- الطرق ----
    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/health"):
            self._send(200, b"", "text/html; charset=utf-8", head=True)
        else:
            self._send(404, b"", "text/plain", head=True)

    def _get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_BYTES, "text/html; charset=utf-8")
        if path == "/health":
            return self._json({"ok": True, "height": CHAIN.height})
        if not path.startswith("/api/"):
            return self._send(404, "not found", "text/plain")
        self._limit()
        if path == "/api/blocks":
            return self._json(cached("blocks", 1.0, recent_blocks))
        if path == "/api/leaderboard":
            return self._json(cached("lb", 3.0, leaderboard))
        if path == "/api/verify":
            ok, info = cached("verify", 60.0, CHAIN.verify)
            return self._json({"valid": ok, "info": info})
        if path == "/api/state":
            uid, name = self._auth()
            ref = urllib.parse.parse_qs(parsed.query).get("ref", [None])[0]
            return self._json(state_for(get_user(uid, name, ref)))
        self._send(404, "not found", "text/plain")

    def _post(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send(404, "not found", "text/plain")
        self._limit()
        uid, name = self._auth()
        if not RL_ACT.allow(uid):
            raise ApiError(429, "rate")
        body = self._read_body()
        with LOCK:
            u = get_user(uid, name)
            if path == "/api/start":
                if u["energy"] < 5:
                    raise ApiError(400, "no_energy")
                DB.execute("UPDATE users SET mining=1 WHERE id=?", (uid,))
            elif path == "/api/stop":
                DB.execute("UPDATE users SET mining=0 WHERE id=?", (uid,))
            elif path == "/api/mode":
                try:
                    m = int(body.get("mode", 1))
                except (TypeError, ValueError):
                    raise ApiError(400, "bad_mode")
                if m not in (1, 2, 3):
                    raise ApiError(400, "bad_mode")
                DB.execute("UPDATE users SET mode=? WHERE id=?", (m, uid))
            elif path == "/api/refill":
                if time.time() - u["last_refill"] < REFILL_COOLDOWN:
                    raise ApiError(400, "cooldown")
                DB.execute("UPDATE users SET energy=MIN(?,energy+?), last_refill=? WHERE id=?",
                           (MAX_ENERGY, REFILL_AMOUNT, time.time(), uid))
            else:
                return self._send(404, "not found", "text/plain")
            DB.commit()
            return self._json(state_for(settle(uid)))


# ───────────────────────── بوت تيليجرام ─────────────────────────
class TgError(Exception):
    def __init__(self, code, desc, retry=0):
        super().__init__(f"{code}: {desc}")
        self.code, self.desc, self.retry = code, desc, retry


def tg(method, _timeout=60, **params):
    """[FIX-16] نداء Telegram API مع تحويل أخطاء HTTP إلى TgError."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        raise TgError(e.code, body.get("description", str(e)), (body.get("parameters") or {}).get("retry_after", 0))


def send(chat, text, kb=None):
    """إرسال رسالة بدون أن يوقف فشلها (مثل حظر المستخدم للبوت) الحلقة."""
    try:
        p = {"chat_id": chat, "text": text}
        if kb:
            p["reply_markup"] = kb
        tg("sendMessage", **p)
    except TgError as e:
        if e.code not in (400, 403):
            print("send failed:", e)
    except Exception as ex:
        print("send failed:", repr(ex))


def handle_update(upd):
    """[FIX-17] معالجة تحديث واحد بصرامة (حقول ناقصة، @bot، مجموعات)."""
    m = upd.get("message")
    if not isinstance(m, dict):
        return
    frm, chat = m.get("from") or {}, m.get("chat") or {}
    text = (m.get("text") or "").strip()
    if not text or "id" not in frm or "id" not in chat or frm.get("is_bot"):
        return
    uid = str(frm["id"])
    name = clean_name(("@" + frm["username"]) if frm.get("username") else frm.get("first_name"), "User")
    cid, private = chat["id"], chat.get("type") == "private"
    cmd = text.split()[0].split("@")[0].lower()
    if not cmd.startswith("/"):
        return
    if not RL_BOT.allow(uid):
        return
    if cmd == "/start":
        parts = text.split()
        ref = parts[1][4:] if len(parts) > 1 and parts[1].startswith("ref_") else None
        get_user(uid, name, ref)
        kb = {"inline_keyboard": [[{"text": "⛏ ابدأ التعدين", "web_app": {"url": WEB_APP_URL}}]]} \
            if (private and WEB_APP_URL.startswith("https")) else None
        send(cid, f"أهلاً {name} 👋\nعدّن بلوكات حقيقية بإثبات العمل (PoW) واربح {COIN}.\n"
                  + ("اضغط الزر لفتح تطبيق التعدين." if kb else "افتح البوت في محادثة خاصة لبدء التعدين."), kb)
    elif cmd == "/balance":
        u = get_user(uid, name)
        send(cid, f"💰 رصيدك: {u['balance']:.4f} {COIN}\n⛏ بلوكات وجدتها: {u['blocks_found']}")
    elif cmd == "/top":
        lines = [f"{i + 1}. {r['name']} — {r['balance']:.2f}" for i, r in enumerate(cached("lb10", 3.0, lambda: leaderboard(10)))]
        send(cid, "🏆 أفضل المعدّنين:\n" + ("\n".join(lines) or "-"))
    elif cmd == "/chain":
        ok, info = cached("verify", 60.0, CHAIN.verify)
        send(cid, f"⛓ ارتفاع السلسلة: {CHAIN.height}\nالصعوبة: {CHAIN.bits} بت\nسلامة السلسلة: {'✅' if ok else '❌'}")


def bot_loop():
    global BOT_USERNAME
    # تهيئة البوت تلقائياً مع إعادة المحاولة
    while not STOP.is_set():
        try:
            BOT_USERNAME = tg("getMe")["result"]["username"]
            tg("deleteWebhook", drop_pending_updates=False)   # لازم لعمل getUpdates
            tg("setMyCommands", commands=[
                {"command": "start", "description": "فتح تطبيق التعدين"},
                {"command": "balance", "description": "رصيدك"},
                {"command": "top", "description": "أفضل المعدّنين"},
                {"command": "chain", "description": "حالة السلسلة"}])
            if WEB_APP_URL.startswith("https"):
                tg("setChatMenuButton", menu_button={"type": "web_app", "text": "⛏ تعدين",
                                                     "web_app": {"url": WEB_APP_URL}})
            print("Bot started: @" + BOT_USERNAME, "| web app:", WEB_APP_URL or "-")
            break
        except TgError as e:
            if e.code == 401:
                print("!! BOT_TOKEN غير صالح (401): البوت متوقف، والموقع يعمل بوضع الضيف.")
                return
            print("bot init failed:", e)
            STOP.wait(10)
        except Exception as ex:
            print("bot init failed:", repr(ex))
            STOP.wait(10)
    else:
        return

    offset, backoff = 0, 2
    while not STOP.is_set():
        try:
            res = tg("getUpdates", _timeout=65, offset=offset, timeout=50, allowed_updates=["message"])
            backoff = 2
            for upd in res.get("result", []):
                if "update_id" not in upd:
                    continue
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception as ex:
                    print("update error:", repr(ex))
        except TgError as e:
            if e.code == 401:
                print("!! BOT_TOKEN غير صالح (401): تم إيقاف البوت.")
                return
            if e.code == 409:
                print("bot conflict (409): نسخة أخرى تستخدم نفس التوكن، انتظار 15s")
                STOP.wait(15)
            elif e.code == 429:
                STOP.wait(e.retry + 1)
            else:
                print("bot error:", e)
                STOP.wait(backoff)
                backoff = min(60, backoff * 2)
        except Exception as ex:
            print("bot network error:", repr(ex))
            STOP.wait(backoff)
            backoff = min(60, backoff * 2)


# ───────────────────────── واجهة الموقع ─────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>HashChain</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#131518;--card:#1e2126;--card2:#23272d;--line:#2d3238;--tx:#eceef1;--mut:#8a919b;--or:#ffa95c;--gr:#34d399;--rd:#e5484d;--bl:#38bdf8;--pu:#a78bfa}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;padding-bottom:84px}
.wrap{max-width:560px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-top:1px solid #ffa95c55;border-radius:16px;padding:16px;margin-bottom:12px}
.lbl{font:600 11px ui-monospace,monospace;letter-spacing:.15em;color:var(--mut);text-transform:uppercase}
.lbl:before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--or);margin-inline-end:8px}
.big{font-size:44px;font-weight:800;margin:10px 0 2px;direction:ltr;text-align:right}
.big small{font-size:24px;color:var(--mut);font-weight:700}
.coin{display:inline-flex;width:34px;height:34px;border-radius:9px;background:var(--or);color:#222;align-items:center;justify-content:center;font-weight:900;font-size:20px;vertical-align:middle;margin-inline-end:8px}
.bar{height:7px;background:#2a2f36;border-radius:9px;overflow:hidden;margin:6px 0 12px}
.bar>i{display:block;height:100%;border-radius:9px;transition:width .6s}
.row{display:flex;justify-content:space-between;font:12px ui-monospace,monospace;color:var(--mut);direction:ltr}
.btn{width:100%;border:0;border-radius:12px;padding:15px;font-size:16px;font-weight:700;cursor:pointer;color:#222;background:var(--or)}
.btn.red{background:var(--rd);color:#fff}.btn.dark{background:#2a2420;color:var(--or);border:1px solid #ffa95c66}
.btn:disabled{opacity:.5}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:10px}
.grid>div{padding:12px 14px;background:var(--card);border:.5px solid var(--line)}
.grid b{display:block;font-size:20px;margin-top:4px;direction:ltr;text-align:right}
.grid span{font-size:12px;color:var(--mut)}
.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-weight:700}
.tag{font-size:12px;border:1px solid #555;color:#bbb;padding:3px 10px;border-radius:8px}
.live{color:var(--gr);border-color:#34d39966;background:#34d39911}
.modes{display:flex;gap:8px;margin:12px 0}
.modes button{flex:1;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card2);color:var(--tx);font-weight:700;cursor:pointer}
.modes button.on{border-color:var(--or);color:var(--or)}
.blk{display:flex;justify-content:space-between;gap:8px;padding:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px;font-size:13px}
.blk .n{color:var(--or);font-weight:800;font-size:17px;direction:ltr}
.blk .m{color:var(--mut);font-size:12px;margin-top:4px}
.blk code{font:11px ui-monospace,monospace;color:var(--mut);direction:ltr;display:block;margin-top:4px;word-break:break-all}
nav{position:fixed;bottom:0;left:0;right:0;background:#181a1e;border-top:1px solid var(--line);display:flex;justify-content:space-around;padding:8px 0 12px}
nav button{background:0;border:0;color:var(--mut);font-size:12px;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer;flex:1}
nav button span{font-size:22px}nav button.on{color:var(--or)}
.tab{display:none}.tab.on{display:block}
h3{margin:4px 0 12px}
.lb{display:flex;justify-content:space-between;padding:10px 4px;border-bottom:1px solid var(--line)}
input.ref{width:100%;background:var(--card2);border:1px solid var(--line);color:var(--tx);padding:12px;border-radius:10px;direction:ltr;margin:10px 0}
.top{background:var(--card);border:1px solid var(--line);border-top:1px solid #ffa95c55;border-radius:14px;overflow:hidden;direction:ltr}
.th{display:flex;align-items:center;gap:12px;padding:12px 14px;border-bottom:1px solid var(--line);font-size:14px}
.th .ti{width:34px;height:34px;border-radius:9px;background:var(--card2);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:16px}
.tr{display:flex;align-items:center;gap:12px;padding:14px;border-bottom:1px solid var(--line)}
.tr:last-child{border-bottom:0}
.rk{flex:none;width:36px;height:36px;border-radius:8px;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font:700 13px ui-monospace,monospace;color:var(--mut)}
.rk.t3{background:#3a2d27;border-color:#6b4a33;color:var(--or)}
.av{flex:none;width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;font-size:15px}
.nm{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.nm b{font-size:16px;word-break:break-word}
.bdg{font-size:11px;padding:1px 8px;border-radius:6px;border:1px solid}
.bdg.b3{color:var(--bl);border-color:var(--bl);background:#38bdf811}
.bdg.b2{color:var(--gr);border-color:var(--gr);background:#34d39911}
.bdg.b1{color:var(--mut);border-color:var(--line)}
.lv{font-size:12px;color:var(--mut)}
.bl{display:flex;align-items:center;gap:6px;margin-top:5px;color:#b4bac3;font:600 15px ui-monospace,monospace;direction:ltr;justify-content:flex-start}
.bl i{font-style:normal;width:15px;height:15px;border-radius:4px;background:var(--or);color:#222;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:900}
.bl small{font-size:11px;color:#5d646e}
.toast{position:fixed;top:44px;left:50%;transform:translateX(-50%);background:#000c;padding:10px 18px;border-radius:10px;display:none;z-index:9}
.off{position:fixed;top:0;left:0;right:0;background:var(--rd);color:#fff;text-align:center;padding:7px;font-size:13px;display:none;z-index:10}
</style></head><body>
<div class="off" id="off"></div>
<div class="toast" id="toast"></div>
<div class="wrap">

<section class="tab on" id="t-mine">
  <div class="card">
    <div class="lbl">WALLET · MAIN VAULT</div>
    <div class="big"><span class="coin">#</span><span id="bal">0</span><small id="bal2"></small></div>
    <div style="color:var(--mut);font-size:13px">إجمالي رصيد __COIN__</div>
    <div style="margin-top:14px" class="row"><span>XP · مستوى <b id="lvl" style="color:var(--pu)">1</b></span><span id="xp"></span></div>
    <div class="bar"><i id="xpb" style="background:var(--pu);width:0"></i></div>
    <div class="row"><span>⚡ ENERGY</span><span id="en"></span></div>
    <div class="bar"><i id="enb" style="background:var(--bl);width:0"></i></div>
    <button class="btn" id="refill">⚡ احصل على طاقة</button>
  </div>

  <div class="card">
    <div class="head"><span>معلومات الكتلة <span class="tag" id="nb">#1</span></span></div>
    <div class="grid">
      <div><span>الصعوبة</span><b id="diff">-</b></div><div><span>إجمالي معدل الهاش</span><b id="nhr">-</b></div>
      <div><span>المكافأة</span><b id="rew">-</b></div><div><span>معدّنون متصلون</span><b id="miners">-</b></div>
    </div>
  </div>

  <div class="card">
    <div class="head"><span>⛏ تعدين</span><span class="tag" id="stag">متوقف</span></div>
    <div class="grid">
      <div><span>معدل الهاش</span><b id="hr">-</b></div><div><span>إجمالي الهاش</span><b id="hs">-</b></div>
      <div><span>كتل وجدتها</span><b id="bf">0</b></div><div><span>ترتيبك</span><b id="rk">-</b></div>
    </div>
    <div class="modes" id="modes"><button data-m="1">×1 عادي</button><button data-m="2">×2 سريع</button><button data-m="3">×3 تيربو</button></div>
    <button class="btn" id="mbtn">ابدأ التعدين</button>
  </div>

  <h3>آخر 25 كتلة</h3><div id="blocks"></div>
</section>

<section class="tab" id="t-ref">
  <div class="card"><div class="lbl">REFERRALS</div><h3 style="margin-top:10px">ادعُ أصدقاءك</h3>
    <p style="color:var(--mut);font-size:14px">تحصل أنت وصديقك على <b style="color:var(--or)">5 __COIN__</b> عند انضمامه عبر رابطك.</p>
    <input class="ref" id="rlink" readonly>
    <button class="btn" id="copy">نسخ الرابط</button>
    <p style="color:var(--mut)">عدد المدعوين: <b id="refs" style="color:var(--tx)">0</b></p></div>
</section>

<section class="tab" id="t-stats">
  <div class="top">
    <div class="th"><span class="ti">🏆</span><b>المعدّنين <span dir="ltr">Top 100</span></b></div>
    <div id="lb"></div>
  </div>
  <button class="btn dark" id="statsBtn" style="margin-top:12px">📊 إحصائيات الشبكة ▾</button>
  <div class="card" id="statsPanel" style="display:none;margin-top:12px"><div class="lbl">NETWORK</div>
    <div class="grid">
      <div><span>ارتفاع السلسلة</span><b id="sh">-</b></div><div><span>الصعوبة (بت)</span><b id="sb">-</b></div>
      <div><span>العرض الكلي</span><b id="ss">-</b></div><div><span>المستخدمون</span><b id="su">-</b></div>
      <div><span>آخر فاصل زمني</span><b id="si">-</b></div><div><span>الهدف</span><b id="st">-</b></div>
    </div>
    <button class="btn dark" id="ver" style="margin-top:12px">⛓ تحقق من سلامة السلسلة</button>
  </div>
</section>

<section class="tab" id="t-wallet">
  <div class="card"><div class="lbl">WALLET</div>
    <div class="big"><span class="coin">#</span><span id="wbal">0</span></div>
    <div style="color:var(--mut)" id="wname"></div>
    <p style="color:var(--mut);font-size:13px;line-height:1.8;margin-top:14px">
    عملة __COIN__ تُنتَج فقط عبر إيجاد بلوك صالح بإثبات العمل (SHA-256 مزدوج). السلسلة مخزّنة ومترابطة بالهاش ويمكن التحقق منها من تبويب الإحصائيات.</p>
  </div>
</section>
</div>

<nav>
 <button data-t="mine" class="on"><span>⛏</span>تعدين</button>
 <button data-t="ref"><span>👥</span>إحالات</button>
 <button data-t="stats"><span>📊</span>إحصائيات</button>
 <button data-t="wallet"><span>👛</span>محفظة</button>
</nav>

<script>
/* [FIX-20] واجهة مرنة: مهلة للشبكة، 502/غير JSON، منع التكرار، تخزين غير متاح، هروب XSS */
const tgw = window.Telegram && Telegram.WebApp;
try { if (tgw) { tgw.ready(); tgw.expand(); } } catch (e) {}
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function rndId(){const a=new Uint8Array(12);try{crypto.getRandomValues(a)}catch(e){for(let i=0;i<12;i++)a[i]=Math.floor(Math.random()*256)}return Array.from(a,b=>b.toString(16).padStart(2,'0')).join('')}
let gid = null; try { gid = localStorage.getItem('gid'); } catch (e) {}
if (!gid || !/^[A-Za-z0-9]{8,40}$/.test(gid)) { gid = rndId(); try { localStorage.setItem('gid', gid); } catch (e) {} }
const H = () => ({'Content-Type':'application/json','X-Init-Data':(tgw&&tgw.initData)||'','X-Guest-Id':gid});
function cleanRef(v){v=String(v||'').replace(/^ref_/,'');return /^\d{1,20}$/.test(v)?v:''}
const ref = cleanRef(new URLSearchParams(location.search).get('ref')) || cleanRef(tgw&&tgw.initDataUnsafe&&tgw.initDataUnsafe.start_param);
const fmt=n=>{n=+n||0;if(n>=1e9)return (n/1e9).toFixed(2)+'G';if(n>=1e6)return (n/1e6).toFixed(2)+'M';if(n>=1e3)return (n/1e3).toFixed(n>=1e4?1:2)+'K';return ''+Math.round(n)};
let toastT=0;
function toast(t){const e=$('toast');e.textContent=t;e.style.display='block';clearTimeout(toastT);toastT=setTimeout(()=>e.style.display='none',2400)}
function banner(t){const e=$('off');e.textContent=t||'';e.style.display=t?'block':'none'}
const ERR={no_energy:'لا توجد طاقة كافية',cooldown:'انتظر قليلاً قبل الاستخدام التالي',auth:'تعذّر تسجيل الدخول — أعد فتح التطبيق',rate:'طلبات كثيرة، تمهّل قليلاً',network:'لا يوجد اتصال بالخادم',server:'خطأ في الخادم، حاول لاحقاً',too_large:'طلب غير صالح',bad_mode:'وضع غير صالح',bad_json:'طلب غير صالح'};
async function api(p,m='GET',b,quiet){
  const ctl=new AbortController(), to=setTimeout(()=>ctl.abort(),8000);
  let r,j;
  try{
    r=await fetch(p,{method:m,headers:H(),body:b?JSON.stringify(b):undefined,signal:ctl.signal,cache:'no-store'});
    try{j=await r.json()}catch(_){j={error:'server'}}
  }catch(e){ banner(ERR.network); if(!quiet)toast(ERR.network); throw {error:'network'}; }
  finally{ clearTimeout(to); }
  if(r.status>=500){ banner(ERR.server) } else if(r.status===401){ banner(ERR.auth) } else banner('');
  if(!r.ok){ if(!quiet)toast(ERR[j.error]||'خطأ'); throw j; }
  return j;
}
let S=null;
function render(s){
  S=s; const b=s.balance.toFixed(4), i=b.indexOf('.')+3;
  $('bal').textContent=b.slice(0,i); $('bal2').textContent=b.slice(i); $('wbal').textContent=s.balance.toFixed(4);
  $('wname').textContent=s.name; $('lvl').textContent=s.level;
  $('xp').textContent=Math.floor(s.xp)+' / '+s.xp_need; $('xpb').style.width=Math.min(100,s.xp/s.xp_need*100)+'%';
  $('en').textContent=Math.floor(s.energy)+' / '+s.max_energy+'  +'+s.regen+' /min'; $('enb').style.width=s.energy/s.max_energy*100+'%';
  const n=s.net; $('nb').textContent='#'+n.next_block; $('diff').textContent=fmt(n.difficulty); $('nhr').textContent=fmt(n.hashrate)+' H/s';
  $('rew').textContent=n.reward.toFixed(2); $('miners').textContent=n.miners;
  $('hr').textContent=fmt(s.hashrate)+' H/s'; $('hs').textContent=fmt(s.hashes); $('bf').textContent=s.blocks_found; $('rk').textContent='#'+s.rank;
  $('stag').textContent=s.mining?'قيد التشغيل':'متوقف'; $('stag').className='tag'+(s.mining?' live':'');
  $('mbtn').textContent=s.mining?'إيقاف التعدين':'ابدأ التعدين'; $('mbtn').className='btn'+(s.mining?' red':'');
  document.querySelectorAll('#modes button').forEach(x=>x.classList.toggle('on',+x.dataset.m===s.mode));
  $('rlink').value=s.ref_link||'(رابط الإحالة متاح داخل تيليجرام)'; $('refs').textContent=s.refs;
  $('sh').textContent=n.height; $('sb').textContent=n.bits; $('ss').textContent=fmt(n.supply); $('su').textContent=n.users;
  $('si').textContent=n.last_interval.toFixed(1)+'s'; $('st').textContent=n.target_time+'s';
  $('refill').disabled=s.refill_in>0; $('refill').textContent=s.refill_in>0?('⚡ متاح بعد '+Math.ceil(s.refill_in/60)+' د'):'⚡ احصل على طاقة (+500)';
}
const busy={poll:false,blocks:false,act:false};
async function guard(k,fn){ if(busy[k]) return; busy[k]=true; try{ await fn() }catch(e){} finally{ busy[k]=false } }
const poll=()=>guard('poll',async()=>{ if(document.hidden) return; render(await api('/api/state'+(ref?'?ref='+encodeURIComponent(ref):''),'GET',null,true)) });
const blocks=()=>guard('blocks',async()=>{ if(document.hidden) return; const l=await api('/api/blocks','GET',null,true);
  $('blocks').innerHTML=l.map(b=>`<div class="blk"><div><div class="n">#${(+b.idx).toLocaleString('en')}</div><div class="m">⛏ ${esc(b.miner)} · مستوى ${+b.level}</div><div class="m">${esc(b.time)}</div><code>${esc(b.hash.slice(0,32))}…</code></div><div style="text-align:left;direction:ltr"><div class="tag">+${(+b.reward).toFixed(2)} #</div><div class="m" style="margin-top:8px">${+b.bits} bits</div></div></div>`).join('') });
const AVC=[['#6d6af0','#8e8bff'],['#3fbf6b','#8fe28f'],['#b36cf5','#e17cf2'],['#38a6f0','#5d7bf5'],['#f08a3c','#f5b86a']];
function avatar(n){n=String(n);const ch=Array.from(n.replace(/^@/,''))[0]||'?';let h=0;for(const c of n)h=(h*31+c.codePointAt(0))>>>0;const p=AVC[h%AVC.length];return `<div class="av" style="background:linear-gradient(135deg,${p[0]},${p[1]})">${esc(ch.toUpperCase())}</div>`}
function balHtml(v){const s=(+v||0).toFixed(4),i=s.split('.')[0],d=s.split('.')[1];return Number(i).toLocaleString('en')+'.'+d[0]+'<small>'+d.slice(1)+'</small>'}
const BD={3:['تيربو','b3'],2:['سريع','b2'],1:['عادي','b1']};
const lb=()=>guard('poll2',async()=>{ const l=await api('/api/leaderboard','GET',null,true);
  $('lb').innerHTML=l.length?l.map((r,i)=>{const b=BD[r.mode]||BD[1];return `<div class="tr"><div class="rk${i<3?' t3':''}">${String(i+1).padStart(2,'0')}</div>${avatar(r.name)}<div style="flex:1;min-width:0"><div class="nm"><b>${esc(r.name)}</b><span class="bdg ${b[1]}">${b[0]}</span><span class="lv">مستوى ${+r.level}</span></div><div class="bl"><i>#</i>${balHtml(r.balance)}</div></div></div>`}).join(''):'<div class="tr" style="color:var(--mut)">لا يوجد معدّنون بعد</div>' });
const act=fn=>guard('act',async()=>{ render(await fn()) });
$('mbtn').onclick=()=>act(()=>api(S&&S.mining?'/api/stop':'/api/start','POST'));
$('refill').onclick=()=>act(async()=>{ const s=await api('/api/refill','POST'); toast('تمت إضافة الطاقة ⚡'); return s });
document.querySelectorAll('#modes button').forEach(x=>x.onclick=()=>act(()=>api('/api/mode','POST',{mode:+x.dataset.m})));
$('copy').onclick=()=>{ try{navigator.clipboard.writeText($('rlink').value);toast('تم النسخ')}catch(e){$('rlink').select();toast('انسخ الرابط يدوياً')} };
$('ver').onclick=()=>guard('ver',async()=>{ const r=await api('/api/verify'); toast(r.valid?'✅ السلسلة سليمة ('+r.info+' بلوك)':'❌ خلل عند البلوك '+r.info) });
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==='t-'+b.dataset.t));
  if(b.dataset.t==='stats')lb();
});
$('statsBtn').onclick=()=>{const p=$('statsPanel'),open=p.style.display==='none';p.style.display=open?'block':'none';$('statsBtn').textContent=open?'📊 إخفاء الإحصائيات ▴':'📊 إحصائيات الشبكة ▾';if(open)p.scrollIntoView({behavior:'smooth',block:'nearest'})};
setInterval(()=>{ if($('t-stats').classList.contains('on')&&!document.hidden) lb() },5000);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){poll();blocks()} });
poll();blocks();setInterval(poll,2000);setInterval(blocks,3000);
</script></body></html>
"""

INDEX_BYTES = INDEX_HTML.replace("__COIN__", COIN).encode("utf-8")


# ───────────────────────── التشغيل ─────────────────────────
def main():
    global CHAIN
    init_db()
    CHAIN = Chain()
    supervise("miner", miner_loop)
    if BOT_TOKEN:
        supervise("bot", bot_loop)
    else:
        print("BOT_TOKEN غير مضبوط: الموقع يعمل بوضع الضيف فقط.")

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 64

    def _term(signum, frame):
        # [FIX-18] Railway يرسل SIGTERM عند كل إعادة نشر: أغلق بنظافة
        print(f"signal {signum}: shutting down…")
        STOP.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    print(f"HashChain running on :{PORT} | db={DB_PATH} | height={CHAIN.height} bits={CHAIN.bits}")
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        srv.server_close()
        with LOCK:
            try:
                DB.commit()
                DB.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                DB.close()
            except Exception as ex:
                print("db close:", ex)
        print("bye.")


if __name__ == "__main__":
    main()
