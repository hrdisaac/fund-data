#!/usr/bin/env python3
"""
Probe 5b: 申万宏源研究 — why the HTTPS certificate check fails, and whether supplying the missing
intermediate certificate fixes it (with full verification). Read-only; writes nothing outside /tmp.
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
SW = {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex", "Accept": "application/json",
      "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
BASE = f"https://{HOST}/institute-sw/api"


def section(t):
    print("=" * 100)
    print(t)


def run(cmd, data=None):
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=40)
    return p.stdout, p.stderr


def chain():
    out, err = run(["openssl", "s_client", "-connect", f"{HOST}:443", "-servername", HOST, "-showcerts"], data=b"")
    text = out.decode("utf-8", "replace")
    for line in text.splitlines():
        if re.match(r"\s*\d+ s:|\s*i:|Verify return code|Verification", line):
            print(f"  {line.strip()}")
    certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    print(f"  server sent {len(certs)} certificate(s)")
    return certs


def aia_url(leaf_pem):
    out, _ = run(["openssl", "x509", "-noout", "-text"], data=leaf_pem.encode())
    m = re.search(r"CA Issuers - URI:(\S+)", out.decode("utf-8", "replace"))
    return m.group(1) if m else None


def fetch_intermediate(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        der = r.read()
    if b"-----BEGIN CERTIFICATE-----" in der:
        return der.decode()
    out, _ = run(["openssl", "x509", "-inform", "DER", "-outform", "PEM"], data=der)
    pem = out.decode()
    sub, _ = run(["openssl", "x509", "-noout", "-subject", "-issuer"], data=pem.encode())
    print(f"  downloaded intermediate: {sub.decode().strip()}")
    return pem


def api_calls(ctx, label):
    d = NOW.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    ds = d.strftime("%Y%m%d")
    calls = [
        ("list", f"{BASE}/index_publish/current/?" + urllib.parse.urlencode({"page": 1, "page_size": 50, "indextype": "一级行业"})),
        ("valuation day", f"{BASE}/index_analysis/index_analysis_report/?" + urllib.parse.urlencode(
            {"page": 1, "page_size": 50, "index_type": "一级行业", "start_date": ds, "end_date": ds, "type": "DAY", "swindexcode": "all"})),
        ("valuation history 801120", f"{BASE}/index_analysis/index_analysis_report/?" + urllib.parse.urlencode(
            {"page": 1, "page_size": 50, "index_type": "一级行业", "start_date": "20000101", "end_date": NOW.strftime("%Y%m%d"),
             "type": "DAY", "swindexcode": "801120"})),
        ("price history 801120", f"{BASE}/index_publish/trend/?" + urllib.parse.urlencode({"swindexcode": "801120", "period": "DAY"})),
    ]
    for name, url in calls:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=SW), timeout=30, context=ctx) as r:
                body = r.read().decode("utf-8", "replace")
            try:
                j = json.loads(body)
            except json.JSONDecodeError:
                print(f"  [{label}] {name}: not JSON: {body[:200]!r}")
                continue
            d = j.get("data") if isinstance(j, dict) else None
            rows = d if isinstance(d, list) else next((d[k] for k in ("results", "list", "items", "rows") if isinstance(d, dict) and isinstance(d.get(k), list)), [])
            meta = {k: v for k, v in d.items() if not isinstance(v, list)} if isinstance(d, dict) else {}
            print(f"  [{label}] {name}: {len(rows)} rows; meta {json.dumps(meta, ensure_ascii=False)[:160]}")
            if rows:
                print(f"      fields: {list(rows[0].keys())}")
                print(f"      first: {json.dumps(rows[0], ensure_ascii=False)[:330]}")
                if len(rows) > 1:
                    print(f"      last:  {json.dumps(rows[-1], ensure_ascii=False)[:330]}")
            else:
                print(f"      raw: {body[:300]}")
        except Exception as e:
            print(f"  [{label}] {name}: ❌ {type(e).__name__}: {str(e)[:200]}")


def main():
    print(f"Probe 5b at {NOW.strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) Certificate chain sent by www.swsresearch.com")
    certs = []
    try:
        certs = chain()
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
    section("B) Supply the missing intermediate (from the certificate's AIA address) and call the API with full verification")
    try:
        url = aia_url(certs[0]) if certs else None
        print(f"  AIA 'CA Issuers' address: {url}")
        if url:
            pem = fetch_intermediate(url)
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cadata=pem)
            api_calls(ctx, "verified")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
    section("C) Diagnosis only: the same calls WITHOUT certificate verification (to learn the data format)")
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    api_calls(insecure, "unverified")
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
