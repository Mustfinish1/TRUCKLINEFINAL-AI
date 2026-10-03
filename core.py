# ============================================================
# TruckLink v5.0 — core.py
# Signal engine, positions, exchange orders, monitoring,
# sizing, scanner, reconciliation, trade orchestration, jobs
# ============================================================

import asyncio
import logging
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd
import ta
from telegram.ext import ContextTypes

from config import (
    log, db, iso, now_utc, parse_dt, to_dec,
    set_state, get_state, trading_enabled, audit,
    DB_PATH, LIVE_MODE, AUTO_TRADE, AUTO_INTERVAL_MINUTES,
    MAX_USDT_PER_TRADE, MAX_PCT_PER_TRADE, RISK_PER_TRADE_PCT,
    MAX_DAILY_LOSS_USDT, MAX_DRAWDOWN_PCT, MAX_LOSS_PCT, EMERGENCY_LOSS_PCT,
    TAKE_PROFIT_PCT, PARTIAL_TP_PCT, TRAILING_TRIGGER_PCT, TRAILING_DISTANCE_PCT,
    BREAKEVEN_SL_PCT, TIME_STOP_HOURS, TIME_STOP_MIN_PROFIT_PCT,
    MAX_OPEN_POSITIONS, MAX_POSITIONS_PER_BUCKET, MIN_SIGNAL_CONFIDENCE,
    SYMBOLS, CMAP, BUCKETS, ADMIN_IDS,
    make_exchange, get_keys, get_fundamentals, get_sentiment,
    license_info, add_daily_pnl, daily_pnl,
)

# ─────────────────────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────────────────────
@dataclass
class Signal:
    ok: bool
    confidence: int = 0
    regime: str = "unknown"
    reason: str = ""
    price: Decimal = Decimal("0")
    sl: Decimal = Decimal("0")
    tp: Decimal = Decimal("0")
    atr_pct: Decimal = Decimal("0")
    rr: Decimal = Decimal("0")
    last_close: Decimal = Decimal("0")
    factors: dict = field(default_factory=dict)

async def fetch_ohlcv_df(ex, sym, tf="15m", limit=250):
    ohlcv = await ex.fetch_ohlcv(sym, tf, None, limit)
    return pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])

async def btc_regime_ok(ex):
    try:
        df = await fetch_ohlcv_df(ex, "BTC/USDT", "1h", 250)
        ema200 = df["close"].ewm(span=200, adjust=False).mean()
        return float(df["close"].iloc[-1]) > float(ema200.iloc[-1])
    except Exception:
        return False

async def compute_signal(ex, sym):
    try:
        df = await fetch_ohlcv_df(ex, sym, "15m", 250)
        if len(df) < 210:
            return Signal(ok=False, reason="insufficient history")

        w = df.copy()
        w["ema50"]    = ta.trend.ema_indicator(w["close"], 50)
        w["ema200"]   = ta.trend.ema_indicator(w["close"], 200)
        w["rsi"]      = ta.momentum.rsi(w["close"], 14)
        macd          = ta.trend.MACD(w["close"])
        w["macd"]     = macd.macd()
        w["macd_sig"] = macd.macd_signal()
        w["atr"]      = ta.volatility.average_true_range(w["high"], w["low"], w["close"], 14)
        w["vol_ma"]   = w["vol"].rolling(20).mean()
        adx           = ta.trend.ADXIndicator(w["high"], w["low"], w["close"], 14)
        w["adx"]      = adx.adx()

        last = w.iloc[-2]  # last CLOSED candle
        price = to_dec(last["close"])
        if price <= 0:
            return Signal(ok=False, reason="bad price")

        atr = to_dec(last["atr"])
        atr_pct = (atr / price) * 100 if price > 0 else Decimal("0")

        # Factor 1 — trend (0-3)
        f_trend = 0
        if pd.notna(last["ema50"]) and pd.notna(last["ema200"]):
            if last["ema50"] > last["ema200"]:
                f_trend += 2
            if last["close"] > last["ema50"]:
                f_trend += 1

        # Factor 2 — momentum (0-3)
        f_momentum = 0
        if pd.notna(last["rsi"]):
            if 50 < last["rsi"] < 70:
                f_momentum += 2
            elif 45 < last["rsi"] <= 50:
                f_momentum += 1
        if pd.notna(last["macd"]) and pd.notna(last["macd_sig"]):
            if last["macd"] > last["macd_sig"]:
                f_momentum += 1

        # Factor 3 — volume (0-2)
        f_volume = 0
        if pd.notna(last["vol_ma"]):
            if last["vol"] > last["vol_ma"] * 1.3:
                f_volume += 2
            elif last["vol"] > last["vol_ma"] * 1.05:
                f_volume += 1

        # Factor 4 — sentiment (0-2)
        _fng, fng_ok = await get_sentiment()
        f_sent = 2 if fng_ok else 0

        confidence = f_trend + f_momentum + f_volume + f_sent

        # Regime classification
        if pd.notna(last["adx"]):
            if last["adx"] > 25 and f_trend >= 2:
                regime = "trending"
            elif last["adx"] < 20:
                regime = "ranging"
            else:
                regime = "transitional"
        else:
            regime = "unknown"

        # Volatility gate
        if atr_pct < Decimal("0.4") or atr_pct > Decimal("5.0"):
            return Signal(
                ok=False,
                reason=f"atr_pct out of range ({atr_pct:.2f}%)",
                atr_pct=atr_pct, regime=regime,
                factors={"trend": f_trend, "momentum": f_momentum,
                         "volume": f_volume, "sentiment": f_sent},
            )

        # BTC regime gate
        btc_ok = await btc_regime_ok(ex)
        reason = (f"trend={f_trend} mom={f_momentum} vol={f_volume} "
                  f"fng={f_sent} btc_ok={btc_ok}")

        if confidence < MIN_SIGNAL_CONFIDENCE:
            return Signal(
                ok=False, confidence=confidence, regime=regime,
                reason=f"low confidence {confidence}",
                atr_pct=atr_pct, price=price, last_close=price,
                factors={"trend": f_trend, "momentum": f_momentum,
                         "volume": f_volume, "sentiment": f_sent},
            )

        if not btc_ok:
            return Signal(
                ok=False, confidence=confidence, regime=regime,
                reason="BTC regime bearish",
                atr_pct=atr_pct, price=price,
                factors={"trend": f_trend, "momentum": f_momentum,
                         "volume": f_volume, "sentiment": f_sent},
            )

        # Trade levels
        sl_dist = max(atr * Decimal("1.5"), price * MAX_LOSS_PCT / 100)
        tp_dist = max(atr * Decimal("2.5"), price * TAKE_PROFIT_PCT / 100)
        sl = price - sl_dist
        tp = price + tp_dist
        rr = (tp - price) / (price - sl) if price > sl else Decimal("0")

        return Signal(
            ok=True, confidence=confidence, regime=regime, reason=reason,
            price=price, sl=sl, tp=tp, atr_pct=atr_pct, rr=rr,
            last_close=price,
            factors={"trend": f_trend, "momentum": f_momentum,
                     "volume": f_volume, "sentiment": f_sent},
        )
    except Exception as e:
        log.exception("compute_signal err")
        return Signal(ok=False, reason=f"signal error: {e}")

# ─────────────────────────────────────────────────────────────
# ORDER / PRECISION HELPERS
# ─────────────────────────────────────────────────────────────
def round_amount_down(ex, sym, amount: Decimal) -> Decimal:
    try:
        m = ex.market(sym)
        lot = to_dec((m.get("limits") or {}).get("amount", {}).get("min") or 0)
        prec = (m.get("precision") or {}).get("amount")
        if prec is not None:
            q = Decimal(1).scaleb(-int(prec)) if isinstance(prec, int) else to_dec(prec)
            if q > 0:
                amount = (amount / q).to_integral_value(rounding=ROUND_DOWN) * q
        if lot > 0 and amount < lot:
            return Decimal("0")
        return amount
    except Exception:
        return amount

def round_price(ex, sym, price: Decimal) -> Decimal:
    try:
        return Decimal(str(ex.price_to_precision(sym, float(price))))
    except Exception:
        return price

def gen_clordid(uid, intent):
    return f"tl{uid}x{intent}x{secrets.token_hex(6)}"[:32]

# ─────────────────────────────────────────────────────────────
# POSITION HELPERS
# ─────────────────────────────────────────────────────────────
def open_positions(uid):
    with db() as c:
        return [dict(x) for x in c.execute(
            "SELECT * FROM positions WHERE user_id=? AND status='OPEN'",
            (uid,)).fetchall()]

def position_for(uid, sym):
    with db() as c:
        r = c.execute(
            "SELECT * FROM positions WHERE user_id=? AND symbol=? AND status='OPEN'",
            (uid, sym)).fetchone()
    return dict(r) if r else None

def bucket_counts(uid):
    counts = {}
    for p in open_positions(uid):
        b = BUCKETS.get(p["symbol"], p["symbol"])
        counts[b] = counts.get(b, 0) + 1
    return counts

def get_peak_equity(uid):
    with db() as c:
        r = c.execute(
            "SELECT peak FROM equity_history WHERE user_id=? ORDER BY day DESC LIMIT 1",
            (uid,)).fetchone()
    return to_dec(r["peak"]) if r else None

def update_equity(uid, equity: Decimal):
    day = now_utc().date().isoformat()
    peak = get_peak_equity(uid)
    new_peak = max(peak, equity) if peak is not None else equity
    with db() as c:
        c.execute(
            "INSERT INTO equity_history(user_id,day,equity,peak) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,day) DO UPDATE SET equity=excluded.equity, peak=excluded.peak",
            (uid, day, str(equity), str(new_peak)))
    return new_peak

# ─────────────────────────────────────────────────────────────
# EXCHANGE-SIDE PROTECTIVE ORDERS
# ─────────────────────────────────────────────────────────────
async def place_stop_loss(ex, sym, amount, sl_price):
    """Place a stop-market sell on OKX. Returns order id or None."""
    try:
        params = {
            "stopLossPrice": float(round_price(ex, sym, sl_price)),
            "reduceOnly": True,
        }
        order = await ex.create_order(sym, "market", "sell", float(amount), None, params)
        return order.get("id")
    except Exception as e:
        log.warning(f"exchange SL placement failed for {sym}: {e}")
        return None

async def place_take_profit(ex, sym, amount, tp_price):
    try:
        params = {
            "takeProfitPrice": float(round_price(ex, sym, tp_price)),
            "reduceOnly": True,
        }
        order = await ex.create_order(sym, "market", "sell", float(amount), None, params)
        return order.get("id")
    except Exception as e:
        log.warning(f"exchange TP placement failed for {sym}: {e}")
        return None

async def cancel_protective_orders(ex, sym, sl_id, tp_id):
    for oid in (sl_id, tp_id):
        if not oid:
            continue
        try:
            await ex.cancel_order(oid, sym)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# POSITION LIFECYCLE
# ─────────────────────────────────────────────────────────────
def db_insert_position(uid, sym, order, sig: Signal):
    filled = to_dec(order.get("filled") or 0)
    avg = to_dec(order.get("average") or order.get("price") or 0)
    cost = to_dec(order.get("cost") or (filled * avg))
    if filled <= 0 or avg <= 0:
        raise RuntimeError("No fill")
    sl = sig.sl if sig.sl > 0 else avg * (Decimal("1") - MAX_LOSS_PCT / 100)
    tp = sig.tp if sig.tp > 0 else avg * (Decimal("1") + TAKE_PROFIT_PCT / 100)
    emerg = avg * (Decimal("1") - EMERGENCY_LOSS_PCT / 100)
    with db() as c:
        c.execute(
            "INSERT INTO positions(user_id,symbol,entry_price,amount,cost,order_id,"
            "sl,emergency_sl,tp,high_price,trailing_active,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, sym, str(avg), str(filled), str(cost), str(order["id"]),
             str(sl), str(emerg), str(tp), str(avg), 0, "OPEN", iso(now_utc())))
        c.execute(
            "INSERT INTO trades(user_id,symbol,side,order_id,amount,price,fee,pnl,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, sym, "BUY", str(order["id"]), str(filled), str(avg),
             "0", "0", iso(now_utc())))

def attach_protective_orders(pos_id, sl_id, tp_id):
    with db() as c:
        c.execute("UPDATE positions SET sl_order_id=?, tp_order_id=? WHERE id=?",
                  (sl_id, tp_id, pos_id))

async def fetch_fill_fee(ex, sym, order_id):
    try:
        trades = await ex.fetch_my_trades(sym, None, 10)
        for t in trades:
            if str(t.get("order")) == str(order_id):
                fee = t.get("fee") or {}
                return to_dec(fee.get("cost", 0))
    except Exception:
        pass
    return Decimal("0")

async def send(bot, uid, txt):
    try:
        await bot.send_message(chat_id=uid, text=txt)
    except Exception as e:
        log.warning(f"send {uid} {e}")

async def close_position(bot, uid, ex, p, reason, cur_price: Decimal):
    sym = p["symbol"]
    try:
        amount = round_amount_down(ex, sym, to_dec(p["amount"]))
        if amount <= 0:
            raise RuntimeError("amount rounds to zero")
        clordid = gen_clordid(uid, f"close{p['id']}")
        order = await ex.create_order(
            sym, "market", "sell", float(amount), None,
            {"clOrdId": clordid, "reduceOnly": True})
        filled = to_dec(order.get("filled") or amount)
        avg = to_dec(order.get("average") or cur_price)
        fee = await fetch_fill_fee(ex, sym, order.get("id"))
        entry = to_dec(p["entry_price"])
        pnl = (avg - entry) * filled - fee
        with db() as c:
            c.execute(
                "UPDATE positions SET status='CLOSED',closed_at=?,close_order_id=? "
                "WHERE id=? AND status='OPEN'",
                (iso(now_utc()), str(order.get("id")), p["id"]))
            c.execute(
                "INSERT INTO trades(user_id,symbol,side,order_id,amount,price,fee,pnl,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, sym, "SELL", str(order.get("id")), str(filled), str(avg),
                 str(fee), str(pnl), iso(now_utc())))
        add_daily_pnl(uid, pnl)
        await cancel_protective_orders(ex, sym, p.get("sl_order_id"), p.get("tp_order_id"))
        await send(bot, uid, f"{reason} {sym} PnL {pnl:+.2f} USDT")
    except Exception as e:
        log.exception("close_position failed")
        await send(bot, uid, f"⚠️ Could not close {sym}: {e}")

async def partial_take_profit(bot, uid, ex, p, cur_price):
    sym = p["symbol"]
    try:
        total = to_dec(p["amount"])
        half = round_amount_down(ex, sym, total / 2)
        if half <= 0:
            return
        order = await ex.create_order(
            sym, "market", "sell", float(half), None,
            {"clOrdId": gen_clordid(uid, f"ptp{p['id']}"), "reduceOnly": True})
        filled = to_dec(order.get("filled") or half)
        avg = to_dec(order.get("average") or cur_price)
        fee = await fetch_fill_fee(ex, sym, order.get("id"))
        entry = to_dec(p["entry_price"])
        pnl = (avg - entry) * filled - fee
        remaining = total - filled
        new_sl = entry * (Decimal("1") + BREAKEVEN_SL_PCT / 100)
        with db() as c:
            c.execute(
                "UPDATE positions SET amount=?, sl=?, partial_tp_done=1, high_price=? "
                "WHERE id=? AND status='OPEN'",
                (str(remaining), str(new_sl),
                 str(max(to_dec(p["high_price"]), cur_price)), p["id"]))
            c.execute(
                "INSERT INTO trades(user_id,symbol,side,order_id,amount,price,fee,pnl,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, sym, "SELL-PARTIAL", str(order.get("id")), str(filled), str(avg),
                 str(fee), str(pnl), iso(now_utc())))
        add_daily_pnl(uid, pnl)
        await send(bot, uid, f"💰 Partial TP {sym} sold {filled} @ {avg} PnL {pnl:+.2f}")
    except Exception as e:
        log.exception("partial_tp failed")

# ─────────────────────────────────────────────────────────────
# MONITORING
# ─────────────────────────────────────────────────────────────
async def monitor_positions(uid, ex, bot):
    for p in open_positions(uid):
        try:
            ticker = await ex.fetch_ticker(p["symbol"])
            cur = to_dec(ticker["last"])
            entry = to_dec(p["entry_price"])
            high = max(to_dec(p["high_price"]), cur)
            pct = (cur - entry) / entry * 100
            sl = to_dec(p["sl"])
            trailing = bool(p["trailing_active"])

            # Partial TP
            if not p["partial_tp_done"] and pct >= PARTIAL_TP_PCT:
                await partial_take_profit(bot, uid, ex, p, cur)
                continue

            # Trailing stop
            if pct >= TRAILING_TRIGGER_PCT:
                trailing = True
                trail_sl = high * (Decimal("1") - TRAILING_DISTANCE_PCT / 100)
                be = entry * (Decimal("1") + BREAKEVEN_SL_PCT / 100)
                sl = max(sl, be, trail_sl)

            with db() as c:
                c.execute(
                    "UPDATE positions SET high_price=?,sl=?,trailing_active=? "
                    "WHERE id=? AND status='OPEN'",
                    (str(high), str(sl), 1 if trailing else 0, p["id"]))

            # Time stop
            try:
                opened = parse_dt(p["opened_at"])
                hours = (now_utc() - opened).total_seconds() / 3600
                if hours >= TIME_STOP_HOURS and pct < TIME_STOP_MIN_PROFIT_PCT:
                    await close_position(bot, uid, ex, p, "⏰ TIME STOP", cur)
                    continue
            except Exception:
                pass

            emerg = to_dec(p["emergency_sl"])
            tp = to_dec(p["tp"])

            if cur <= emerg:
                await close_position(bot, uid, ex, p, "🚨 EMERGENCY SL", cur)
            elif cur <= sl:
                await close_position(bot, uid, ex, p, "🛑 STOP/TRAIL", cur)
            elif cur >= tp:
                await close_position(bot, uid, ex, p, "💰 TP", cur)
        except Exception:
            log.exception("monitor err")

# ─────────────────────────────────────────────────────────────
# POSITION SIZING (ATR-based risk-parity)
# ─────────────────────────────────────────────────────────────
def size_position(equity_usdt: Decimal, atr_pct: Decimal, price: Decimal) -> Decimal:
    stop_dist_pct = max(atr_pct * Decimal("1.5"), MAX_LOSS_PCT)
    risk_usdt = equity_usdt * RISK_PER_TRADE_PCT / 100
    size = risk_usdt / (stop_dist_pct / 100)
    size = min(size, MAX_USDT_PER_TRADE, equity_usdt * MAX_PCT_PER_TRADE / 100)
    return max(size, Decimal("0"))

# ─────────────────────────────────────────────────────────────
# SIGNAL BROADCAST
# ─────────────────────────────────────────────────────────────
async def broadcast_signal(bot, sym, sig: Signal):
    with db() as c:
        c.execute(
            "INSERT INTO signals_log(symbol,confidence,direction,price,sl,tp,regime,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sym, sig.confidence, "LONG", str(sig.price), str(sig.sl),
             str(sig.tp), sig.regime, sig.reason, iso(now_utc())))
    msg = (
        f"🚨 SIGNAL {sym} LONG\n"
        f"Confidence: {sig.confidence}/10\n"
        f"Regime: {sig.regime}\n"
        f"Entry: {sig.price}\n"
        f"SL: {sig.sl}\n"
        f"TP: {sig.tp}\n"
        f"ATR: {sig.atr_pct:.2f}% | R:R {sig.rr:.2f}\n"
        f"Factors: {sig.factors}"
    )
    with db() as c:
        ids = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM subscriptions WHERE expiry > ?",
            (iso(now_utc()),)).fetchall()]
    ids = set(ids) | ADMIN_IDS
    for uid in ids:
        try:
            await send(bot, uid, msg)
            await asyncio.sleep(0.05)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# ENTRY SCANNER
# ─────────────────────────────────────────────────────────────
async def scan_entries(uid, tier, ex, bot, broadcast=False):
    if not trading_enabled(uid):
        return

    if daily_pnl(uid) <= -MAX_DAILY_LOSS_USDT:
        await send(bot, u
