#!/usr/bin/env python3
"""
BTC Data Pipeline & Strategy Backtester
— 从 Binance 拉取历史数据
— 计算技术指标
— 回测策略输出胜率/盈亏比/夏普/最大回撤
— Runs on GitHub Actions (where Binance API is accessible)
"""

import requests, json, os, hashlib, hmac, time, math
from datetime import datetime, timezone
import urllib.parse

# ============================================================
# CONFIG
# ============================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")

BASE = "https://api.binance.com"
HEADERS = {"X-MBX-APIKEY": API_KEY}

# Strategy parameters (same as live bot)
STRATEGY_RATIO = 0.80
MAX_POSITION_RATIO = 0.05
LEVERAGE = 10
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.03
SCORE_LONG = 6
SCORE_SHORT = -6

# ============================================================
# HELPERS
# ============================================================

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")

def binance_signed_get(path, params=None):
    """Signed request to Binance API"""
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{query}&signature={signature}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    return r.json() if r.ok else None

def binance_get(path, params=None):
    """Public (unsigned) request"""
    if params is None:
        params = {}
    r = requests.get(f"{BASE}{path}", params=params, timeout=30)
    return r.json() if r.ok else None


# ============================================================
# DATA FETCH
# ============================================================

def fetch_klines(symbol="BTCUSDT", interval="1h", limit=1000):
    """Fetch K-line data"""
    data = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        log(f"❌ 获取 {symbol} {interval} K线失败")
        return []
    
    klines = []
    for k in data:
        klines.append({
            "ts": k[0] // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_ts": k[6] // 1000,
        })
    log(f"📥 获取 {len(klines)} 根 {symbol} {interval} K线")
    return klines


def fetch_all_klines(symbol="BTCUSDT", interval="1h", total_days=90):
    """
    Fetch historical data beyond single limit (1000).
    Binance allows max 1000 per request, so we paginate backwards.
    """
    if interval == "1h":
        limit = 1000  # ~41 days
        ms_per_candle = 3600000
    elif interval == "4h":
        limit = 1000  # ~166 days
        ms_per_candle = 14400000
    elif interval == "15m":
        limit = 1000  # ~10 days
        ms_per_candle = 900000
    else:
        limit = 500
        ms_per_candle = 86400000  # 1d
    
    needed = int(total_days * 24 * 3600000 / ms_per_candle)
    all_klines = []
    end_time = int(time.time() * 1000)
    
    while len(all_klines) < needed:
        params = {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end_time}
        data = binance_get("/api/v3/klines", params)
        if not data or len(data) == 0:
            break
        for k in data:
            all_klines.append({
                "ts": k[0] // 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        end_time = data[0][0] - 1  # go before the first candle
        if len(data) < limit:
            break
        time.sleep(0.1)
    
    # Reverse chronological → chronological
    all_klines.reverse()
    log(f"📥 总计获取 {len(all_klines)} 根 {symbol} {interval} K线 ({total_days}天)")
    return all_klines


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def compute_ema(data, period):
    """Exponential Moving Average"""
    result = []
    multiplier = 2 / (period + 1)
    ema = data[0]
    for i, val in enumerate(data):
        if i == 0:
            ema = val
        else:
            ema = (val - ema) * multiplier + ema
        result.append(ema)
    return result

def compute_sma(data, period):
    """Simple Moving Average"""
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(data[i-period+1:i+1]) / period)
    return result

def compute_rsi(data, period=14):
    """Relative Strength Index"""
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period if len(gains) >= period else 0
    avg_loss = sum(losses[:period]) / period if len(losses) >= period else 0
    
    rsi = [None] * (period + 1)  # pad initial
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rs = 100
        else:
            rs = avg_gain / avg_loss
        rsi_val = 100 - (100 / (1 + rs))
        rsi.append(rsi_val)
    
    return rsi

def compute_macd(data, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram"""
    ema_fast = compute_ema(data, fast)
    ema_slow = compute_ema(data, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(data))]
    signal_line = compute_ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(len(data))]
    return macd_line, signal_line, histogram

def compute_bollinger(data, period=20, std_mult=2):
    """Bollinger Bands"""
    sma = compute_sma(data, period)
    upper = []
    lower = []
    for i in range(len(data)):
        if sma[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = data[i-period+1:i+1]
            variance = sum((x - sma[i])**2 for x in window) / period
            std = math.sqrt(variance)
            upper.append(sma[i] + std_mult * std)
            lower.append(sma[i] - std_mult * std)
    return upper, lower, sma

def compute_atr(high, low, close, period=14):
    """Average True Range"""
    tr = []
    for i in range(len(close)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i-1])
            lc = abs(low[i] - close[i-1])
            tr.append(max(hl, hc, lc))
    atr = []
    for i in range(len(tr)):
        if i < period:
            atr.append(None)
        else:
            atr.append(sum(tr[i-period+1:i+1]) / period)
    return atr


# ============================================================
# STRATEGY BACKTEST
# ============================================================

# Session definitions
SESSIONS = [
    (0, 2, 1.0), (2, 7, 0.8), (7, 9, 1.2),
    (9, 11, 1.5), (11, 13, 1.3), (13, 16, 2.0),
    (16, 20, 1.5), (20, 22, 1.2), (22, 24, 0.9)
]

def get_session_weight(hour):
    for start, end, weight in SESSIONS:
        if start <= hour < end:
            return weight
    return 1.0

def backtest(klines):
    """
    V2 Strategy Backtest:
    - Multi-factor scoring using technical indicators
    - Session-aware dynamic thresholds
    - 10x leverage, 5% per trade
    - 1:1.5 R/R
    """
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]
    timestamps = [k["ts"] for k in klines]
    
    # Compute indicators
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    rsi = compute_rsi(closes, 14)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    bb_upper, bb_lower, bb_mid = compute_bollinger(closes)
    atr = compute_atr(highs, lows, closes, 14)
    
    # Backtest
    position = None  # {"side":"long"/"short", "entry":price, "sl":price, "tp":price, "entry_idx":i}
    trades = []
    balance = 100000  # start with $100k virtual
    
    for i in range(100, len(klines)):  # skip initial warm-up
        if i >= len(closes): break
        
        price = closes[i]
        hour = datetime.utcfromtimestamp(timestamps[i]).hour
        session_weight = get_session_weight(hour)
        
        # Skip if indicators not ready
        if rsi[i] is None or bb_upper[i] is None or atr[i] is None:
            continue
        
        # === Calculate Score ===
        score = 0
        factors = {}
        
        # 1. EMA trend
        if ema9[i] > ema21[i]:
            score += 2; factors["ema"] = 2
        else:
            score -= 2; factors["ema"] = -2
        
        # 2. RSI
        if rsi[i] > 70:
            score -= 1  # overbought
            factors["rsi"] = -1
        elif rsi[i] < 30:
            score += 1  # oversold
            factors["rsi"] = 1
        elif rsi[i] > 55:
            score += 1; factors["rsi"] = 1
        elif rsi[i] < 45:
            score -= 1; factors["rsi"] = -1
        else:
            factors["rsi"] = 0
        
        # 3. MACD
        if macd_hist[i] > 0 and macd_hist[i] > macd_hist[i-1]:
            score += 1; factors["macd"] = 1
        elif macd_hist[i] < 0 and macd_hist[i] < macd_hist[i-1]:
            score -= 1; factors["macd"] = -1
        else:
            factors["macd"] = 0
        
        # 4. Bollinger position
        if price < bb_lower[i]:
            score += 1; factors["bb"] = 1
        elif price > bb_upper[i]:
            score -= 1; factors["bb"] = -1
        else:
            factors["bb"] = 0
        
        # 5. Volume confirmation
        avg_vol = sum(volumes[max(0,i-20):i]) / min(20, i)
        if volumes[i] > avg_vol * 1.5:
            if score > 0:
                score += 1; factors["vol"] = 1
            elif score < 0:
                score -= 1; factors["vol"] = -1
            else:
                factors["vol"] = 0
        else:
            factors["vol"] = 0
        
        # Apply session weight
        weighted_score = score * session_weight
        
        # Threshold adjustment
        threshold_mult = 1.3 if session_weight < 0.9 else (0.85 if session_weight > 1.8 else 1.0)
        dl = round(SCORE_LONG * threshold_mult)
        ds = round(SCORE_SHORT * threshold_mult)
        
        # === Position Management ===
        if position:
            side, entry, sl, tp, entry_idx = position["side"], position["entry"], position["sl"], position["tp"], position["entry_idx"]
            
            # Check stop loss / take profit
            if side == "long":
                if price <= sl or price >= tp:
                    pnl_pct = (price - entry) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "tp" if price >= tp else "sl"})
                    position = None
                elif weighted_score <= ds:
                    # Signal reversed → close
                    pnl_pct = (price - entry) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "reverse"})
                    position = None
            else:  # short
                if price >= sl or price <= tp:
                    pnl_pct = (entry - price) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "tp" if price <= tp else "sl"})
                    position = None
                elif weighted_score >= dl:
                    pnl_pct = (entry - price) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "reverse"})
                    position = None
        
        # === Open new position ===
        if position is None and abs(weighted_score) >= min(abs(dl), abs(ds)):
            if weighted_score >= dl:
                # Long
                sl_price = price * (1 - STOP_LOSS_PCT)
                tp_price = price * (1 + TAKE_PROFIT_PCT)
                position = {"side": "long", "entry": price, "sl": sl_price, "tp": tp_price, "entry_idx": i, "score": weighted_score}
            elif weighted_score <= ds:
                # Short
                sl_price = price * (1 + STOP_LOSS_PCT)
                tp_price = price * (1 - TAKE_PROFIT_PCT)
                position = {"side": "short", "entry": price, "sl": sl_price, "tp": tp_price, "entry_idx": i, "score": weighted_score}
    
    # Close any remaining position at last price
    if position:
        price = closes[-1]
        side = position["side"]
        entry = position["entry"]
        pnl_pct = (price - entry) / entry * LEVERAGE * 100 if side == "long" else (entry - price) / entry * LEVERAGE * 100
        trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": len(closes) - position["entry_idx"], "reason": "end_of_data"})
    
    return trades


def analyze_results(trades):
    """Calculate performance metrics"""
    if not trades:
        return {"error": "no trades"}
    
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    
    total = len(trades)
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    profit_factor = abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses)) if losses else float('inf')
    
    # Max drawdown
    equity = [100000]
    for t in trades:
        equity.append(equity[-1] * (1 + t["pnl_pct"] / 100))
    peak = equity[0]
    dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = max(dd, (peak - e) / peak * 100)
    max_dd = dd
    
    # Sharpe (simplified, assuming 2% risk-free)
    returns = [t["pnl_pct"] / 100 for t in trades]
    avg_return = sum(returns) / len(returns)
    std_return = (sum((r - avg_return)**2 for r in returns) / len(returns))**0.5 if len(returns) > 1 else 0
    sharpe = (avg_return * 365 * 24 / 2 - 0.02) / (std_return * (365 * 24 / 2)**0.5) if std_return > 0 else 0
    
    # Win/loss by reason
    tp_trades = [t for t in trades if t.get("reason") == "tp"]
    sl_trades = [t for t in trades if t.get("reason") == "sl"]
    rev_trades = [t for t in trades if t.get("reason") == "reverse"]
    
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "tp_exits": len(tp_trades),
        "sl_exits": len(sl_trades),
        "reverse_exits": len(rev_trades),
        "avg_holding_bars": round(sum(t.get("bars", 0) for t in trades) / total, 1) if total > 0 else 0,
    }


def main():
    print("=" * 70)
    print("📊 BTC Strategy Backtester — " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 70)
    
    if not API_KEY or not SECRET_KEY:
        print("⛔ Binance API keys not set")
        return
    
    # Verify API key
    acct = binance_signed_get("/api/v3/account")
    if not acct or "canTrade" not in acct:
        print("⛔ Binance API Key 验证失败")
        if acct:
            print(f"   错误: {acct}")
        return
    
    can_trade = acct.get("canTrade", False)
    can_withdraw = acct.get("canWithdraw", False)
    print(f"✅ Binance API Key 有效 | 可交易: {can_trade} | 可提现: {can_withdraw}")
    
    # Fetch data at multiple timeframes
    print("\n📥 正在获取历史数据...")
    
    klines_1h = fetch_all_klines("BTCUSDT", "1h", 90)   # ~90 days
    klines_4h = fetch_all_klines("BTCUSDT", "4h", 180)  # ~180 days
    
    current_price = binance_get("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
    if current_price:
        print(f"\n💹 BTC 当前价格: ${float(current_price['price']):,.2f}")
    
    # Run backtest on 1h data
    print("\n" + "=" * 70)
    print("📈 回测 V2 策略 (1h K线, 90天)")
    print("=" * 70)
    
    trades = backtest(klines_1h)
    results = analyze_results(trades)
    
    if "error" in results:
        print(f"❌ {results['error']}")
    else:
        print(f"\n{'指标':20s} {'数值':>12s}")
        print("-" * 35)
        print(f"{'总交易次数':20s} {results['total_trades']:>12}")
        print(f"{'胜率':20s} {results['win_rate_pct']:>11.1f}%")
        print(f"{'盈利次数':20s} {results['wins']:>12}")
        print(f"{'亏损次数':20s} {results['losses']:>12}")
        print(f"{'平均盈利':20s} {results['avg_win_pct']:>+11.2f}%")
        print(f"{'平均亏损':20s} {results['avg_loss_pct']:>+11.2f}%")
        print(f"{'总盈亏':20s} {results['total_pnl_pct']:>+11.2f}%")
        print(f"{'盈亏因子':20s} {results['profit_factor']:>12.2f}")
        print(f"{'最大回撤':20s} {results['max_drawdown_pct']:>11.2f}%")
        print(f"{'夏普比率':20s} {results['sharpe_ratio']:>12.2f}")
        print(f"{'止盈退出':20s} {results['tp_exits']:>12}")
        print(f"{'止损退出':20s} {results['sl_exits']:>12}")
        print(f"{'信号反转退出':20s} {results['reverse_exits']:>12}")
        print(f"{'平均持仓(小时)':20s} {results['avg_holding_bars']:>12.1f}")
        
        # Last 10 trades
        print(f"\n📋 最近10笔交易:")
        print(f"{'#':>3} {'方向':>6} {'入场':>8} {'出场':>8} {'盈亏%':>8} {'原因':>10}")
        print("-" * 50)
        for idx, t in enumerate(trades[-10:]):
            print(f"{idx+1:>3} {t['side']:>6} {t['entry']:>8.0f} {t['exit']:>8.0f} {t['pnl_pct']:>+7.2f}% {t.get('reason','?'):>10}")
    
    # Save results
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_btc_price": float(current_price['price']) if current_price else None,
        "data_range": {
            "1h_candles": len(klines_1h),
            "4h_candles": len(klines_4h),
        },
        "backtest_results": results if "error" not in results else None,
        "recent_trades": trades[-20:] if trades else [],
    }
    
    with open("/tmp/backtest_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n💾 结果已保存到 /tmp/backtest_results.json")

    # Save K-line data for future use
    log(f"\n💾 保存 K 线数据供离线分析...")
    with open("/tmp/btc_klines_1h.json", "w") as f:
        json.dump(klines_1h, f)
    with open("/tmp/btc_klines_4h.json", "w") as f:
        json.dump(klines_4h, f)
    log(f"   1h K线: {len(klines_1h)} 根")
    log(f"   4h K线: {len(klines_4h)} 根")


if __name__ == "__main__":
    main()
