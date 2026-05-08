#!/usr/bin/env python3
"""
BTC/USDT Perpetual Trading Bot — 10x Leverage, Risk-Managed
Strategy: Multi-Factor Trend Following + Sentiment Scoring

Configuration:
  单笔资金: 5% of trading capital
  杠杆: 10x
  盈亏比: 1:1.5 (SL 2%, TP 3%)
  目标胜率: >55%
  资金分配: 80% 策略交易 / 20% 跟单

Signals:
  - Market Intel: macro verdict (bullish/bearish)
  - BTC 7d trend: momentum score
  - BTC ETF flows: institutional sentiment
  - Crypto news sentiment
  - Top trader consensus from signal feed
"""

import requests, json, os, time, sys
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.environ.get("AI_TRADER_TOKEN", "")
BASE = "https://ai4trade.ai"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Capital allocation
TOTAL_CASH_ESTIMATE = 97600.0  # will be updated from API
STRATEGY_RATIO = 0.80           # 80% for strategy
COPYTRADE_RATIO = 0.20          # 20% for copy trading
MAX_POSITION_RATIO = 0.05       # 5% per trade (of strategy capital)
LEVERAGE = 10
STOP_LOSS_PCT = 0.02            # 2% from entry
TAKE_PROFIT_PCT = 0.03          # 3% from entry → 1:1.5 R/R
# Score thresholds
SCORE_LONG = 6                  # ≥ 6 → open long
SCORE_SHORT = -6                # ≤ -6 → open short
# Re-evaluation interval
MIN_SECONDS_BETWEEN_TRADES = 600  # 10 min between decisions

STATE_FILE = "/tmp/btc_trader_state.json"

# ============================================================
# HELPERS
# ============================================================

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    sys.stdout.flush()

def api_get(path, timeout=25):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=timeout)
            if r.ok:
                return r.json()
            log(f"GET {path} HTTP {r.status_code} (attempt {attempt+1}/3)")
            # 401 = auth failure, don't retry
            if r.status_code == 401:
                log("⛔ API token 认证失败 — 请检查 AI_TRADER_TOKEN")
                return None
        except requests.Timeout:
            log(f"GET {path} 超时 (attempt {attempt+1}/3)")
        except Exception as e:
            log(f"GET {path} 异常: {e} (attempt {attempt+1}/3)")
        if attempt < 2:
            time.sleep(5)
    return None

def api_post(path, data=None, timeout=25):
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}{path}", headers=HEADERS, json=data or {}, timeout=timeout)
            if r.ok:
                return r.json()
            log(f"POST {path} HTTP {r.status_code} (attempt {attempt+1}/3)")
            # Try to parse error body
            try:
                err = r.json()
                log(f"  错误详情: {json.dumps(err)[:200]}")
            except:
                pass
            if r.status_code == 401:
                return None
        except requests.Timeout:
            log(f"POST {path} 超时 (attempt {attempt+1}/3)")
        except Exception as e:
            log(f"POST {path} 异常: {e} (attempt {attempt+1}/3)")
        if attempt < 2:
            time.sleep(5)
    return None


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"last_trade_ts": 0, "decision_history": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================
# DATA COLLECTION
# ============================================================

def get_account():
    """Fetch account info and current positions."""
    me = api_get("/api/claw/agents/me")
    if not me:
        return None, None, None

    cash = float(me.get("cash", 0))
    points = int(me.get("points", 0))
    name = me.get("name", "?")

    # Also get positions
    pos_data = api_get("/api/positions")
    positions = pos_data.get("positions", []) if pos_data else []

    return name, cash, positions


def get_market_overview():
    """Fetch macro overview, signals, ETF flows, and news."""
    data = {}

    # 1. Overview
    ov = api_get("/api/market-intel/overview")
    if ov:
        data["macro_verdict"] = ov.get("macro_verdict", "neutral")
        data["macro_bullish"] = ov.get("macro_bullish_count", 0)
        data["macro_total"] = ov.get("macro_total_count", 0)

    # 2. Macro signals (for BTC trend)
    ms = api_get("/api/market-intel/macro-signals")
    if ms and ms.get("available"):
        data["signals"] = ms.get("signals", [])

    # 3. ETF flows
    etf = api_get("/api/market-intel/etf-flows")
    if etf and etf.get("available"):
        summary = etf.get("summary", {})
        data["etf_direction"] = summary.get("direction", "neutral")
        data["etf_net_score"] = summary.get("net_score", 0)

    # 4. Crypto news
    news = api_get("/api/market-intel/news?category=crypto&limit=10")
    if news:
        cats = news.get("categories", [])
        if cats:
            items = cats[0].get("items", [])
            sentiments = []
            for item in items:
                label = item.get("overall_sentiment_label", "Neutral")
                sentiments.append(label)
            data["news_sentiments"] = sentiments

    return data


def get_trader_sentiment():
    """Analyze what top traders are doing with BTC from the signal feed."""
    feed = api_get("/api/signals/feed?limit=50&sort=new")
    if not feed:
        return None

    signals = feed.get("signals", feed.get("results", feed.get("data", [])))
    if not isinstance(signals, list):
        return None

    btc_signals = [s for s in signals
                   if isinstance(s, dict) and s.get("symbol", "").upper() in ("BTC", "BTCUSDT")]

    if not btc_signals:
        # Fall back: look at recent BTC price direction from feed
        return 0  # neutral

    longs = sum(1 for s in btc_signals if s.get("side") in ("buy", "long"))
    shorts = sum(1 for s in btc_signals if s.get("side") in ("sell", "short"))
    total = longs + shorts

    if total == 0:
        return 0

    net = (longs - shorts) / total
    return net  # -1 to +1


# ============================================================
# SIGNAL SCORING
# ============================================================

def calculate_signal(data, trader_net):
    """
    Multi-factor scoring:
      Macro verdict: bullish +3, neutral 0, bearish -3
      BTC 7d trend:  >5% +2, 0-5% +1, -5-0% -1, <-5% -2
      ETF flows:     inflow +2, neutral 0, outflow -2
      News:          mostly bullish +1, mixed 0, bearish -1
      Trader consensus: net * 2
    """
    score = 0
    factors = {}

    # --- Macro ---
    macro = data.get("macro_verdict", "neutral")
    if macro == "bullish":
        score += 3
        factors["macro"] = 3
    elif macro == "bearish":
        score -= 3
        factors["macro"] = -3
    else:
        factors["macro"] = 0

    # --- BTC 7d trend ---
    btc_trend_val = 0
    for sig in data.get("signals", []):
        if sig.get("id") == "btc_trend":
            val = float(sig.get("value", 0))
            btc_trend_val = val
            if val > 5:
                score += 2
                factors["btc_trend"] = 2
            elif val > 0:
                score += 1
                factors["btc_trend"] = 1
            elif val > -5:
                score -= 1
                factors["btc_trend"] = -1
            else:
                score -= 2
                factors["btc_trend"] = -2
            break
    else:
        factors["btc_trend"] = 0

    # --- ETF flows ---
    etf_dir = data.get("etf_direction", "neutral")
    if etf_dir == "inflow":
        score += 2
        factors["etf"] = 2
    elif etf_dir == "outflow":
        score -= 2
        factors["etf"] = -2
    else:
        factors["etf"] = 0

    # --- News sentiment ---
    sentiments = data.get("news_sentiments", [])
    if sentiments:
        bullish_kw = ["bullish", "positive", "Somewhat-Bullish"]
        bearish_kw = ["bearish", "negative", "Somewhat-Bearish"]
        bull_count = sum(1 for s in sentiments if any(k in s.lower() for k in bullish_kw))
        bear_count = sum(1 for s in sentiments if any(k in s.lower() for k in bearish_kw))
        if bull_count > bear_count:
            score += 1
            factors["news"] = 1
        elif bear_count > bull_count:
            score -= 1
            factors["news"] = -1
        else:
            factors["news"] = 0
    else:
        factors["news"] = 0

    # --- Trader consensus ---
    if trader_net is not None:
        trader_score = round(trader_net * 2)
        score += trader_score
        factors["traders"] = trader_score
    else:
        factors["traders"] = 0

    factors["total"] = score
    factors["btc_7d_pct"] = btc_trend_val
    return score, factors


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def get_strategy_positions(positions):
    """Identify positions that belong to our strategy (not copy trading)."""
    our_positions = []
    for p in positions:
        source = p.get("source", "")
        # Copy trading positions have 'copied:' prefix
        if source and source.startswith("copied:"):
            continue
        our_positions.append(p)
    return our_positions


def decide_action(factors, positions, state, cash, now_ts):
    """
    Decide what to do:
      - If no position and score suggests direction → open
      - If position exists and score reverses → close old, open opposite
      - If position exists and score stays same → hold
    """
    score = factors["total"]
    our_pos = get_strategy_positions(positions)
    btc_pos = [p for p in our_pos if p.get("symbol", "").upper() in ("BTC", "BTCUSDT")]

    # --- Time guard ---
    last_ts = state.get("last_trade_ts", 0)
    if now_ts - last_ts < MIN_SECONDS_BETWEEN_TRADES:
        log(f"⏳ 距离上次交易仅 {(now_ts - last_ts)}s，低于最小间隔 {MIN_SECONDS_BETWEEN_TRADES}s，跳过")
        return "hold", None

    # --- Ensure at least SCORE_LONG threshold ---
    if abs(score) < min(abs(SCORE_LONG), abs(SCORE_SHORT)):
        # Signal too weak
        if btc_pos:
            log(f"📊 信号强度不足 (score={score})，但持有现有 BTC 仓位，继续持仓观察")
            return "hold_existing", btc_pos[0]
        log(f"📊 信号强度不足 (score={score})，不交易")
        return "hold", None

    action_type = "long" if score >= SCORE_LONG else ("short" if score <= SCORE_SHORT else "hold")

    if action_type == "hold":
        if btc_pos:
            return "hold_existing", btc_pos[0]
        return "hold", None

    # --- Check positions ---
    if btc_pos:
        pos = btc_pos[0]
        pos_side = pos.get("side", "")
        # Check if signal matches position direction
        if (action_type == "long" and pos_side in ("buy", "long")) or \
           (action_type == "short" and pos_side in ("sell", "short")):
            log(f"🔄 信号方向 ({action_type}) 与当前仓位方向一致，继续持有")
            return "hold_existing", pos
        else:
            # Signal reversed — close then open
            log(f"🔁 信号反转! score={score} 建议 {action_type.upper()}，当前为 {pos_side.upper()}，将平仓反手")
            return "reverse", pos

    # --- No existing BTC position → open new ---
    return "open", None


def calculate_position_size(cash):
    """Calculate position size: 5% of strategy capital, 10x leverage."""
    strategy_capital = cash * STRATEGY_RATIO
    position_value = strategy_capital * MAX_POSITION_RATIO * LEVERAGE
    return strategy_capital, position_value


def execute_trade(action, pos, factors, cash, data):
    """Execute the trade via AI-Trader API."""
    now_ts = int(time.time())
    strategy_capital, position_value = calculate_position_size(cash)

    if action == "hold":
        return

    # Get current BTC price
    # We need a price — use AI-Trader's own data or a public API
    current_price = get_btc_price()
    if not current_price:
        log("⛔ 无法获取 BTC 当前价格，跳过交易")
        return

    if action == "reverse":
        # Close existing position first
        log(f"🔄 平仓现有 {pos.get('symbol')} 仓位")
        close_side = "sell" if pos.get("side") in ("buy", "long") else "buy"
        close_qty = float(pos.get("quantity", 0))
        if close_qty > 0:
            r = api_post("/api/signals/realtime", {
                "market": "crypto",
                "action": close_side,
                "symbol": "BTC",
                "price": current_price,
                "quantity": close_qty,
                "content": f"[STRATEGY CLOSE] score={factors['total']} PnL tracking",
                "executed_at": "now"
            })
            if r:
                log(f"✅ 平仓信号发布成功")
            time.sleep(3)

    if action in ("open", "reverse"):
        # Determine direction
        is_long = factors["total"] >= SCORE_LONG
        side = "buy" if is_long else "sell"
        direction = "LONG" if is_long else "SHORT"

        # Position size
        btc_qty = round(position_value / current_price, 6)

        # SL and TP prices
        if is_long:
            sl_price = round(current_price * (1 - STOP_LOSS_PCT), 2)
            tp_price = round(current_price * (1 + TAKE_PROFIT_PCT), 2)
        else:
            sl_price = round(current_price * (1 + STOP_LOSS_PCT), 2)
            tp_price = round(current_price * (1 - TAKE_PROFIT_PCT), 2)

        risk_amount = position_value * STOP_LOSS_PCT
        reward_amount = position_value * TAKE_PROFIT_PCT

        content = (
            f"[BTC-STRATEGY] {direction} conf={factors['total']}/? "
            f"entry=${current_price:.0f} TP=${tp_price:.0f} SL=${sl_price:.0f} "
            f"lev={LEVERAGE}x qty={btc_qty} "
            f"R=${risk_amount:.0f} R:R=1:{TAKE_PROFIT_PCT/STOP_LOSS_PCT:.1f} "
            f"macro={factors.get('macro',0)} trend={factors.get('btc_trend',0)} "
            f"etf={factors.get('etf',0)} news={factors.get('news',0)} "
            f"traders={factors.get('traders',0)}"
        )

        log(f"🚀 开仓 {direction} BTC @ ${current_price:.0f}")
        log(f"   数量: {btc_qty} BTC | 杠杆: {LEVERAGE}x")
        log(f"   名义价值: ${position_value:,.0f} | 风险敞口: {strategy_capital * MAX_POSITION_RATIO:,.0f}")
        log(f"   TP: ${tp_price:.0f} ({TAKE_PROFIT_PCT*100:.0f}%) | SL: ${sl_price:.0f} ({STOP_LOSS_PCT*100:.0f}%)")
        log(f"   盈亏比: 1:{TAKE_PROFIT_PCT/STOP_LOSS_PCT:.1f}")

        r = api_post("/api/signals/realtime", {
            "market": "crypto",
            "action": side,
            "symbol": "BTC",
            "price": current_price,
            "quantity": btc_qty,
            "content": content,
            "executed_at": "now"
        })

        if r:
            log(f"✅ 交易信号发布成功! (ID: {r.get('signal_id', r.get('id', '?'))})")
            # Publish strategy explanation
            publish_strategy(direction, factors, current_price, btc_qty, tp_price, sl_price)
            # Update state
            state = load_state()
            state["last_trade_ts"] = int(time.time())
            state["decision_history"].append({
                "ts": now_ts,
                "action": f"open_{direction}",
                "price": current_price,
                "qty": btc_qty,
                "score": factors["total"],
                "factors": {k: v for k, v in factors.items() if k != "total"}
            })
            state["decision_history"] = state["decision_history"][-50:]
            save_state(state)
        else:
            log("❌ 交易信号发布失败")
            return


def publish_strategy(direction, factors, price, qty, tp, sl):
    """Publish a strategy post explaining the decision."""
    score = factors["total"]
    title = f"🤖 AI策略: BTC {direction} | 评分 {score} | ${price:,.0f}"

    factor_lines = [
        f"📊 宏观: {factors.get('macro',0)}",
        f"📈 BTC趋势: {factors.get('btc_trend',0)} (7d: {factors.get('btc_7d_pct',0):.2f}%)",
        f"🏦 ETF流向: {factors.get('etf',0)}",
        f"📰 新闻情绪: {factors.get('news',0)}",
        f"👥 交易员共识: {factors.get('traders',0)}"
    ]
    content = (
        f"【Hermes AI 自动交易策略】\n\n"
        f"方向: {direction} | 杠杆: {LEVERAGE}x | 评分: {score}\n\n"
        f"入场: ${price:,.0f}\n"
        f"止盈: ${tp:,.0f} ({TAKE_PROFIT_PCT*100:.0f}%)\n"
        f"止损: ${sl:,.0f} ({STOP_LOSS_PCT*100:.0f}%)\n"
        f"数量: {qty} BTC\n"
        f"盈亏比: 1:{TAKE_PROFIT_PCT/STOP_LOSS_PCT:.1f}\n\n"
        f"决策因子:\n" + "\n".join(factor_lines) + "\n\n"
        f"#风险控制 #BTC #合约 #{direction}"
    )

    r = api_post("/api/signals/strategy", {
        "market": "crypto",
        "title": title,
        "content": content,
        "symbols": "BTC, BTCUSDT",
        "tags": "auto, strategy, btc, futures"
    })
    if r:
        log(f"📝 策略分析发布成功")
    else:
        log("⚠️ 策略分析发布失败")


def get_btc_price():
    """Get current BTC/USDT price from multiple sources."""
    # Source 1: Binance public API
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=10)
        if r.ok:
            return float(r.json()["price"])
    except:
        pass

    # Source 2: Bybit
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", timeout=10)
        if r.ok:
            data = r.json()
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
    except:
        pass

    # Source 3: OKX
    try:
        r = requests.get("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", timeout=10)
        if r.ok:
            data = r.json()
            return float(data["data"][0]["last"])
    except:
        pass

    # Source 4: CoinGecko
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10)
        if r.ok:
            return r.json()["bitcoin"]["usd"]
    except:
        pass

    # Source 5: Use latest position or signal as reference
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            history = state.get("decision_history", [])
            if history:
                return history[-1].get("price")
    except:
        pass

    log("⚠️ 所有 BTC 价格源均不可用")
    return None


# ============================================================
# MAIN
# ============================================================

def main():
    log("=" * 60)
    log("🤖 Hermes AI — BTC/USDT 策略交易机器人 启动")
    log("=" * 60)

    # Validate token
    if not TOKEN:
        log("⛔ 错误: AI_TRADER_TOKEN 未设置")
        sys.exit(1)

    # 1. Get account state
    name, cash, positions = get_account()
    if name is None:
        log("⛔ 无法获取账户信息，退出")
        sys.exit(1)

    now_ts = int(time.time())
    log(f"📊 账户: {name} | 现金: ${cash:,.2f}")
    strategy_capital = cash * STRATEGY_RATIO
    log(f"💰 策略资金: ${strategy_capital:,.2f} ({STRATEGY_RATIO*100:.0f}%)")
    log(f"💼 跟单资金: ${cash * COPYTRADE_RATIO:,.2f} ({COPYTRADE_RATIO*100:.0f}%)")

    our_pos = get_strategy_positions(positions)
    btc_positions = [p for p in our_pos if p.get("symbol", "").upper() in ("BTC", "BTCUSDT")]
    copy_positions = [p for p in positions if p.get("source", "").startswith("copied:")]

    log(f"📋 全部持仓: {len(positions)}")
    log(f"   ├─ 跟单持仓: {len(copy_positions)}")
    log(f"   └─ 自营持仓: {len(our_pos)}")
    for p in btc_positions:
        log(f"   → BTC: {p.get('side','?')} qty={p.get('quantity',0)} entry=${float(p.get('entry_price',0)):.0f}")

    # 2. Fetch market data
    log("\n🔍 采集市场数据...")
    market_data = get_market_overview()
    if not market_data:
        log("⛔ 无法获取市场数据，退出")
        sys.exit(1)

    macro = market_data.get("macro_verdict", "?")
    log(f"  宏观: {macro} ({market_data.get('macro_bullish',0)}/{market_data.get('macro_total',0)} bullish)")
    log(f"  ETF: {market_data.get('etf_direction','?')} (score={market_data.get('etf_net_score',0)})")
    sentiments = market_data.get("news_sentiments", [])
    if sentiments:
        bull = sum(1 for s in sentiments if 'bullish' in s.lower())
        bear = sum(1 for s in sentiments if 'bearish' in s.lower())
        log(f"  新闻: {bull} bullish / {bear} bearish / {len(sentiments)} total")

    # 3. Analyze trader sentiment
    trader_net = get_trader_sentiment()
    if trader_net is not None:
        log(f"  交易员BTC情绪: {trader_net:+.2f}")
    else:
        log(f"  交易员BTC情绪: N/A")

    # 4. Calculate signal score
    score, factors = calculate_signal(market_data, trader_net)
    log(f"\n📐 策略评分: {score}")
    log(f"  宏观={factors.get('macro',0)} BTC趋势={factors.get('btc_trend',0)} ETF={factors.get('etf',0)} 新闻={factors.get('news',0)} 交易员={factors.get('traders',0)}")

    # 5. Decide
    action, target_pos = decide_action(factors, positions, load_state(), cash, now_ts)
    log(f"\n🎯 决策: {action.upper()}")

    # 6. Execute
    if action in ("open", "reverse"):
        execute_trade(action, target_pos, factors, cash, market_data)
    elif action == "hold_existing" and target_pos:
        log(f"📌 继续持有现有 BTC 仓位")
        pos = target_pos
        entry = float(pos.get("entry_price", 0))
        pnl = float(pos.get("pnl", 0)) if pos.get("pnl") else 0
        side = pos.get("side", "?")
        qty = float(pos.get("quantity", 0))
        log(f"   {side.upper()} {qty} BTC @ ${entry:.0f} | PnL: ${pnl:+.2f}")
    else:
        log(f"🟢 不交易，等待信号")

    log("\n" + "=" * 60)
    log("✅ 本轮执行完成")
    log("=" * 60)


if __name__ == "__main__":
    main()
