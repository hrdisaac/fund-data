#!/usr/bin/env python3
"""
Probe 14: everything still open for the 港股 / dividend / sector indices. Read-only.
Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
  A) Hang Seng Indexes website: page model, data endpoints found in its scripts, candidate endpoints, factsheet PDFs
  B) Eastmoney daily K for Hang Seng sector indices    C) Eastmoney search with shorter keywords
  D) CSI index-perf for every candidate code (close + PE)    E) 蛋卷 PE history for dividend indices
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HSI = "https://www.hsi.com.hk"


def get(url, ref, timeout=30, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return (body, r.headers.get("Content-Type", "")) if raw else body.decode("utf-8", "replace")


def safe(label, fn):
    try:
        fn()
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:140]}")
    time.sleep(0.6)


def section(t):
    print("=" * 100 + "\n" + t)


def main():
    print(f"Probe 14 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) Hang Seng Indexes website")
    page_url = HSI + "/zh-hk/indexes/all-indexes/index-details/?seriesCode=hscgsi"
    found = set()
    def model():
        t = get(HSI + "/content/wpb/hsil/zh_hk/indexes/all-indexes/index-details.model.json", page_url)
        urls = sorted(set(re.findall(r'"(/[^"]*(?:/data/|/api/|\.do|\.json)[^"]*)"', t)))
        print(f"  page model: {len(t)} bytes; URLs inside: {urls[:20]}")
        found.update(urls)
    safe("page model", model)
    def scripts():
        html = get(page_url, HSI)
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        print(f"  page: {len(html)} bytes, {len(srcs)} scripts: {srcs[:8]}")
        hits = set()
        for s in srcs[:10]:
            u = s if s.startswith("http") else HSI + s
            try:
                js = get(u, page_url, timeout=40)
            except Exception as e:
                print(f"    {s[:60]}: ❌ {type(e).__name__}")
                continue
            for m in re.findall(r'["\'`](/(?:data|api|wpb|content)[^"\'`\s]{3,120})["\'`]', js):
                if any(k in m for k in ("data", "api", ".do", ".json", "index", "perf", "valuation", "factsheet")):
                    hits.add(m)
            time.sleep(0.3)
        print(f"  endpoints mentioned in the scripts ({len(hits)}): {sorted(hits)[:40]}")
        found.update(hits)
    safe("scripts", scripts)
    cands = ["/data/schi/rt/index-series/hscgsi/performance.do", "/data/eng/rt/index-series/hscgsi/performance.do",
             "/data/schi/indexes/hscgsi/performance.do", "/data/eng/indexes/hscgsi/valuation.do",
             "/content/wpb/hsil/zh_hk/indexes/all-indexes/index-details.model.json?seriesCode=hscgsi"]
    for p in cands + sorted(u for u in found if "hscgsi" in u.lower() or "{" not in u)[:10]:
        def run(p=p):
            body, ctype = get(HSI + p if p.startswith("/") else p, page_url, raw=True)
            text = body.decode("utf-8", "replace")
            k = max(text.lower().find("pe"), 0)
            print(f"  {p[:90]}: {len(body)} bytes {ctype!r}; around 'pe': {text[max(0, k - 60):k + 200]!r}")
        safe(p[:60], run)
    for code in ("hscgsi", "hshci"):
        for lang in ("en", "sc", "tc"):
            def pdf(code=code, lang=lang):
                url = f"{HSI}/static/uploads/contents/{'en' if lang == 'en' else 'zh_' + lang}/dl_centre/factsheets/{code}{'e' if lang == 'en' else lang[0]}.pdf"
                body, ctype = get(url, HSI, raw=True)
                print(f"  factsheet {code} {lang}: {len(body)} bytes {ctype!r}; PDF {body[:5] == b'%PDF-'}; 'P/E' visible {b'P/E' in body or b'PE Ratio' in body}")
            safe(f"factsheet {code} {lang}", pdf)

    section("B) Eastmoney daily K (Hang Seng sector indices)")
    for sid in ("124.HSCGSI", "124.HSHCI", "124.HSCICH", "124.HSSCHYS", "100.HSCGSI", "100.HSHCI"):
        def run(sid=sid):
            j = json.loads(get(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={sid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
                               "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg=19900101&end=20500101&lmt=10000", "https://quote.eastmoney.com/", timeout=40))
            k = ((j.get("data") or {}).get("klines")) or []
            print(f"  {sid}: {len(k)} bars; first {k[0] if k else '-'}; last {k[-1] if k else '-'}")
        safe(sid, run)

    section("C) Eastmoney search, index results only")
    codes = {}
    for q in ("港股通央企红利", "央企红利", "高股息策略", "中证高股息", "全指信息", "全指医药", "全指家电", "养老产业", "红利低波100", "消费红利", "港股通红利", "恒生红利"):
        def run(q=q):
            j = json.loads(get("https://searchapi.eastmoney.com/api/suggest/get?input=" + urllib.parse.quote(q)
                               + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=20", "https://www.eastmoney.com/"))
            rows = [(r.get("Code"), r.get("Name"), r.get("QuoteID")) for r in ((j.get("QuotationCodeTable") or {}).get("Data") or [])
                    if r.get("SecurityTypeName") == "指数"]
            print(f"  {q}: {rows[:8]}")
            for c, n, qid in rows:
                codes.setdefault(c, (n, qid))
        safe(q, run)

    section("D) CSI index-perf (close + PE)")
    check = {"000993": "全指信息", "000991": "全指医药", "930697": "家用电器", "399812": "养老产业", "930955": "红利低波100", "H30094": "消费红利",
             "H30269": "红利低波", "931722": "港股通央企红利?", "930914": "港股通高股息", "000922": "中证红利"}
    for c, (n, qid) in codes.items():
        if re.fullmatch(r"[0-9A-Z]{6}", str(c)) and c not in check:
            check[c] = n
    end = datetime.now(BJ).strftime("%Y%m%d")
    for c, n in check.items():
        def run(c=c, n=n):
            j = json.loads(get(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={c}&startDate=20100101&endDate={end}",
                               "https://www.csindex.com.cn/", timeout=40))
            d = j.get("data") or []
            pe = [x for x in d if x.get("peg") not in (None, "", 0)]
            print(f"  {c} ({n}): {len(d)} rows from {d[0]['tradeDate'] if d else '-'}; PE from {pe[0]['tradeDate'] if pe else '-'}; "
                  f"name {d[-1].get('indexNameCn') if d else '-'}; last {(d[-1].get('tradeDate'), d[-1].get('close'), d[-1].get('peg')) if d else '-'}")
        safe(c, run)

    section("E) 蛋卷 PE history")
    for c in ("CSIH30094", "CSIH30269", "SH000922", "SZ399989", "SH000932", "CSI930914", "CSI931722"):
        def run(c=c):
            j = json.loads(get(f"https://danjuanfunds.com/djapi/index_eva/pe_history/{c}?day=all", "https://danjuanfunds.com/djmodule/value-center"))
            d = j.get("data") or {}
            lst = next((v for v in d.values() if isinstance(v, list) and v), []) if isinstance(d, dict) else []
            print(f"  {c}: {len(lst)} points; first {lst[0] if lst else '-'}; last {lst[-1] if lst else '-'}")
        safe(c, run)
    print("=" * 100 + "\nProbe 14 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
