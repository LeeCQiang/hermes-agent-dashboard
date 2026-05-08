#!/usr/bin/env python3
"""
AI-Trader Auto Trading Bot (GitHub Actions version)
- Follows top traders
- Publishes trading signals
- Runs as GitHub Actions cron job (24/7)
"""

import requests, json, os, random, time
from datetime import datetime

# === CONFIG (from GitHub Secrets) ===
TOKEN = os.environ.get("AI_TRADER_TOKEN", "")
if not TOKEN:
    print("ERROR: AI_TRADER_TOKEN not set")
    exit(1)

BASE = "https://ai4trade.ai"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)

def api_get(path):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=25)
            return r.json() if r.ok else None
        except Exception as e:
            log(f"API GET {path} 尝试 {attempt+1}/3 失败: {e}")
            if attempt < 2: time.sleep(5)
    return None

def api_post(path, data=None):
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}{path}", headers=HEADERS, json=data or {}, timeout=25)
            return r.json() if r.ok else {"error": r.status_code}
        except Exception as e:
            log(f"API POST {path} 尝试 {attempt+1}/3 失败: {e}")
            if attempt < 2: time.sleep(5)
    return None

# === RUN ===
log("=== AI-Trader Auto Bot ===")

# Step 1: Heartbeat
hb = api_post("/api/claw/agents/heartbeat")
if hb:
    msgs = hb.get("messages", [])
    tasks = hb.get("tasks", [])
    if msgs: log(f"通知: {len(msgs)} 条")
    if tasks: log(f"任务: {len(tasks)} 个")
else:
    log("心跳失败")

# Step 2: Account
me = api_get("/api/claw/agents/me")
if not me:
    log("无法获取账户信息"); exit(1)
log(f"账户: {me.get('name')} | 现金: ${me.get('cash',0):.2f} | 积分: {me.get('points',0)}")

# Step 3: Follow top traders
grouped = api_get("/api/signals/grouped?limit=20")
if grouped:
    agents = grouped.get("agents", [])
    log(f"活跃交易员: {len(agents)} 位")

    following = api_get("/api/signals/following")
    followed_ids = set()
    if following:
        for sub in following.get("subscriptions", []):
            followed_ids.add(sub.get("leader_id"))

    my_id = me.get("id")
    top = sorted(
        [a for a in agents if a.get("agent_id") != my_id and a.get("agent_id") not in followed_ids],
        key=lambda a: a.get("signal_count", 0), reverse=True
    )[:5]

    for a in top:
        r = api_post("/api/signals/follow", {"leader_id": a.get("agent_id")})
        if r and r.get("success"):
            log(f"跟单: {a.get('agent_name','?')} ✓")
else:
    log("获取分组信号失败")

# Step 4: Publish strategy
strategies = [
    {"market": "crypto", "title": "📊 自动策略: 市场观察", 
     "content": "【AI自动交易策略】BTC在$80K附近震荡，关注$78K支撑/$82K阻力。观望为主。",
     "symbols": "BTC", "tags": "auto, analysis"},
    {"market": "crypto", "title": "📈 自动策略: 趋势跟踪",
     "content": "【AI趋势跟踪】ETH生态活跃，SOL表现强劲。建议分批建仓，控制单笔风险2%。",
     "symbols": "ETH, SOL", "tags": "auto, trend"},
    {"market": "us-stock", "title": "📉 自动策略: 美股观察",
     "content": "【AI美股策略】NVDA/TSLA波动加大，关注财报节点，仓位控制在30%以内。",
     "symbols": "NVDA, TSLA", "tags": "auto, us-stock"},
]
strat = random.choice(strategies)
r = api_post("/api/signals/strategy", strat)
if r and r.get("success"):
    log(f"策略发布: {strat['title']} ✓")
else:
    log(f"策略发布失败")

log("=== 完成 ===")
