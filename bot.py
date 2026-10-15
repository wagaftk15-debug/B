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
Start Command:       python main.py
"""
import hashlib, hmac, json, os, random, sqlite3, threading, time, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
    os.makedirs(d, exist_ok=True)
    return p


# ───────────────────────── الإعدادات ─────────────────────────
PORT = int(os.environ.get("PORT", 8080))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = _pick_db_path()
ALLOW_GUEST = os.environ.get("ALLOW_GUEST", "1") == "1"
_dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "") or os.environ.get("RAILWAY_STATIC_URL", "").replace("https://", "")
WEB_APP_URL = (os.environ.get("WEB_APP_URL") or (f"https://{_dom}" if _dom else "")).rstrip("/")

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
MAX_TICK_HASHES = 400000        # حماية للسيرفر
GENESIS_PREV = "0" * 64

LOCK = threading.RLock()
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row


def init_db():
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
        """)
        DB.commit()


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
        with LOCK:
            r = DB.execute("SELECT * FROM blocks ORDER BY idx DESC LIMIT 1").fetchone()
            if r:
                self.height = r["idx"]
                self.prev = r["hash"]
                self.bits = r["bits"]
                self.tpl_ts = int(r["found_at"])
                self.last_found = r["found_at"]
            else:
                # بلوك التكوين Genesis
                pre = block_prefix(0, GENESIS_PREV, 1760000000, "genesis", 0.0, 1)
                nonce = 0
                while int(block_hash(pre, nonce), 16) >= target_for(1):
                    nonce += 1
                DB.execute("INSERT INTO blocks VALUES(0,?,?,?,?,?,?,?,?,?)",
                           (GENESIS_PREV, 1760000000, "genesis", "Genesis", 0.0, 1, nonce,
                            block_hash(pre, nonce), time.time()))
                DB.commit()
                self.load()

    def next_idx(self):
        return self.height + 1

    def template(self, miner_id):
        i = self.next_idx()
        return block_prefix(i, self.prev, self.tpl_ts, miner_id, reward_for(i), self.bits)

    def accept(self, miner_id, miner_name, nonce):
        """تحقق من الحل ثم أضف البلوك."""
        i = self.next_idx()
        rw = reward_for(i)
        pre = block_prefix(i, self.prev, self.tpl_ts, miner_id, rw, self.bits)
        h = block_hash(pre, nonce)
        if int(h, 16) >= target_for(self.bits):
            return False
        now = time.time()
        DB.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (i, self.prev, self.tpl_ts, miner_id, miner_name, rw, self.bits, nonce, h, now))
        DB.execute("UPDATE users SET balance=balance+?, xp=xp+50, blocks_found=blocks_found+1 WHERE id=?",
                   (rw, miner_id))
        DB.commit()
        interval = now - self.last_found
        self.last_interval = interval
        # تعديل الصعوبة
        if interval < TARGET_BLOCK_TIME / 2:
            self.bits = min(MAX_BITS, self.bits + 1)
        elif interval > TARGET_BLOCK_TIME * 2:
            self.bits = max(MIN_BITS, self.bits - 1)
        self.height, self.prev, self.last_found, self.tpl_ts = i, h, now, int(now)
        return True

    def verify(self):
        with LOCK:
            rows = DB.execute("SELECT * FROM blocks ORDER BY idx").fetchall()
        prev = GENESIS_PREV
        for r in rows:
            if r["prev"] != prev:
                return False, r["idx"]
            pre = block_prefix(r["idx"], r["prev"], r["ts"], r["miner_id"], r["reward"], r["bits"])
            h = block_hash(pre, r["nonce"])
            if h != r["hash"] or int(h, 16) >= target_for(r["bits"]):
                return False, r["idx"]
            prev = r["hash"]
        return True, len(rows)


CHAIN = None


# ───────────────────────── المستخدمون ─────────────────────────
def xp_need(level):
    return int(100 * level ** 1.5 + 64)


def level_of(xp):
    l = 1
    while xp >= xp_need(l):
        xp -= xp_need(l)
        l += 1
    return l, xp


def user_hashrate(u):
    lvl, _ = level_of(u["xp"])
    return int(BASE_HASHRATE * u["mode"] * (1 + 0.05 * (lvl - 1)))


def get_user(uid, name=None, referrer=None):
    with LOCK:
        u = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            now = time.time()
            DB.execute("INSERT INTO users(id,name,energy,last_ts,created,referrer) VALUES(?,?,?,?,?,?)",
                       (uid, name or f"User{uid[-4:]}", MAX_ENERGY, now, now, None))
            if referrer and referrer != uid and DB.execute("SELECT 1 FROM users WHERE id=?", (referrer,)).fetchone():
                DB.execute("UPDATE users SET referrer=?, balance=balance+? WHERE id=?", (referrer, REF_BONUS, uid))
                DB.execute("UPDATE users SET balance=balance+? WHERE id=?", (REF_BONUS, referrer))
            DB.commit()
            u = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        elif name and u["name"] != name:
            DB.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
            DB.commit()
        return settle(uid)


def settle(uid, now=None):
    """تحديث الطاقة حسب الزمن."""
    now = now or time.time()
    with LOCK:
        u = DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        dt = max(0.0, now - (u["last_ts"] or now))
        e, mining = u["energy"], u["mining"]
        e += ENERGY_REGEN * dt
        if mining:
            e -= ENERGY_DRAIN * u["mode"] * dt
        e = max(0.0, min(MAX_ENERGY, e))
        if mining and e <= 0:
            mining = 0
        DB.execute("UPDATE users SET energy=?, mining=?, last_ts=? WHERE id=?", (e, mining, now, uid))
        DB.commit()
        return DB.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


# ───────────────────────── المعدّن (خيط خلفي) ─────────────────────────
def miner_loop():
    while True:
        t0 = time.time()
        try:
            with LOCK:
                rows = DB.execute("SELECT id FROM users WHERE mining=1").fetchall()
                users = [settle(r["id"]) for r in rows]
                users = [u for u in users if u["mining"]]
                if users:
                    total = sum(user_hashrate(u) for u in users)
                    scale = min(1.0, MAX_TICK_HASHES / max(1, total))
                    random.shuffle(users)
                    found = False
                    for u in users:
                        n = max(1, int(user_hashrate(u) * scale))
                        pre = CHAIN.template(u["id"])
                        tgt = target_for(CHAIN.bits)
                        start = random.getrandbits(48)
                        sha = hashlib.sha256
                        for k in range(n):
                            nonce = start + k
                            h = sha(sha((pre + str(nonce)).encode()).digest()).digest()
                            if int.from_bytes(h, "big") < tgt:
                                if CHAIN.accept(u["id"], u["name"], nonce):
                                    found = True
                                    n = k + 1
                                break
                        DB.execute("UPDATE users SET hashes=hashes+?, xp=xp+? WHERE id=?",
                                   (n, n / 800.0, u["id"]))
                        if found:
                            break
                    DB.commit()
        except Exception as ex:
            print("miner error:", ex)
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


# ───────────────────────── واجهة API ─────────────────────────
def check_init_data(init_data):
    """التحقق من توقيع تيليجرام Mini App."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400 * 7:
            return None
        usr = json.loads(pairs["user"])
        name = usr.get("username") and "@" + usr["username"] or usr.get("first_name", "User")
        return str(usr["id"]), name
    except Exception:
        return None


BOT_USERNAME = ""


def state_for(u):
    lvl, xp_in = level_of(u["xp"])
    with LOCK:
        online = DB.execute("SELECT id FROM users WHERE mining=1").fetchall()
        net = 0
        for r in online:
            uu = DB.execute("SELECT * FROM users WHERE id=?", (r["id"],)).fetchone()
            net += user_hashrate(uu)
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
        "ref_link": (f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}" if BOT_USERNAME else ""),
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
    out = []
    for r in rows:
        out.append({"idx": r["idx"], "hash": r["hash"], "miner": r["miner_name"], "reward": r["reward"],
                    "level": level_of(r["xp"])[0], "time": time.strftime("%d.%m.%Y, %H:%M:%S", time.localtime(r["found_at"])),
                    "nonce": r["nonce"], "bits": r["bits"]})
    return out


def leaderboard(limit=20):
    with LOCK:
        rows = DB.execute("SELECT name,balance,blocks_found,xp FROM users ORDER BY balance DESC LIMIT ?",
                          (limit,)).fetchall()
    return [{"name": r["name"], "balance": r["balance"], "blocks": r["blocks_found"],
             "level": level_of(r["xp"])[0]} for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "HashChain/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _auth(self):
        t = check_init_data(self.headers.get("X-Init-Data", ""))
        if t:
            return t
        if ALLOW_GUEST:
            g = self.headers.get("X-Guest-Id", "")
            if g and g.isalnum() and 8 <= len(g) <= 40:
                return "g" + g, "Guest" + g[:4]
        return None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.replace("__COIN__", COIN), "text/html; charset=utf-8")
        if path == "/health":
            return self._json({"ok": True, "height": CHAIN.height})
        if path == "/api/blocks":
            return self._json(recent_blocks())
        if path == "/api/leaderboard":
            return self._json(leaderboard())
        if path == "/api/verify":
            ok, info = CHAIN.verify()
            return self._json({"valid": ok, "info": info})
        if path == "/api/state":
            a = self._auth()
            if not a:
                return self._json({"error": "auth"}, 401)
            ref = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("ref", [None])[0]
            u = get_user(a[0], a[1], ref)
            return self._json(state_for(u))
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        a = self._auth()
        if not a:
            return self._json({"error": "auth"}, 401)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
        except Exception:
            body = {}
        with LOCK:
            u = get_user(a[0], a[1])
            if path == "/api/start":
                if u["energy"] < 5:
                    return self._json({"error": "no_energy"}, 400)
                DB.execute("UPDATE users SET mining=1 WHERE id=?", (u["id"],))
            elif path == "/api/stop":
                DB.execute("UPDATE users SET mining=0 WHERE id=?", (u["id"],))
            elif path == "/api/mode":
                m = int(body.get("mode", 1))
                if m not in (1, 2, 3):
                    return self._json({"error": "bad_mode"}, 400)
                DB.execute("UPDATE users SET mode=? WHERE id=?", (m, u["id"]))
            elif path == "/api/refill":
                if time.time() - u["last_refill"] < REFILL_COOLDOWN:
                    return self._json({"error": "cooldown"}, 400)
                DB.execute("UPDATE users SET energy=MIN(?,energy+?), last_refill=? WHERE id=?",
                           (MAX_ENERGY, REFILL_AMOUNT, time.time(), u["id"]))
            else:
                return self._send(404, "not found", "text/plain")
            DB.commit()
            u = settle(u["id"])
            return self._json(state_for(u))


# ───────────────────────── بوت تيليجرام ─────────────────────────
def tg(method, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def bot_loop():
    global BOT_USERNAME
    try:
        BOT_USERNAME = tg("getMe")["result"]["username"]
        print("Bot started: @" + BOT_USERNAME)
        if WEB_APP_URL:
            tg("setChatMenuButton", menu_button={"type": "web_app", "text": "⛏ تعدين", "web_app": {"url": WEB_APP_URL}})
    except Exception as ex:
        print("bot init failed:", ex)
        return
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                m = upd.get("message") or {}
                text = (m.get("text") or "").strip()
                frm = m.get("from") or {}
                if not text or not frm:
                    continue
                uid = str(frm["id"])
                name = ("@" + frm["username"]) if frm.get("username") else frm.get("first_name", "User")
                chat = m["chat"]["id"]
                if text.startswith("/start"):
                    parts = text.split()
                    ref = parts[1][4:] if len(parts) > 1 and parts[1].startswith("ref_") else None
                    get_user(uid, name, ref)
                    kb = {"inline_keyboard": [[{"text": "⛏ ابدأ التعدين", "web_app": {"url": WEB_APP_URL}}]]} \
                        if WEB_APP_URL.startswith("https") else None
                    tg("sendMessage", chat_id=chat, reply_markup=kb,
                       text=f"أهلاً {name} 👋\nعدّن بلوكات حقيقية بإثبات العمل (PoW) واربح {COIN}.\n"
                            "اضغط الزر لفتح تطبيق التعدين." + ("" if kb else f"\n(WEB_APP_URL غير مضبوط)"))
                elif text.startswith("/balance"):
                    u = get_user(uid, name)
                    tg("sendMessage", chat_id=chat,
                       text=f"💰 رصيدك: {u['balance']:.4f} {COIN}\n⛏ بلوكات وجدتها: {u['blocks_found']}")
                elif text.startswith("/top"):
                    lines = [f"{i+1}. {r['name']} — {r['balance']:.2f}" for i, r in enumerate(leaderboard(10))]
                    tg("sendMessage", chat_id=chat, text="🏆 أفضل المعدّنين:\n" + "\n".join(lines))
                elif text.startswith("/chain"):
                    ok, info = CHAIN.verify()
                    tg("sendMessage", chat_id=chat,
                       text=f"⛓ ارتفاع السلسلة: {CHAIN.height}\nالصعوبة: {CHAIN.bits} بت\nسلامة السلسلة: {'✅' if ok else '❌'}")
        except Exception as ex:
            print("bot error:", ex)
            time.sleep(5)


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
.toast{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:#000c;padding:10px 18px;border-radius:10px;display:none;z-index:9}
</style></head><body>
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
  <div class="card"><div class="lbl">NETWORK</div>
    <div class="grid">
      <div><span>ارتفاع السلسلة</span><b id="sh">-</b></div><div><span>الصعوبة (بت)</span><b id="sb">-</b></div>
      <div><span>العرض الكلي</span><b id="ss">-</b></div><div><span>المستخدمون</span><b id="su">-</b></div>
      <div><span>آخر فاصل زمني</span><b id="si">-</b></div><div><span>الهدف</span><b id="st">-</b></div>
    </div>
    <button class="btn dark" id="ver" style="margin-top:12px">⛓ تحقق من سلامة السلسلة</button>
  </div>
  <h3>🏆 المتصدرون</h3><div class="card" id="lb"></div>
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
const tgw = window.Telegram && Telegram.WebApp;
if (tgw){ tgw.ready(); tgw.expand(); }
let gid = localStorage.getItem('gid');
if(!gid){ gid=[...crypto.getRandomValues(new Uint8Array(12))].map(b=>b.toString(16).padStart(2,'0')).join('').slice(0,24); localStorage.setItem('gid',gid); }
const H = ()=>({'Content-Type':'application/json','X-Init-Data':(tgw&&tgw.initData)||'','X-Guest-Id':gid});
const ref = new URLSearchParams(location.search).get('ref') || (tgw&&tgw.initDataUnsafe&&tgw.initDataUnsafe.start_param||'').replace('ref_','') || '';
const $=id=>document.getElementById(id);
const fmt=n=>{n=+n;if(n>=1e9)return (n/1e9).toFixed(2)+'G';if(n>=1e6)return (n/1e6).toFixed(2)+'M';if(n>=1e3)return (n/1e3).toFixed(n>=1e4?1:2)+'K';return ''+Math.round(n)};
function toast(t){const e=$('toast');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',2200)}
async function api(p,m='GET',b){const r=await fetch(p,{method:m,headers:H(),body:b?JSON.stringify(b):undefined});const j=await r.json();if(!r.ok){toast({no_energy:'لا توجد طاقة كافية',cooldown:'انتظر قليلاً قبل الاستخدام التالي',auth:'تعذّر تسجيل الدخول'}[j.error]||'خطأ');throw j}return j}
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
  $('rlink').value=s.ref_link||'(الرابط يظهر بعد تشغيل البوت)'; $('refs').textContent=s.refs;
  $('sh').textContent=n.height; $('sb').textContent=n.bits; $('ss').textContent=fmt(n.supply); $('su').textContent=n.users;
  $('si').textContent=n.last_interval.toFixed(1)+'s'; $('st').textContent=n.target_time+'s';
  $('refill').disabled=s.refill_in>0; $('refill').textContent=s.refill_in>0?('⚡ متاح بعد '+Math.ceil(s.refill_in/60)+' د'):'⚡ احصل على طاقة (+500)';
}
async function poll(){try{render(await api('/api/state'+(ref?'?ref='+encodeURIComponent(ref):'')))}catch(e){}}
async function blocks(){try{const l=await api('/api/blocks');$('blocks').innerHTML=l.map(b=>`<div class="blk"><div><div class="n">#${b.idx.toLocaleString('en')}</div><div class="m">⛏ ${b.miner} · مستوى ${b.level}</div><div class="m">${b.time}</div><code>${b.hash.slice(0,32)}…</code></div><div style="text-align:left;direction:ltr"><div class="tag">+${b.reward.toFixed(2)} #</div><div class="m" style="margin-top:8px">${b.bits} bits</div></div></div>`).join('')}catch(e){}}
async function lb(){try{const l=await api('/api/leaderboard');$('lb').innerHTML=l.map((r,i)=>`<div class="lb"><span>${i+1}. ${r.name} <small style="color:var(--mut)">Lv${r.level}</small></span><b>${r.balance.toFixed(2)}</b></div>`).join('')}catch(e){}}
$('mbtn').onclick=async()=>{try{render(await api(S.mining?'/api/stop':'/api/start','POST'))}catch(e){}};
$('refill').onclick=async()=>{try{render(await api('/api/refill','POST'));toast('تمت إضافة الطاقة ⚡')}catch(e){}};
document.querySelectorAll('#modes button').forEach(x=>x.onclick=async()=>{try{render(await api('/api/mode','POST',{mode:+x.dataset.m}))}catch(e){}});
$('copy').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText($('rlink').value);toast('تم النسخ')};
$('ver').onclick=async()=>{const r=await api('/api/verify');toast(r.valid?'✅ السلسلة سليمة ('+r.info+' بلوك)':'❌ خلل عند البلوك '+r.info)};
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==='t-'+b.dataset.t));
  if(b.dataset.t==='stats')lb();
});
poll();blocks();setInterval(poll,2000);setInterval(blocks,3000);
</script></body></html>
"""


# ───────────────────────── التشغيل ─────────────────────────
def main():
    global CHAIN
    init_db()
    CHAIN = Chain()
    threading.Thread(target=miner_loop, daemon=True).start()
    if BOT_TOKEN:
        threading.Thread(target=bot_loop, daemon=True).start()
    else:
        print("BOT_TOKEN غير مضبوط: الموقع يعمل بوضع الضيف فقط.")
    print(f"HashChain running on :{PORT}  | height={CHAIN.height} bits={CHAIN.bits}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
