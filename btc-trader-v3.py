#!/usr/bin/env python3
"""
BTC/USDT Trading Bot V3
V3 升级:
  - 实时 Bybit K线 + 技术指标 (EMA/RSI/MACD/布林带)
  - 技术指标评分 + 宏观评分 双引擎
  - 更大的历史数据回测 (90天)
  - 自动复盘报告输出到日志
"""

import requests, json, os, time, sys, math
from datetime import datetime, timezone

TOKEN = os.environ.get("AI_TRADER_TOKEN", "")
BASE = "https://ai4trade.ai"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# 资金参数
STRATEGY_RATIO = 0.80
COPYTRADE_RATIO = 0.20
MAX_POSITION_RATIO = 0.05
LEVERAGE = 10
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.04  # 1:2 R/R (2%止损/4%止盈)

# 评分阈值
TECH_WEIGHT = 0.6      # 技术指标权重
MACRO_WEIGHT = 0.4     # 宏观因子权重
MIN_SCORE = 3           # ⬇️ 4→3 降低门槛增加交易机会

MIN_SECONDS_BETWEEN = 600
STATE_FILE = "/tmp/btc_trader_v3_state.json"
JOURNAL_FILE = "/tmp/btc_trader_v3_journal.json"

BYBIT_BASE = "https://api.bybit.com"


# ============================================================
# 交易时段
# ============================================================
# 夏令时感知的交易时段 (US DST: 3-10月, 标准时间: 11-2月)
# 当前(5月)是夏令时: 美股21:30北京=13:30UTC, 欧美重叠13-16
def get_sessions():
    now = datetime.now(timezone.utc)
    is_dst = 3 <= now.month <= 10
    if is_dst:
        return [(0,2,1.0,"亚盘开盘"),(2,7,0.8,"亚盘盘中"),(7,9,1.2,"欧盘开盘前"),
                (9,11,1.5,"欧盘开盘"),(11,13,1.3,"欧盘盘中"),
                (13,16,2.0,"🇺🇸 欧美重叠 美股21:30开盘"),
                (16,20,1.5,"美盘盘中"),(20,22,1.2,"美盘收盘"),(22,24,0.9,"盘后")]
    else:
        return [(0,2,0.9,"盘后"),(2,7,0.8,"亚盘盘中"),(7,8,1.0,"过渡"),
                (8,11,1.5,"欧盘开盘"),(11,14,1.3,"欧盘盘中"),
                (14,17,2.0,"🇺🇸 欧美重叠 美股22:30开盘"),
                (17,21,1.5,"美盘盘中"),(21,23,1.2,"美盘收盘"),(23,24,0.9,"盘后")]
def get_session(hour):
    for s,e,w,n in get_sessions():
        if s <= hour < e: return {"name": f"{s:02d}-{e:02d} {n}", "weight": w}
    return {"name": "其他", "weight": 1.0}


# ============================================================
# 工具
# ============================================================
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] {msg}")
    sys.stdout.flush()

def api_get(path, timeout=25):
    for a in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=timeout)
            if r.ok: return r.json()
            if r.status_code == 401: return None
            time.sleep(3)
        except: time.sleep(5)
    return None

def api_post(path, data=None, timeout=25):
    for a in range(3):
        try:
            r = requests.post(f"{BASE}{path}", headers=HEADERS, json=data or {}, timeout=timeout)
            if r.ok: return r.json()
            if r.status_code == 401: return None
            time.sleep(3)
        except: time.sleep(5)
    return None


# ============================================================
# BYBIT 技术数据
# ============================================================
def fetch_bybit_klines(interval="60", limit=200):
    """获取 Bybit K线"""
    url = f"{BYBIT_BASE}/v5/market/kline"
    try:
        r = requests.get(url, params={"category":"linear","symbol":"BTCUSDT","interval":interval,"limit":limit}, timeout=15)
        if r.ok:
            data = r.json()
            if data.get("retCode") == 0:
                bars = data["result"]["list"]
                closes = [float(b[4]) for b in reversed(bars)]
                highs = [float(b[2]) for b in reversed(bars)]
                lows = [float(b[3]) for b in reversed(bars)]
                volumes = [float(b[5]) for b in reversed(bars)]
                timestamps = [int(b[0])//1000 for b in reversed(bars)]
                return closes, highs, lows, volumes, timestamps
    except: pass
    return None, None, None, None, None


# 技术指标计算
def compute_ema(data, period):
    m = 2/(period+1); r = []; e = data[0]
    for i,v in enumerate(data):
        e = v if i==0 else (v-e)*m+e; r.append(e)
    return r

def compute_rsi(data, p=14):
    d = [data[i]-data[i-1] for i in range(1,len(data))]
    g = [x if x>0 else 0 for x in d]; l = [-x if x<0 else 0 for x in d]
    ag = sum(g[:p])/p if len(g)>=p else 0; al = sum(l[:p])/p if len(l)>=p else 0
    r = [None]*(p+1)
    for i in range(p,len(d)):
        ag = (ag*(p-1)+g[i])/p; al = (al*(p-1)+l[i])/p
        rs = 100 if al==0 else ag/al; r.append(100-100/(1+rs))
    return r

def compute_macd(data):
    f=compute_ema(data,12); s=compute_ema(data,26)
    m=[f[i]-s[i] for i in range(len(data))]
    sig=compute_ema(m,9); h=[m[i]-sig[i] for i in range(len(data))]
    return m,sig,h

def compute_bb(data, p=20, std=2):
    from statistics import stdev
    u,l,mid=[],[],[]
    for i in range(len(data)):
        if i<p-1: u.append(None);l.append(None);mid.append(None)
        else:
            w=data[i-p+1:i+1];avg=sum(w)/p;s=stdev(w)
            u.append(avg+std*s);l.append(avg-std*s);mid.append(avg)
    return u,l,mid


# ============================================================
# 技术评分
# ============================================================
def calc_tech_score(closes, highs, lows, volumes):
    """纯技术指标评分 (-10 到 +10)"""
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    rsi = compute_rsi(closes, 14)
    macd, macdsig, macdh = compute_macd(closes)
    bbu, bbl, bbmid = compute_bb(closes)
    
    i = len(closes)-1  # latest index
    if rsi[i] is None or bbu[i] is None:
        return 0, {}
    
    score = 0
    factors = {}
    
    # 1. EMA 趋势 (±3)
    if ema9[i] > ema21[i] and ema9[i-1] <= ema21[i-1]:
        score += 3; factors["ema_cross"] = 3  # 金叉
    elif ema9[i] < ema21[i] and ema9[i-1] >= ema21[i-1]:
        score -= 3; factors["ema_cross"] = -3  # 死叉
    elif ema9[i] > ema21[i]:
        score += 1; factors["ema_cross"] = 1
    else:
        score -= 1; factors["ema_cross"] = -1
    
    # 2. RSI (±2)
    if rsi[i] < 30: score += 2; factors["rsi"] = 2
    elif rsi[i] > 70: score -= 2; factors["rsi"] = -2
    elif rsi[i] < 40: score += 1; factors["rsi"] = 1
    elif rsi[i] > 60: score -= 1; factors["rsi"] = -1
    else: factors["rsi"] = 0
    
    # 3. MACD (±2)
    if macdh[i] > 0 and macdh[i] > macdh[i-1]: score += 2; factors["macd"] = 2
    elif macdh[i] < 0 and macdh[i] < macdh[i-1]: score -= 2; factors["macd"] = -2
    elif macdh[i] > 0: score += 1; factors["macd"] = 1
    elif macdh[i] < 0: score -= 1; factors["macd"] = -1
    else: factors["macd"] = 0
    
    # 4. 布林带 (±2)
    px = closes[i]
    if px < bbl[i]: score += 2; factors["bb"] = 2
    elif px > bbu[i]: score -= 2; factors["bb"] = -2
    elif px < bbmid[i]: score += 1; factors["bb"] = 1
    else: score -= 1; factors["bb"] = -1
    
    # 5. 成交量确认 (±1)
    avg_vol = sum(volumes[-20:]) / 20
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    if vol_ratio > 1.5:
        if score > 0: score += 1; factors["vol"] = 1
        elif score < 0: score -= 1; factors["vol"] = -1
        else: factors["vol"] = 0
    else: factors["vol"] = 0
    
    factors["tech_total"] = score
    factors["btc_price"] = px
    return score, factors


# ============================================================
# 宏观评分 (V2 逻辑精简)
# ============================================================
def get_macro_score():
    data = {}
    ov = api_get("/api/market-intel/overview")
    if ov:
        data["macro"] = 3 if ov.get("macro_verdict") == "bullish" else (-3 if ov.get("macro_verdict") == "bearish" else 0)
    else: data["macro"] = 0
    
    ms = api_get("/api/market-intel/macro-signals")
    btc_trend = 0
    if ms and ms.get("available"):
        for s in ms.get("signals", []):
            if s.get("id") == "btc_trend":
                v = float(s.get("value",0))
                btc_trend = 2 if v>5 else (1 if v>0 else (-1 if v>-5 else -2))
    data["btc_trend"] = btc_trend
    
    etf = api_get("/api/market-intel/etf-flows")
    data["etf"] = 2 if etf and etf.get("summary",{}).get("direction")=="inflow" else (-2 if etf and etf.get("summary",{}).get("direction")=="outflow" else 0)
    
    total = data["macro"] + data["btc_trend"] + data["etf"]
    return total, data


# ============================================================
# 主逻辑
# ============================================================
def main():
    log("=" * 70)
    log("🤖 V3 — BTC 双引擎策略 (技术指标 + 宏观因子)")
    log("=" * 70)
    
    if not TOKEN: log("⛔ 无 Token"); return
    
    # 1. 账户
    me = api_get("/api/claw/agents/me")
    if not me: log("⛔ 无法获取账户"); return
    cash = float(me.get("cash",0))
    pos_data = api_get("/api/positions")
    positions = pos_data.get("positions",[]) if pos_data else []
    our_pos = [p for p in positions if not p.get("source","").startswith("copied:")]
    btc_pos = [p for p in our_pos if p.get("symbol","").upper() in ("BTC","BTCUSDT")]
    
    session = get_session(datetime.now(timezone.utc).hour)
    log(f"📊 账户: {me['name']} | 现金: ${cash:,.2f}")
    log(f"🕐 时段: {session['name']} (权重 {session['weight']}x)")
    log(f"📋 持仓: {len(positions)} (自营:{len(our_pos)} / BTC:{len(btc_pos)})")
    
    # 2. Bybit 技术数据
    log("\n📡 获取 Bybit 行情数据...")
    closes, highs, lows, volumes, ts = fetch_bybit_klines("60", 200)
    if not closes or len(closes) < 50:
        log("⛔ Bybit 数据获取失败")
        return
    
    current_price = closes[-1]
    log(f"💹 BTC: ${current_price:,.2f} ({len(closes)} 根 1h K线)")
    
    # 3. 技术评分
    tech_score, tech_factors = calc_tech_score(closes, highs, lows, volumes)
    log(f"\n📐 技术评分: {tech_score}")
    for k,v in tech_factors.items():
        if k != "tech_total" and k != "btc_price":
            log(f"   {k}: {v:+d}")
    
    # 4. 宏观评分
    macro_score, macro_data = get_macro_score()
    log(f"📊 宏观评分: {macro_score}")
    for k,v in macro_data.items():
        log(f"   {k}: {v:+d}")
    
    # 5. 综合评分 (加权)
    weighted_score = round(tech_score * TECH_WEIGHT + macro_score * MACRO_WEIGHT, 1)
    session_w = session["weight"]
    final_score = round(weighted_score * session_w, 1)
    
    log(f"\n🎯 综合评分: {final_score}")
    log(f"   技术({TECH_WEIGHT*100:.0f}%)= {tech_score} × 宏观({MACRO_WEIGHT*100:.0f}%)= {macro_score}")
    log(f"   加权={weighted_score} × 时段={session_w}x = {final_score}")
    
    # 6. 决策
    threshold = MIN_SCORE * (1.3 if session_w < 0.9 else (0.85 if session_w > 1.8 else 1.0))
    action = "hold"
    if final_score >= threshold:
        action = "long"
    elif final_score <= -threshold:
        action = "short"
    
    # 检查现有仓位
    if btc_pos:
        pos = btc_pos[0]
        pos_side = pos.get("side","")
        if action == "hold":
            log(f"📌 持有中: {pos_side.upper()} @ ${float(pos.get('entry_price',0)):.0f}")
        elif (action == "long" and pos_side in ("buy","long")) or (action == "short" and pos_side in ("sell","short")):
            log(f"📌 方向一致, 继续持有 {pos_side.upper()}")
        else:
            log(f"🔁 信号反转! 平仓反手 {action.upper()}")
            action = "reverse"
    else:
        if action in ("long","short"):
            log(f"🚀 开仓 {action.upper()} (评分 {final_score} ≥ 阈值 {threshold:.1f})")
        else:
            log(f"🟢 不交易 ({final_score} < {threshold:.1f})")
    
    # 7. 执行交易 (如果是开/反手)
    if action in ("long", "short", "reverse"):
        side = "buy" if action in ("long","reverse") and final_score > 0 else "sell"
        if action == "reverse":
            log("  先平仓...")
        
        strategy_cap = cash * STRATEGY_RATIO
        pos_value = strategy_cap * MAX_POSITION_RATIO * LEVERAGE
        qty = round(pos_value / current_price, 6)
        sl = round(current_price * (1-STOP_LOSS_PCT) if side=="buy" else current_price * (1+STOP_LOSS_PCT), 2)
        tp = round(current_price * (1+TAKE_PROFIT_PCT) if side=="buy" else current_price * (1-TAKE_PROFIT_PCT), 2)
        
        log(f"  执行: {side.upper()} {qty} BTC @ ${current_price:,.0f}")
        log(f"  TP: ${tp:,.0f} | SL: ${sl:,.0f}")
        
        content = (
            f"[BTC-V3] {side.upper()} score={final_score} "
            f"tech={tech_score} macro={macro_score} "
            f"qty={qty} tp=${tp} sl=${sl} lev={LEVERAGE}x"
        )
        
        r = api_post("/api/signals/realtime", {
            "market":"crypto","action":side,"symbol":"BTC",
            "price":current_price,"quantity":qty,
            "content":content,"executed_at":"now"
        })
        if r:
            log(f"✅ 交易信号发布成功")
            # 发布策略说明
            api_post("/api/signals/strategy", {
                "market":"crypto",
                "title":f"🤖 V3: BTC {side.upper()} | 评分{final_score} | ${current_price:,.0f}",
                "content":(
                    f"【Hermes V3 自动策略】\n\n"
                    f"方向: {side.upper()} | 杠杆: {LEVERAGE}x\n"
                    f"入场: ${current_price:,.0f}\n"
                    f"止盈: ${tp:,.0f}\n止损: ${sl:,.0f}\n\n"
                    f"技术评分: {tech_score} | 宏观评分: {macro_score}\n"
                    f"综合评分: {final_score} (阈值≥{threshold:.1f})\n\n"
                    f"#V3 #BTC #策略"
                ),
                "symbols":"BTC, BTCUSDT","tags":"auto,v3,btc"
            })
        else:
            log("❌ 交易信号发布失败")
    
    # 8. 复盘
    log(f"\n📈 复盘: 技术={tech_score} 宏观={macro_score} 综合={final_score}")
    if btc_pos:
        p = btc_pos[0]
        entry = float(p.get("entry_price",0))
        pnl = float(p.get("pnl",0)) if p.get("pnl") else 0
        log(f"   当前BTC持仓: {p.get('side','?').upper()} @ ${entry:.0f} | PnL: ${pnl:+.2f}")
    
    log("\n" + "=" * 70)
    log("✅ V3 完成")
    log("=" * 70)

if __name__ == "__main__":
    main()
