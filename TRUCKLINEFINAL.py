# ===========================================================
# TruckLink FINAL v2.0 - ALL IN ONE
# LIVE SAFE $20 Cap + $20/$50 USDT + Referral Self-Promo
# File: trucklink_final.py
# ===========================================================
import os, json, time, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import ccxt
import pandas as pd
import ta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
load_dotenv()

# ====== SETTINGS - EDIT HERE ONLY ======
LIVE_MODE = True # False = Demo, True = Real Money
MAX_USDT_PER_TRADE = 20.0
MAX_DAILY_LOSS_USDT = 30.0
MAX_LOSS_PCT = 1.5
TAKE_PROFIT_PCT = 2.2
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PUT_TOKEN_HERE")
OKX_API_KEY = os.getenv("OKX_API_KEY", "PUT_KEY")
OKX_SECRET = os.getenv("OKX_SECRET", "PUT_SECRET")
OKX_PASSWORD = os.getenv("OKX_PASSWORD", "PUT_PASS")
YOUR_USDT_TRC20 = os.getenv("USDT_WALLET", "TYourTronWalletHere")
LICENSE_FILE = "licenses.json"
PNL_FILE = "daily_pnl.json"
REFERRAL_FILE = "referrals.json"
TIERS = {"BASIC": {"price": 20, "days": 30}, "SHARP": {"price": 50, "days": 30}}
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

print(f"{'🔴 LIVE' if LIVE_MODE else '🟢 DEMO'} | Cap ${MAX_USDT_PER_TRADE} | SL {MAX_LOSS_PCT}%")

# OKX
exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_SECRET, 'password': OKX_PASSWORD, 'options': {'defaultType': 'spot'}})
if not LIVE_MODE: exchange.set_sandbox_mode(True)

# ====== FILES ======
def load_json(f):
    try:
        with open(f, "r") as x: return json.load(x)
    except: return {}
def save_json(f, d):
    with open(f, "w") as x: json.dump(x, f, indent=2)

# LICENSE
def load_licenses(): return load_json(LICENSE_FILE)
def save_licenses(d): save_json(LICENSE_FILE, d)
def create_license(uid, tier):
    expiry = datetime.now() + timedelta(days=TIERS[tier]["days"])
    db = load_licenses(); db[str(uid)] = {"user_id": str(uid), "tier": tier, "expiry": expiry.isoformat()}; save_licenses(db); return db[str(uid)]
def check_license(uid):
    db = load_licenses(); lic = db.get(str(uid))
    if not lic: return {"valid": False, "reason": "No license"}
    if datetime.now() > datetime.fromisoformat(lic["expiry"]): return {"valid": False, "reason": "Expired"}
    return {"valid": True, "tier": lic["tier"], "expiry": datetime.fromisoformat(lic["expiry"])}

# PNL
def get_daily_pnl():
    d = load_json(PNL_FILE)
    if not d or d.get("date")!= datetime.now().date().isoformat(): return 0
    return d.get("pnl", 0)
def update_daily_pnl(chg):
    today = datetime.now().date().isoformat(); d = load_json(PNL_FILE)
    if not d or d.get("date")!= today: d = {"date": today, "pnl": 0}
    d["pnl"] += chg; save_json(PNL_FILE, d); return d["pnl"]

# REFERRAL
def load_refs(): return load_json(REFERRAL_FILE)
def save_refs(d): save_json(REFERRAL_FILE, d)
def get_ref_code(uid): return f"TRK{str(uid)[-6:]}"
def create_user_if_new(uid, username, referred_by=None):
    db = load_refs(); suid = str(uid)
    if suid not in db:
        db[suid] = {"user_id": suid, "username": username, "ref_code": get_ref_code(uid), "referred_by": referred_by, "referrals": [], "earned_days": 0, "earned_usdt": 0, "cash_paid": 0}
        if referred_by:
            for inv_id, data in db.items():
                if data["ref_code"] == referred_by and inv_id!= suid:
                    if suid not in data["referrals"]: data["referrals"].append(suid)
                    break
        save_refs(db)
    return db[suid]
def add_reward(inviter_id, friend_tier):
    db = load_refs(); inviter = db.get(str(inviter_id))
    if not inviter: return 0,0
    bonus_days = 15 if friend_tier == "SHARP" else 7
    lic_db = load_licenses(); lic = lic_db.get(str(inviter_id))
    if lic:
        expiry = datetime.fromisoformat(lic["expiry"]); lic["expiry"] = (expiry + timedelta(days=bonus_days)).isoformat(); save_licenses(lic_db)
    bonus_usdt = 0
    if len(inviter["referrals"]) >= 3 and inviter["cash_paid"] == 0:
        bonus_usdt = 10; inviter["cash_paid"] = 10; inviter["earned_usdt"] += 10
    inviter["earned_days"] += bonus_days; save_refs(db)
    return bonus_days, bonus_usdt

# TRON VERIFY AUTO
def verify_trc20_usdt(tx_hash, expected_amount, your_wallet):
    try:
        url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}"
        r = requests.get(url, timeout=15).json()
        if r.get('contractRet')!= 'SUCCESS': return False, 0, "TX not SUCCESS yet, wait 60s"
        transfers = r.get('trc20TransferInfo', [])
        actual = 0; found = False
        for t in transfers:
            if t.get('contract_address') == USDT_TRC20_CONTRACT and t.get('to_address','').lower() == your_wallet.lower():
                actual = int(t.get('amount_str','0')) / 1_000_000; found = True; break
        if not found: return False, 0, f"No USDT to {your_wallet[:6]}... in this TX"
        if actual + 0.5 < expected_amount: return False, actual, f"Paid ${actual:.2f}, need ${expected_amount}"
        used = load_json("used_tx.json")
        if tx_hash in used: return False, actual, "TX already used"
        return True, actual, "OK"
    except Exception as e:
        return False, 0, f"TronScan error: {e}"

def mark_tx_used(tx_hash, uid, amount, tier):
    used = load_json("used_tx.json"); used[tx_hash] = {"user_id": str(uid), "amount": amount, "tier": tier, "time": time.time()}; save_json("used_tx.json", used)

# ANALYSIS
def get_fundamentals(coin_id="bitcoin"):
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false", timeout=10).json()
        rank = r.get('market_cap_rank', 999)
        return {"is_healthy": rank <= 120, "score": 90 if rank<=20 else 60}
    except: return {"is_healthy": True, "score": 50}
def get_sentiment():
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()
        val = int(fg['data'][0]['value']); label = fg['data'][0]['value_classification']
        return {"value": val, "label": label, "can_trade": 25 < val < 85}
    except: return {"value": 50, "label": "Unknown", "can_trade": True}
def get_signals(df):
    try:
        df['ema50'] = ta.trend.ema_indicator(df['close'], 50); df['ema200'] = ta.trend.ema_indicator(df['close'], 200)
        df['rsi'] = ta.momentum.rsi(df['close'], 14)
        macd = ta.trend.MACD(df['close']); df['macd'] = macd.macd(); df['macd_signal'] = macd.macd_signal()
        last = df.iloc[-1]; tech = last['ema50'] > last['ema200'] and 45 < last['rsi'] < 70 and last['macd'] > last['macd_signal']
        return tech, last
    except: return False, df.iloc[-1]

# COMMANDS
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or "user"
    referred_by = context.args[0].upper() if context.args else None
    create_user_if_new(update.effective_user.id, username, referred_by)
    lic = check_license(update.effective_user.id)
    s = f"✅ {lic['tier']} till {lic['expiry'].date()}" if lic["valid"] else "🔒 No license - /subscribe"
    await update.message.reply_text(f"🤖 *TruckLink FINAL v2 LIVE* \n{s}\nCap ${MAX_USDT_PER_TRADE} | SL {MAX_LOSS_PCT}%\n\n/subscribe /verify /trade /stop /referral /myrefs /balance", parse_mode='Markdown')

async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💳 *USDT TRC20 Payment*\nBASIC $20/mo = 2 pairs\nSHARP $50/mo = 15 pairs + early warning\n\nWallet:\n`{YOUR_USDT_TRC20}`\n\nAfter pay:\n`/verify YOUR_TX_HASH SHARP`\n\nNeed TRC20 network! Fee $1", parse_mode='Markdown')

async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /verify <tx_hash> <BASIC or SHARP>")
            return
        tx_hash = context.args[0].strip(); tier = context.args[1].upper()
        if tier not in TIERS:
            await update.message.reply_text("Tier BASIC or SHARP")
            return
        expected = TIERS[tier]["price"]; uid = update.effective_user.id
        await update.message.reply_text(f"🔍 Verifying ${expected} on TronScan...\nTX {tx_hash[:10]}...")

        # AUTO VERIFY - If YOUR_USDT_TRC20 is still default, skip check for demo
        if YOUR_USDT_TRC20.startswith("TYour"):
            is_valid, actual, msg = True, expected, "Demo wallet - auto approved"
        else:
            is_valid, actual, msg = verify_trc20_usdt(tx_hash, expected, YOUR_USDT_TRC20)

        if not is_valid:
            await update.message.reply_text(f"❌ Not verified: {msg}\nWait 60s after sending and retry.")
            return

        create_license(uid, tier); mark_tx_used(tx_hash, uid, actual, tier)

        # Referral reward
        refs = load_refs(); me = refs.get(str(uid))
        if me and me["referred_by"]:
            for inv_id, data in refs.items():
                if data["ref_code"] == me["referred_by"]:
                    bd, bu = add_reward(inv_id, tier)
                    try: await context.bot.send_message(chat_id=int(inv_id), text=f"🎉 Friend bought {tier}! +{bd} days" + (f" + ${bu} USDT (3 friends!)" if bu>0 else ""))
                    except: pass
                    break

        await update.message.reply_text(f"✅ *{tier} LIVE Verified!* ${actual:.2f}\nCap ${MAX_USDT_PER_TRADE}/trade\nUse /trade", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or "user"
    referred_by = context.args[0].upper() if context.args else None
    user_data = create_user_if_new(update.effective_user.id, username, referred_by)
    bot_name = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_name}?start={user_data['ref_code']}"
    await update.message.reply_text(f"🔗 *Your Referral Link*\n`{link}`\n\nCode: `{user_data['ref_code']}`\nFriends: {len(user_data['referrals'])}\nDays earned: {user_data['earned_days']}\nCash earned: ${user_data['earned_usdt']}\n\n🎁 Per friend:\nBASIC ($20) = +7 days\nSHARP ($50) = +15 days\n3 friends = +$10 USDT\n\nShare in WhatsApp groups!", parse_mode='Markdown')

async def myrefs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_refs(); user = db.get(str(update.effective_user.id))
    if not user or not user["referrals"]:
        await update.message.reply_text("No refs yet. /referral to get link")
        return
    await update.message.reply_text(f"👥 Invited {len(user['referrals'])}: {', '.join(user['referrals'][:10])}\nEarned {user['earned_days']} days + ${user['earned_usdt']}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lic = check_license(update.effective_user.id); await update.message.reply_text(f"{lic} | Daily PnL ${get_daily_pnl():.2f} / -${MAX_DAILY_LOSS_USDT}")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bal = exchange.fetch_balance(); await update.message.reply_text(f"LIVE ${bal['USDT']['free']:.2f} | Cap ${MAX_USDT_PER_TRADE}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛑 Closing...")
    try:
        bal = exchange.fetch_balance()
        for sym in ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT"]:
            try:
                coin = sym.split("/")[0]; free = bal.get(coin,{}).get('free',0)
                if free>0.00001: exchange.create_market_sell_order(sym, free)
            except: pass
        await update.message.reply_text("✅ All closed")
    except Exception as e:
        await update.message.reply_text(f"Close manually on OKX app: {e}")

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; lic = check_license(uid)
    if not lic["valid"]:
        await update.message.reply_text("🔒 No license /subscribe")
        return
    if get_daily_pnl() <= -MAX_DAILY_LOSS_USDT:
        await update.message.reply_text(f"🛑 Daily loss ${MAX_DAILY_LOSS_USDT} hit - Locked till tomorrow")
        return
    is_sharp = lic["tier"] == "SHARP"; symbols = ["BTC/USDT","ETH/USDT"] if not is_sharp else ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT"]
    cmap = {"BTC/USDT":"bitcoin","ETH/USDT":"ethereum","SOL/USDT":"solana","BNB/USDT":"binancecoin"}
    for sym in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(sym, '15m', limit=200)
            df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','vol'])
            tech, last = get_signals(df)
            fund = get_fundamentals(cmap.get(sym,"bitcoin")) if is_sharp else {"is_healthy": True}
            senti = get_sentiment() if is_sharp else {"can_trade": True, "value": 50, "label": "N/A"}
            if not tech or not fund["is_healthy"] or not senti["can_trade"]: continue
            bal = exchange.fetch_balance(); usdt_free = bal['USDT']['free']
            trade_usdt = min(usdt_free * 0.15, MAX_USDT_PER_TRADE); amount = trade_usdt / last['close']
            exchange.create_market_buy_order(sym, amount)
            entry = last['close']; sl = entry * (1 - MAX_LOSS_PCT/100); tp = entry * (1 + TAKE_PROFIT_PCT/100)
            await update.message.reply_text(f"✅ LIVE {sym}\nEntry ${entry:.2f} SL ${sl:.2f} (-1.5%) TP ${tp:.2f} Size ${trade_usdt:.2f}")
            while True:
                curr = exchange.fetch_ticker(sym)['last']; pct = (curr-entry)/entry*100; pnl_u = (curr-entry)*amount
                if -1.0 < pct <= -0.7 and is_sharp: await update.message.reply_text(f"⚠️ EARLY {sym} {pct:.2f}%")
                if -1.5 < pct <= -1.0: await update.message.reply_text(f"🚨 PRE-SL {sym} {pct:.2f}%")
                if pct <= -MAX_LOSS_PCT:
                    exchange.create_market_sell_order(sym, amount); update_daily_pnl(pnl_u)
                    await update.message.reply_text(f"🛑 SL {sym} {pct:.2f}% = ${pnl_u:.2f} - Cap protected")
                    break
                if pct >= TAKE_PROFIT_PCT:
                    exchange.create_market_sell_order(sym, amount); update_daily_pnl(pnl_u)
                    await update.message.reply_text(f"💰 TP {sym} +{pct:.2f}% = +${pnl_u:.2f}")
                    break
                time.sleep(15)
        except Exception as e:
            await update.message.reply_text(f"{sym} err: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd)); app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd)); app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("myrefs", myrefs_cmd)); app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd)); app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    print(f"FINAL v2 running - {'LIVE' if LIVE_MODE else 'DEMO'} - Referral ON - Cap ${MAX_USDT_PER_TRADE}")
    app.run_polling()

if __name__ == "__main__": main()
