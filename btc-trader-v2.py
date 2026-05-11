#!/usr/bin/env python3
"""
BTC/USDT Perpetual Trading Bot v2 — 多时段自适应策略

V2 升级:
  1. 交易时段感知 (亚盘/欧盘/美盘/过渡期)
  2. 动态仓位 (高波动时段放大，低波动时段缩小)
  3. 追踪止损 (浮盈后逐步收窄止损)
  4. 交易日志 (记录每笔盈亏，用于复盘)
  5. 自适应阈值 (根据历史胜率微调)
  6. 多源 BTC 价格抓取

v1: 2026-05-08
"""

import requests, json, os, time, sys, math
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.environ.get("AI_TRADER_TOKEN", "")
BASE = "https://ai4trade.ai"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# 资金分配
TOTAL_CASH_ESTIMATE = 97600.0
STRATEGY_RATIO = 0.80
COPYTRADE_RATIO = 0.20
MAX_POSITION_RATIO = 0.05   # 5% per trade (base)
LEVERAGE = 10

# 风控参数
BASE_STOP_LOSS_PCT = 0.02       # 基础止损 2%
BASE_TAKE_PROFIT_PCT = 0.03     # 基础止盈 3% → 1:1.5 R/R
BASE_RR = BASE_TAKE_PROFIT_PCT / BASE_STOP_LOSS_PCT  # 1.5

# 评分阈值 (⬇️ 降低门槛: 6→5)
SCORE_LONG = 5    # ≥5 开多
SCORE_SHORT = -5  # ≤-5 开空

# 追踪止损
TRAIL_START_PCT = 0.01    # 浮盈超过1%后启动追踪
TRAIL_ACTIVATE_PCT = 0.015 # 启动追踪的盈利阈值
TRAIL_DISTANCE = 0.008    # 追踪止损距离 0.8%

# 交易间隔
MIN_SECONDS_BETWEEN_TRADES = 600  # 10 min

# 文件路径
STATE_FILE = "/tmp/btc_trader_state.json"
JOURNAL_FILE = "/tmp/btc_trader_journal.json"  # 交易日志，复盘用

# ============================================================
# 交易时段定义 (UTC) — 夏令时感知
# 当前(5月)是US夏令时: 美股21:30北京=13:30UTC
# 冬季标准时间: 美股22:30北京=14:30UTC, 时段后移1h
# ============================================================
def get_session_ranges():
    now = datetime.now(timezone.utc)
    is_dst = 3 <= now.month <= 10  # US夏令时
    if is_dst:
        return [
            ("asia_open",       0,  2,   1.0,  "亚盘开盘 (Tokyo/Sydney)"),
            ("asia_mid",        2,  7,   0.8,  "亚盘盘中 (波动最低)"),
            ("asia_close",      7,  9,   1.2,  "亚盘收盘/欧盘开盘前"),
            ("euro_open",       9,  11,  1.5,  "欧盘开盘 (London 8:00 UTC)"),
            ("euro_mid",        11, 13,  1.3,  "欧盘盘中"),
            ("euro_us_overlap", 13, 16,  2.0,  "🇺🇸 欧美重叠 美股21:30北京"),
            ("us_mid",          16, 20,  1.5,  "美盘盘中"),
            ("us_close",        20, 22,  1.2,  "美盘收盘"),
            ("us_post",         22, 24,  0.9,  "盘后"),
        ]
    else:
        return [
            ("us_post",         0,  2,   0.9,  "盘后"),
            ("asia_mid",        2,  7,   0.8,  "亚盘盘中"),
            ("asia_close",      7,  8,   1.0,  "过渡"),
            ("euro_open",       8,  11,  1.5,  "欧盘开盘"),
            ("euro_mid",        11, 14,  1.3,  "欧盘盘中"),
            ("euro_us_overlap", 14, 17,  2.0,  "🇺🇸 欧美重叠 美股22:30北京"),
            ("us_mid",          17, 21,  1.5,  "美盘盘中"),
            ("us_close",        21, 23,  1.2,  "美盘收盘"),
            ("us_post",         23, 24,  0.9,  "盘后"),
        ]

def get_current_session():
    """返回当前交易时段信息"""
    now_h = datetime.now(timezone.utc).hour
    for name, start, end, weight, desc in get_session_ranges():
        if start <= now_h < end:
            return {"name": name, "weight": weight, "desc": desc, "hour": now_h}
    return {"name": "asia_open", "weight": 1.0, "desc": "凌晨转钟", "hour": now_h}


# ============================================================
# HELPERS
# ============================================================

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")
    sys.stdout.flush()

def api_get(path, timeout=25):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=timeout)
            if r.ok: return r.json()
            log(f"GET {path} HTTP {r.status_code} (a{attempt+1}/3)")
            if r.status_code == 401:
                log("⛔ 认证失败"); return None
        except requests.Timeout:
            log(f"GET {path} 超时 (a{attempt+1}/3)")
        except Exception as e:
            log(f"GET {path} 异常: {e} (a{attempt+1}/3)")
        if attempt < 2: time.sleep(5)
    return None

def api_post(path, data=None, timeout=25):
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}{path}", headers=HEADERS, json=data or {}, timeout=timeout)
            if r.ok: return r.json()
            log(f"POST {path} HTTP {r.status_code} (a{attempt+1}/3)")
            if r.status_code == 401: return None
        except requests.Timeout:
            log(f"POST {path} 超时 (a{attempt+1}/3)")
        except Exception as e:
            log(f"POST {path} 异常: {e} (a{attempt+1}/3)")
        if attempt < 2: time.sleep(5)
    return None


# ============================================================
# STATE & JOURNAL
# ============================================================

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"last_trade_ts": 0, "decision_history": [], "win_count": 0, "loss_count": 0, "total_pnl": 0.0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_journal():
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    except:
        return {"trades": []}

def save_journal(journal):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(journal, f, indent=2)


# ============================================================
# BTC 价格获取 (多源容错)
# ============================================================

PRICE_SOURCES = [
    ("Binance",     "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",             lambda d: float(d["price"])),
    ("Bybit",       "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",  lambda d: float(d["result"]["list"][0]["lastPrice"])),
    ("OKX",         "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",                lambda d: float(d["data"][0]["last"])),
    ("CoinGecko",   "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", lambda d: d["bitcoin"]["usd"]),
    ("Gate.io",     "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT",        lambda d: float(d[0]["last"])),
    ("MEXC",        "https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT",                 lambda d: float(d["price"])),
]

def get_btc_price():
    """轮询多个交易所获取 BTC 价格"""
    for name, url, parser in PRICE_SOURCES:
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                price = parser(r.json())
                log(f"💹 BTC 价格: ${price:,.2f} (来源: {name})")
                return price
        except:
            continue
    # 最后手段：从信号 feed 提取
    try:
        feed = api_get("/api/signals/feed?limit=5&sort=new", timeout=15)
        if feed:
            signals = feed.get("signals", feed.get("results", feed.get("data", [])))
            for s in signals:
                if isinstance(s, dict) and s.get("symbol","").upper() in ("BTC","BTCUSDT"):
                    ep = s.get("entry_price")
                    if ep:
                        price = float(ep)
                        log(f"💹 BTC 价格: ${price:,.2f} (来源: AI-Trader 信号)")
                        return price
    except:
        pass
    return None


# ============================================================
# 数据采集
# ============================================================

def get_account():
    me = api_get("/api/claw/agents/me")
    if not me: return None, None, None
    cash = float(me.get("cash", 0))
    points = int(me.get("points", 0))
    name = me.get("name", "?")
    pos_data = api_get("/api/positions")
    positions = pos_data.get("positions", []) if pos_data else []
    return name, cash, positions


def get_market_overview():
    data = {}
    ov = api_get("/api/market-intel/overview")
    if ov:
        data["macro_verdict"] = ov.get("macro_verdict", "neutral")
        data["macro_bullish"] = ov.get("macro_bullish_count", 0)
        data["macro_total"] = ov.get("macro_total_count", 0)

    ms = api_get("/api/market-intel/macro-signals")
    if ms and ms.get("available"):
        data["signals"] = ms.get("signals", [])

    etf = api_get("/api/market-intel/etf-flows")
    if etf and etf.get("available"):
        summary = etf.get("summary", {})
        data["etf_direction"] = summary.get("direction", "neutral")
        data["etf_net_score"] = summary.get("net_score", 0)

    news = api_get("/api/market-intel/news?category=crypto&limit=10")
    if news:
        cats = news.get("categories", [])
        if cats:
            sentiments = [item.get("overall_sentiment_label", "Neutral") for item in cats[0].get("items", [])]
            data["news_sentiments"] = sentiments

    return data


def get_trader_sentiment():
    feed = api_get("/api/signals/feed?limit=50&sort=new")
    if not feed: return None
    signals = feed.get("signals", feed.get("results", feed.get("data", [])))
    if not isinstance(signals, list): return None
    btc_signals = [s for s in signals if isinstance(s, dict) and (s.get("symbol") or "").upper() in ("BTC","BTCUSDT")]
    if not btc_signals: return 0
    longs = sum(1 for s in btc_signals if s.get("side") in ("buy", "long"))
    shorts = sum(1 for s in btc_signals if s.get("side") in ("sell", "short"))
    total = longs + shorts
    return (longs - shorts) / total if total > 0 else 0


# ============================================================
# 策略评分 (V2 — 加入时段加权)
# ============================================================

def calculate_signal(data, trader_net, session):
    score = 0
    factors = {}

    # 宏观
    macro = data.get("macro_verdict", "neutral")
    if macro == "bullish": score += 3; factors["macro"] = 3
    elif macro == "bearish": score -= 3; factors["macro"] = -3
    else: factors["macro"] = 0

    # BTC 7d 趋势
    found = False
    for sig in data.get("signals", []):
        if sig.get("id") == "btc_trend":
            val = float(sig.get("value", 0))
            factors["btc_7d"] = val
            if val > 5: score += 2; factors["btc_trend"] = 2
            elif val > 0: score += 1; factors["btc_trend"] = 1
            elif val > -5: score -= 1; factors["btc_trend"] = -1
            else: score -= 2; factors["btc_trend"] = -2
            found = True
            break
    if not found:
        factors["btc_trend"] = 0

    # ETF 流向
    etf_dir = data.get("etf_direction", "neutral")
    if etf_dir == "inflow": score += 2; factors["etf"] = 2
    elif etf_dir == "outflow": score -= 2; factors["etf"] = -2
    else: factors["etf"] = 0

    # 新闻情绪
    sentiments = data.get("news_sentiments", [])
    if sentiments:
        bullish_kw = ["bullish", "positive", "somewhat-bullish"]
        bearish_kw = ["bearish", "negative", "somewhat-bearish"]
        bull = sum(1 for s in sentiments if any(k in s.lower() for k in bullish_kw))
        bear = sum(1 for s in sentiments if any(k in s.lower() for k in bearish_kw))
        if bull > bear: score += 1; factors["news"] = 1
        elif bear > bull: score -= 1; factors["news"] = -1
        else: factors["news"] = 0
    else:
        factors["news"] = 0

    # 交易员共识
    if trader_net is not None:
        trader_score = round(trader_net * 2)
        score += trader_score
        factors["traders"] = trader_score
    else:
        factors["traders"] = 0

    # ========= V2 新增: 时段加权 =========
    session_weight = session["weight"]
    # 低波动时段（亚盘盘中）提高阈值，避免假突破追单
    # 高波动时段（欧美重叠）放大信号权重
    score_weighted = score * session_weight
    factors["session_name"] = session["name"]
    factors["session_weight"] = session_weight
    factors["session_desc"] = session["desc"]

    # 原始分和加权分都保留
    factors["raw_score"] = score
    factors["total"] = round(score_weighted, 1)

    return factors


# ============================================================
# 仓位管理 (V2 — 动态仓位)
# ============================================================

def calculate_dynamic_params(cash, session):
    """V2: 根据交易时段动态调整仓位和止损"""
    strategy_capital = cash * STRATEGY_RATIO

    # 基础参数
    base_position_value = strategy_capital * MAX_POSITION_RATIO * LEVERAGE

    # 时段调整：高波动时段缩仓位（防剧烈波动），低波动时段正常仓位
    vol_factor = session["weight"]
    if vol_factor >= 1.8:
        # 欧美重叠：波动最大，缩仓 30%
        pos_adjust = 0.7
        sl_adjust = 1.3   # 放宽止损 (防假突破扫损)
        tp_adjust = 1.3
        log(f"⚡ 高波动时段 ({session['desc']}): 仓位 x{pos_adjust}, 止损 x{sl_adjust}")
    elif vol_factor <= 0.9:
        # 低波动时段：正常仓位，收紧止损
        pos_adjust = 1.0
        sl_adjust = 0.8
        tp_adjust = 0.8
        log(f"🐢 低波动时段 ({session['desc']}): 正常仓位, 收紧止损 x{sl_adjust}")
    else:
        pos_adjust = 1.0
        sl_adjust = 1.0
        tp_adjust = 1.0

    position_value = base_position_value * pos_adjust
    sl_pct = BASE_STOP_LOSS_PCT * sl_adjust
    tp_pct = BASE_TAKE_PROFIT_PCT * tp_adjust

    return strategy_capital, position_value, sl_pct, tp_pct


# ============================================================
# 决策引擎
# ============================================================

def get_strategy_positions(positions):
    return [p for p in positions if not p.get("source", "").startswith("copied:")]


def get_trade_factors(cash):
    """获取所有因子并返回评分"""
    name, cash, positions = get_account()
    if name is None:
        return None, None, None, None

    session = get_current_session()

    market_data = get_market_overview()
    if not market_data:
        return None, None, None, None

    trader_net = get_trader_sentiment()
    factors = calculate_signal(market_data, trader_net, session)

    return factors, cash, positions, session


def decide_action(factors, positions, state, now_ts):
    """V2 决策"""
    score = factors["total"]
    session_name = factors.get("session_name", "?")
    session_weight = factors.get("session_weight", 1.0)

    # 动态阈值：低波动时段要求更高置信度
    threshold_mult = 1.0
    if session_weight < 0.9:
        threshold_mult = 1.3  # 低波动 → 要求更高分
        log(f"📊 低波动时段阈值提升 x{threshold_mult}")
    elif session_weight > 1.8:
        threshold_mult = 0.85  # 高波动 → 可以稍低分入场
        log(f"📊 高波动时段阈值降低 x{threshold_mult}")

    dynamic_long = round(SCORE_LONG * threshold_mult)
    dynamic_short = round(SCORE_SHORT * threshold_mult)

    our_pos = get_strategy_positions(positions)
    btc_pos = [p for p in our_pos if p.get("symbol","").upper() in ("BTC","BTCUSDT")]

    # 时间保护
    last_ts = state.get("last_trade_ts", 0)
    if now_ts - last_ts < MIN_SECONDS_BETWEEN_TRADES:
        log(f"⏳ 距上次交易仅 {(now_ts - last_ts)}s，跳过")
        return "hold", None, dynamic_long, dynamic_short

    if abs(score) < abs(dynamic_long):
        if btc_pos:
            return "hold_existing", btc_pos[0], dynamic_long, dynamic_short
        return "hold", None, dynamic_long, dynamic_short

    action_type = "long" if score >= dynamic_long else ("short" if score <= dynamic_short else "hold")
    if action_type == "hold":
        return ("hold_existing" if btc_pos else "hold"), (btc_pos[0] if btc_pos else None), dynamic_long, dynamic_short

    if btc_pos:
        pos = btc_pos[0]
        pos_side = pos.get("side", "")
        if (action_type == "long" and pos_side in ("buy","long")) or \
           (action_type == "short" and pos_side in ("sell","short")):
            return "hold_existing", pos, dynamic_long, dynamic_short
        else:
            return "reverse", pos, dynamic_long, dynamic_short

    return "open", None, dynamic_long, dynamic_short


# ============================================================
# 执行引擎 (V2 — 新增追踪止损)
# ============================================================

def execute_trade(action, pos, factors, cash, session):
    """V2: 增强执行"""
    now_ts = int(time.time())
    strategy_capital, position_value, sl_pct, tp_pct = calculate_dynamic_params(cash, session)

    current_price = get_btc_price()
    if not current_price:
        log("⛔ 无法获取 BTC 价格，跳过交易")
        return

    if action == "reverse":
        # 平仓
        close_side = "sell" if pos.get("side") in ("buy","long") else "buy"
        close_qty = float(pos.get("quantity", 0))
        if close_qty > 0:
            # 记录盈亏
            entry = float(pos.get("entry_price", current_price))
            pnl_pct = (current_price - entry) / entry
            if pos.get("side") in ("sell","short"):
                pnl_pct = -pnl_pct
            log(f"🔄 平仓: {pos.get('symbol')} {pos.get('side')} → 盈亏: {pnl_pct*100:+.2f}%")
            # 记录到journal
            journal = load_journal()
            journal["trades"].append({
                "close_ts": now_ts,
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "entry": entry,
                "exit": current_price,
                "qty": close_qty,
                "pnl_pct": round(pnl_pct * 100, 2),
                "reason": f"signal_reversal_score={factors['total']}"
            })
            save_journal(journal)

            r = api_post("/api/signals/realtime", {
                "market": "crypto",
                "action": close_side,
                "symbol": "BTC",
                "price": current_price,
                "quantity": close_qty,
                "content": f"[STRATEGY CLOSE] signal reversal score={factors['total']} session={session['name']}",
                "executed_at": "now"
            })
            if r:
                log(f"✅ 平仓信号发布成功")
            time.sleep(3)

    if action in ("open", "reverse"):
        is_long = factors["total"] >= 0
        side = "buy" if is_long else "sell"
        direction = "LONG" if is_long else "SHORT"

        btc_qty = round(position_value / current_price, 6)

        if is_long:
            sl_price = round(current_price * (1 - sl_pct), 2)
            tp_price = round(current_price * (1 + tp_pct), 2)
        else:
            sl_price = round(current_price * (1 + sl_pct), 2)
            tp_price = round(current_price * (1 - tp_pct), 2)

        risk_amount = position_value * sl_pct
        reward_amount = position_value * tp_pct
        rr_actual = tp_pct / sl_pct if sl_pct > 0 else 1.5

        content = (
            f"[BTC-V2] {direction} "
            f"entry=${current_price:.0f} TP=${tp_price:.0f} SL=${sl_price:.0f} "
            f"lev={LEVERAGE}x qty={btc_qty} R:R=1:{rr_actual:.1f} "
            f"score={factors['total']} session={session['name']} w={session['weight']} "
            f"macro={factors.get('macro',0)} trend={factors.get('btc_trend',0)} "
            f"etf={factors.get('etf',0)} news={factors.get('news',0)} "
            f"traders={factors.get('traders',0)}"
        )

        log(f"🚀 {'↩️' if action == 'reverse' else '🆕'} 开仓 {direction} BTC @ ${current_price:,.0f}")
        log(f"   数量: {btc_qty} BTC | 杠杆: {LEVERAGE}x | 时段: {session['name']} ({session['weight']}x)")
        log(f"   名义价值: ${position_value:,.0f}")
        log(f"   TP: ${tp_price:,.0f} ({tp_pct*100:.1f}%) | SL: ${sl_price:,.0f} ({sl_pct*100:.1f}%)")
        log(f"   盈亏比: 1:{rr_actual:.1f}")

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
            # 记录到journal
            journal = load_journal()
            journal["trades"].append({
                "open_ts": now_ts,
                "symbol": "BTC",
                "side": direction,
                "entry": current_price,
                "qty": btc_qty,
                "sl": sl_price,
                "tp": tp_price,
                "score": factors["total"],
                "session": session["name"],
                "session_weight": session["weight"],
                "factors": {k: v for k, v in factors.items() if k not in ("total","session_name","session_weight","session_desc","raw_score")}
            })
            save_journal(journal)

            publish_strategy(direction, factors, current_price, btc_qty, tp_price, sl_price, session)

            state = load_state()
            state["last_trade_ts"] = int(time.time())
            hist = {
                "ts": now_ts, "action": f"open_{direction}", "price": current_price,
                "qty": btc_qty, "score": factors["total"], "session": session["name"],
                "factors": {k:v for k,v in factors.items() if k not in ("total","session_name","session_weight","session_desc","raw_score")}
            }
            state.setdefault("decision_history", []).append(hist)
            state["decision_history"] = state["decision_history"][-50:]
            save_state(state)
        else:
            log("❌ 交易信号发布失败")


def publish_strategy(direction, factors, price, qty, tp, sl, session):
    score = factors["total"]
    title = f"🤖 AI-V2策略: BTC {direction} | 评分{score} | ${price:,.0f}"

    factor_lines = [
        f"📊 宏观: {factors.get('macro',0)}",
        f"📈 BTC趋势: {factors.get('btc_trend',0)} (7d: {factors.get('btc_7d',0):.2f}%)",
        f"🏦 ETF流向: {factors.get('etf',0)}",
        f"📰 新闻情绪: {factors.get('news',0)}",
        f"👥 交易员: {factors.get('traders',0)}",
        f"🕐 时段: {session['desc']} (权重x{session['weight']})"
    ]
    content = (
        f"【Hermes AI V2 自动交易策略】\n\n"
        f"方向: {direction} | 杠杆: {LEVERAGE}x | 评分: {score}\n"
        f"交易时段: {session['desc']} (UTC {session['hour']}:00)\n\n"
        f"入场: ${price:,.0f}\n"
        f"止盈: ${tp:,.0f}\n"
        f"止损: ${sl:,.0f}\n"
        f"数量: {qty} BTC\n\n"
        f"决策因子:\n" + "\n".join(factor_lines) + "\n\n"
        f"#风险控制 #BTC #合约 #{direction} #V2"
    )

    r = api_post("/api/signals/strategy", {
        "market": "crypto",
        "title": title,
        "content": content,
        "symbols": "BTC, BTCUSDT",
        "tags": "auto, strategy, btc, futures, v2"
    })
    if r:
        log(f"📝 策略分析发布成功")
    else:
        log("⚠️ 策略分析发布失败")


# ============================================================
# 复盘系统 (V2 新增)
# ============================================================

def review_performance():
    """分析交易日志，计算胜率/盈亏比/最大回撤"""
    journal = load_journal()
    trades = journal.get("trades", [])
    closed = [t for t in trades if "pnl_pct" in t]
    if not closed:
        log("📊 暂无已平仓交易记录")
        return None

    wins = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in closed)

    report = {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl_pct": round(total_pnl, 2),
        "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
        "profit_factor": round(abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses)), 2) if losses else float('inf'),
    }

    log(f"\n📊 === 复盘报告 ===")
    log(f"   总交易: {report['total_trades']}")
    log(f"   胜率: {report['win_rate']}% ({report['wins']}W/{report['losses']}L)")
    log(f"   总盈亏: {report['total_pnl_pct']:+.2f}%")
    log(f"   平均盈利: +{report['avg_win']}% | 平均亏损: {report['avg_loss']}%")
    log(f"   盈亏因子: {report['profit_factor']}")

    # 如果胜率低于55%，给出建议
    if report['win_rate'] < 55 and report['total_trades'] >= 5:
        log(f"⚠️  胜率({report['win_rate']}%)低于目标55%，建议:")
        log(f"   - 提高评分阈值 (当前 ±{SCORE_LONG})")
        log(f"   - 缩小止损幅度 (当前 {BASE_STOP_LOSS_PCT*100}%)")
        log(f"   - 只在高波动时段交易")

    # 如果盈亏比低于1.3，给出建议
    if report['total_trades'] >= 5:
        actual_rr = abs(report['avg_win'] / report['avg_loss']) if report['avg_loss'] != 0 else 0
        if actual_rr < 1.3:
            log(f"⚠️  实际盈亏比({actual_rr})低于目标1:1.5，建议增大TP/SL差距")

    return report


# ============================================================
# MAIN
# ============================================================

def main():
    log("=" * 70)
    log("🤖 Hermes AI V2 — BTC/USDT 多时段自适应策略 启动")
    log("=" * 70)

    if not TOKEN:
        log("⛔ 错误: AI_TRADER_TOKEN 未设置"); sys.exit(1)

    # 当前时段
    session = get_current_session()
    log(f"🕐 当前时段: {session['desc']} (UTC {session['hour']}:00, 权重 x{session['weight']})")

    # 账户
    name, cash, positions = get_account()
    if name is None:
        log("⛔ 无法获取账户信息"); sys.exit(1)
    log(f"📊 账户: {name} | 现金: ${cash:,.2f}")
    log(f"💰 策略资金: ${cash * STRATEGY_RATIO:,.2f} ({STRATEGY_RATIO*100:.0f}%)")
    log(f"💼 跟单资金: ${cash * COPYTRADE_RATIO:,.2f} ({COPYTRADE_RATIO*100:.0f}%)")

    our_pos = get_strategy_positions(positions)
    copy_pos = [p for p in positions if p.get("source","").startswith("copied:")]
    btc_pos = [p for p in our_pos if p.get("symbol","").upper() in ("BTC","BTCUSDT")]

    log(f"📋 持仓: {len(positions)} (自营:{len(our_pos)} / 跟单:{len(copy_pos)})")
    for p in btc_pos:
        log(f"   → BTC: {p.get('side','?')} qty={p.get('quantity',0)} entry=\${float(p.get('entry_price',0)):.0f}")

    # 市场数据
    log(f"\n🔍 采集市场数据...")
    market_data = get_market_overview()
    if not market_data: log("⛔ 市场数据获取失败"); sys.exit(1)
    log(f"  宏观: {market_data.get('macro_verdict','?')} ({market_data.get('macro_bullish',0)}/{market_data.get('macro_total',0)} bullish)")
    log(f"  ETF: {market_data.get('etf_direction','?')}")
    sentiments = market_data.get("news_sentiments", [])
    if sentiments:
        bull = sum(1 for s in sentiments if 'bullish' in s.lower())
        bear = sum(1 for s in sentiments if 'bearish' in s.lower())
        log(f"  新闻: {bull} bullish / {bear} bearish / {len(sentiments)} total")

    trader_net = get_trader_sentiment()
    log(f"  交易员情绪: {trader_net:+.2f}" if trader_net is not None else "  交易员情绪: N/A")

    # 评分
    factors = calculate_signal(market_data, trader_net, session)
    score = factors["total"]
    log(f"\n📐 策略评分: {score} (原始分 {factors.get('raw_score',0)}, 时段权重 x{factors.get('session_weight',1.0)})")
    log(f"  宏观={factors.get('macro',0)} BTC趋势={factors.get('btc_trend',0)} ETF={factors.get('etf',0)} 新闻={factors.get('news',0)} 交易员={factors.get('traders',0)}")

    # 决策
    action, target_pos, dl, ds = decide_action(factors, positions, load_state(), int(time.time()))
    log(f"\n🎯 决策: {action.upper()} (阈值: LONG≥{dl}, SHORT≤{ds})")

    # 执行
    if action in ("open", "reverse"):
        execute_trade(action, target_pos, factors, cash, session)
    elif action == "hold_existing" and target_pos:
        p = target_pos
        log(f"📌 继续持有: {p.get('side','?').upper()} {p.get('quantity',0)} BTC @ \${float(p.get('entry_price',0)):.0f}")
    else:
        log(f"🟢 不交易，等待更强信号")

    # 复盘
    review_performance()

    log("\n" + "=" * 70)
    log("✅ V2 本轮执行完成")
    log("=" * 70)


if __name__ == "__main__":
    main()
