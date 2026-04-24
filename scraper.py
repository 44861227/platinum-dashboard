#!/usr/bin/env python3
"""
铂金价格自动抓取脚本
每日收盘后由 GitHub Actions 自动运行
"""

import json
import datetime
import os
import sys
import urllib.request
import urllib.error
import ssl
import time

DATA_FILE = "data.json"
TODAY = datetime.date.today().strftime("%Y-%m-%d")
NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 忽略 SSL 证书错误（部分 API 需要）
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

def fetch_url(url, headers=None, timeout=15):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36")
    req.add_header("Accept", "application/json,text/html,*/*")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)

# ── 1. 汇率 ──────────────────────────────────────────────────────
def fetch_rate():
    print("正在抓取汇率...")
    sources = [
        ("frankfurter.app", "https://api.frankfurter.app/latest?from=USD&to=CNY",
         lambda d: float(d["rates"]["CNY"])),
        ("exchangerate-api", "https://api.exchangerate-api.com/v4/latest/USD",
         lambda d: float(d["rates"]["CNY"])),
        ("open.er-api", "https://open.er-api.com/v6/latest/USD",
         lambda d: float(d["rates"]["CNY"])),
    ]
    for name, url, parser in sources:
        try:
            data = fetch_url(url)
            rate = parser(data)
            print(f"  ✅ 汇率({name}): 1 USD = {rate} CNY")
            return rate
        except Exception as e:
            print(f"  ⚠ {name} 失败: {e}")
            time.sleep(1)
    print("  ⚠ 所有汇率接口失败，使用默认值 7.20")
    return 7.20

# ── 2. LBMA 铂金价格 ──────────────────────────────────────────────
def fetch_lbma():
    print("正在抓取 LBMA 铂金价格...")

    # 接口1: kitco metals API
    try:
        data = fetch_url("https://proxy.kitco.com/getPM?symbol=PT&unit=toz&currency=USD",
                         headers={"Origin": "https://www.kitco.com", "Referer": "https://www.kitco.com/"})
        if data and "price" in str(data):
            pm = float(data.get("price") or data.get("ask") or data.get("bid"))
            if pm > 100:
                print(f"  ✅ LBMA(kitco): {pm} USD/oz")
                return {"pm": pm, "am": round(pm - 3, 2)}
    except Exception as e:
        print(f"  ⚠ kitco 失败: {e}")

    # 接口2: metalpriceapi (免费额度)
    try:
        data = fetch_url("https://api.metalpriceapi.com/v1/latest?api_key=demo&base=XPT&currencies=USD")
        if data and data.get("success"):
            rate_xpt = float(data["rates"].get("USD", 0))
            if rate_xpt > 100:
                print(f"  ✅ LBMA(metalpriceapi): {rate_xpt} USD/oz")
                return {"pm": rate_xpt, "am": round(rate_xpt - 3, 2)}
    except Exception as e:
        print(f"  ⚠ metalpriceapi 失败: {e}")

    # 接口3: 抓取 stooq.com 铂金报价页面
    try:
        req = urllib.request.Request("https://stooq.com/q/l/?s=xptusd&f=sd2t2ohlcv&h&e=csv")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15, context=CTX) as resp:
            lines = resp.read().decode().strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split(",")
                pm = float(parts[4])  # close price
                if pm > 100:
                    print(f"  ✅ LBMA(stooq): {pm} USD/oz")
                    return {"pm": pm, "am": round(pm - 3, 2)}
    except Exception as e:
        print(f"  ⚠ stooq 失败: {e}")

    # 接口4: Yahoo Finance 铂金 (XPT=X)
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/XPT%3DX?interval=1d&range=1d"
        data = fetch_url(url, headers={"Accept": "application/json"})
        pm = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
        if pm > 100:
            print(f"  ✅ LBMA(yahoo): {pm} USD/oz")
            return {"pm": pm, "am": round(pm - 3, 2)}
    except Exception as e:
        print(f"  ⚠ yahoo finance 失败: {e}")

    # 接口5: 从现有 data.json 读取最后一条作为保底
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        last_pm = existing["latest"]["lbma"]["pm"]
        print(f"  ⚠ 所有接口失败，沿用上次数据: {last_pm} USD/oz")
        return {"pm": last_pm, "am": round(last_pm - 3, 2)}
    except Exception as e:
        print(f"  ⚠ 读取历史数据失败: {e}")

    return None

# ── 3. SGE 铂金价格 ───────────────────────────────────────────────
def fetch_sge(lbma_pm, rate):
    print("正在抓取 SGE 铂金价格...")

    # 接口1: 新浪财经 铂金行情
    try:
        url = "https://hq.sinajs.cn/list=nf_PT9995"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://finance.sina.com.cn/")
        with urllib.request.urlopen(req, timeout=10, context=CTX) as resp:
            text = resp.read().decode("gbk", errors="ignore")
        import re
        m = re.search(r'"([^"]+)"', text)
        if m:
            parts = m.group(1).split(",")
            if len(parts) >= 4:
                close = float(parts[3])
                open_ = float(parts[1])
                high  = float(parts[4]) if len(parts) > 4 else close * 1.003
                low   = float(parts[5]) if len(parts) > 5 else close * 0.997
                if 100 < close < 5000:
                    change = round(close - open_, 2)
                    print(f"  ✅ SGE(新浪): {close} 元/克")
                    return {"close": close, "open": open_, "high": high, "low": low,
                            "change": change, "weightedAvg": close, "volume": 0}
    except Exception as e:
        print(f"  ⚠ 新浪财经 失败: {e}")

    # 接口2: 东方财富 铂金
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get?secid=110.Pt9995&fields=f43,f44,f45,f46,f170"
        data = fetch_url(url, headers={"Referer": "https://quote.eastmoney.com/"})
        d = data.get("data", {})
        close = d.get("f43", 0) / 100 if d.get("f43") else 0
        if close > 100:
            open_ = d.get("f46", close * 100) / 100
            high  = d.get("f44", close * 100) / 100
            low   = d.get("f45", close * 100) / 100
            change = round(close - open_, 2)
            print(f"  ✅ SGE(东方财富): {close} 元/克")
            return {"close": close, "open": open_, "high": high, "low": low,
                    "change": change, "weightedAvg": close, "volume": 0}
    except Exception as e:
        print(f"  ⚠ 东方财富 失败: {e}")

    # 保底: LBMA 换算
    if lbma_pm and rate:
        estimated = round(lbma_pm * rate / 31.1035, 2)
        print(f"  ⚠ SGE接口全部失败，使用LBMA换算估算: {estimated} 元/克")
        return {"close": estimated, "open": estimated, "high": estimated, "low": estimated,
                "change": 0, "weightedAvg": estimated, "volume": 0, "estimated": True}

    # 最终保底：读历史数据
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
        last = existing["latest"]["sge"]
        print(f"  ⚠ 沿用上次SGE数据: {last['close']} 元/克")
        return last
    except:
        pass

    return None

# ── 4. 更新 data.json ─────────────────────────────────────────────
def update_data(sge, lbma, rate):
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        data = {"history": [], "monthly2025": {"lbmaCny": [], "sge": []},
                "yearly": {"years": ["2022","2023","2024","2025","2026"],
                           "sgeAvg": [217.29, 227.76, 229.08, 316.61, None]}}

    data["latest"] = {
        "date": TODAY, "updatedAt": NOW,
        "sge": sge, "lbma": lbma, "rate": rate
    }

    history = data.get("history", [])
    lbma_cny = round(lbma["pm"] * rate / 31.1035, 2)
    today_entry = {"date": TODAY, "sge": sge["close"], "lbmaCny": lbma_cny}

    existing = {h["date"]: i for i, h in enumerate(history)}
    if TODAY in existing:
        history[existing[TODAY]] = today_entry
        print(f"  更新 {TODAY} 历史记录")
    else:
        history.append(today_entry)
        print(f"  追加 {TODAY} 到历史记录（共 {len(history)} 条）")

    data["history"] = sorted(history, key=lambda x: x["date"])[-120:]

    # 更新2026年均价
    data_2026 = [h for h in data["history"] if h["date"].startswith("2026-")]
    if data_2026:
        avg = round(sum(h["sge"] for h in data_2026) / len(data_2026), 2)
        years = data["yearly"]["years"]
        avgs  = data["yearly"]["sgeAvg"]
        if "2026" in years:
            avgs[years.index("2026")] = avg

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ data.json 更新成功！日期：{TODAY}，汇率：{rate}，LBMA：{lbma['pm']}，SGE：{sge['close']}")

# ── 主流程 ────────────────────────────────────────────────────────
def main():
    print(f"=== 铂金数据抓取脚本 | {NOW} ===\n")

    rate = fetch_rate()
    lbma = fetch_lbma()
    sge  = fetch_sge(lbma["pm"] if lbma else None, rate)

    if not lbma:
        print("\n❌ LBMA 数据完全缺失且无历史备份，退出")
        sys.exit(1)

    if not sge:
        sge = {"close": round(lbma["pm"] * rate / 31.1035, 2),
               "open": 0, "high": 0, "low": 0, "change": 0,
               "weightedAvg": 0, "volume": 0, "estimated": True}

    update_data(sge, lbma, rate)

if __name__ == "__main__":
    main()
