#!/usr/bin/env python3
"""
Probe 16b (active / QDII / FOF funds): find each fund's REAL index from its name (passive: 跟踪标的; active: the main equity index of 业绩比较基准),
by Eastmoney search → candidate index codes → price (CSI close + PE, else Eastmoney K-line) → R² against the fund's NAV.
Only funds whose current mapping (data/funds_map.json) is not \"tracked\". Read-only; stops starting new funds after 8 min.
Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
"""
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
T0 = time.monotonic()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(url, ref, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def f10(code):
    t = get(f"https://fundf10.eastmoney.com/jbgk_{code}.html", "https://fundf10.eastmoney.com/")
    out = {}
    for k in ("跟踪标的", "业绩比较基准"):
        m = re.search(k + r"</th>\s*<td[^>]*>(.*?)</td>", t, re.S)
        out[k] = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    return out


def bench_main(text):
    """业绩比较基准 → the equity index with the largest weight (no bond / deposit / rate parts)."""
    best = None
    for part in re.split(r"[+＋]", text):
        if any(w in part for w in ("债", "存款", "利率", "货币", "国债")) or "指数" not in part:
            continue
        m = re.search(r"([\d.]+)\s*%", part)
        w = float(m.group(1)) if m else 100.0
        name = re.split(r"指数", part)[0] + "指数"
        name = re.sub(r"^[\s(（]*", "", name).replace("经汇率调整后的", "").strip()
        if best is None or w > best[1]:
            best = (name, w)
    return best


def clean(name):
    n = re.sub(r"[（(][^）)]*[）)]", "", name)
    for w in ("收益率", "人民币", "港元", "（", "）", "*", "×"):
        n = n.replace(w, "")
    return n.replace("指数", "").strip()


def search(name):
    j = json.loads(get("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(name)
                       + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=20", "https://www.eastmoney.com/"))
    return [(r.get("Code"), r.get("Name"), r.get("QuoteID")) for r in ((j.get("QuotationCodeTable") or {}).get("Data") or [])
            if r.get("SecurityTypeName") == "指数"][:3]


def series(code, qid):
    """{date: close}, PE start date (CSI only), source."""
    start = (datetime.now(BJ) - timedelta(days=420)).strftime("%Y%m%d")
    if re.fullmatch(r"[0-9A-Z]{6}", str(code)):
        try:
            j = json.loads(get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate=20100101"
                               f"&endDate={datetime.now(BJ).strftime('%Y%m%d')}", "https://www.csindex.com.cn/", timeout=40))
            d = j.get("data") or []
            if d:
                pe = next((x["tradeDate"] for x in d if x.get("peg") not in (None, "", 0)), None)
                return {f"{x['tradeDate'][:4]}-{x['tradeDate'][4:6]}-{x['tradeDate'][6:]}": float(x["close"]) for x in d[-300:] if x.get("close")}, pe, "CSI"
        except Exception:
            pass
    j = json.loads(get(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={qid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
                       f"&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg={start}&end=20500101&lmt=400", "https://quote.eastmoney.com/", timeout=40))
    k = ((j.get("data") or {}).get("klines")) or []
    return {x.split(",")[0]: float(x.split(",")[1]) for x in k}, None, "EM"


def nav(code):
    t = get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", "https://fund.eastmoney.com/", timeout=40)
    m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", t, re.S)
    return {datetime.fromtimestamp(int(x[0]) / 1000, BJ).date().isoformat(): float(x[1]) for x in (json.loads(m.group(1)) if m else []) if x[1]}


def r2(a, b):
    common = sorted(set(a) & set(b))[-251:]
    if len(common) < 120:
        return None
    x = [math.log(a[common[k]] / a[common[k - 1]]) for k in range(1, len(common))]
    y = [math.log(b[common[k]] / b[common[k - 1]]) for k in range(1, len(common))]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
    sxx, syy = sum((p - mx) ** 2 for p in x), sum((q - my) ** 2 for q in y)
    return None if sxx <= 0 or syy <= 0 else round(100 * sxy * sxy / (sxx * syy), 1)


def main():
    print(f"Probe 16b at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    fmap = json.load(open(os.path.join(ROOT, "data", "funds_map.json"), encoding="utf-8"))["funds"]
    todo = [c for c, f in fmap.items() if f.get("method") != "tracked" and f.get("kind") in ("active", "fof", "qdii", "other")]   # part b: active / QDII / FOF funds
    print(f"{len(todo)} funds to check\n")
    summary = []
    for code in todo:
        if time.monotonic() - T0 > 480:
            print("time budget used up")
            break
        f = fmap[code]
        try:
            info = f10(code)
            if f["kind"] in ("passive", "enhanced", "etf"):
                target, src = info["跟踪标的"], "跟踪标的"
            else:
                b = bench_main(info["业绩比较基准"])
                target, src = (b[0] if b else ""), f"基准 {b[1]:g}%" if b else "基准"
            if not target or "无跟踪标的" in target:
                print(f"{code} {f['name'][:14]} [{f['kind']}]: no target ({info['业绩比较基准'][:60]})")
                continue
            q = clean(target)
            cands = search(q) or search(q[:6])
            fn = nav(code)
            res = []
            for c, n, qid in cands:
                try:
                    s, pe, srcname = series(c, qid)
                    res.append((c, n, qid, srcname, pe, r2(fn, s)))
                except Exception as e:
                    res.append((c, n, qid, "×", None, None))
                time.sleep(0.3)
            best = max(res, key=lambda x: x[5] or -1) if res else None
            print(f"{code} {f['name'][:14]} [{f['kind']}] {src}: \"{target[:30]}\" → search \"{q}\": "
                  + "; ".join(f"{c} {n} {s}{' PE ' + pe if pe else ''} R² {r}" for c, n, qid, s, pe, r in res)
                  + f"  | now {f.get('index')} ({f.get('method')}, R² {f.get('r2')})")
            summary.append((code, best))
        except Exception as e:
            print(f"{code}: ❌ {type(e).__name__}: {str(e)[:100]}")
        time.sleep(0.5)
    ok = sum(1 for _, b in summary if b and b[5] and b[5] >= 80)
    print(f"\n{len(summary)} checked; best candidate R² ≥ 80 %: {ok}; took {time.monotonic() - T0:.0f}s")
    print("Probe 16b done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
