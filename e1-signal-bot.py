#!/usr/bin/env python3
"""
E1 Signal Bot — Heikin Ashi + EMA50 + Stoch RSI 趋势交易信号检测
每1H运行一次，检查BTC/USDT最新K线是否触发开仓信号，推送到Telegram
"""
import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ── 配置 ──
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "845741348")
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
STATE_FILE = "/tmp/e1_state.json"

# ── 策略参数 ──
EMA_PERIOD = 50
STOCH_K = 14
STOCH_D = 3
STOCH_SMOOTH = 3
OVERSOLD = 20
OVERBOUGHT = 80

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def send_telegram(text):
    if not TELEGRAM_TOKEN:
        log("跳过通知: 未配置TELEGRAM_BOT_TOKEN")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if r.ok:
            log(f"Telegram推送成功: {text[:60]}...")
            return True
        else:
            log(f"Telegram推送失败: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        log(f"Telegram请求异常: {e}")
        return False

def fetch_binance_klines(symbol, interval, limit=200):
    """从Binance获取K线数据"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            log(f"Binance API错误: {r.status_code} {r.text[:100]}")
            return None
        klines = r.json()
        rows = []
        for k in klines:
            rows.append({
                "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5])
            })
        return rows
    except Exception as e:
        log(f"获取K线异常: {e}")
        return None

def calc_ha(rows):
    """计算Heikin Ashi"""
    for i, row in enumerate(rows):
        if i == 0:
            row["ha_open"] = row["open"]
        else:
            row["ha_open"] = (rows[i-1]["ha_open"] + rows[i-1]["ha_close"]) / 2
        row["ha_close"] = (row["open"] + row["high"] + row["low"] + row["close"]) / 4
        row["ha_high"] = max(row["high"], row["ha_open"], row["ha_close"])
        row["ha_low"] = min(row["low"], row["ha_open"], row["ha_close"])
        row["ha_bullish"] = row["ha_close"] > row["ha_open"]
    return rows

def calc_ema(rows, period):
    """计算EMA"""
    if len(rows) < period:
        return rows
    multiplier = 2 / (period + 1)
    rows[0]["ema"] = rows[0]["close"]
    for i in range(1, len(rows)):
        rows[i]["ema"] = (rows[i]["close"] - rows[i-1].get("ema", rows[i]["close"])) * multiplier + rows[i-1].get("ema", rows[i]["close"])
    return rows

def calc_stoch_rsi(rows, k_period=14, d_period=3, smooth=3):
    """计算Stoch RSI %K and %D"""
    if len(rows) < k_period + smooth + d_period:
        return rows

    closes = [r["close"] for r in rows]

    # 计算RSI
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_values = [50.0] * len(closes)
    for i in range(k_period, len(closes)):
        avg_gain = sum(gains[i-k_period:i]) / k_period
        avg_loss = sum(losses[i-k_period:i]) / k_period
        if avg_loss == 0:
            rsi_values[i] = 100
        else:
            rs = avg_gain / avg_loss
            rsi_values[i] = 100 - (100 / (1 + rs))

    # 计算Stoch of RSI
    stoch_values = [50.0] * len(closes)
    for i in range(k_period, len(closes)):
        low_rsi = min(rsi_values[i-k_period+1:i+1])
        high_rsi = max(rsi_values[i-k_period+1:i+1])
        if high_rsi - low_rsi == 0:
            stoch_values[i] = 50
        else:
            stoch_values[i] = (rsi_values[i] - low_rsi) / (high_rsi - low_rsi) * 100

    # 平滑
    k_line = [50.0] * len(closes)
    for i in range(smooth - 1, len(closes)):
        k_line[i] = sum(stoch_values[i-smooth+1:i+1]) / smooth

    d_line = [50.0] * len(closes)
    for i in range(d_period - 1, len(closes)):
        d_line[i] = sum(k_line[i-d_period+1:i+1]) / d_period

    for i, row in enumerate(rows):
        row["stoch_k"] = k_line[i]
        row["stoch_d"] = d_line[i]

    return rows

def detect_signals(rows):
    """检查最新K线是否触发信号"""
    if len(rows) < EMA_PERIOD + 20:
        return []

    signals = []
    row = rows[-1]       # 最新完整K线（或当前）
    prev = rows[-2]      # 前一根

    price = row["close"]
    ema_val = row["ema"]

    # 做多信号
    if price > ema_val and row["ha_bullish"]:
        if prev["stoch_k"] <= OVERSOLD and row["stoch_k"] > OVERSOLD:
            signals.append({
                "direction": "LONG",
                "time": row["time"].isoformat(),
                "price": round(price, 2),
                "ema50": round(ema_val, 2),
                "stoch_k": round(row["stoch_k"], 1),
                "reason": f"EMA50上方 + HA看多 + StochRSI上穿{OVERSOLD}"
            })

    # 做空信号
    if price < ema_val and not row["ha_bullish"]:
        if prev["stoch_k"] >= OVERBOUGHT and row["stoch_k"] < OVERBOUGHT:
            signals.append({
                "direction": "SHORT",
                "time": row["time"].isoformat(),
                "price": round(price, 2),
                "ema50": round(ema_val, 2),
                "stoch_k": round(row["stoch_k"], 1),
                "reason": f"EMA50下方 + HA看空 + StochRSI下穿{OVERBOUGHT}"
            })

    return signals

def load_state():
    """读取上次通知的K线时间"""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"last_signal_time": ""}

def save_state(state):
    """保存通知状态"""
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def format_signal_msg(sig):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    return (
        f"{emoji} <b>E1 策略信号: {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"时间: {sig['time']}\n"
        f"价格: ${sig['price']}\n"
        f"EMA50: ${sig['ema50']}\n"
        f"StochRSI: {sig['stoch_k']}\n"
        f"依据: {sig['reason']}\n"
        f"\n#E1 #BTC #{sig['direction']}"
    )

def main():
    log("=== E1 Signal Bot Run ===")
    log(f"符号: {SYMBOL}, 周期: {INTERVAL}")

    # 获取数据
    rows = fetch_binance_klines(SYMBOL, INTERVAL, 200)
    if not rows or len(rows) < 60:
        log("数据不足，跳过")
        return

    log(f"获取 {len(rows)} 根K线: {rows[0]['time'].strftime('%m-%d %H:%M')} ~ {rows[-1]['time'].strftime('%m-%d %H:%M')}")

    # 计算指标
    rows = calc_ha(rows)
    rows = calc_ema(rows, EMA_PERIOD)
    rows = calc_stoch_rsi(rows, STOCH_K, STOCH_D, STOCH_SMOOTH)

    # 检测信号
    signals = detect_signals(rows)

    if not signals:
        log("当前无新信号")
        return

    # 去重: 只通知未通知过的K线
    state = load_state()
    new_signals = [s for s in signals if s["time"] != state.get("last_signal_time")]

    if not new_signals:
        log("信号已通知过，跳过")
        return

    # 推送
    for sig in new_signals:
        msg = format_signal_msg(sig)
        log(f"检测到信号: {sig['direction']} @ ${sig['price']}")
        send_telegram(msg)

    # 更新状态
    state["last_signal_time"] = new_signals[-1]["time"]
    save_state(state)
    log(f"状态已更新: last_signal_time = {state['last_signal_time']}")

    # 输出当前指标（供workflow日志查看）
    last = rows[-1]
    print(f"\n--- 当前指标 ---")
    print(f"价格: ${last['close']:.2f}")
    print(f"EMA50: ${last['ema']:.2f}")
    print(f"HA: {'看多' if last['ha_bullish'] else '看空'}")
    print(f"StochRSI %K: {last['stoch_k']:.1f}")
    print(f"StochRSI %D: {last['stoch_d']:.1f}")
    print(f"信号数: {len(new_signals)}")

if __name__ == "__main__":
    main()
