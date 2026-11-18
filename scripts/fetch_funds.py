#!/usr/bin/env python3
"""
Fund-level data for the fund viewer (第三期). Standard library only; runs in GitHub Actions.

  python scripts/fetch_funds.py hqcodes   — the backup 同花顺 hqcode table for ALL funds (daily, a few minutes):
      the app fetches minute estimates by the fund code itself (probe 12: 85 of 89 identical, none needs the hqcode);
      only when that returns nothing does it read data/hqcode/<first two digits>.json. Incremental: codes never looked up
      first, then codes checked more than RECHECK_DAYS ago; 10 codes per request.
"""
import bisect
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_market as fm          # noqa: E402

FUNDS_VERSION = "v1.1"
ALL_FUNDS_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
THS_LOOKUP = "https://fund.10jqka.com.cn/data/client/myfund/{codes}"
THS_HEADERS = {"Referer": "https://fund.10jqka.com.cn/"}
BATCH = 10
RECHECK_DAYS = 7
HQ_BUDGET = float(os.environ.get("HQCODE_BUDGET", 180))
F10_URL = "https://fundf10.eastmoney.com/jbgk_{code}.html"
PZ_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
SW_MEMBERS = "https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/?swindexcode={code}&page={page}&page_size=50"
R2_DAYS, R2_USE, R2_REF = 250, 0.8, 0.6        # fit ≥ 80 %: use the index's signals; 60–80 %: shown for reference only
# common tracked indices → the code the signal monitor uses (CSI index-perf / 港股 codes); names are normalised before matching
ALIASES = {"沪深300": "000300", "中证500": "000905", "中证1000": "000852", "中证2000": "932000", "中证A500": "000510", "中证800": "000906",
           "中证全指": "000985", "上证综合": "000001", "上证指数": "000001", "上证50": "000016", "科创50": "000688", "上证科创板50成份": "000688",
           "创业板": "399006", "创业板指": "399006", "中证红利": "000922", "中证白酒": "399997", "中证银行": "399986", "中证军工": "399967",
           "中证全指证券公司": "399975", "证券公司": "399975", "中证煤炭": "399998", "中证医疗": "399989", "中证传媒": "399971",
           "中证新能源汽车": "399976", "中证生物科技主题": "930743", "中证全指医疗器械": "H30217", "中证全指半导体产品与设备": "H30184",
           "中证人工智能主题": "930713", "中证光伏产业": "931151", "中证主要消费": "000932", "中证内地消费主题": "000942",
           "中证畜牧养殖": "930707", "中华交易服务半导体芯片行业": "990001", "恒生": "HKHSI", "恒生科技": "HKHSTECH", "恒生中国企业": "HKHSCEI",
           # v1.1 (first real run, 2026-11-19): sector indices whose names contain a broad index's name
           "中证全指信息技术": "000993", "中证全指医药卫生": "000991", "中证全指家用电器": "930697", "中证养老产业": "399812",
           "中证红利低波动": "H30269", "中证红利低波动100": "930955"}
SW_NAMES = {}                                   # filled from the signal monitor (申万一级 names → codes)


def log(msg):
    print(msg, flush=True)


def hq_dir():
    return os.path.join(fm.DATA_DIR, "hqcode")


def load_all_funds():
    """Eastmoney's list of all funds → [(code, name, type)] (probe 11b: 28,380 funds in one file)."""
    raw = fm.get_bytes(ALL_FUNDS_URL, {"Referer": "https://fund.eastmoney.com/"}, attempts=3, timeout=40, deadline=60)
    text = raw.decode("utf-8", "replace")
    m = re.search(r"=\s*(\[.*\])\s*;?\s*$", text, re.S)
    if not m:
        raise RuntimeError("fundcode_search.js: no array")
    out = []
    for x in json.loads(m.group(1)):
        if isinstance(x, list) and len(x) >= 4 and re.fullmatch(r"\d{6}", str(x[0])):
            out.append((str(x[0]), str(x[2])[:40], str(x[3])[:20]))
    if len(out) < 1000:
        raise RuntimeError(f"fundcode_search.js: only {len(out)} funds")
    return out


def load_shards():
    """{code: (hqcode or None, checked date)} from data/hqcode/<two digits>.json."""
    out = {}
    d = hq_dir()
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not re.fullmatch(r"\d{2}\.json", fn):
            continue
        j = fm.read_json(os.path.join(d, fn), {}) or {}
        codes, checked = j.get("codes") or {}, j.get("checked") or {}
        for c, day in checked.items():
            out[c] = (codes.get(c), day)
    return out


def save_shards(table, keys):
    written = 0
    for k in sorted(keys):
        items = {c: v for c, v in table.items() if c[:2] == k}
        doc = {"codes": {c: v[0] for c, v in sorted(items.items()) if v[0]}, "checked": {c: v[1] for c, v in sorted(items.items())}}
        written += bool(fm.write_if_changed(os.path.join(hq_dir(), f"{k}.json"), doc, ignore=()))
    return written


def lookup(codes):
    """One 同花顺 request for up to 10 codes → {code: hqcode}."""
    j = fm.get_json(THS_LOOKUP.format(codes=",".join(codes)), THS_HEADERS, attempts=2, timeout=25)
    out = {}
    for x in (j.get("data") if isinstance(j, dict) else None) or []:
        if isinstance(x, dict) and str(x.get("code", "")) in codes:
            h = str(x.get("hqcode") or "").strip()
            if re.fullmatch(r"[0-9A-Z]{4,8}", h):
                out[str(x["code"])] = h
    return out


def build_hqcodes(today, budget=None):
    budget = HQ_BUDGET if budget is None else budget
    t0 = time.monotonic()
    funds = load_all_funds()
    table = load_shards()
    stale = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    new = [c for c, _, _ in funds if c not in table]
    old = sorted((c for c, _, _ in funds if c in table and table[c][1] <= stale), key=lambda c: table[c][1])
    todo = new + old
    changed, found, asked, failures = set(), 0, 0, 0
    for k in range(0, len(todo), BATCH):
        if time.monotonic() - t0 > budget:
            break
        part = todo[k:k + BATCH]
        try:
            got = lookup(part)
            failures = 0
        except Exception as e:
            failures += 1
            log(f"  lookup {part[0]}…: {type(e).__name__}: {str(e)[:100]}")
            if failures >= 3:
                log("  3 failures in a row: stopping for this run")
                break
            continue
        asked += len(part)
        for c in part:
            table[c] = (got.get(c), today.isoformat())     # not found: checked anyway, looked up again after RECHECK_DAYS
            changed.add(c[:2])
        found += len(got)
        fm.pause()
    written = save_shards(table, changed)
    have = sum(1 for v in table.values() if v[0])
    left = max(0, len(new) - asked)
    log(f"hqcode table: {len(funds)} funds listed; looked up {asked} this run ({found} with a hqcode); table now {len(table)} checked, "
        f"{have} with a hqcode; {left} never looked up yet; {written} files written; took {time.monotonic() - t0:.0f}s")
    return {"listed": len(funds), "asked": asked, "found": found, "checked": len(table), "have": have, "left": left}


# ====================================================================== 第三期: which index each fund follows

def norm(name):
    n = re.sub(r"[（(][^）)]*[）)]", "", str(name or ""))
    for w in ("指数", "收益率", "人民币", " ", "　"):
        n = n.replace(w, "")
    return n.strip()


def classify(code, name, ftype):
    """passive / enhanced / active / etf / bond / money / qdii / fof / other (from Eastmoney's type)."""
    t = ftype or ""
    if "QDII" in t:
        return "qdii"
    if t.startswith("货币"):
        return "money"
    if t.startswith("债券") or t == "指数型-固收":
        return "bond"
    if t.startswith("FOF"):
        return "fof"
    if t.startswith("指数型"):
        if code[:2] in ("15", "51", "56", "58"):
            return "etf"
        return "enhanced" if "增强" in (name or "") else "passive"
    if t.startswith("股票") or t.startswith("混合"):
        return "active"
    return "other"


def f10(code):
    """跟踪标的 and 业绩比较基准 from the F10 page (probe 10)."""
    text = fm.get_bytes(F10_URL.format(code=code), {"Referer": "https://fundf10.eastmoney.com/"}, attempts=2, timeout=30, deadline=45).decode("utf-8", "replace")
    out = {}
    for key in ("跟踪标的", "业绩比较基准"):
        m = re.search(key + r"</th>\s*<td[^>]*>(.*?)</td>", text, re.S)
        out[key] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:80] if m else ""
    return out


def nav_history(code):
    """Accumulated NAV (dividends added back) from pingzhongdata → (dates, values)."""
    text = fm.get_bytes(PZ_URL.format(code=code), {"Referer": "https://fund.eastmoney.com/"}, attempts=2, timeout=40, deadline=60).decode("utf-8", "replace")
    m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
    pts = json.loads(m.group(1)) if m else []
    out = {}
    for x in pts:
        if isinstance(x, list) and len(x) >= 2 and x[1] is not None and float(x[1]) > 0:
            out[datetime.fromtimestamp(int(x[0]) / 1000, fm.BJ).date().isoformat()] = float(x[1])
    ds = sorted(out)
    return ds, [out[d] for d in ds]


def match_tracked(tracked, names):
    """tracked-index text → index code (aliases first, then the monitor's own names); None if no match."""
    t = norm(tracked)
    if not t or "无跟踪标的" in tracked:
        return None
    table = {norm(k): v for k, v in ALIASES.items()}
    table.update({norm(k): v for k, v in names.items()})
    # exact names only (v1.1): "contains" matched 中证全指信息技术 to 中证全指 and 中证红利低波动 to 中证红利 in the first run
    return table.get(t)


def r2(fund_dates, fund_vals, idx):
    """R² of daily log returns over the last R2_DAYS common days; idx = (dates, closes)."""
    fmap = dict(zip(fund_dates, fund_vals))
    imap = dict(zip(*idx))
    common = sorted(set(fmap) & set(imap))[-(R2_DAYS + 1):]
    if len(common) < 120:
        return None
    a = [math.log(fmap[common[k]] / fmap[common[k - 1]]) for k in range(1, len(common))]
    b = [math.log(imap[common[k]] / imap[common[k - 1]]) for k in range(1, len(common))]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa, sbb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return None if saa <= 0 or sbb <= 0 else sab * sab / (saa * sbb)


def sw_members(today):
    """{stock code: 申万一级 code}, cached for a week in data/history/sw_members.json."""
    path = os.path.join(fm.DATA_DIR, "history", "sw_members.json")
    old = fm.read_json(path, {}) or {}
    if old.get("date") and (today - fm.d_of(old["date"])).days < 7 and old.get("members"):
        return old["members"]
    ctx = fm.sw_context()
    members = {}
    for code in sorted(SW_NAMES.values()):
        page = 1
        while page <= 20:
            j = fm.get_json(SW_MEMBERS.format(code=code, page=page), fm.SW_HEADERS, attempts=2, timeout=40, context=ctx)
            d = (j or {}).get("data") or {}
            for x in d.get("results") or []:
                if re.fullmatch(r"\d{6}", str(x.get("stockcode", ""))):
                    members[str(x["stockcode"])] = code
            if not d.get("next"):
                break
            page += 1
            fm.pause()
    if len(members) < 1000:
        raise RuntimeError(f"申万 members: only {len(members)} stocks")
    fm.write_if_changed(path, {"date": today.isoformat(), "members": members}, ignore=())
    return members


def build_map(today):
    t0 = time.monotonic()
    codes = read_funds()
    allf = {c: (n, t) for c, n, t in load_all_funds()}
    sig = fm.read_json(os.path.join(fm.DATA_DIR, "signals.json"), {}) or {}
    monitored = {e["code"]: e.get("name", e["code"]) for e in sig.get("indices", []) if isinstance(e, dict) and e.get("code")}
    names = {v: k for k, v in monitored.items()}
    SW_NAMES.update({v: k for k, v in monitored.items() if k.startswith("801")})
    closes = {}
    for c in monitored:
        det = fm.read_json(os.path.join(fm.DATA_DIR, "signals", f"{c}.json"), {}) or {}
        if det.get("dates") and det.get("close"):
            closes[c] = ([f"{x // 10000}-{x // 100 % 100:02d}-{x % 100:02d}" for x in det["dates"]], det["close"])
    hold = (fm.read_json(os.path.join(fm.DATA_DIR, "holdings.json"), {}) or {}).get("funds") or {}
    try:
        members = sw_members(today)
    except Exception as e:
        members = {}
        log(f"申万 members not available ({type(e).__name__}: {str(e)[:100]}): no look-through this run")
    funds, add, errors = {}, set(), {}
    for code in codes:
        name, ftype = allf.get(code, ("", ""))
        kind = classify(code, name, ftype)
        row = {"name": name, "type": ftype, "kind": kind, "tracked": "", "index": None, "method": "none", "r2": None, "r2_index": None}
        try:
            if kind in ("passive", "enhanced", "etf"):
                info = f10(code)
                row["tracked"] = info["跟踪标的"]
                ic = match_tracked(info["跟踪标的"], names)
                if ic:
                    if ic in monitored:
                        row["index"], row["method"] = ic, "tracked"
                    else:
                        add.add(ic)
                        row["method"] = "tracked_pending"; row["pending_index"] = ic
                fm.pause()
            if kind in ("passive", "enhanced", "etf", "active", "fof", "qdii"):
                ds, vs = nav_history(code)
                fm.write_if_changed(os.path.join(fm.DATA_DIR, "history", "fund_nav", f"{code}.json"),
                                    {"code": code, "dates": ds, "nav": [round(v, 4) for v in vs]}, ignore=())
                row["nav_from"] = ds[0] if ds else None
                best = max(((r2(ds, vs, cl), c) for c, cl in closes.items()), key=lambda x: (x[0] or -1))
                if best[0] is not None:
                    row["r2"], row["r2_index"] = round(best[0] * 100, 1), best[1]
                if row["method"] in ("none", "tracked_pending") and row["r2"] is not None and row["r2"] >= R2_REF * 100:
                    row["index"], row["method"] = best[1], "r2"
                fm.pause()
            h = hold.get(code)
            if kind in ("active", "fof", "enhanced") and h and members:
                w = {}
                for st in h.get("stocks") or []:
                    sc, wt = str(st[1]), float(st[3])
                    k = members.get(sc)
                    w[k or "other"] = w.get(k or "other", 0) + wt
                row["look"] = [[k, monitored.get(k, "其他（港股、未分类）"), round(v, 2)] for k, v in sorted(w.items(), key=lambda kv: -kv[1])]
                row["look_cover"] = round(sum(w.values()), 1)
                row["look_period"] = h.get("period") or h.get("report_date") or ""
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"[:160]
            log(f"  {code}: {errors[code]}")
        funds[code] = row
        log(f"  {code} {name[:14]} [{kind}] → {row['index'] or '-'} ({row['method']}{'' if row['r2'] is None else ', R² ' + str(row['r2']) + '% vs ' + str(row['r2_index'])})"
            + (f"; look-through {row['look'][:3]}" if row.get("look") else ""))
    doc = {"schema": 1, "version": FUNDS_VERSION, "generated_beijing": datetime.now(fm.BJ).strftime("%Y-%m-%d %H:%M:%S"),
           "funds": funds, "add_indices": sorted(add), "errors": errors,
           "rules": {"r2_use": R2_USE * 100, "r2_ref": R2_REF * 100, "r2_days": R2_DAYS}}
    saved = fm.write_if_changed(os.path.join(fm.DATA_DIR, "funds_map.json"), doc, ignore=("generated_beijing",))
    by = {}
    for r in funds.values():
        by[r["method"]] = by.get(r["method"], 0) + 1
    log(f"funds_map.json {'saved' if saved else 'unchanged'}: {len(funds)} funds, {by}; indices to add {sorted(add)}; took {time.monotonic() - t0:.0f}s")
    return doc


def read_funds():
    path = os.path.join(fm.ROOT, "funds.txt")
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            for tok in re.split(r"[\s,;，；]+", line.split("#", 1)[0]):
                if re.fullmatch(r"\d{6}", tok) and tok not in out:
                    out.append(tok)
    return out


def main():
    log(f"fetch_funds.py {FUNDS_VERSION}")
    now = datetime.now(fm.BJ)
    if os.environ.get("MARKET_TODAY"):
        now = datetime.fromisoformat(os.environ["MARKET_TODAY"]).replace(tzinfo=fm.BJ)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "hqcodes":
        try:
            build_hqcodes(now.date())
        except Exception as e:
            log(f"hqcode table not updated: {type(e).__name__}: {e}")
            sys.exit(1)
        return
    if mode == "map":
        try:
            build_map(now.date())
        except Exception as e:
            log(f"funds_map.json not updated: {type(e).__name__}: {e}")
            sys.exit(1)
        return
    log("usage: fetch_funds.py hqcodes | map")
    sys.exit(2)


if __name__ == "__main__":
    main()
