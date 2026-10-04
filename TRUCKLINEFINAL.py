import asyncio, time, re
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from config import *
from core import trade_for_user, user_lock, monitor_job, auto_job, open_positions

_rate = {}
def rate_ok(uid):
    now=time.time()
    arr=_rate.get(uid, [])
    arr=[t for t in arr if now-t<60]
    if len(arr)>=5: return False
    arr.append(now); _rate[uid]=arr; return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    username=update.effective_user.username or ""
    args=context.args
    ref_code=args[0] if args else None
    create_user(uid, username, ref_code)
    lic=license_info(uid)
    kb=[
        [InlineKeyboardButton("🎁 FREE Trial", callback_data="trial"), InlineKeyboardButton("💳 Subscribe", callback_data="buy")],
        [InlineKeyboardButton("🔑 Set Keys", callback_data="setkeys"), InlineKeyboardButton("📊 Trade", callback_data="trade")],
        [InlineKeyboardButton("👥 Referral", callback_data="referral"), InlineKeyboardButton("📈 Status", callback_data="status")],
    ]
    txt=f"""🚀 **TruckLink AutoBot v5.4.2 SECURE**

Welcome {username or uid}!

TIERS:
• BASIC $20 - 2 pairs / 60 days
• PRO $50 - 3 pairs / 60 days
• ELITE $70 - 7 pairs / 60 days

✅ Fake hash & underpay detection ON
✅ ATR sizing + Circuit breaker + Rate limit

License: {lic['tier'] if lic['valid'] else 'NONE'} {'✅' if lic['valid'] else '❌'}

👇 Choose:"""
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    d=q.data
    if d=="trial": await trial_cmd(update, context, is_cb=True)
    elif d=="buy": await buy_cmd(update, context, is_cb=True)
    elif d=="setkeys": await q.message.reply_text("Use: /setkeys API_KEY SECRET PASSPHRASE")
    elif d=="trade": await trade_cmd(update, context, is_cb=True)
    elif d=="referral": await referral_cmd(update, context, is_cb=True)
    elif d=="status": await status_cmd(update, context, is_cb=True)
    elif d.startswith("sub_"):
        tier=d.split("_")[1]
        inv=create_invoice(q.from_user.id, tier)
        await q.message.reply_text(f"💳 **{tier} ${TIERS[tier]['price']}**\n\nInvoice: `{inv['invoice_id']}`\nSEND EXACT: `${inv['exact']:.6f}` USDT TRC20\nTO: `{USDT_WALLET}`\n\nExpires: 30 min\nThen: /verify {inv['invoice_id']} TX_HASH", parse_mode="Markdown")

async def trial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb=False):
    uid=(update.callback_query.from_user.id if is_cb else update.effective_user.id)
    if not rate_ok(uid): return
    ok,msg=grant_trial(uid)
    target=update.callback_query.message if is_cb else update.message
    if ok:
        await target.reply_text(f"🎁 Trial activated! BASIC 24h free\n2 pairs enabled\nUse /setkeys then /trade")
    else:
        await target.reply_text(f"❌ {msg}\nUse /buy")

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb=False):
    kb=[[InlineKeyboardButton(f"BASIC ${TIERS['BASIC']['price']}", callback_data="sub_BASIC")],
        [InlineKeyboardButton(f"PRO ${TIERS['PRO']['price']} - 3 pairs", callback_data="sub_PRO")],
        [InlineKeyboardButton(f"ELITE ${TIERS['ELITE']['price']} - 7 pairs", callback_data="sub_ELITE")]]
    target=update.callback_query.message if is_cb else update.message
    await target.reply_text("💳 Choose tier:", reply_markup=InlineKeyboardMarkup(kb))

async def setkeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not rate_ok(uid): await update.message.reply_text("⏳ Wait 1 min"); return
    if len(context.args)<3:
        await update.message.reply_text("Usage: /setkeys API_KEY SECRET PASSPHRASE\nGet from OKX -> API -> Create (trade permission)")
        return
    ak,sec,pp=context.args[0], context.args[1], context.args[2]
    # test keys
    keys={"apiKey": ak, "secret": sec, "password": pp}
    ex=make_exchange(keys)
    try:
        await ex.load_markets()
        await ex.fetch_balance()
        save_keys(uid, ak, sec, pp)
        await update.message.reply_text("✅ Keys saved & verified!\nUse /trade to scan or wait for auto trade")
    except Exception as e:
        await update.message.reply_text(f"❌ Keys fail: {e}\nCheck permissions (need trade)")
    finally: await ex.close()

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb=False):
    uid=(update.callback_query.from_user.id if is_cb else update.effective_user.id)
    if not rate_ok(uid):
        tgt=update.callback_query.message if is_cb else update.message
        await tgt.reply_text("⏳ Rate limit - wait 1 min"); return
    tgt=update.callback_query.message if is_cb else update.message
    async with user_lock(uid):
        try:
            await tgt.reply_text("🔍 Scanning with ATR filter...")
            await trade_for_user(uid, context.bot, broadcast=True)
        except Exception as e:
            log.exception(f"trade_cmd {e}")
            await tgt.reply_text(f"⚠️ Scan error: {e}\nTry /signals or /status")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not rate_ok(uid): return
    keys=get_keys(uid)
    if not keys: await update.message.reply_text("❌ /setkeys first"); return
    lic=license_info(uid)
    if not lic["valid"]: await update.message.reply_text("❌ No license /trial"); return
    from core import compute_signal
    ex=make_exchange(keys)
    try:
        await ex.load_markets()
        syms=SYMBOLS.get(lic["tier"], SYMBOLS["BASIC"])
        txt="📡 Signals:\n"
        for s in syms:
            sig=await compute_signal(ex, s)
            if sig.ok: txt+=f"✅ {s} {sig.confidence}/10 Entry {float(sig.price):.2f} SL {float(sig.sl):.2f} {sig.reason}\n"
            else: txt+=f"⏸ {s} {sig.reason}\n"
        await update.message.reply_text(txt)
    finally: await ex.close()

async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if len(context.args)<2:
        await update.message.reply_text("Usage: /verify INVOICE_ID TX_HASH"); return
    inv_id=context.args[0].upper(); tx=context.args[1]
    inv=get_invoice(inv_id, uid)
    if not inv: await update.message.reply_text("❌ Invoice not found"); return
    if inv["status"]=="PAID": await update.message.reply_text("✅ Already paid"); return
    try: exp=parse_dt(inv["expires_at"])
    except: exp=now_utc()
    if exp < now_utc(): await update.message.reply_text("❌ Invoice expired - /buy new one"); return
    await update.message.reply_text("🔍 Verifying on-chain - checking wallet, amount, confirmations...")
    from decimal import Decimal
    expected=to_dec(inv["exact_amount"])
    ok, received, msg = await verify_trc20_usdt(tx, expected, USDT_WALLET)
    if not ok:
        await update.message.reply_text(f"❌ {msg}"); return
    ok2, res = settle_payment(uid, inv_id, tx, received)
    if ok2:
        await update.message.reply_text(f"✅ Payment confirmed ${float(received):.6f}\nTier {res['tier']} until {res['expiry'].date()} ({res['days']} days)\nNow /setkeys then /trade")
    else:
        await update.message.reply_text(f"❌ {res}")

async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb=False):
    uid=(update.callback_query.from_user.id if is_cb else update.effective_user.id)
    with db() as c:
        u=c.execute("SELECT ref_code FROM users WHERE user_id=?", (uid,)).fetchone()
        code=u["ref_code"] if u else f"TRUCK{uid}"
        count=c.execute("SELECT COUNT(*) as cnt FROM referrals WHERE inviter_id=?", (uid,)).fetchone()["cnt"]
        rewards=c.execute("SELECT * FROM referral_rewards WHERE inviter_id=?", (uid,)).fetchall()
    botname=(await context.bot.get_me()).username
    link=f"https://t.me/{botname}?start={code}"
    tgt=update.callback_query.message if is_cb else update.message
    await tgt.reply_text(f"👥 **Referral**\n\nYour link (1-click copy):\n`{link}`\n\nReferrals: {count}\nBonus: +5 days per paid referral\n\nRewards: {len(rewards)} pending\n\nShare to earn free months!", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb=False):
    uid=(update.callback_query.from_user.id if is_cb else update.effective_user.id)
    lic=license_info(uid)
    pos=open_positions(uid)
    pnl=daily_pnl(uid)
    enabled=trading_enabled(uid)
    txt=f"""📈 **Status v5.4.2**

License: {lic.get('tier','NONE')} {'✅' if lic.get('valid') else '❌'} {lic.get('expiry','')}
Trading: {'🟢 ON' if enabled else '🔴 OFF'}
Daily PnL: ${float(pnl):.2f}
Open: {len(pos)}/{MAX_POSITIONS}
Auto: {'ON' if AUTO_TRADE else 'OFF'} {AUTO_INTERVAL_MINUTES}m
LIVE: {LIVE_MODE}

Pairs: {SYMBOLS.get(lic.get('tier','BASIC'))}
"""
    tgt=update.callback_query.message if is_cb else update.message
    await tgt.reply_text(txt, parse_mode="Markdown")

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; set_state(f"TRADING_ENABLED_{uid}", "false")
    await update.message.reply_text("⏸ Paused")
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; set_state(f"TRADING_ENABLED_{uid}", "true")
    await update.message.reply_text("▶️ Resumed")

def main():
    require_production_config()
    init_db()
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trial", trial_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("setkeys", setkeys_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))
    jq=app.job_queue
    jq.run_repeating(monitor_job, interval=60, first=10)
    if AUTO_TRADE:
        jq.run_repeating(auto_job, interval=AUTO_INTERVAL_MINUTES*60, first=30)
    log.info(f"Bot v5.4.2 SECURE LIVE={LIVE_MODE} AUTO={AUTO_TRADE}")
    app.run_polling()

if __name__=="__main__": main()
