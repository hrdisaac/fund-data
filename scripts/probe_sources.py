#!/usr/bin/env python3
"""
Probe 7: data sources for the signal monitor (§8 第一期). Any time of day. Read-only, writes nothing.
Paste it into scripts/probe_sources.py, run "Probe data sources" from the Actions tab, paste the whole log back.

  A) CSI index-perf: which of the §8.4 broad indices it serves, and how far back close / PE / amount go
  B) Eastmoney daily K-lines from GitHub (创业板指, 科创50, 上证指数): does push2his answer a server?
  C) 申万 index_publish/trend: full price + amount history in one request? same units as index_analysis_report?
  D) 蛋卷 pe_history / pb_history for indices CSI may not value (创业板指, 科创50, 上证指数)
"""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROBE = "Probe 7"
BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
UT = "bd1d9ddb04089700cf9c27f6f7426281"
SW_AIA = "http://cacerts.digicert.cn/GeoTrustG2TLSCNRSA4096SHA2562022CA1.crt"
SW_HEADERS = {"Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex", "Accept": "application/json"}
CSI_HEADERS = {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"}


def section(t):
    print("=" * 100)
    print(t)


def get(url, headers, ctx=None, timeout=45):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers)
    t0 = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout, context=ctx) as r:
        body = r.read()
    return json.loads(body.decode("utf-8", "replace")), len(body), time.monotonic() - t0


def safe(label, fn):
    try:
        fn()
    except urllib.error.HTTPError as e:
        print(f"  {label}: ❌ HTTP {e.code}")
    except Exception as e:
        print(f"  {label}: ❌ {type(e).__name__}: {str(e)[:200]}")
    time.sleep(0.8)


def first_with(rows, key):
    for r in rows:
        v = r.get(key)
        if v not in (None, "", "-", 0, "0"):
            return r.get("tradeDate"), v
    return None, None


# ---------------------------------------------------------------- A) CSI
def csi(code, start):
    def run():
        url = (f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}"
               f"&startDate={start}&endDate={datetime.now(BJ).strftime('%Y%m%d')}")
        j, size, secs = get(url, CSI_HEADERS)
        rows = j.get("data") if isinstance(j, dict) else None
        if not isinstance(rows, list) or not rows:
            print(f"  {code} from {start}: no rows; reply keys {list(j)[:6] if isinstance(j, dict) else type(j).__name__}; "
                  f"msg {str(j.get('msg') if isinstance(j, dict) else '')[:80]}")
            return
        rows = sorted(rows, key=lambda r: str(r.get("tradeDate")))
        name = rows[-1].get("indexNameCn")
        fc, c = first_with(rows, "close")
        fp, p = first_with(rows, "peg")
        fa, a = first_with(rows, "tradingValue")
        last = rows[-1]
        print(f"  {code} {name} (from {start}): {len(rows)} rows {rows[0].get('tradeDate')}…{last.get('tradeDate')}, "
              f"{size // 1024} KB in {secs:.1f}s")
        print(f"      first close {fc} ({c}); first PE {fp} ({p}); first amount {fa} ({a})")
        print(f"      last row: close {last.get('close')}, PE {last.get('peg')}, amount {last.get('tradingValue')}")
    safe(code, run)


# ---------------------------------------------------------------- B) Eastmoney
def em_kline(secid):
    def run():
        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&ut={UT}"
               f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1"
               f"&beg=19900101&end=20500101&lmt=10000")
        j, size, secs = get(url, {"Referer": "https://quote.eastmoney.com/"})
        d = j.get("data") or {}
        k = d.get("klines") or []
        print(f"  {secid} {d.get('name')}: {len(k)} daily bars, {size // 1024} KB in {secs:.1f}s; rc {j.get('rc')}")
        if k:
            print(f"      first {k[0]}")
            print(f"      last  {k[-1]}   (date,open,close,high,low,volume,amount)")
    safe(secid, run)


# ---------------------------------------------------------------- C) 申万
def sw_ctx():
    raw = urllib.request.urlopen(urllib.request.Request(SW_AIA, headers={"User-Agent": UA}), timeout=20).read()
    pem = raw.decode() if b"BEGIN CERTIFICATE" in raw else ssl.DER_cert_to_PEM_cert(raw)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=pem)
    return ctx


def sw_trend(ctx, code, extra, label):
    out = {}

    def run():
        q = urllib.parse.urlencode({"swindexcode": code, "period": "DAY", **extra})
        j, size, secs = get(f"https://www.swsresearch.com/institute-sw/api/index_publish/trend/?{q}", SW_HEADERS, ctx, 60)
        rows = j.get("data") if isinstance(j, dict) else None
        if isinstance(rows, dict):
            rows = rows.get("results") or rows.get("data") or rows
        if not isinstance(rows, list) or not rows:
            print(f"  trend {code} {label}: no rows; reply {json.dumps(j, ensure_ascii=False)[:200]}")
            return
        rows = sorted(rows, key=lambda r: str(r.get("bargaindate")))
        print(f"  trend {code} {label}: {len(rows)} rows {rows[0].get('bargaindate')}…{rows[-1].get('bargaindate')}, "
              f"{size // 1024} KB in {secs:.1f}s")
        print(f"      newest row: {json.dumps(rows[-1], ensure_ascii=False)}")
        for r in rows[-5:]:
            out[str(r.get("bargaindate"))[:10]] = r
    safe(f"trend {code} {label}", run)
    return out


def sw_report(ctx, code):
    out = {}

    def run():
        today = datetime.now(BJ).date()
        q = urllib.parse.urlencode({"page": 1, "page_size": 10, "index_type": "一级行业",
                                    "start_date": (today - timedelta(days=14)).isoformat(), "end_date": today.isoformat(),
                                    "type": "DAY", "swindexcode": code})
        j, size, secs = get(f"https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/?{q}",
                            SW_HEADERS, ctx, 60)
        rows = ((j.get("data") or {}).get("results")) or []
        print(f"  report {code}: {len(rows)} rows in {secs:.1f}s")
        for r in rows:
            out[str(r.get("bargaindate"))[:10]] = r
    safe(f"report {code}", run)
    return out


def sw_compare(trend, report):
    common = sorted(set(trend) & set(report))
    if not common:
        print(f"  compare: no common days (trend {sorted(trend)[-3:]}, report {sorted(report)[-3:]})")
        return
    for d in common[-3:]:
        t, r = trend[d], report[d]
        print(f"  {d}: close trend {t.get('closeindex')} / report {r.get('closeindex')}; "
              f"amount trend {t.get('bargainamount')} / report {r.get('bargainamount')}; "
              f"volume trend {t.get('bargainsum')}")


# ---------------------------------------------------------------- D) 蛋卷
def danjuan(kind, code):
    def run():
        j, size, secs = get(f"https://danjuanfunds.com/djapi/index_eva/{kind}_history/{code}?day=all",
                            {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"})
        d = j.get("data") if isinstance(j, dict) else None
        lst = None
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    lst = v
                    break
        if not lst:
            print(f"  {kind} {code}: no list; reply {json.dumps(j, ensure_ascii=False)[:200]}")
            return
        ts = sorted(x.get("ts") for x in lst if x.get("ts"))
        day = lambda t: datetime.fromtimestamp(t / 1000, BJ).strftime("%Y-%m-%d") if t else None
        print(f"  {kind} {code}: {len(lst)} points {day(ts[0])}…{day(ts[-1])}; newest {json.dumps(lst[-1], ensure_ascii=False)[:160]}")
    safe(f"{kind} {code}", run)


def main():
    print(f"{PROBE} at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    section("A) CSI index-perf: broad indices of §8.4 (25-year request; 000300 also asked from 2005 and 2011)")
    for code in ("000300", "000001", "000688", "932000", "000510", "000016", "399006", "399001"):
        csi(code, "20010101")
    csi("000300", "20050101")
    csi("000300", "20110101")

    section("B) Eastmoney daily K-lines from a GitHub server (push2his)")
    for secid in ("0.399006", "1.000688", "1.000001", "1.000300"):
        em_kline(secid)

    section("C) 申万 trend (prices + amount) vs index_analysis_report; units on common days")
    try:
        ctx = sw_ctx()
        t = sw_trend(ctx, "801120", {}, "plain")
        r = sw_report(ctx, "801120")
        sw_compare(t, r)
        sw_trend(ctx, "801120", {"start_date": "2026-10-01", "end_date": datetime.now(BJ).date().isoformat()}, "with dates")
        t2 = sw_trend(ctx, "801780", {}, "plain")            # 银行: a second industry for the unit check
        r2 = sw_report(ctx, "801780")
        sw_compare(t2, r2)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")

    section("D) 蛋卷 PE/PB history for indices CSI may not value")
    for code in ("SZ399006", "SH000688", "SH000001", "SH000300"):
        danjuan("pe", code)
        danjuan("pb", code)
    print("=" * 100)
    print(f"{PROBE} done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
