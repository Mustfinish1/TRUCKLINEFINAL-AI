# TruckLink v5.3 FINAL
import asyncio
from datetime import timedelta
from decimal import Decimal
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    log, db, iso, now_utc, parse_dt, to_dec,
    TELEGRAM_TOKEN, USDT_WALLET, ADMIN_IDS,
    LIVE_MODE, AUTO_TRADE, AUTO_INTERVAL_MINUTES,
    TIERS, SYMBOLS,
    require_production_config, init_db,
    set_state, get_state, trading_enabled,
    is_admin, create_user, license_info,
    create_invoice, get_invoice, validate_tx_hash,
    verify_trc20_usdt, settle_payment,
    save_keys, get_keys, make_exchange,
)
from core import (
    user_lock, open_positions,
    compute_signal, close_position, trade_for_user,
    auto_job, monitor_job, reconcile_job, backup_db_job,
    get_peak_equity,
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    referred = context.args[0].upper() if context.args else None
    create_user(uid, update.effective_user.username, referred)
    lic = license_info(uid)
    if lic.get("is_admin"):
        await update.message.reply_text("👑 ADMIN\n/status /trade /signals /positions /balance\n/admin /pendingrefs /backupnow /broadcast /checktx /pause /resume\n/referral")
        return
    if not lic["valid"]:
        await update.message.reply_text("🛡️ TruckLink v5.3\nBASIC $20 · 2 pairs\nPRO $50 · 3 pairs\nELITE $70 · 7 pairs\n\n/subscribe BASIC|PRO|ELITE\nHave code? /setreferral CODE")
    elif not get_keys(uid):
        await update.message.reply_text(f"✅ {lic['tier']} till {lic['expiry'].date()}\n/setkeys API_KEY SECRET PASSPHRASE")
    else:
        await update.message.reply_text(f"🤖 {lic['tier']} till {lic['expiry'].date()}\n/trade /positions /balance /status /signals /referral")

async def subscribe_cmd(update, context):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username)
    if is_admin(uid): await update.message.reply_text("👑 Admin free"); return
    tier = (context.args[0].upper() if context.args else "")
    if tier not in TIERS: await update.message.reply_text("Usage: /subscribe BASIC|PRO|ELITE"); return
    inv = create_invoice(uid, tier)
    await update.message.reply_text(f"💳 {tier}\nBase ${inv['base']:.2f}\nEXACT ${inv['exact']:.2f} USDT TRC20\nInvoice {inv['invoice_id']}\nExpires {inv['expires'].strftime('%H:%M UTC')}\n\nSend EXACT ${inv['exact']:.2f} to:\n{USDT_WALLET}\n\nThen /verify {inv['invoice_id']} TXHASH")

async def verify_cmd(update, context):
    uid = update.effective_user.id
    if is_admin(uid): await update.message.reply_text("👑 Admin no pay"); return
    if len(context.args)!=2: await update.message.reply_text("Usage: /verify INVOICE_ID TXHASH"); return
    invoice_id, tx = context.args
    inv = get_invoice(invoice_id, uid)
    if not inv: await update.message.reply_text("Invoice not found"); return
    if inv["status"]!="PENDING": await update.message.reply_text("Not pending"); return
    if now_utc() > parse_dt(inv["expires_at"]): await update.message.reply_text("Expired /subscribe again"); return
    await update.message.reply_text("🔍 Verifying...")
    ok, received, msg = await verify_trc20_usdt(tx, Decimal(inv["exact_amount"]), USDT_WALLET)
    if not ok: await update.message.reply_text("❌ "+msg); return
    ok2, result = settle_payment(uid, invoice_id, tx, received)
    if not ok2: await update.message.reply_text("❌ "+str(result)); return
    await update.message.reply_text(f"✅ {result['tier']} {result['days']} days till {result['expiry'].date()}\nPaid ${received:.6f}\n/setkeys")

async def setkeys_cmd(update, context):
    uid = update.effective_user.id
    if not license_info(uid)["valid"]: await update.message.reply_text("❌ Subscribe first"); return
    if len(context.args)!=3: await update.message.reply_text("Usage: /setkeys API_KEY SECRET PASSPHRASE"); return
    ak, sec, pp = context.args
    ex=None
    try:
        import ccxt.async_support as ccxt_async
        ex = ccxt_async.okx({"apiKey": ak, "secret": sec, "password": pp, "enableRateLimit": True, "options": {"defaultType": "spot"}})
        if not LIVE_MODE: ex.set_sandbox_mode(True)
        bal = await ex.fetch_balance()
        save_keys(uid, ak, sec, pp)
        try: await update.message.delete()
        except: pass
        free = to_dec(bal.get("USDT", {}).get("free") or 0)
        await update.effective_chat.send_message(f"✅ Keys OK · USDT free ${free:.2f}")
    except Exception as e: await update.message.reply_text(f"❌ Keys fail: {e}")
    finally:
        if ex:
            try: await ex.close()
            except: pass

async def positions_cmd(update, context):
    rows = open_positions(update.effective_user.id)
    if not rows: await update.message.reply_text("No positions"); return
    await update.message.reply_text("\n".join([f"{p['symbol']} entry {to_dec(p['entry_price'])}" for p in rows]))

async def balance_cmd(update, context):
    uid = update.effective_user.id
    keys = get_keys(uid)
    if not keys: await update.message.reply_text("❌ /setkeys"); return
    ex = make_exchange(keys)
    try:
        bal = await ex.fetch_balance()
        free = to_dec(bal.get("USDT", {}).get("free") or 0)
        total = to_dec(bal.get("USDT", {}).get("total") or 0)
        await update.message.reply_text(f"USDT free ${free:.2f}\nTotal ${total:.2f}")
    except Exception as e: await update.message.reply_text(f"❌ {e}")
    finally: await ex.close()

async def status_cmd(update, context):
    uid = update.effective_user.id
    lic = license_info(uid)
    with db() as c:
        row = c.execute("SELECT pnl FROM daily_pnl WHERE user_id=? AND day=?", (uid, now_utc().date().isoformat())).fetchone()
    pnl = to_dec(row["pnl"]) if row else Decimal("0")
    pos = len(open_positions(uid))
    await update.message.reply_text(f"{lic.get('tier','NONE')} valid={lic['valid']}\nPos {pos} · PnL {pnl:+.2f}\nTrading {'🟢' if trading_enabled(uid) else '🔴'}")

async def trade_cmd(update, context):
    uid = update.effective_user.id
    if not license_info(uid)["valid"]: await update.message.reply_text("❌ Subscribe"); return
    if not get_keys(uid): await update.message.reply_text("❌ /setkeys"); return
    await update.message.reply_text("🚀 Scanning… you will get result in 15s")
    try:
        await trade_for_user(uid, context.bot, broadcast=True)
    except Exception as e:
        log.exception(f"trade fail {uid}")
        await update.message.reply_text(f"⚠️ Scan error: {e}\nTry /signals")

async def signals_cmd(update, context):
    await update.message.reply_text("🔎 Scanning (no orders)…")
    uid = update.effective_user.id
    keys = get_keys(uid)
    if not keys: await update.message.reply_text("❌ /setkeys"); return
    ex = make_exchange(keys)
    try:
        await ex.load_markets()
        lic = license_info(uid)
        any_sig=False
        for sym in SYMBOLS[lic["tier"]]:
            try:
                sig = await compute_signal(ex, sym)
                if sig.ok:
                    any_sig=True
                    await update.message.reply_text(f"🚨 {sym} LONG conf {sig.confidence}/10\nEntry {sig.price} SL {sig.sl}")
            except Exception as e:
                await update.message.reply_text(f"⚠️ {sym} {e}")
        if not any_sig: await update.message.reply_text("No qualifying signals now.")
    finally: await ex.close()

async def stop_cmd(update, context):
    uid = update.effective_user.id
    rows = open_positions(uid)
    if not rows: await update.message.reply_text("No positions"); return
    await update.message.reply_text("🛑 Closing…")
    keys = get_keys(uid)
    ex = make_exchange(keys)
    try:
        async with user_lock(uid):
            for p in rows:
                try:
                    t = await ex.fetch_ticker(p["symbol"])
                    await close_position(context.bot, uid, ex, p, "MANUAL", to_dec(t["last"]))
                except: pass
    finally: await ex.close()

async def pause_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    set_state("EMERGENCY_STOP","true")
    await update.message.reply_text("⏸ Paused")

async def resume_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    set_state("EMERGENCY_STOP","false")
    await update.message.reply_text("▶️ Resumed")

async def resume_uid_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    set_state(f"TRADING_ENABLED_{context.args[0]}","true")
    await update.message.reply_text(f"▶️ Re-enabled {context.args[0]}")

async def referral_cmd(update, context):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        n = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (uid,)).fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM referral_rewards WHERE inviter_id=? AND status='PENDING'", (uid,)).fetchone()["n"]
        approved = c.execute("SELECT COALESCE(SUM(bonus_days),0) d FROM referral_rewards WHERE inviter_id=? AND status='APPROVED'", (uid,)).fetchone()["d"]
    me = (await context.bot.get_me()).username
    await update.message.reply_text(
        f"🔥 YOUR REFERRAL LINK (COPY & SEND):\n\nhttps://t.me/{me}?start={u['ref_code']}\n\nYOUR CODE: `{u['ref_code']}`\n\nReferrals {n} · Approved {approved}d · Pending {pending}\n\nFriend clicks link = AUTO registered under you.",
        parse_mode="Markdown")

async def setreferral_cmd(update, context):
    uid = update.effective_user.id
    if not context.args: await update.message.reply_text("Usage: /setreferral CODE"); return
    code = context.args[0].upper().strip()
    with db() as c:
        row = c.execute("SELECT user_id FROM users WHERE ref_code=?", (code,)).fetchone()
        if not row: await update.message.reply_text("❌ Code not found"); return
        if row["user_id"]==uid: await update.message.reply_text("❌ Can't refer yourself"); return
        if c.execute("SELECT * FROM referrals WHERE referred_id=?", (uid,)).fetchone():
            await update.message.reply_text("❌ Already have referrer"); return
        c.execute("INSERT INTO referrals(inviter_id, referred_id, created_at) VALUES(?,?,?)", (row["user_id"], uid, iso(now_utc())))
    await update.message.reply_text(f"✅ Referrer set to {code}")

async def leaderboard_cmd(update, context):
    with db() as c:
        rows = c.execute("SELECT u.username, COUNT(r.referred_id) n FROM users u LEFT JOIN referrals r ON u.user_id=r.inviter_id GROUP BY u.user_id ORDER BY n DESC LIMIT 10").fetchall()
    await update.message.reply_text("\n".join([f"{i}. @{r['username']} - {r['n']}" for i,r in enumerate(rows,1)]))

async def pending_refs_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    with db() as c:
        rows = c.execute("SELECT id, inviter_id, referred_id, tier, bonus_days FROM referral_rewards WHERE status='PENDING' ORDER BY created_at DESC LIMIT 20").fetchall()
    if not rows: await update.message.reply_text("No pending ✅"); return
    await update.message.reply_text("⏳ PENDING\n"+"\n".join([f"ID {r['id']} Inv {r['inviter_id']} Ref {r['referred_id']} +{r['bonus_days']}d" for r in rows])+"\n\n/approveref ID")

async def approve_ref_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    rid=int(context.args[0])
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM referral_rewards WHERE id=? AND status='PENDING'",(rid,)).fetchone()
        if not r: c.execute("ROLLBACK"); await update.message.reply_text("Not found"); return
        sub=c.execute("SELECT expiry FROM subscriptions WHERE user_id=?",(r["inviter_id"],)).fetchone()
        if sub:
            try:
                old=parse_dt(sub["expiry"])
                base=old if old>now_utc() else now_utc()
                new=base+timedelta(days=int(r["bonus_days"]))
                c.execute("UPDATE subscriptions SET expiry=? WHERE user_id=?",(iso(new), r["inviter_id"]))
            except: pass
        c.execute("UPDATE referral_rewards SET status='APPROVED', approved_at=? WHERE id=?",(iso(now_utc()), rid))
        c.execute("COMMIT")
    await update.message.reply_text(f"✅ Approved {rid}")

async def reject_ref_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    rid=int(context.args[0])
    with db() as c: c.execute("UPDATE referral_rewards SET status='REJECTED' WHERE id=?",(rid,))
    await update.message.reply_text(f"❌ Rejected {rid}")

async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    with db() as c:
        users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        pays=c.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"]
    await update.message.reply_text(f"👑 v5.3\nUsers {users} Pays {pays}\nLIVE {LIVE_MODE} AUTO {AUTO_TRADE}")

async def broadcast_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    msg=" ".join(context.args)
    if not msg: return
    with db() as c: ids=[r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
    sent=0
    for uid in ids:
        try: await context.bot.send_message(uid, "📢 "+msg); sent+=1
        except: pass
    await update.message.reply_text(f"Sent {sent}")

async def checktx_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    tx=context.args[0]
    if not validate_tx_hash(tx): await update.message.reply_text("Invalid hash"); return
    await update.message.reply_text("Use /verify to check")

async def backup_now_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("📦 Backup…")
    await backup_db_job(context)
    await update.message.reply_text("✅ Sent")

def main():
    require_production_config()
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    handlers = {
        "start": start_cmd, "subscribe": subscribe_cmd, "verify": verify_cmd,
        "setkeys": setkeys_cmd, "positions": positions_cmd, "referral": referral_cmd,
        "setreferral": setreferral_cmd, "leaderboard": leaderboard_cmd,
        "status": status_cmd, "balance": balance_cmd, "trade": trade_cmd,
        "signals": signals_cmd, "stop": stop_cmd, "admin": admin_cmd,
        "broadcast": broadcast_cmd, "checktx": checktx_cmd, "pendingrefs": pending_refs_cmd,
        "approveref": approve_ref_cmd, "rejectref": reject_ref_cmd,
        "backupnow": backup_now_cmd, "pause": pause_cmd, "resume": resume_cmd, "resumeuid": resume_uid_cmd,
    }
    for name,fn in handlers.items(): app.add_handler(CommandHandler(name,fn))
    app.job_queue.run_repeating(monitor_job, interval=30, first=15)
    app.job_queue.run_repeating(reconcile_job, interval=600, first=120)
    app.job_queue.run_repeating(backup_db_job, interval=86400, first=120)
    if AUTO_TRADE: app.job_queue.run_repeating(auto_job, interval=AUTO_INTERVAL_MINUTES*60, first=60)
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
