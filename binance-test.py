#!/usr/bin/env python3
"""Binance API Key 诊断脚本 — 在 GitHub Runner 上执行"""
import requests, json, os, hashlib, hmac, time, urllib.parse

API_KEY = os.environ.get("BINANCE_API_KEY", "")
SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
print(f"API_KEY set: {bool(API_KEY)}")
print(f"SECRET_KEY set: {bool(SECRET_KEY)}")
print(f"API_KEY length: {len(API_KEY)}")
print(f"SECRET_KEY length: {len(SECRET_KEY)}")

# Test 1: Public endpoint
print("\n=== Test 1: Public Ping ===")
try:
    r = requests.get("https://api.binance.com/api/v3/ping", timeout=10)
    print(f"Status: {r.status_code}, Body: {r.text}")
except Exception as e:
    print(f"Failed: {e}")

# Test 2: Public BTC price
print("\n=== Test 2: BTC Price ===")
try:
    r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=10)
    print(f"Status: {r.status_code}, Body: {r.text[:100]}")
except Exception as e:
    print(f"Failed: {e}")

# Test 3: Signed request
print("\n=== Test 3: Signed Account Info ===")
try:
    params = {"timestamp": int(time.time() * 1000)}
    query = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com/api/v3/account?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    r = requests.get(url, headers=headers, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Body: {r.text[:500]}")
except Exception as e:
    print(f"Failed: {e}")

# Test 4: Signed with recvWindow (sometimes needed)
print("\n=== Test 4: Signed + recvWindow ===")
try:
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 60000}
    query = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com/api/v3/account?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    r = requests.get(url, headers=headers, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Body: {r.text[:500]}")
except Exception as e:
    print(f"Failed: {e}")
