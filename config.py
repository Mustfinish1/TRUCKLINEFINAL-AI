# ============================================================
# TruckLink v5.0 — config.py
# Env, DB, crypto, users, licensing, invoices, payments, exchange factory
# ============================================================

import os
import re
import sqlite3
import secrets
import logging
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

import aiohttp
import ccxt.async_support as ccxt_async
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# ENV / CONSTANTS
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "").strip()
USDT_WALLET         = os.getenv("USDT_WALLET", "").strip()
OWNER_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ADMIN_IDS           = {int(x.strip()) for x in os.getenv("ADMIN_IDS", OWNER_CHAT_ID).split(",") if x.strip().isdigit()}
DB_PATH             = Path(os.getenv("DB_PATH", "trucklink.db"))
FERNET_KEY          = os.getenv("FERNET_KEY", "").strip()

LIVE_MODE                 = os.getenv("LIVE_MODE", "false").lower() == "true"
AUTO_TRADE                = os.getenv("AUTO_TRADE", "false").lower() == "true"
AUTO_INTERVAL_MINUTES     = int(os.getenv("AUTO_INTERVAL_MINUTES", "5"))

MAX_USDT_PER_TRADE        = Decimal(os.getenv("MAX_USDT_PER_TRADE", "20"))
MAX_PCT_PER_TRADE         = Decimal(os.getenv("MAX_PCT_PER_TRADE", "15"))
RISK_PER_TRADE_PCT        = Decimal(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
MAX_DAILY_LOSS_USDT       = Decimal(os.getenv("MAX_DAILY_LOSS_USDT", "30"))
MAX_DRAWDOWN_PCT          = Decimal(os.getenv("MAX_DRAWDOWN_PCT", "10"))
MAX_LOSS_PCT              = Decimal(os.getenv("MAX_LOSS_PCT", "1.5"))
EMERGENCY_LOSS_PCT        = Decimal(os.getenv("EMERGENCY_LOSS_PCT", "2.5"))
TAKE_PROFIT_PCT           = Decimal(os.getenv("TAKE_PROFIT_PCT", "2.2"))
PARTIAL_TP_PCT            = Decimal(os.getenv("PARTIAL_TP_PCT", "1.5"))
TRAILING_TRIGGER_PCT      = Decimal(os.getenv("TRAILING_TRIGGER_PCT", "1.0"))
TRAILING_DISTANCE_PCT     = Decimal(os.getenv("TRAILING_DISTANCE_PCT", "0.6"))
BREAKEVEN_SL_PCT          = Decimal(os.getenv("BREAKEVEN_SL_PCT", "0.1"))
TIME_STOP_HOURS           = int(os.getenv("TIME_STOP_HOURS", "4"))
TIME_STOP_MIN_PROFIT_PCT  = Decimal(os.getenv("TIME_STOP_MIN_PROFIT_PCT", "0.5"))
MAX_OPEN_POSITIONS        = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MAX_POSITIONS_PER_BUCKET  = int(os.getenv("MAX_POSITIONS_PER_BUCKET", "1"))
MIN_SIGNAL_CONFIDENCE     = int(os.getenv("MIN_SIGNAL_CONFIDENCE", "7"))
INVOICE_EXPIRY_MINUTES    = int(os.getenv("INVOICE_EXPIRY_MINUTES", "30"))
COINGECKO_CACHE_SECONDS   = int(os.getenv("COINGECKO_CACHE_SECONDS", "600"))

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TX_RE               = re.compile(r"^[0-9a-fA-F]{64}$")

TIERS = {
    "BASIC": {"price": Decimal("20"), "first_days": 60, "renew_days": 30, "pairs": 2},
    "PRO":   {"price": Decimal("50"), "first_days": 60, "renew_days": 30, "pairs": 3},
    "ELITE": {"price": Decimal("70"), "first_days": 60, "renew_days": 30, "pairs": 7},
}
SYMBOLS = {
    "BASIC": ["BTC/USDT", "ETH/USDT"],
    "PRO":   ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "ELITE": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "LINK/USDT", "XRP/USDT", "PEPE/USDT"],
}
CMAP = {"BTC/USDT":"bitcoin","ETH/USDT":"ethereum","SOL/USDT":"solana",
        "BNB/USDT":"binancecoin","LINK/USDT":"chainlink","XRP/USDT":"ripple","PEPE/USDT":"pepe"}

BUCKETS = {
    "BTC/USDT":  "majors",
    "ETH/USDT":  "majors",
    "SOL/USDT":  "alt_l1",
    "BNB/USDT":  "alt_l1",
    "LINK/USDT": "defi",
    "XRP/USDT":  "payments",
    "PEPE/USDT": "meme",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trucklink")

# ─────────────────────────────────────────────────────────────
# BOOTSTRAP VALIDATION
# ─────────────────────────────────────────────────────────────
def require_production_config():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN required")
    if not USDT_WALLET or USDT_WALLET.startswith("TYour"):
        raise RuntimeError("USDT_WALLET real TRC20 required")
    if not FERNET_KEY:
        raise RuntimeError('FERNET_KEY required: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    if AUTO_TRADE and not LIVE_MODE:
        raise RuntimeError("AUTO_TRADE=true requires LIVE_MODE=true")

if FERNET_KEY:
    try:
        FERNET = Fernet(FERNET_KEY.encode())
    except Exception as e:
        raise RuntimeError("FERNET_KEY invalid") from e
else:
    FERNET = None

# ─────────────────────────────────────────────────────────────
# TIME / CRYPTO / NUMERIC HELPERS
# ─────────────────────────────────────────────────────────────
def now_utc(): return datetime.now(timezone.utc)
def iso(dt):   return dt.astimezone(timezone.utc).isoformat()
def parse_dt(s): return datetime.fromisoformat(s)
def encrypt(v):  return FERNET.encrypt(v.encode()).decode()
def decrypt(v):  return FERNET.decrypt(v.encode()).decode()
def to_dec(x, default="0"):
    try: return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError): return Decimal(default)

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT,
            ref_code TEXT UNIQUE NOT NULL, referred_by TEXT, created_at TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS subscriptions(
            user_id INTEGER PRIMARY KEY, tier TEXT NOT NULL, expiry TEXT NOT NULL,
            first_purchase INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id));

        CREATE TABLE IF NOT EXISTS invoices(
            invoice_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, tier TEXT NOT NULL,
            base_amount TEXT NOT NULL, exact_amount TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING', paid_tx TEXT UNIQUE, paid_amount TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id));

        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tx_hash TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL, invoice_id TEXT NOT NULL UNIQUE, tier TEXT NOT NULL,
            amount TEXT NOT NULL, paid_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id));

        CREATE TABLE IF NOT EXISTS api_credentials(
            user_id INTEGER PRIMARY KEY,
            api_key TEXT NOT NULL, api_secret TEXT NOT NULL, passphrase TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id));

        CREATE TABLE IF NOT EXISTS positions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            entry_price TEXT NOT NULL, amount TEXT NOT NULL, cost TEXT NOT NULL,
            order_id TEXT NOT NULL UNIQUE, client_order_id TEXT,
            sl TEXT NOT NULL, emergency_sl TEXT NOT NULL, tp TEXT NOT NULL,
            partial_tp_done INTEGER NOT NULL DEFAULT 0,
            sl_order_id TEXT, tp_order_id TEXT,
            high_price TEXT NOT NULL,
            trailing_active INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',
            opened_at TEXT NOT NULL, closed_at TEXT, close_order_id TEXT,
            UNIQUE(user_id, symbol, status));

        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            order_id TEXT, amount TEXT, price TEXT, fee TEXT, pnl TEXT,
            created_at TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS daily_pnl(
            user_id INTEGER NOT NULL, day TEXT NOT NULL, pnl TEXT NOT NULL,
            PRIMARY KEY(user_id, day));

        CREATE TABLE IF NOT EXISTS equity_history(
            user_id INTEGER NOT NULL, day TEXT NOT NULL,
            equity TEXT NOT NULL, peak TEXT NOT NULL,
            PRIMARY KEY(user_id, day));

        CREATE TABLE IF NOT EXISTS bot_state(
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

        CREATE TABLE IF NOT EXISTS referrals(
            inviter_id INTEGER NOT NULL, referred_id INTEGER NOT NULL,
            rewarded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            PRIMARY KEY(inviter_id, referred_id));

        CREATE TABLE IF NOT EXISTS referral_rewards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL, referred_id INTEGER NOT NULL,
            tier TEXT NOT NULL, bonus_days INTEGER NOT NULL, bonus_usdt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL, approved_at TEXT);

        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS signals_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            confidence INTEGER NOT NULL, direction TEXT NOT NULL,
            price TEXT, sl TEXT, tp TEXT, regime TEXT, reason TEXT,
            created_at TEXT NOT NULL);
        """)

# ─────────────────────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────────────────────
def set_state(key, value):
    with db() as c:
        c.execute("INSERT INTO bot_state(key,value,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, str(value), iso(now_utc())))

def get_state(key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def trading_enabled(uid):
    if get_state("EMERGENCY_STOP") == "true": return False
    if get_state(f"TRADING_ENABLED_{uid}") == "false": return False
    return True

def audit(uid, act, det=""):
    with db() as c:
        c.execute("INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)",
                  (uid, act, det[:2000], iso(now_utc())))

# ─────────────────────────────────────────────────────────────
# USERS / LICENSING
# ─────────────────────────────────────────────────────────────
def ref_code(uid): return f"TRK{uid}"

def create_user(uid, username, referred_by=None):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row: return row
        inviter = None
        if referred_by:
            inviter = c.execute("SELECT user_id FROM users WHERE ref_code=?",
                                (referred_by.upper(),)).fetchone()
            if inviter and int(inviter["user_id"]) == uid: inviter = None
        c.execute("INSERT INTO users(user_id,username,ref_code,referred_by,created_at) VALUES(?,?,?,?,?)",
                  (uid, username or "user", ref_code(uid),
                   referred_by.upper() if inviter else None, iso(now_utc())))
        if inviter:
            c.execute("INSERT OR IGNORE INTO referrals(inviter_id,referred_id,created_at) VALUES(?,?,?)",
                      (inviter["user_id"], uid, iso(now_utc())))
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def is_admin(uid): return uid in ADMIN_IDS

def get_subscription(uid):
    with db() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    if not row: return None
    try: valid = now_utc() <= parse_dt(row["expiry"])
    except Exception: valid = False
    return dict(row) | {"valid": valid}

def license_info(uid):
    if is_admin(uid):
        return {"valid": True, "tier": "ELITE",
                "expiry": now_utc() + timedelta(days=3650), "is_admin": True}
    sub = get_subscription(uid)
    if not sub or not sub["valid"]:
        return {"valid": False, "is_first_time": sub is None}
    return {"valid": True, "tier": sub["tier"], "expiry": parse_dt(sub["expiry"]),
            "is_first_time": False, "is_admin": False}

# ─────────────────────────────────────────────────────────────
# INVOICES
# ─────────────────────────────────────────────────────────────
def create_invoice(uid, tier):
    if tier not in TIERS: raise ValueError("Invalid tier")
    base = TIERS[tier]["price"]
    suffix = Decimal(secrets.randbelow(99) + 1) / Decimal("100")
    exact = base + suffix
    invoice_id = secrets.token_urlsafe(12)
    created = now_utc()
    expires = created + timedelta(minutes=INVOICE_EXPIRY_MINUTES)
    with db() as c:
        c.execute("INSERT INTO invoices(invoice_id,user_id,tier,base_amount,exact_amount,"
                  "created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?)",
                  (invoice_id, uid, tier, str(base), str(exact), iso(created), iso(expires), "PENDING"))
    audit(uid, "invoice_created", f"{invoice_id} {tier} {exact}")
    return {"invoice_id": invoice_id, "tier": tier, "base": base,
            "exact": exact, "expires": expires}

def get_invoice(iid, uid):
    with db() as c:
        row = c.execute("SELECT * FROM invoices WHERE invoice_id=? AND user_id=?",
                        (iid, uid)).fetchone()
    return dict(row) if row else None

# ─────────────────────────────────────────────────────────────
# PAYMENT VERIFICATION (Tron)
# ─────────────────────────────────────────────────────────────
def validate_tx_hash(tx): return bool(TX_RE.fullmatch(tx or ""))

async def verify_trc20_usdt(tx_hash, expected_amount, wallet):
    if not validate_tx_hash(tx_hash):
        return False, Decimal("0"), "Invalid TX hash (64 hex)"
    if not wallet or wallet.startswith("TYour"):
        return False, Decimal("0"), "Wallet not configured"
    with db() as c:
        if c.execute("SELECT 1 FROM payments WHERE tx_hash=?", (tx_hash,)).fetchone():
            return False, Decimal("0"), "TX already used"

    url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return False, Decimal("0"), f"Tronscan HTTP {resp.status}"
                data = await resp.json()
    except Exception as e:
        return False, Decimal("0"), f"Tronscan error: {e}"

    if not data or data.get("contractRet") != "SUCCESS":
        return False, Decimal("0"), f"TX status {data.get('contractRet','NOT FOUND')}"
    if data.get("confirmed") is not True:
        return False, Decimal("0"), "Not confirmed yet"
    try:
        ts = int(data.get("timestamp", 0)) / 1000
        age = time.time() - ts
        if age < 0 or age > INVOICE_EXPIRY_MINUTES * 60 + 900:
            return False, Decimal("0"), "Outside payment window"
    except Exception:
        return False, Decimal("0"), "Timestamp parse fail"

    transfers = data.get("trc20TransferInfo") or []
    received = Decimal("0")
    for t in transfers:
        if t.get("contract_address", "").lower() != USDT_TRC20_CONTRACT.lower(): continue
        if t.get("to_address", "").lower() != wallet.lower(): continue
        try: received += Decimal(str(t.get("amount_str", "0"))) / Decimal("1000000")
        except Exception: continue

    if received < Decimal(str(expected_amount)):
        return False, received, f"Underpaid ${received:.6f} (need ${Decimal(str(expected_amount)):.2f})"
    return True, received, "Verified"

def settle_payment(uid, invoice_id, tx_hash, received):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        invoice = c.execute("SELECT * FROM invoices WHERE invoice_id=? AND user_id=?",
                            (invoice_id, uid)).fetchone()
        if not invoice: c.execute("ROLLBACK"); return False, "Invoice not found"
        if invoice["status"] != "PENDING": c.execute("ROLLBACK"); return False, "Invoice not pending"
        if now_utc() > parse_dt(invoice["expires_at"]): c.execute("ROLLBACK"); return False, "Invoice expired"
        if c.execute("SELECT 1 FROM payments WHERE tx_hash=?", (tx_hash,)).fetchone():
            c.execute("ROLLBACK"); return False, "TX already used"

        sub = c.execute("SELECT * FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
        first = sub is None
        days = TIERS[invoice["tier"]]["first_days"] if first else TIERS[invoice["tier"]]["renew_days"]
        start = now_utc()
        if sub:
            try:
                old = parse_dt(sub["expiry"])
                if old > start: start = old
            except Exception: pass
        expiry = start + timedelta(days=days)
        tier = invoice["tier"]

        c.execute("INSERT INTO subscriptions(user_id,tier,expiry,first_purchase,updated_at) "
                  "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "tier=excluded.tier,expiry=excluded.expiry,updated_at=excluded.updated_at",
                  (uid, tier, iso(expiry), 1 if first else 0, iso(now_utc())))
        c.execute("INSERT INTO payments(tx_hash,user_id,invoice_id,tier,amount,paid_at) VALUES(?,?,?,?,?,?)",
                  (tx_hash, uid, invoice_id, tier, str(received), iso(now_utc())))
        c.execute("UPDATE invoices SET status='PAID',paid_tx=?,paid_amount=? WHERE invoice_id=?",
                  (tx_hash, str(received), invoice_id))

        user_row = c.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,)).fetchone()
        if user_row and user_row["referred_by"]:
            inviter_row = c.execute("SELECT user_id FROM users WHERE ref_code=?",
                                    (user_row["referred_by"],)).fetchone()
            if inviter_row:
                inviter_id = inviter_row["user_id"]
                exists = c.execute("SELECT 1 FROM referral_rewards WHERE referred_id=? AND inviter_id=?",
                                   (uid, inviter_id)).fetchone()
                if not exists:
                    bonus_days = {"BASIC":7,"PRO":10,"ELITE":15}[tier]
                    cnt = c.execute("SELECT COUNT(*) cnt FROM referrals WHERE inviter_id=?",
                                    (inviter_id,)).fetchone()["cnt"]
                    bonus_usdt = "10" if cnt >= 2 else "0"
                    c.execute("INSERT INTO referral_rewards(inviter_id,referred_id,tier,bonus_days,"
                              "bonus_usdt,status,created_at) VALUES(?,?,?,?,?,?,?)",
                              (inviter_id, uid, tier, bonus_days, bonus_usdt, "PENDING", iso(now_utc())))
        c.execute("COMMIT")

    audit(uid, "payment_settled", f"{invoice_id} {tx_hash} {received}")
    return True, {"tier": tier, "days": days, "expiry": expiry, "first": first}

# ─────────────────────────────────────────────────────────────
# API KEYS / EXCHANGE FACTORY
# ─────────────────────────────────────────────────────────────
def save_keys(uid, ak, sec, pp):
    with db() as c:
        c.execute("INSERT INTO api_credentials(user_id,api_key,api_secret,passphrase,updated_at) "
                  "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "api_key=excluded.api_key,api_secret=excluded.api_secret,"
                  "passphrase=excluded.passphrase,updated_at=excluded.updated_at",
                  (uid, encrypt(ak), encrypt(sec), encrypt(pp), iso(now_utc())))
    audit(uid, "keys_updated")

def get_keys(uid):
    with db() as c:
        row = c.execute("SELECT * FROM api_credentials WHERE user_id=?", (uid,)).fetchone()
    if not row: return None
    try:
        return {"apiKey": decrypt(row["api_key"]),
                "secret": decrypt(row["api_secret"]),
                "password": decrypt(row["passphrase"])}
    except Exception:
        return None

def make_exchange(keys):
    ex = ccxt_async.okx({
        "apiKey": keys["apiKey"], "secret": keys["secret"], "password": keys["password"],
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if not LIVE_MODE: ex.set_sandbox_mode(True)
    return ex

# ─────────────────────────────────────────────────────────────
# MARKET DATA CACHE
# ─────────────────────────────────────────────────────────────
_CACHE = {}

async def _cached_get_json(url, ttl, key):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit["t"] < ttl:
        return hit["v"]
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return hit["v"] if hit else None
                data = await resp.json()
        _CACHE[key] = {"t": now, "v": data}
        return data
    except Exception:
        return hit["v"] if hit else None

async def get_fundamentals(cid):
    data = await _cached_get_json(
        f"https://api.coingecko.com/api/v3/coins/{cid}?localization=false&tickers=false",
        COINGECKO_CACHE_SECONDS, f"cg_{cid}")
    if not data: return False
    try: return data.get("market_cap_rank", 999) <= 150
    except Exception: return False

async def get_sentiment():
    data = await _cached_get_json(
        "https://api.alternative.me/fng/?limit=1", 300, "fng")
    if not data: return 50, False
    try:
        v = int(data["data"][0]["value"])
        return v, 20 < v < 90
    except Exception:
        return 50, False
