#!/usr/bin/env python3
"""
BTC Data Pipeline v2 — 多交易所数据源
- Bybit (主力, 无区域限制)
- OKX (备用)
- Binance (公开API兜底)
- 无需 API Key
"""

import requests, json, os, time, math
from datetime import datetime, timezone

BASE_URLS = {
    "bybit": "https://api.bybit.com",
    "okx": "https://www.okx.com",
    "binance": "https://api.binance.com",
}

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")

def fetch_bybit_klines(symbol="BTCUSDT", interval="60", limit=200):
    """Bybit klines. interval: 60=1h, 15=15m, 240=4h, D=1d"""
    url = f"{BASE_URLS['bybit']}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.ok:
            data = r.json()
            if data.get("retCode") == 0:
                return data["result"]["list"]
        log(f"Bybit error: {r.text[:200]}")
    except Exception as e:
        log(f"Bybit failed: {e}")
    return None

def fetch_okx_klines(symbol="BTC-USDT", bar="1H", limit=300):
    """OKX klines. bar: 1H, 15m, 4H, 1D"""
    url = f"{BASE_URLS['okx']}/api/v5/market/candles"
    params = {"instId": symbol, "bar": bar, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.ok:
            data = r.json()
            if data.get("code") == "0":
                return data["data"]
        log(f"OKX error: {r.text[:200]}")
    except Exception as e:
        log(f"OKX failed: {e}")
    return None

def fetch_bybit_all(symbol="BTCUSDT", interval="60", total_bars=2000):
    """Fetch historical data with pagination from Bybit"""
    all_bars = []
    cursor = None
    max_pages = 20
    page = 0
    
    while len(all_bars) < total_bars and page < max_pages:
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        
        try:
            r = requests.get(f"{BASE_URLS['bybit']}/v5/market/kline", params=params, timeout=15)
            if r.ok:
                data = r.json()
                if data.get("retCode") == 0:
                    bars = data["result"]["list"]
                    if not bars:
                        break
                    all_bars.extend(bars)
                    cursor = data["result"].get("nextPageCursor", "")
                    if not cursor:
                        break
                    page += 1
                    time.sleep(0.2)
                else:
                    break
            else:
                break
        except Exception as e:
            log(f"Bybit pagination error: {e}")
            break
    
    # Parse Bybit bars: [timestamp, open, high, low, close, volume, turnover]
    klines = []
    bybit_fields = ["ts", "open", "high", "low", "close", "volume", "turnover"]
    for bar in reversed(all_bars):  # Bybit returns newest first
        entry = {}
        for i, field in enumerate(bybit_fields):
            if i < len(bar):
                entry[field] = float(bar[i]) if field != "ts" else int(bar[i]) // 1000
        entry["ts"] = int(bar[0]) // 1000
        entry["source"] = "bybit"
        klines.append(entry)
    
    return klines


def okx_to_standard(okx_bars):
    """Convert OKX bars to standard format"""
    # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    klines = []
    for bar in reversed(okx_bars):
        klines.append({
            "ts": int(bar[0]) // 1000,
            "open": float(bar[1]),
            "high": float(bar[2]),
            "low": float(bar[3]),
            "close": float(bar[4]),
            "volume": float(bar[5]),
            "source": "okx",
        })
    return klines


def fetch_current_price():
    """Multi-source BTC price — Binance first now that IP restriction is removed"""
    sources = [
        ("Binance", f"{BASE_URLS['binance']}/api/v3/ticker/price?symbol=BTCUSDT",
         lambda d: float(d["price"])),
        ("Bybit", f"{BASE_URLS['bybit']}/v5/market/tickers?category=linear&symbol=BTCUSDT",
         lambda d: float(d["result"]["list"][0]["lastPrice"])),
        ("OKX", f"{BASE_URLS['okx']}/api/v5/market/ticker?instId=BTC-USDT",
         lambda d: float(d["data"][0]["last"])),
    ]
    for name, url, parser in sources:
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                return parser(r.json()), name
        except:
            continue
    return None, None


# ============================================================
# TECHNICAL INDICATORS (same as before)
# ============================================================

def compute_ema(data, period):
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
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(data[i-period+1:i+1]) / period)
    return result

def compute_rsi(data, period=14):
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period if len(gains) >= period else 0
    avg_loss = sum(losses[:period]) / period if len(losses) >= period else 0
    rsi = [None] * (period + 1)
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
    ema_fast = compute_ema(data, fast)
    ema_slow = compute_ema(data, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(data))]
    signal_line = compute_ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(len(data))]
    return macd_line, signal_line, histogram

def compute_bollinger(data, period=20, std_mult=2):
    sma = compute_sma(data, period)
    upper, lower = [], []
    for i in range(len(data)):
        if sma[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = data[max(0,i-period+1):i+1]
            variance = sum((x - sma[i])**2 for x in window) / len(window)
            std = math.sqrt(variance)
            upper.append(sma[i] + std_mult * std)
            lower.append(sma[i] - std_mult * std)
    return upper, lower, sma


# ============================================================
# SESSION + SCORING — 夏令时感知
# ============================================================

def get_sessions():
    now = datetime.now(timezone.utc)
    is_dst = 3 <= now.month <= 10
    if is_dst:
        return [(0,2,1.0),(2,7,0.8),(7,9,1.2),(9,11,1.5),(11,13,1.3),(13,16,2.0),(16,20,1.5),(20,22,1.2),(22,24,0.9)]
    else:
        return [(0,2,0.9),(2,7,0.8),(7,8,1.0),(8,11,1.5),(11,14,1.3),(14,17,2.0),(17,21,1.5),(21,23,1.2),(23,24,0.9)]

def get_session_weight(hour):
    for start, end, weight in get_sessions():
        if start <= hour < end:
            return weight
    return 1.0

SCORE_LONG = 5
SCORE_SHORT = -5
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.03
LEVERAGE = 10

def backtest(klines):
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]
    timestamps = [k["ts"] for k in klines]
    
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    rsi = compute_rsi(closes, 14)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    bb_upper, bb_lower, bb_mid = compute_bollinger(closes)
    
    position = None
    trades = []
    
    for i in range(100, len(closes)):
        price = closes[i]
        hour = datetime.utcfromtimestamp(timestamps[i]).hour
        session_weight = get_session_weight(hour)
        
        if rsi[i] is None or bb_upper[i] is None:
            continue
        
        score = 0
        factors = {}
        
        # EMA trend
        if ema9[i] > ema21[i]:
            score += 2; factors["ema"] = 2
        else:
            score -= 2; factors["ema"] = -2
        
        # RSI
        if rsi[i] > 70: score -= 1; factors["rsi"] = -1
        elif rsi[i] < 30: score += 1; factors["rsi"] = 1
        elif rsi[i] > 55: score += 1; factors["rsi"] = 1
        elif rsi[i] < 45: score -= 1; factors["rsi"] = -1
        else: factors["rsi"] = 0
        
        # MACD
        if macd_hist[i] > 0 and i > 0 and macd_hist[i] > macd_hist[i-1]:
            score += 1; factors["macd"] = 1
        elif macd_hist[i] < 0 and i > 0 and macd_hist[i] < macd_hist[i-1]:
            score -= 1; factors["macd"] = -1
        else: factors["macd"] = 0
        
        # Bollinger
        if price < bb_lower[i]: score += 1; factors["bb"] = 1
        elif price > bb_upper[i]: score -= 1; factors["bb"] = -1
        else: factors["bb"] = 0
        
        # Volume
        avg_vol = sum(volumes[max(0,i-20):i]) / min(20, i)
        if volumes[i] > avg_vol * 1.5:
            if score > 0: score += 1; factors["vol"] = 1
            elif score < 0: score -= 1; factors["vol"] = -1
            else: factors["vol"] = 0
        else: factors["vol"] = 0
        
        weighted_score = score * session_weight
        threshold_mult = 1.3 if session_weight < 0.9 else (0.85 if session_weight > 1.8 else 1.0)
        dl = round(SCORE_LONG * threshold_mult)
        ds = round(SCORE_SHORT * threshold_mult)
        
        if position:
            side, entry, sl, tp, entry_idx = position["side"], position["entry"], position["sl"], position["tp"], position["entry_idx"]
            if side == "long":
                if price <= sl or price >= tp:
                    pnl_pct = (price - entry) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "tp" if price >= tp else "sl"})
                    position = None
                elif weighted_score <= ds:
                    pnl_pct = (price - entry) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "reverse"})
                    position = None
            else:
                if price >= sl or price <= tp:
                    pnl_pct = (entry - price) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "tp" if price <= tp else "sl"})
                    position = None
                elif weighted_score >= dl:
                    pnl_pct = (entry - price) / entry * LEVERAGE * 100
                    trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": i - entry_idx, "reason": "reverse"})
                    position = None
        
        if position is None and abs(weighted_score) >= min(abs(dl), abs(ds)):
            if weighted_score >= dl:
                sl_price = price * (1 - STOP_LOSS_PCT)
                tp_price = price * (1 + TAKE_PROFIT_PCT)
                position = {"side": "long", "entry": price, "sl": sl_price, "tp": tp_price, "entry_idx": i, "score": weighted_score}
            elif weighted_score <= ds:
                sl_price = price * (1 + STOP_LOSS_PCT)
                tp_price = price * (1 - TAKE_PROFIT_PCT)
                position = {"side": "short", "entry": price, "sl": sl_price, "tp": tp_price, "entry_idx": i, "score": weighted_score}
    
    if position:
        price = closes[-1]
        side = position["side"]
        entry = position["entry"]
        pnl_pct = (price - entry) / entry * LEVERAGE * 100 if side == "long" else (entry - price) / entry * LEVERAGE * 100
        trades.append({"side": side, "entry": entry, "exit": price, "pnl_pct": round(pnl_pct, 2), "bars": len(closes) - position["entry_idx"], "reason": "end_of_data"})
    
    return trades


def analyze_results(trades):
    if not trades:
        return {"error": "no trades", "total_trades": 0}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    total = len(trades)
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    profit_factor = abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses)) if losses else float('inf')
    
    equity = [100000]
    for t in trades:
        equity.append(equity[-1] * (1 + t["pnl_pct"] / 100))
    peak = equity[0]
    dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = max(dd, (peak - e) / peak * 100)
    max_dd = dd
    
    returns = [t["pnl_pct"] / 100 for t in trades]
    avg_return = sum(returns) / len(returns) if returns else 0
    std_return = (sum((r - avg_return)**2 for r in returns) / len(returns))**0.5 if len(returns) > 1 else 0
    sharpe = (avg_return * 365 * 24 / 2 - 0.02) / (std_return * (365 * 24 / 2)**0.5) if std_return > 0 else 0
    
    tp_trades = [t for t in trades if t.get("reason") == "tp"]
    sl_trades = [t for t in trades if t.get("reason") == "sl"]
    rev_trades = [t for t in trades if t.get("reason") == "reverse"]
    
    return {
        "total_trades": total, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2), "avg_loss_pct": round(avg_loss, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "tp_exits": len(tp_trades), "sl_exits": len(sl_trades), "reverse_exits": len(rev_trades),
        "avg_holding_bars": round(sum(t.get("bars", 0) for t in trades) / total, 1) if total > 0 else 0,
    }


def main():
    print("=" * 70)
    print(f"📊 BTC Backtester v2 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    
    # Verify Binance signed API now that IP restriction is removed
    print("\n🔑 验证 Binance API Key...")
    bn_key = os.environ.get("BINANCE_API_KEY", "")
    bn_secret = os.environ.get("BINANCE_SECRET_KEY", "")
    if bn_key and bn_secret:
        try:
            import hashlib, hmac, urllib.parse
            ts = int(time.time() * 1000)
            params = {"timestamp": ts, "recvWindow": 60000}
            query = urllib.parse.urlencode(sorted(params.items()))
            sig = hmac.new(bn_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f"{BASE_URLS['binance']}/api/v3/account?{query}&signature={sig}", 
                            headers={"X-MBX-APIKEY": bn_key}, timeout=15)
            if r.ok:
                data = r.json()
                balances = [b for b in data.get('balances', []) if float(b.get('free', 0)) > 0 or float(b.get('locked', 0)) > 0]
                print(f"✅ Binance 连接成功! 账户有效, {len(balances)} 个币种有余额")
                for b in balances[:3]:
                    print(f"   {b['asset']}: {float(b['free']):.4f}")
            else:
                print(f"❌ Binance 签名请求失败: HTTP {r.status_code}")
                print(f"   响应: {r.text[:200]}")
        except Exception as e:
            print(f"❌ Binance 测试异常: {e}")
    else:
        print("⚠️  未设置 BINANCE_API_KEY — 跳过验证")
    
    # Current price
    price, source = fetch_current_price()
    if price:
        print(f"💹 BTC: ${price:,.2f} (来源: {source})")
    else:
        print("⚠️ 无法获取 BTC 价格")
    
    # Fetch from Bybit (most reliable for GitHub runners)
    print("\n📥 获取 Bybit 历史数据 (90天)...")
    klines = fetch_bybit_all("BTCUSDT", "60", 2000)  # ~83天 1h K线
    if not klines or len(klines) < 100:
        print(f"Bybit 仅获取到 {len(klines) if klines else 0} 根, 尝试 OKX...")
        okx_bars = fetch_okx_klines("BTC-USDT", "1H", 2000)
        if okx_bars:
            klines = okx_to_standard(okx_bars)
    
    if not klines or len(klines) < 100:
        print("❌ 所有数据源均失败")
        return
    
    print(f"✅ 获取 {len(klines)} 根 K 线 (1h)")
    print(f"   时间范围: {datetime.utcfromtimestamp(klines[0]['ts']).strftime('%Y-%m-%d')} → {datetime.utcfromtimestamp(klines[-1]['ts']).strftime('%Y-%m-%d')}")
    print(f"   当前价格: ${klines[-1]['close']:,.2f}")
    
    # Run backtest
    print("\n" + "=" * 70)
    print("📈 V2 策略回测")
    print("=" * 70)
    
    trades = backtest(klines)
    results = analyze_results(trades)
    
    if results.get("error"):
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
        
        print(f"\n📋 最近交易:")
        col_w = 8
        print(f"{'#':>3} {'方向':>6} {'入场':>8} {'出场':>8} {'盈亏%':>8} {'原因':>10}")
        print("-" * 48)
        for idx, t in enumerate(trades[-10:]):
            print(f"{idx+1:>3} {t['side']:>6} {t['entry']:>8.0f} {t['exit']:>8.0f} {t['pnl_pct']:>+7.2f}% {t.get('reason','?'):>10}")
    
    # Save results
    os.makedirs("/tmp/backtest", exist_ok=True)
    with open("/tmp/backtest/results.json", "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "btc_price": price,
            "candles_1h": len(klines),
            "backtest": results,
            "recent_trades": trades[-30:],
        }, f, indent=2)
    print(f"\n💾 结果已保存")


if __name__ == "__main__":
    main()
