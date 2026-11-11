#!/usr/bin/env python3
"""
Probe 5c: 申万宏源研究 valuation endpoint (PE/PB/dividend yield) — which request format works, and how far
back the history goes. Uses the verified-certificate fix from probe 5b. Read-only.
Paste it into scripts/probe_sources.py, run "Probe data sources", paste the log.
"""
import json
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
NOW = datetime.now(BJ)
HOST = "www.swsresearch.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SW = {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/analysisIndex", "Accept": "application/json",
      "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
URL = f"https://{HOST}/institute-sw/api/index_analysis/index_analysis_report/"


def verified_context():
    p = subprocess.run(["openssl", "s_client", "-connect", f"{HOST}:443", "-servername", HOST], input=b"", capture_output=True, timeout=40)
    leaf = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", p.stdout.decode("utf-8", "replace"), re.S).group(0)
    txt = subprocess.run(["openssl", "x509", "-noout", "-text"], input=leaf.encode(), capture_output=True).stdout.decode()
    aia = re.search(r"CA Issuers - URI:(\S+)", txt).group(1)
    der = urllib.request.urlopen(urllib.request.Request(aia, headers={"User-Agent": UA}), timeout=30).read()
    pem = der.decode() if b"BEGIN CERTIFICATE" in der else subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-outform", "PEM"], input=der, capture_output=True).stdout.decode()
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=pem)
    print(f"  verified TLS context ready (intermediate from {aia})")
    return ctx


def call(ctx, label, params):
    url = URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=SW), timeout=40, context=ctx) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:250]
        print(f"  {label}: ❌ HTTP {e.code} {body!r}")
        return None
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:200]}")
        return None
    d = j.get("data") if isinstance(j, dict) else None
    rows = d.get("results") if isinstance(d, dict) and isinstance(d.get("results"), list) else (d if isinstance(d, list) else [])
    cnt = d.get("count") if isinstance(d, dict) else None
    print(f"  {label}: ✅ {len(rows)} rows, count {cnt}")
    if rows:
        print(f"      fields: {list(rows[0].keys())}")
        print(f"      first: {json.dumps(rows[0], ensure_ascii=False)[:400]}")
    return {"rows": rows, "count": cnt}


def main():
    print(f"Probe 5c at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    print("=" * 100)
    print("A) Verified connection")
    ctx = verified_context()
    d = NOW.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    dash, plain = d.isoformat(), d.strftime("%Y%m%d")
    print("=" * 100)
    print(f"B) One day ({dash}), all level-1 industries — request variants")
    base = {"page": 1, "page_size": 50, "index_type": "一级行业", "type": "DAY", "swindexcode": "all"}
    ok = None
    for label, extra in [("dates YYYY-MM-DD", {"start_date": dash, "end_date": dash}),
                         ("dates YYYYMMDD", {"start_date": plain, "end_date": plain}),
                         ("dates YYYY-MM-DD, index_type 市场表征", {"start_date": dash, "end_date": dash, "index_type": "市场表征"}),
                         ("no swindexcode", {"start_date": dash, "end_date": dash, "swindexcode": None})]:
        p = {**base, **extra}
        p = {k: v for k, v in p.items() if v is not None}
        r = call(ctx, label, p)
        if r and r["rows"] and ok is None and "一级行业" == p.get("index_type"):
            ok = p
    print("=" * 100)
    print("C) History depth for one industry (801120 食品饮料), daily")
    if ok:
        p = {**ok, "swindexcode": "801120", "start_date": "2000-01-01" if "-" in ok["start_date"] else "20000101",
             "end_date": dash if "-" in ok["start_date"] else plain}
        r = call(ctx, "801120 since 2000, page 1", p)
        if r and r["count"]:
            last = (int(r["count"]) + 49) // 50
            r2 = call(ctx, f"801120 since 2000, last page {last}", {**p, "page": last})
            for rr, name in ((r, "page 1"), (r2, "last page")):
                if rr and rr["rows"]:
                    ds = sorted(str(x.get("bargaindate")) for x in rr["rows"])
                    print(f"      {name} dates: {ds[0]} … {ds[-1]}")
        print("  page_size 500?")
        call(ctx, "801120 since 2000, page_size 500", {**p, "page_size": 500})
    else:
        print("  (skipped: no working variant in B)")
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
