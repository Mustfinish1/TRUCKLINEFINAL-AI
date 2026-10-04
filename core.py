import asyncio
from decimal import Decimal
from collections import defaultdict
import pandas as pd
import numpy as np
from config import log, db, iso, now_utc, to_dec, get_state, set_state, trading_enabled, get_keys, make_exchange, SYMBOLS, RISK_PER_TRADE, MAX_POSITIONS, MAX_DAILY_LOSS, daily_pnl

_locks = defaultdict(asyncio.Lock)
def user_lock(uid): return _locks[uid]

class Signal:
    def __init__(self, ok=False, confidence=0, price=Decimal("0"), sl=Decimal("0"), tp=Decimal("0"), reason=""):
        self.ok=ok; self.confidence=confidence; self.price=price; self.sl=sl; self.tp=tp; self.reason=reason

async def calculate_atr_position_size(exchange, symbol: str, risk_per_trade_usdt: float) -> float:
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=20)
        if len(ohlcv)<15: return risk_per_trade_usdt
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df['prev_close']=df['close'].shift(1)
        df['tr1']=df['high']-df['low']
        df['tr2']=(df['high']-df['prev_close']).abs()
        df['tr3']=(df['low']-df['prev_close']).abs()
        df['tr']=df[['tr1','tr2','tr3']].max(axis=1)
        atr=df['tr'].rolling(14).mean().iloc[-1]
        price=df['close'].iloc[-1]
        if atr==0 or np.isnan(atr): return risk_per_trade_usdt
        atr_pct=atr/price
        size=risk_per_trade_usdt/(atr_pct*2.5)
        return max(5.0, min(size, 100.0))
    except: return risk_per_trade_usdt

async def compute_signal(ex, sym: str) -> Signal:
    try:
        ohlcv = await ex.fetch_ohlcv(sym, timeframe='1h', limit=100)
        if len(ohlcv)<50: return Signal(ok=False, reason="No data")
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df['ema_50']=df['close'].ewm(span=50).mean()
        df['ema_200']=df['close'].ewm(span=200).mean()
        df['rsi']=100-(100/(1+df['close'].diff().clip(lower=0).rolling(14).mean()/ (-df['close'].diff().clip(upper=0).rolling(14).mean())))
        df['tr1']=df['high']-df['low']
        df['tr2']=(df['high']-df['close'].shift(1)).abs()
        df['tr3']=(df['low']-df['close'].shift(1)).abs()
        df['tr']=df[['tr1','tr2','tr3']].max(axis=1)
        df['atr']=df['tr'].rolling(14).mean()
        last=df.iloc[-1]
        price=float(last['close']); ema50=float(last['ema_50']); ema200=float(last['ema_200'])
        rsi=float(last['rsi']) if not np.isnan(last['rsi']) else 50
        atr=float(last['atr']) if not np.isnan(last['atr']) else price*0.02
        if price<ema200: return Signal(ok=False, reason=f"Bear {price:.2f}<EMA200 {ema200:.2f}")
        if rsi>75 or rsi<30: return Signal(ok=False, reason=f"RSI {rsi:.1f}")
        conf=0; reasons=[]
        if price>ema50>ema200: conf+=3; reasons.append("Uptrend")
        if 50<rsi<68: conf+=2; reasons.append(f"RSI {rsi:.1f}")
        if last['close']>last['open']: conf+=1; reasons.append("Bull")
        if df['volume'].iloc[-1]>df['volume'].rolling(20).mean().iloc[-1]: conf+=2; reasons.append("Vol")
        if conf<7: return Signal(ok=False, reason=f"Low {conf}/10")
        sl=Decimal(str(price-atr*1.8)); tp=Decimal(str(price+atr*2.5))
        return Signal(ok=True, confidence=conf, price=Decimal(str(price)), sl=sl, tp=tp, reason=" | ".join(reasons))
    except Exception as e:
        return Signal(ok=False, reason=str(e))

def open_positions(uid):
    with db() as c: return c.execute("SELECT * FROM positions WHERE user_id=? AND status='OPEN'", (uid,)).fetchall()

async def close_position(bot, uid, ex, position, reason, current_price):
    try:
        sym=position["symbol"]
        try:
            if position["sl_order_id"]: await ex.cancel_order(position["sl_order_id"], sym)
        except: pass
        amount=to_dec(position["amount"])
        side="sell" if position["side"]=="long" else "buy"
        await ex.create_market_order(sym, side, float(amount))
        entry=to_dec(position["entry_price"])
        pnl=(current_price-entry)*amount if position["side"]=="long" else (entry-current_price)*amount
        with db() as c:
            c.execute("UPDATE positions SET status='CLOSED', close_price=?, close_reason=?, pnl=?, closed_at=? WHERE id=?", (str(current_price), reason, str(pnl), iso(now_utc()), position["id"]))
            day=now_utc().date().isoformat()
            cur=c.execute("SELECT pnl FROM daily_pnl WHERE user_id=? AND day=?", (uid, day)).fetchone()
            new_val=to_dec(cur["pnl"])+pnl if cur else pnl
            c.execute("INSERT INTO daily_pnl(user_id,day,pnl) VALUES(?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET pnl=excluded.pnl", (uid, day, str(new_val)))
        if bot:
            try: await bot.send_message(uid, f"✅ Closed {sym} {reason} PnL ${float(pnl):+.2f}")
            except: pass
        return True
    except Exception as e:
        log.exception(f"close {e}"); return False

async def trade_for_user(uid, bot, broadcast=False):
    from config import license_info, daily_pnl
    lic=license_info(uid)
    if not lic["valid"]:
        if broadcast and bot: await bot.send_message(uid, "❌ No license /trial"); return
    if not trading_enabled(uid):
        if broadcast and bot: await bot.send_message(uid, "⏸ Paused"); return
    if daily_pnl(uid) < Decimal(f"-{MAX_DAILY_LOSS}"):
        if broadcast and bot: await bot.send_message(uid, f"🛡️ Loss limit -{MAX_DAILY_LOSS}"); return
    if len(open_positions(uid))>=MAX_POSITIONS:
        if broadcast and bot: await bot.send_message(uid, f"⚠️ Max {MAX_POSITIONS}"); return
    keys=get_keys(uid)
    if not keys:
        if broadcast and bot: await bot.send_message(uid, "❌ /setkeys first"); return
    ex=make_exchange(keys)
    try:
        await ex.load_markets()
        tier=lic["tier"]; symbols=SYMBOLS.get(tier, SYMBOLS["BASIC"])
        for sym in symbols:
            try:
                sig=await compute_signal(ex, sym)
                if not sig.ok: continue
                size_usdt=await calculate_atr_position_size(ex, sym, float(RISK_PER_TRADE*20))
                amount=float(Decimal(str(size_usdt))/sig.price)
                bal=await ex.fetch_balance()
                free=float(to_dec(bal.get("USDT",{}).get("free") or 0))
                if free<size_usdt:
                    if broadcast and bot: await bot.send_message(uid, f"❌ Low ${free:.2f}<${size_usdt:.2f} {sym}"); continue
                await ex.create_market_order(sym, "buy", amount)
                sl_order=None
                try: sl_order=await ex.create_order(sym, 'stop', 'sell', amount, None, {'stopPrice': float(sig.sl)})
                except Exception as e: log.warning(f"SL fail {e}")
                with db() as c:
                    c.execute("INSERT INTO positions(user_id, symbol, side, entry_price, amount, sl, tp, sl_order_id, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (uid, sym, "long", str(float(sig.price)), str(amount), str(sig.sl), str(sig.tp), sl_order['id'] if sl_order else None, "OPEN", iso(now_utc())))
                if bot: await bot.send_message(uid, f"🚀 LONG {sym}\nEntry {float(sig.price):.4f} SL {float(sig.sl):.4f} TP {float(sig.tp):.4f}\nSize ${size_usdt:.2f} ({sig.confidence}/10) {sig.reason}\nSL {'✅' if sl_order else '❌'}")
                if broadcast: break
            except Exception as e:
                log.exception(f"trade {sym} {e}")
                if broadcast and bot: await bot.send_message(uid, f"⚠️ {sym} {e}")
    finally: await ex.close()

async def monitor_job(context):
    with db() as c: positions=c.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
    for p in positions:
        try:
            keys=get_keys(p["user_id"])
            if not keys: continue
            ex=make_exchange(keys)
            try:
                t=await ex.fetch_ticker(p["symbol"])
                last=to_dec(t["last"])
                if last<=to_dec(p["sl"]) or last>=to_dec(p["tp"]):
                    await close_position(context.bot, p["user_id"], ex, p, "TP/SL", last)
            finally: await ex.close()
        except: await asyncio.sleep(1)

async def auto_job(context):
    if get_state("EMERGENCY_STOP")=="true": return
    with db() as c: uids=[r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
    for uid in uids:
        if not trading_enabled(uid): continue
        try: await trade_for_user(uid, context.bot, False); await asyncio.sleep(2)
        except: pass

async def reconcile_job(context): pass
async def backup_db_job(context): pass
