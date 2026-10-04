import os, sqlite3, logging, re, uuid, random
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trucklink")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USDT_WALLET = os.getenv("USDT_WALLET", "T...")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()]
LIVE_MODE = os.getenv("LIVE_MODE","false").lower()=="true"
AUTO_TRADE = os.getenv("AUTO_TRADE","false").lower()=="true"
AUTO_INTERVAL_MINUTES = int(os.getenv("AUTO_INTERVAL_MINUTES","30"))
DB_PATH = os.getenv("DB_PATH","trucklink.db")

TIERS = {
    "BASIC": {"price": 20, "days": 60, "pairs": 2},
    "PRO": {"price": 50, "days": 60, "pairs": 3},
    "ELITE": {"price": 70, "days": 60, "pairs": 7},
}
SYMBOLS = {
    "BASIC": ["BTC/USDT", "ETH/USDT"],
    "PRO": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "ELITE": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "PEPE/USDT", "LINK/USDT"],
}
RISK_PER_TRADE = 0.02
MAX_POSITIONS = 3
MAX_DAILY_LOSS = 15

FERNET_KEY = os.getenv("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing in Railway! Add your existing key.")
fernet = Fernet(FERNET_KEY.encode())

def encrypt_api_keys(s: str) -> str: return fernet.encrypt(s.encode()).decode()
def decrypt_api_keys(t: str) -> str:
    try: return fernet.decrypt(t.encode()).decode()
    except: return t

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def iso(dt): return dt.astimezone(timezone.utc).isoformat()
def now_utc(): return datetime.now(timezone.utc)
def parse_dt(s): return datetime.fromisoformat(s.replace("Z","+00:00"))
def to_dec(v):
    try: return Decimal(str(v))
    except: return Decimal("0")

def require_production_config():
    assert TELEGRAM_TOKEN, "TELEGRAM_TOKEN missing"
    assert FERNET_KEY, "FERNET_KEY missing"

def set_state(k,v):
    with db() as c: c.execute("INSERT OR REPLACE INTO states(key,val) VALUES(?,?)", (k,v))
def get_state(k, default="false"):
    with db() as c:
        r=c.execute("SELECT val FROM states WHERE key=?", (k,)).fetchone()
        return r["val"] if r else default
def trading_enabled(uid): return get_state(f"TRADING_ENABLED_{uid}", "true")=="true" and get_state("EMERGENCY_STOP","false")=="false"
def is_admin(uid): return uid in ADMIN_IDS

def create_user(user_id: int, username: str = None, referred_code: str = None):
    with db() as c:
        ex = c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not ex:
            ref_code = f"TRUCK{user_id}"[-12:].upper()
            c.execute("INSERT INTO users(user_id, username, ref_code, created_at) VALUES(?,?,?,?)", (user_id, username or "", ref_code, iso(now_utc())))
            if referred_code:
                inv = c.execute("SELECT user_id FROM users WHERE ref_code=?", (referred_code,)).fetchone()
                if inv and inv["user_id"]!=user_id:
                    if not c.execute("SELECT * FROM referrals WHERE referred_id=?", (user_id,)).fetchone():
                        c.execute("INSERT INTO referrals(inviter_id, referred_id, created_at) VALUES(?,?,?)", (inv["user_id"], user_id, iso(now_utc())))
        elif username:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))

def license_info(uid):
    if is_admin(uid): return {"valid": True, "tier":"ELITE", "expiry": now_utc()+timedelta(days=365), "is_admin": True}
    with db() as c:
        r=c.execute("SELECT tier, expiry FROM subscriptions WHERE user_id=? ORDER BY expiry DESC LIMIT 1", (uid,)).fetchone()
        if not r: return {"valid": False, "tier":"NONE"}
        try:
            exp=parse_dt(r["expiry"])
            return {"valid": exp>now_utc(), "tier": r["tier"], "expiry": exp, "is_admin": False}
        except: return {"valid": False, "tier":"NONE"}

def daily_pnl(uid):
    day = now_utc().date().isoformat()
    with db() as c:
        r = c.execute("SELECT pnl FROM daily_pnl WHERE user_id=? AND day=?", (uid, day)).fetchone()
    return to_dec(r["pnl"]) if r else Decimal("0")

def grant_trial(uid):
    with db() as c:
        if c.execute("SELECT * FROM subscriptions WHERE user_id=? AND expiry>? ", (uid, iso(now_utc()))).fetchone():
            return False, "You already have active subscription"
        if c.execute("SELECT * FROM trials WHERE user_id=?", (uid,)).fetchone():
            return False, "Trial already used"
        exp = now_utc() + timedelta(hours=24)
        c.execute("INSERT OR REPLACE INTO subscriptions(user_id, tier, expiry) VALUES(?,?,?)", (uid, "BASIC", iso(exp)))
        c.execute("INSERT INTO trials(user_id, created_at) VALUES(?,?)", (uid, iso(now_utc())))
        return True, "Granted"

def check_circuit_breaker(uid):
    pnl = daily_pnl(uid)
    if pnl < Decimal(f"-{MAX_DAILY_LOSS}"):
        set_state(f"TRADING_ENABLED_{uid}", "false")
        return True
    return False

def create_invoice(uid, tier):
    base = float(TIERS[tier]["price"])
    exact = base + random.uniform(0.000001, 0.000999)
    inv_id = str(uuid.uuid4())[:8].upper()
    exp = now_utc() + timedelta(minutes=30)
    with db() as c:
        c.execute("INSERT INTO invoices(invoice_id, user_id, tier, base_amount, exact_amount, status, expires_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (inv_id, uid, tier, str(base), str(exact), "PENDING", iso(exp), iso(now_utc())))
    return {"invoice_id": inv_id, "base": base, "exact": exact, "expires": exp}

def get_invoice(inv_id, uid):
    with db() as c: return c.execute("SELECT * FROM invoices WHERE invoice_id=? AND user_id=?", (inv_id, uid)).fetchone()
def validate_tx_hash(h): return bool(re.match(r"^[a-fA-F0-9]{64}$", h))

# SECURED - DETECTS FAKE HASH & UNDERPAYMENT
async def verify_trc20_usdt(tx_hash, expected: Decimal, wallet: str):
    USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    if not validate_tx_hash(tx_hash):
        return False, Decimal("0"), "Invalid hash format"
    with db() as c:
        if c.execute("SELECT * FROM invoices WHERE tx_hash=?", (tx_hash,)).fetchone():
            return False, Decimal("0"), "❌ TX already used"
        if c.execute("SELECT * FROM payments WHERE tx_hash=?", (tx_hash,)).fetchone():
            return False, Decimal("0"), "❌ TX already used"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}", timeout=15) as r:
                if r.status!= 200:
                    return False, Decimal("0"), f"Tronscan error {r.status}"
                j = await r.json()
                if not j.get("confirmed"):
                    return False, Decimal("0"), "Not confirmed yet - wait 1 min"
                if j.get("confirmations",0) < 20:
                    return False, Decimal("0"), f"Need 20 conf, got {j.get('confirmations')}"
                if j.get("contractRet")!= "SUCCESS":
                    return False, Decimal("0"), f"TX failed {j.get('contractRet')}"
                trc20 = j.get("trc20TransferInfo", []) or j.get("tokenTransferInfo", {}).get("transfersAllList", [])
                found = False
                received_amount = Decimal("0")
                for t in trc20:
                    to_addr = t.get("to_address") or t.get("toAddress") or ""
                    contract = t.get("contract_address") or t.get("contractAddress") or ""
                    amt_str = t.get("amount_str") or t.get("amount") or "0"
                    try:
                        amt = Decimal(str(amt_str)) / Decimal("1000000") if Decimal(str(amt_str)) > Decimal("1000000") else Decimal(str(amt_str))
                    except:
                        amt = Decimal(str(t.get("quant",0))) / Decimal("1000000")
                    if USDT_CONTRACT not in str(t) and contract!= USDT_CONTRACT:
                        continue
                    if to_addr.lower()!= wallet.lower() and to_addr!= wallet:
                        continue
                    found = True
                    received_amount = amt
                    break
                if not found:
                    return False, Decimal("0"), f"❌ No USDT to {wallet[:6]}... in this TX - fake hash"
                if received_amount < expected - Decimal("0.00001"):
                    return False, received_amount, f"❌ Underpayment: got ${received_amount:.6f} need EXACT ${expected:.6f}. Send ${expected - received_amount:.6f} more"
                return True, received_amount, "OK"
    except Exception as e:
        log.exception(f"verify error")
        return False, Decimal("0"), f"Verify error {e} - retry"

def settle_payment(uid, invoice_id, tx_hash, received):
    with db() as c:
        inv=c.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not inv: return False, "Invoice not found"
        tier=inv["tier"]
        days=TIERS[tier]["days"]
        old=c.execute("SELECT expiry FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
        base=now_utc()
        if old:
            try:
                o=parse_dt(old["expiry"])
                if o>base: base=o
            except: pass
        new_exp=base+timedelta(days=days)
        c.execute("INSERT OR REPLACE INTO subscriptions(user_id, tier, expiry) VALUES(?,?,?)", (uid, tier, iso(new_exp)))
        c.execute("UPDATE invoices SET status='PAID', tx_hash=?, paid_amount=? WHERE invoice_id=?", (tx_hash, str(received), invoice_id))
        c.execute("INSERT INTO payments(id, user_id, tier, amount, tx_hash, created_at) VALUES(?,?,?,?,?,?)", (invoice_id, uid, tier, str(received), tx_hash, iso(now_utc())))
        ref=c.execute("SELECT inviter_id FROM referrals WHERE referred_id=?", (uid,)).fetchone()
        if ref:
            c.execute("INSERT INTO referral_rewards(inviter_id, referred_id, tier, bonus_days, status, created_at) VALUES(?,?,?,?,?,?)",
                      (ref["inviter_id"], uid, tier, 5, "PENDING", iso(now_utc())))
    return True, {"tier": tier, "days": days, "expiry": new_exp}

def save_keys(uid, ak, sec, pp):
    enc_ak=encrypt_api_keys(ak); enc_sec=encrypt_api_keys(sec); enc_pp=encrypt_api_keys(pp)
    with db() as c: c.execute("INSERT OR REPLACE INTO api_keys(user_id, apiKey, secret, password, updated_at) VALUES(?,?,?,?,?)", (uid, enc_ak, enc_sec, enc_pp, iso(now_utc())))
def get_keys(uid):
    with db() as c:
        r=c.execute("SELECT * FROM api_keys WHERE user_id=?", (uid,)).fetchone()
        if not r: return None
        try:
            return {"apiKey": decrypt_api_keys(r["apiKey"]), "secret": decrypt_api_keys(r["secret"]), "password": decrypt_api_keys(r["password"])}
        except:
            return None

def make_exchange(keys):
    import ccxt.async_support as ccxt_async
    ex=ccxt_async.okx({"apiKey": keys["apiKey"], "secret": keys["secret"], "password": keys["password"], "enableRateLimit": True, "options":{"defaultType":"spot"}})
    if not LIVE_MODE: ex.set_sandbox_mode(True)
    return ex

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS states(key TEXT PRIMARY KEY, val TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS daily_pnl(user_id INTEGER, day TEXT, pnl TEXT, PRIMARY KEY(user_id, day))")
        c.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, ref_code TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS referrals(inviter_id INTEGER, referred_id INTEGER PRIMARY KEY, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS referral_rewards(id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER, referred_id INTEGER, tier TEXT, bonus_days INTEGER, status TEXT, created_at TEXT, approved_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS subscriptions(user_id INTEGER PRIMARY KEY, tier TEXT, expiry TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS trials(user_id INTEGER PRIMARY KEY, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS invoices(invoice_id TEXT PRIMARY KEY, user_id INTEGER, tier TEXT, base_amount TEXT, exact_amount TEXT, status TEXT, expires_at TEXT, created_at TEXT, tx_hash TEXT, paid_amount TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, user_id INTEGER, tier TEXT, amount TEXT, tx_hash TEXT, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, symbol TEXT, side TEXT, entry_price TEXT, amount TEXT, sl TEXT, tp TEXT, sl_order_id TEXT, status TEXT, created_at TEXT, close_price TEXT, close_reason TEXT, pnl TEXT, closed_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS api_keys(user_id INTEGER PRIMARY KEY, apiKey TEXT, secret TEXT, password TEXT, updated_at TEXT)")
