#!/usr/bin/env python3
"""
Probe 6: a live source for the industry heat map (申万一级 if possible). Run it DURING TRADING HOURS
(9:30–15:00 Beijing). Read-only. Paste it into scripts/probe_sources.py, run "Probe data sources", paste the log.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
EM = {"Referer": "https://quote.eastmoney.com/"}
UT = "bd1d9ddb04089700cf9c27f6f7426281"
SW_AIA = "http://cacerts.digicert.cn/GeoTrustG2TLSCNRSA4096SHA2562022CA1.crt"


def now():
    return datetime.now(BJ).strftime("%H:%M:%S")


def section(t):
    print("=" * 100)
    print(t)


def get(url, headers, ctx=None, raw=False):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30, context=ctx) as r:
        body = r.read()
        return (r, body) if raw else json.loads(body.decode("utf-8", "replace"))


def safe(fn):
    try:
        fn()
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")


def em_boards():
    for fs in ("m:90+t:2", "m:90+t:2+f:!50", "m:90+t:3", "m:90+s:4", "m:90+t:1"):
        def run(fs=fs):
            url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f20&fs={fs}"
                   f"&fields=f12,f13,f14,f3,f20,f104,f105&ut={UT}")
            d = get(url, EM).get("data") or {}
            diff = d.get("diff") or []
            print(f"  fs={fs}: total {d.get('total')}; largest by market cap: {[(r.get('f12'), r.get('f14')) for r in diff[:12]]}")
        safe(run)


def sw_current(ctx, label):
    q = urllib.parse.urlencode({"page": 1, "page_size": 50, "indextype": "一级行业"})
    r, body = get(f"https://www.swsresearch.com/institute-sw/api/index_publish/current/?{q}",
                  {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex", "Accept": "application/json",
                   "Origin": "https://example.github.io"}, ctx, raw=True)
    j = json.loads(body.decode("utf-8", "replace"))
    rows = ((j.get("data") or {}).get("results")) or []
    hdr = {k: r.headers.get(k) for k in ("Access-Control-Allow-Origin", "Cache-Control", "Date", "Last-Modified")}
    print(f"  [{label} {now()}] {len(rows)} rows; headers {hdr}")
    for x in rows[:3]:
        print(f"    {json.dumps(x, ensure_ascii=False)}")
    return {x.get("swindexcode"): x for x in rows}


def sina():
    for codes in ("sw1_801010,sw1_801120", "sw2_801010", "s_sh801010"):
        def run(codes=codes):
            r, body = get(f"https://hq.sinajs.cn/list={codes}", {"Referer": "https://finance.sina.com.cn/"}, raw=True)
            print(f"  sina {codes}: {body.decode('gbk', 'replace')[:300]!r}")
        safe(run)


def main():
    print(f"Probe 6 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing (should be during trading hours)\n")
    section("A) Eastmoney live board lists: how many boards, which levels")
    em_boards()
    section("B) 申万 current values: do they change during trading? does the site allow a browser to read them (CORS)?")
    try:
        raw = urllib.request.urlopen(urllib.request.Request(SW_AIA, headers={"User-Agent": UA}), timeout=20).read()
        pem = raw.decode() if b"BEGIN CERTIFICATE" in raw else ssl.DER_cert_to_PEM_cert(raw)
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=pem)
        a = sw_current(ctx, "first")
        time.sleep(90)
        b = sw_current(ctx, "90 s later")
        changed = [c for c in a if c in b and json.dumps(a[c], sort_keys=True) != json.dumps(b[c], sort_keys=True)]
        print(f"  industries whose values changed in 90 s: {len(changed)} of {len(a)}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")
    section("C) Sina: 申万 index quote candidates")
    sina()
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
