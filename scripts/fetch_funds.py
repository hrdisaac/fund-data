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
import urllib.parse
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_market as fm          # noqa: E402

FUNDS_VERSION = "v1.4"
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
           "中证红利低波动": "H30269", "中证红利低波动100": "930955",
           # v1.2 (probe 13): 港股通高股息 has CSI close + PE from 2016-11; 消费红利 is in 蛋卷's table (CSIH30094)
           "中证港股通高股息投资港元": "930914", "中证港股通高股息投资": "930914", "中证主要消费红利": "H30094",
           # v1.3 (probe 14): CSI codes confirmed with close + PE; v1.1 codes 000993 / 000991 / 930697 / 399812 / 930955 also confirmed
           "中证港股通央企红利": "931233", "中证高股息策略": "931053"}
SW_NAMES = {}                                   # filled from the signal monitor (申万一级 names → codes)
SEARCH_URL = ("https://searchapi.eastmoney.com/api/suggest/get?input={q}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=20")
CSI_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate={start}&endDate={end}"
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={qid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg={start}&end=20500101&lmt=400")
R2_PASSIVE = 90                                 # a searched index must fit a passive fund this well to count as its real index
BENCH_SLACK = 5                                 # an active fund uses its benchmark index if R² ≥ 80 % and ≤ 5 points below the best


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


def bench_main(text):
    """业绩比较基准 → (equity index name, weight) with the largest weight; bond / deposit / rate parts ignored."""
    best = None
    for part in re.split(r"[+＋]", text or ""):
        if any(w in part for w in ("债", "存款", "利率", "货币")) or "指数" not in part:
            continue
        m = re.search(r"([\d.]+)\s*%", part)
        w = float(m.group(1)) if m else 100.0
        name = re.sub(r"[\d.]+\s*%\s*[×*xX]?", "", part)
        name = re.split(r"指数", name)[0] + "指数"
        name = re.sub(r"^[\s(（×*]*", "", name).replace("经汇率调整后的", "").strip()
        if best is None or w > best[1]:
            best = (name, w)
    return best


def search_variants(name):
    """Eastmoney's search knows short names (全指信息, 养老产业), not full ones: full → no prefix → first 4 characters."""
    n = norm(name).replace("低波动", "低波")
    out = [n]
    short = []
    for pre in ("中证全指", "中证", "国证", "中华交易服务", "上证", "深证", "恒生"):
        if n.startswith(pre) and len(n) > len(pre) + 1:
            short.append(("全指" if pre == "中证全指" else "") + n[len(pre):])
            break
    out += short
    for x in list(out):
        for suf in ("行业", "产业", "主题", "成份"):
            if x.endswith(suf) and len(x) > len(suf) + 1:
                out.append(x[:-len(suf)])
    # first four characters only of the name WITHOUT its prefix ("中证全指…" → "全指信息", never "中证全指" / "中证红利")
    out += [x[:4] for x in short if len(x) > 4]
    seen, res = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x); res.append(x)
    return res[:6]


def index_prices(code, qid, today):
    """Last ~300 closes of a candidate index: CSI (close + PE start) for CSI codes, else Eastmoney's K-line."""
    start = (today - timedelta(days=430)).strftime("%Y%m%d")
    if re.fullmatch(r"[0-9A-Z]{6}", str(code)):
        try:
            j = fm.get_json(CSI_PERF.format(code=code, start="20100101", end=today.strftime("%Y%m%d")), {"Referer": "https://www.csindex.com.cn/"},
                            attempts=2, timeout=40)
            d = (j or {}).get("data") or []
            if d:
                pe = next((x["tradeDate"] for x in d if x.get("peg") not in (None, "", 0)), None)
                return ([f"{x['tradeDate'][:4]}-{x['tradeDate'][4:6]}-{x['tradeDate'][6:]}" for x in d[-300:]], [float(x["close"]) for x in d[-300:]],
                        "csi", pe)
        except Exception:
            pass
    j = fm.get_json(EM_KLINE.format(qid=qid, start=start), {"Referer": "https://quote.eastmoney.com/"}, attempts=3, timeout=40)
    k = ((j or {}).get("data") or {}).get("klines") or []
    return [x.split(",")[0] for x in k], [float(x.split(",")[1]) for x in k], "em", None


def resolve(name, fund, today, monitored):
    """Index name → the best candidate {code, qid, name, source, pe_from, r2} by Eastmoney search + R² against the fund."""
    cands = []
    for q in search_variants(name):
        try:
            j = fm.get_json(SEARCH_URL.format(q=urllib.parse.quote(q)), {"Referer": "https://www.eastmoney.com/"}, attempts=2, timeout=25)
        except Exception:
            continue
        rows = [(str(r.get("Code")), str(r.get("Name")), str(r.get("QuoteID"))) for r in ((j or {}).get("QuotationCodeTable") or {}).get("Data") or []
                if r.get("SecurityTypeName") == "指数" and r.get("QuoteID")]
        cands += [x for x in rows[:3] if x[0] not in {c[0] for c in cands}]
        fm.pause()
        if cands:
            break
    best = None
    for code, nm, qid in cands[:3]:
        if code in monitored:
            fit = r2(fund[0], fund[1], monitored[code]) if monitored[code] else None
            cand = {"code": code, "qid": qid, "name": nm, "source": "monitored", "pe_from": None, "r2": fit}
        else:
            try:
                ds, cl, src, pe = index_prices(code, qid, today)
            except Exception:
                continue
            fit = r2(fund[0], fund[1], (ds, cl))
            cand = {"code": code, "qid": qid, "name": nm, "source": src, "pe_from": pe, "r2": fit}
        if fit is not None and (best is None or fit > best["r2"]):
            best = cand
        fm.pause()
    if best:
        best["r2"] = round(best["r2"] * 100, 1)
    return best


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
    funds, add, errors = {}, {}, {}
    mon_closes = {c: closes.get(c) for c in monitored}
    for code in codes:
        name, ftype = allf.get(code, ("", ""))
        kind = classify(code, name, ftype)
        row = {"name": name, "type": ftype, "kind": kind, "tracked": "", "index": None, "method": "none", "r2": None, "r2_index": None}
        try:
            info = f10(code) if kind in ("passive", "enhanced", "etf", "active", "fof", "qdii") else {"跟踪标的": "", "业绩比较基准": ""}
            row["benchmark"] = info["业绩比较基准"][:80]
            if kind in ("passive", "enhanced", "etf", "active", "fof", "qdii"):
                fm.pause()
                ds, vs = nav_history(code)
                fm.write_if_changed(os.path.join(fm.DATA_DIR, "history", "fund_nav", f"{code}.json"),
                                    {"code": code, "dates": ds, "nav": [round(v, 4) for v in vs]}, ignore=())
                row["nav_from"] = ds[0] if ds else None
                best = max(((r2(ds, vs, cl), c) for c, cl in closes.items()), key=lambda x: (x[0] or -1))
                if best[0] is not None:
                    row["r2"], row["r2_index"] = round(best[0] * 100, 1), best[1]
                fund = (ds, vs)
                if kind in ("passive", "enhanced", "etf"):
                    row["tracked"] = info["跟踪标的"]
                    ic = match_tracked(info["跟踪标的"], names) or sw_code(info["跟踪标的"])
                    found = {"code": ic, "qid": None, "name": info["跟踪标的"], "source": "alias"} if ic else None
                    if not found and info["跟踪标的"] and "无跟踪标的" not in info["跟踪标的"]:
                        found = resolve(info["跟踪标的"], fund, today, mon_closes)
                        if found and (found["r2"] or 0) < R2_PASSIVE:
                            found = None
                    if found:
                        row["found"] = found
                        if found["code"] in monitored:
                            row["index"], row["method"] = found["code"], "tracked"
                        else:
                            add[found["code"]] = {k: found.get(k) for k in ("code", "qid", "name", "source")}
                            row["method"] = "tracked_pending"; row["pending_index"] = found["code"]
                else:
                    b = bench_main(info["业绩比较基准"])
                    if b:
                        ic = match_tracked(b[0], names) or sw_code(b[0])
                        bf = ({"code": ic, "qid": None, "name": b[0], "source": "alias",
                               "r2": round(r2(ds, vs, closes[ic]) * 100, 1) if ic in closes and r2(ds, vs, closes[ic]) is not None else None}
                              if ic else resolve(b[0], fund, today, mon_closes))
                        if bf:
                            row["bench"] = {"code": bf["code"], "name": b[0], "weight": b[1], "r2": bf.get("r2")}
                            if bf["code"] not in monitored and (bf.get("r2") or 0) >= R2_USE * 100:
                                add[bf["code"]] = {k: bf.get(k) for k in ("code", "qid", "name", "source")}
                            br = row["bench"]["r2"]
                            if (bf["code"] in monitored and br is not None and br >= R2_USE * 100
                                    and (row["r2"] is None or br >= row["r2"] - BENCH_SLACK)):
                                row["index"], row["method"] = bf["code"], "benchmark"
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
           "funds": funds, "add_indices": [add[k] for k in sorted(add)], "errors": errors,
           "rules": {"r2_use": R2_USE * 100, "r2_ref": R2_REF * 100, "r2_days": R2_DAYS}}
    saved = fm.write_if_changed(os.path.join(fm.DATA_DIR, "funds_map.json"), doc, ignore=("generated_beijing",))
    by = {}
    for r in funds.values():
        by[r["method"]] = by.get(r["method"], 0) + 1
    log(f"funds_map.json {'saved' if saved else 'unchanged'}: {len(funds)} funds, {by}; indices to add {sorted(add)}; took {time.monotonic() - t0:.0f}s")
    return doc


def sw_code(name):
    """"申万医药生物行业指数" → the 申万一级 code of that industry (from the signal monitor's names)."""
    m = re.match(r"申万(.+?)(?:行业)?指数", str(name or ""))
    return SW_NAMES.get(m.group(1)) if m else None


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
