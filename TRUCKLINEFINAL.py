# ============================================================
# TruckLink v5.0 — bot.py
# Telegram commands + main()
# ============================================================

import asyncio
import logging
import time
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
    send as core_send,
    auto_job, monitor_job, reconcile_job, backup_db_job,
    get_peak_equity,
)

# ─────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    referred = context.args[0].upper() if context.args else None
    create_user(uid, update.effective_user.username, referred)
    lic = license_info(uid)
    if lic.get("is_admin"):
        await update.message.reply_text(
            "👑 ADMIN\n"
            "/status /trade /signals /positions /balance\n"
            "/admin /pendingrefs /backupnow /broadcast /checktx /pause /resume")
        return
    if not lic["valid"]:
        await update.message.reply_text(
            "🛡️ TruckLink v5.0\n"
            "BASIC $20 · 2 pairs · 60d first\n"
            "PRO $50 · 3 pairs · 60d\n"
            "ELITE $70 · 7 pairs · 60d\n\n"
            "/subscribe BASIC|PRO|ELITE")
    elif not get_keys(uid):
        await update.message.reply_text(
            f"✅ {lic['tier']} till {lic['expiry'].date()}\n"
            "/setkeys API_KEY SECRET PASSPHRASE")
    else:
        await update.message.reply_text(
            f"🤖 {lic['tier']} till {lic['expiry'].date()}\n"
            "/trade /positions /balance /status /signals /referral")

async def subscribe_cmd(update, context):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username)
    if is_admin(uid):
        await update.message.reply_text("👑 Admin free"); return
    tier = (context.args[0].upper() if context.args else "")
    if tier not in TIERS:
        await update.message.reply_text("Usage: /subscribe BASIC|PRO|ELITE"); return
    inv = create_invoice(uid, tier)
    await update.message.reply_text(
        f"💳 {tier}\nBase ${inv['base']:.2f}\nEXACT ${inv['exact']:.2f} USDT TRC20\n"
        f"Invoice {inv['invoice_id']}\nExpires {inv['expires'].strftime('%H:%M UTC')}\n\n"
        f"Send EXACT ${inv['exact']:.2f} to:\n{USDT_WALLET}\n\n"
        f"Then /verify {inv['invoice_id']} TXHASH")

async def verify_cmd(update, context):
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text("👑 Admin no pay"); return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /verify INVOICE_ID TXHASH"); return
    invoice_id, tx = context.args
    inv = get_invoice(invoice_id, uid)
    if not inv:
        await update.message.reply_text("Invoice not found"); return
    if inv["status"] != "PENDING":
        await update.message.reply_text("Not pending"); return
    if now_utc() > parse_dt(inv["expires_at"]):
        await update.message.reply_text("Expired /subscribe again"); return
    await update.message.reply_text("🔍 Verifying...")
    ok, received, msg = await verify_trc20_usdt(tx, Decimal(inv["exact_amount"]), USDT_WALLET)
    if not ok:
        await update.message.reply_text("❌ " + msg); return
    ok2, result = settle_payment(uid, invoice_id, tx, received)
    if not ok2:
        await update.message.reply_text("❌ " + str(result)); return
    await update.message.reply_text(
        f"✅ {result['tier']} {result['days']} days till {result['expiry'].date()}\n"
        f"Paid ${received:.6f}\n/setkeys\nReferral bonus PENDING admin approval")

async def setkeys_cmd(update, context):
    uid = update.effective_user.id
    if not license_info(uid)["valid"]:
        await update.message.reply_text("❌ Subscribe first"); return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /setkeys API_KEY SECRET PASSPHRASE"); return
    ak, sec, pp = context.args
    ex = None
    try:
        import ccxt.async_support as ccxt_async
        ex = ccxt_async.okx({"apiKey": ak, "secret": sec, "password": pp,
                             "enableRateLimit": True, "options": {"defaultType": "spot"}})
        if not LIVE_MODE: ex.set_sandbox_mode(True)
        bal = await ex.fetch_balance()
        save_keys(uid, ak, sec, pp)
        try: await update.message.delete()
        except Exception: pass
        free = to_dec(bal.get("USDT", {}).get("free") or 0)
        await update.effective_chat.send_message(
            f"✅ Keys OK · USDT free ${free:.2f}\n🔐 Encrypted\n"
            f"⚠️ READ+TRADE only · NO WITHDRAW permission")
    except Exception as e:
        await update.message.reply_text(f"❌ Keys fail: {e}")
    finally:
        if ex:
            try: await ex.close()
            except Exception: pass

async def positions_cmd(update, context):
    rows = open_positions(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No positions"); return
    lines = []
    for p in rows:
        lines.append(
            f"{p['symbol']} entry {to_dec(p['entry_price']):.6f} "
            f"SL {to_dec(p['sl']):.6f} "
            f"{'TRAIL ' if p['trailing_active'] else ''}"
            f"{'PTP ' if p['partial_tp_done'] else ''}"
            f"exch_SL {'✅' if p.get('sl_order_id') else '❌'}")
    await update.message.reply_text("\n".join(lines))

async def balance_cmd(update, context):
    uid = update.effective_user.id
    keys = get_keys(uid)
    if not keys:
        await update.message.reply_text("❌ /setkeys"); return
    ex = make_exchange(keys)
    try:
        bal = await ex.fetch_balance()
        free = to_dec(bal.get("USDT", {}).get("free") or 0)
        total = to_dec(bal.get("USDT", {}).get("total") or 0)
        peak = get_peak_equity(uid) or total
        dd = ((peak - total) / peak * 100) if peak > 0 else Decimal("0")
        await update.message.reply_text(
            f"USDT free ${free:.2f}\nTotal ${total:.2f}\nPeak ${peak:.2f}\nDD {dd:.2f}%")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
    finally:
        await ex.close()

async def status_cmd(update, context):
    uid = update.effective_user.id
    lic = license_info(uid)
    with db() as c:
        row = c.execute("SELECT pnl FROM daily_pnl WHERE user_id=? AND day=?",
                        (uid, now_utc().date().isoformat())).fetchone()
    pnl = to_dec(row["pnl"]) if row else Decimal("0")
    pos = len(open_positions(uid))
    keys = get_keys(uid)
    trading = trading_enabled(uid)
    await update.message.reply_text(
        f"{'👑 ADMIN ' if lic.get('is_admin') else ''}{lic.get('tier','NONE')} "
        f"valid={lic['valid']} exp={lic.get('expiry')}\n"
        f"Keys {'✅' if keys else '❌'} · Pos {pos} · PnL {pnl:+.2f}\n"
        f"Trading {'🟢' if trading else '🔴'} · LIVE {LIVE_MODE} · AUTO {AUTO_TRADE}")

async def trade_cmd(update, context):
    uid = update.effective_user.id
    if not license_info(uid)["valid"]:
        await update.message.reply_text("❌ Subscribe"); return
    if not get_keys(uid):
        await update.message.reply_text("❌ /setkeys"); return
    if not LIVE_MODE:
        await update.message.reply_text("🧪 LIVE_MODE=false — enable in .env"); return
    await update.message.reply_text("🚀 Scanning…")
    await trade_for_user(uid, context.bot, broadcast=True)

async def signals_cmd(update, context):
    await update.message.reply_text("🔎 Scanning market for signals (no orders placed)…")
    uid = update.effective_user.id
    lic = license_info(uid)
    if not lic["valid"]:
        await update.message.reply_text("❌ Subscribe"); return
    keys = get_keys(uid)
    if not keys:
        await update.message.reply_text("❌ /setkeys"); return
    ex = make_exchange(keys)
    try:
        await ex.load_markets()
        any_sig = False
        for sym in SYMBOLS[lic["tier"]]:
            sig = await compute_signal(ex, sym)
            if sig.ok:
                any_sig = True
                await update.message.reply_text(
                    f"🚨 {sym} LONG conf {sig.confidence}/10\n"
                    f"Entry {sig.price} SL {sig.sl} TP {sig.tp}\n"
                    f"{sig.reason}")
        if not any_sig:
            await update.message.reply_text("No qualifying signals right now.")
    finally:
        await ex.close()

async def stop_cmd(update, context):
    uid = update.effective_user.id
    keys = get_keys(uid)
    if not keys:
        await update.message.reply_text("❌ /setkeys"); return
    rows = open_positions(uid)
    if not rows:
        await update.message.reply_text("No positions"); return
    await update.message.reply_text("🛑 Closing all positions…")
    ex = make_exchange(keys)
    try:
        async with user_lock(uid):
            for p in rows:
                try:
                    t = await ex.fetch_ticker(p["symbol"])
                    await close_position(context.bot, uid, ex, p, "🛑 MANUAL", to_dec(t["last"]))
                except Exception:
                    log.exception("manual close")
    finally:
        await ex.close()

async def pause_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    set_state("EMERGENCY_STOP", "true")
    await update.message.reply_text("⏸ Global trading paused")

async def resume_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    set_state("EMERGENCY_STOP", "false")
    await update.message.reply_text("▶️ Global trading resumed")

async def resume_uid_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    try: uid = int(context.args[0])
    except Exception: return
    set_state(f"TRADING_ENABLED_{uid}", "true")
    await update.message.reply_text(f"▶️ Re-enabled trading for {uid}")

async def referral_cmd(update, context):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        n = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (uid,)).fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM referral_rewards WHERE inviter_id=? AND status='PENDING'",
                            (uid,)).fetchone()["n"]
        approved = c.execute("SELECT COALESCE(SUM(bonus_days),0) d FROM referral_rewards "
                             "WHERE inviter_id=? AND status='APPROVED'", (uid,)).fetchone()["d"]
    me = (await context.bot.get_me()).username
    await update.message.reply_text(
        f"🔥 https://t.me/{me}?start={u['ref_code']}\n"
        f"Referrals {n} · Approved {approved}d · Pending {pending} (admin approval)")

async def leaderboard_cmd(update, context):
    with db() as c:
        rows = c.execute(
            "SELECT u.username, COUNT(r.referred_id) n FROM users u "
            "LEFT JOIN referrals r ON u.user_id=r.inviter_id "
            "GROUP BY u.user_id ORDER BY n DESC LIMIT 10").fetchall()
    await update.message.reply_text("\n".join(
        [f"{i}. @{r['username']} - {r['n']}" for i, r in enumerate(rows, 1)]))

async def pending_refs_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    with db() as c:
        rows = c.execute("SELECT id, inviter_id, referred_id, tier, bonus_days, bonus_usdt, "
                         "created_at FROM referral_rewards WHERE status='PENDING' "
                         "ORDER BY created_at DESC LIMIT 20").fetchall()
    if not rows:
        await update.message.reply_text("No pending ✅"); return
    msg = "⏳ PENDING\n\n"
    for r in rows:
        msg += f"ID {r['id']} Inv {r['inviter_id']} Ref {r['referred_id']} {r['tier']} +{r['bonus_days']}d ${r['bonus_usdt']}\n"
    msg += "\n/approveref ID /rejectref ID"
    await update.message.reply_text(msg)

async def approve_ref_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    try: rid = int(context.args[0])
    except Exception: return
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM referral_rewards WHERE id=? AND status='PENDING'",
                      (rid,)).fetchone()
        if not r:
            c.execute("ROLLBACK")
            await update.message.reply_text("Not found"); return
        inviter_id = r["inviter_id"]
        sub = c.execute("SELECT expiry FROM subscriptions WHERE user_id=?", (inviter_id,)).fetchone()
        if sub:
            try:
                old = parse_dt(sub["expiry"])
                base = old if old > now_utc() else now_utc()
                new = base + timedelta(days=int(r["bonus_days"]))
                c.execute("UPDATE subscriptions SET expiry=? WHERE user_id=?", (iso(new), inviter_id))
            except Exception:
                log.exception("extend fail")
        c.execute("UPDATE referral_rewards SET status='APPROVED', approved_at=? WHERE id=?",
                  (iso(now_utc()), rid))
        c.execute("UPDATE referrals SET rewarded=1 WHERE inviter_id=? AND referred_id=?",
                  (r["inviter_id"], r["referred_id"]))
        c.execute("COMMIT")
    await update.message.reply_text(f"✅ Approved ID {rid} +{r['bonus_days']}d")
    try:
        await context.bot.send_message(chat_id=int(r["inviter_id"]),
                                       text=f"🎉 Referral {r['tier']} approved +{r['bonus_days']}d!")
    except Exception: pass

async def reject_ref_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    try: rid = int(context.args[0])
    except Exception: return
    with db() as c:
        r = c.execute("SELECT * FROM referral_rewards WHERE id=? AND status='PENDING'",
                      (rid,)).fetchone()
        if not r:
            await update.message.reply_text("Not found"); return
        c.execute("UPDATE referral_rewards SET status='REJECTED', approved_at=? WHERE id=?",
                  (iso(now_utc()), rid))
    await update.message.reply_text(f"❌ Rejected ID {rid}")

async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only"); return
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        pays = c.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"]
        poss = c.execute("SELECT COUNT(*) n FROM positions WHERE status='OPEN'").fetchone()["n"]
        rev = c.execute("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) x FROM payments").fetchone()["x"]
        pend = c.execute("SELECT COUNT(*) n FROM referral_rewards WHERE status='PENDING'").fetchone()["n"]
    await update.message.reply_text(
        f"👑 v5.0\nUsers {users} · Pays {pays} · Rev ${rev:.2f}\n"
        f"Open {poss} · Pending refs {pend}\n"
        f"LIVE {LIVE_MODE} · AUTO {AUTO_TRADE} · "
        f"Emergency {'ON' if get_state('EMERGENCY_STOP')=='true' else 'off'}")

async def broadcast_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast msg"); return
    with db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, "📢 " + msg); sent += 1
            await asyncio.sleep(0.1)
        except Exception: pass
    await update.message.reply_text(f"Sent {sent}")

async def checktx_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: return
    tx = context.args[0]
    if not validate_tx_hash(tx):
        await update.message.reply_text("Invalid hash"); return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx}") as r:
                data = await r.json()
        with db() as c:
            used = c.execute("SELECT * FROM payments WHERE tx_hash=?", (tx,)).fetchone()
        await update.message.reply_text(
            f"Status {data.get('contractRet')} · Confirmed {data.get('confirmed')} · "
            f"Used {'YES' if used else 'NO'}")
    except Exception as e:
        await update.message.reply_text(f"Err {e}")

async def backup_now_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("📦 Creating backup…")
    await backup_db_job(context)
    await update.message.reply_text("✅ Backup sent")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    require_production_config()
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    handlers = {
        "start": start_cmd, "subscribe": subscribe_cmd, "verify": verify_cmd,
        "setkeys": setkeys_cmd, "positions": positions_cmd, "referral": referral_cmd,
        "leaderboard": leaderboard_cmd, "status": status_cmd, "balance": balance_cmd,
        "trade": trade_cmd, "signals": signals_cmd, "stop": stop_cmd,
        "admin": admin_cmd, "broadcast": broadcast_cmd, "checktx": checktx_cmd,
        "pendingrefs": pending_refs_cmd, "approveref": approve_ref_cmd,
        "rejectref": reject_ref_cmd, "backupnow": backup_now_cmd,
        "pause": pause_cmd, "resume": resume_cmd, "resumeuid": resume_uid_cmd,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))

    app.job_queue.run_repeating(monitor_job, interval=30, first=15)
    app.job_queue.run_repeating(reconcile_job, interval=600, first=120)
    app.job_queue.run_repeating(backup_db_job, interval=86400, first=120)

    if AUTO_TRADE:
        app.job_queue.run_repeating(auto_job,
                                    interval=AUTO_INTERVAL_MINUTES * 60,
                                    first=60)

    log.info(f"TruckLink v5.0 LIVE={LIVE_MODE} AUTO={AUTO_TRADE} "
             f"interval={AUTO_INTERVAL_MINUTES}m")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
