#!/usr/bin/env python3
"""
Probe 11b: can the app find a fund's 同花顺 hqcode by itself, the moment a fund is added? Read-only.
  A) JSONP variants of the lookup  B) the estimate API with fund codes
  C) a full hqcode table on GitHub: the list of all funds (Eastmoney) and a multi-code lookup at 同花顺
The lookup URL returns plain JSON (no CORS) — the browser can only read script-style answers (JSONP).
Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
"""
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def show(label, url, ref="https://fund.10jqka.com.cn/"):
    try:
        t0 = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
            cors = r.headers.get("Access-Control-Allow-Origin")
        print(f"  {label}: HTTP 200 {ctype!r} CORS={cors!r} {len(body)} bytes {time.monotonic() - t0:.1f}s; starts {body[:140]!r}")
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:150]}")
    time.sleep(0.7)


def main():
    print(f"Probe 11b at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    print("=" * 100)
    print("A) the hqcode lookup with JSONP-style parameters (a JSONP answer starts with the callback name)")
    base = "https://fund.10jqka.com.cn/data/client/myfund/001593"
    for q in ("", "?callback=cb123", "?jsonp=cb123", "?cb=cb123", "?jsoncallback=cb123", "?return=jsonp&jsonp=cb123"):
        show("myfund 001593" + q, base + q)
    print("=" * 100)
    print("B) the estimate API (script-loadable) with the fund code instead of the hqcode")
    d = datetime.now(BJ).strftime("%m%d")
    for name in ("vm_fd_JST320", "vm_fd_001593", "vm_fd_161725", "vm_fd_159601", "vm_fd_110022"):
        show(name, f"https://gz-fund.10jqka.com.cn/?module=api&controller=index&action=chart&info={name}&start={d}&_={int(time.time() * 1000)}")
    print("=" * 100)
    print("C) all funds: Eastmoney's full list, and 同花顺 lookups for several codes in one request")
    try:
        t0 = time.monotonic()
        req = urllib.request.Request("https://fund.eastmoney.com/js/fundcode_search.js", headers={"User-Agent": UA, "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
        import json, re
        arr = json.loads(re.search(r"=\s*(\[.*\])\s*;?\s*$", body, re.S).group(1))
        types = {}
        for x in arr:
            types[x[3]] = types.get(x[3], 0) + 1
        print(f"  fundcode_search.js: {len(arr)} funds, {len(body) // 1024} KB in {time.monotonic() - t0:.1f}s; first {arr[0]}; types {dict(sorted(types.items(), key=lambda kv: -kv[1])[:12])}")
    except Exception as e:
        print(f"  fundcode_search.js: ❌ {type(e).__name__}: {str(e)[:150]}")
    codes = ["001593", "008888", "110022", "005827", "013416", "000001", "000248", "004788", "006195", "021049"]
    for sep, n in ((",", 2), (",", 10), ("|", 2), ("_", 2)):
        url = "https://fund.10jqka.com.cn/data/client/myfund/" + sep.join(codes[:n])
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://fund.10jqka.com.cn/"})
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace")
            import re
            got = re.findall(r'"code":"(\d{6})".*?"hqcode":"([^"]+)"', body)
            print(f"  {n} codes joined by {sep!r}: {len(body)} bytes {time.monotonic() - t0:.1f}s; (code, hqcode) found {got}")
        except Exception as e:
            print(f"  {n} codes joined by {sep!r}: ❌ {type(e).__name__}: {str(e)[:150]}")
        time.sleep(0.7)
    print("=" * 100)
    print("Probe 11b done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
