#!/usr/bin/env python3
"""
Evidence for the signal monitor (handoff §8.11 / 8.12, 第二期). Standard library only; runs weekly in GitHub Actions.

For every index of the signal monitor, over its whole history and using only what was known on each day:
  1. rule comparison: ① always invested  ② 20/51 only  ③ 20/51 + trend filter  ④ plans v1, v2, v3 (+ v3 with the weekly
     MACD filter), simulated with real 场外 fund costs (execution at the next day's close; 申购 0.12 %; 赎回 < 7 days 1.5 %,
     < 1 year 0.5 %, then 0; idle cash at the money-market yield; 港股: only on days both markets are open, redemption
     proceeds idle for 10 days)
  2. event studies: every event, swing signal, regime entry, v3 tier and plain-language level → 20 / 60 / 120 day results
     against \"any day of the same index\"
  3. five grades (§8.12), pooled over all indices
  4. extra checks (Round 41): weekly MACD, time from a crash's top to its bottom, Fibonacci retracements, Kelly sizing
Output: data/evidence.json (grades, comparisons, extra checks) and data/evidence/<code>.json (weekly equity curves).
Rules are frozen (§8.18): nothing here changes a signal; it only measures them.
"""
import bisect
import datetime as _dt
import json
import math
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_market as fm          # noqa: E402
import fetch_signals as fs         # noqa: E402

EVIDENCE_VERSION = "v1.1"
TIME_BUDGET = float(os.environ.get("EVIDENCE_TIME_BUDGET", 25 * 60))
STARTED = time.monotonic()

# ---- costs (§8.11; cash yield 1.0 % per the user on 2026-11-19)
BUY_FEE = 0.0012
REDEEM = [(7, 0.015), (365, 0.005)]          # held < 7 days 1.5 %, < 365 days 0.5 %, else 0
CASH_YIELD = 0.010
HK_SETTLE_DAYS = 10                           # 港股 QDII redemption proceeds arrive after ~T+7–10: no interest meanwhile
MIN_TRADE = 0.005                             # ignore weight drift below 0.5 % of the portfolio
ROUND_TRIP = BUY_FEE + 0.005                  # cost charged to an event's 60-day result for grading (§8.12)

# C 类份额 (agreed 2026-11-19): no purchase fee, no redemption fee after 7 days, a sales service fee on the holding
C_SERVICE_FEE = 0.003                         # 0.3 % a year, accrued daily
C_REDEEM = [(7, 0.015)]
# groups fixed in advance (agreed 2026-11-19; industries by the valuation-basis table, i.e. by their economics)
GROUPS = ["宽基", "港股", "周期与金融", "稳定与成长", "其他行业"]

HORIZONS = (20, 60, 120)
SPLIT = "2016-01-01"                          # the two periods of §8.11
STRATEGIES = ["always", "cross", "cross_trend", "v1", "v2", "v3", "v3_weekly"]
STRATEGY_NAMES = {"always": "一直满仓", "cross": "仅 20/51", "cross_trend": "20/51 + 趋势过滤", "v1": "仓位计划 v1",
                  "v2": "仓位计划 v2", "v3": "仓位计划 v3", "v3_weekly": "v3 + 周线 MACD 过滤"}

# events with a direction (+1 buy-type, −1 sell-type); regime 震荡 is reported without a grade
BUY_EVENTS = ["金叉", "回踩", "发散向上", "B1", "B2", "B3", "周线绿柱缩短过半", "周线翻红"]
SELL_EVENTS = ["死叉", "发散向下", "S1", "S2", "S3"]
REGIME_DIR = {"牛市初期": 1, "牛市中期": 1, "底部反转初期": 1, "熊市末期 / 筑底": 1, "过热 / 牛市末期": -1, "熊市": -1, "震荡": 0}
V3_TIERS = {"初步止跌": 1, "止跌加强": 1, "趋势确认": 1, "初步转弱": -1, "转弱加强": -1, "趋势破坏": -1, "安全阀": -1}
LEVEL_DIR = {"buy3": 1, "buy2": 1, "buy1": 1, "sell1": -1, "sell2": -1, "sell3": -1}


def log(msg):
    print(msg, flush=True)


def time_left():
    return TIME_BUDGET - (time.monotonic() - STARTED)


# ====================================================================== simulation (pure)

def redeem_rate(held_days, table=None):
    for limit, rate in (table or REDEEM):
        if held_days < limit:
            return rate
    return 0.0


def simulate(dates, closes, decisions, exec_ok, start, hk=False, cash_yield=CASH_YIELD, share="A"):
    """decisions[i]: target weight 0..1 decided at the close of day i (None = no new decision).
    Execution at the close of the first later day with exec_ok; the latest pending decision wins.
    share "A": purchase fee, redemption 1.5 % / 0.5 % / 0; "C": no purchase fee, 1.5 % only within 7 days, 0.3 % a year
    sales service fee on the holding. Returns dict(value[], weight[], trades, fees) from day `start` (value 1.0 there)."""
    buy_fee = BUY_FEE if share == "A" else 0.0
    table = REDEEM if share == "A" else C_REDEEM
    n = len(dates)
    lots, cash, pending = [], 1.0, []            # lots: [buy date, units]; pending: [release date, amount]
    value, weight = [None] * n, [None] * n
    want, trades, fees = None, 0, 0.0
    value[start], weight[start] = 1.0, 0.0
    if decisions[start] is not None:
        want = decisions[start]
    for j in range(start + 1, n):
        d0, d1 = fm.d_of(dates[j - 1]), fm.d_of(dates[j])
        cash *= (1 + cash_yield) ** ((d1 - d0).days / 365.0)
        if share == "C" and lots:                       # sales service fee, taken from the holding day by day
            keep = 1 - C_SERVICE_FEE * (d1 - d0).days / 365.0
            fees += sum(u for _, u in lots) * closes[j - 1] * (1 - keep)
            for lot in lots:
                lot[1] *= keep
        if pending:
            keep = []
            for rel, amt in pending:
                if rel <= d1:
                    cash += amt
                else:
                    keep.append((rel, amt))
            pending = keep
        p = closes[j]
        units = sum(u for _, u in lots)
        total = units * p + cash + sum(a for _, a in pending)
        if want is not None and exec_ok[j]:
            diff = want * total - units * p
            if diff >= MIN_TRADE * total and cash > 1e-12:
                amount = min(diff, cash)
                net = amount / (1 + buy_fee)
                fees += amount - net
                lots.append([d1, net / p])
                cash -= amount
                trades += 1
            elif diff <= -MIN_TRADE * total and units > 0:
                to_sell = min(-diff / p, units)
                proceeds = 0.0
                while to_sell > 1e-15 and lots:
                    bd, u = lots[0]
                    take = min(u, to_sell)
                    gross = take * p
                    fee = gross * redeem_rate((d1 - bd).days, table)
                    fees += fee
                    proceeds += gross - fee
                    to_sell -= take
                    if take >= u - 1e-15:
                        lots.pop(0)
                    else:
                        lots[0][1] = u - take
                if hk:
                    pending.append((d1 + timedelta(days=HK_SETTLE_DAYS), proceeds))
                else:
                    cash += proceeds
                trades += 1
            want = None
        if decisions[j] is not None:
            want = decisions[j]
        units = sum(u for _, u in lots)
        total = units * p + cash + sum(a for _, a in pending)
        value[j] = total
        weight[j] = units * p / total if total > 0 else 0.0
    return {"value": value, "weight": weight, "trades": trades, "fees": fees}


def metrics(dates, sim, start):
    v = sim["value"]
    vals = [(dates[j], v[j]) for j in range(start, len(dates)) if v[j] is not None]
    if len(vals) < 2:
        return None
    years = (fm.d_of(vals[-1][0]) - fm.d_of(vals[0][0])).days / 365.25
    cagr = (vals[-1][1] / vals[0][1]) ** (1 / years) - 1 if years > 0 else 0.0
    peak, mdd = vals[0][1], 0.0
    for _, x in vals:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    by_year = {}
    for d, x in vals:
        by_year[d[:4]] = x
    ys = sorted(by_year)
    worst, prev = None, vals[0][1]
    for y in ys:
        days = sum(1 for d, _ in vals if d[:4] == y)
        r = by_year[y] / prev - 1
        if days >= 120:
            worst = r if worst is None else min(worst, r)
        prev = by_year[y]
    w = [sim["weight"][j] for j in range(start, len(dates)) if sim["weight"][j] is not None]
    return {"cagr": round(cagr * 100, 2), "mdd": round(mdd * 100, 2), "worst_year": None if worst is None else round(worst * 100, 2),
            "trades": sim["trades"], "trades_per_year": round(sim["trades"] / years, 2) if years > 0 else None,
            "avg_weight": round(sum(w) / len(w) * 100, 1), "fees_per_year": round(sim["fees"] / years * 100, 3) if years > 0 else None,
            "years": round(years, 1), "from": vals[0][0], "to": vals[-1][0]}


# ====================================================================== decisions of the rule sets

def changes_only(ws, start):
    """A decision only on the days the target changes (and on the first day): the position is NOT rebalanced back to the
    target every day as prices move — you act only when the plan changes (v1 rebalanced daily: 30+ trades a year)."""
    out, prev = [None] * len(ws), None
    for i in range(start, len(ws)):
        w = ws[i]
        if w is not None and (prev is None or w != prev):
            out[i] = w
            prev = w
    return out


def cross_decisions(A, n, trend_filter):
    out, state = [None] * n, None
    for i in range(n):
        tr = A["trend"][i]
        for ev in A["events"][i]:
            if ev == "金叉" and (not trend_filter or tr in ("上升", "转折")):
                state = 1.0
            elif ev == "死叉" and (not trend_filter or tr in ("下降", "转折")):
                state = 0.0
        if state is not None:
            out[i] = state
    return out


def weekly_macd(dates, closes):
    """Weekly MACD (12, 26, 9 on the last close of each ISO week). Returns (events {day index: [names]}, ok[] for the
    v3 filter: True on days whose latest completed week shows a shrinking green bar (≥ half of its trough) or a red bar)."""
    n = len(dates)
    last_of_week, order = {}, []
    for i, d in enumerate(dates):
        y, w, _ = fm.d_of(d).isocalendar()
        if (y, w) not in last_of_week:
            order.append((y, w))
        last_of_week[(y, w)] = i
    idx = [last_of_week[k] for k in order]
    wc = [closes[i] for i in idx]
    e12, e26 = fs.ema(wc, 12), fs.ema(wc, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = fs.ema(dif, 9)
    bar = [2 * (a - b) for a, b in zip(dif, dea)]
    events, good = {}, [False] * len(idx)
    trough, armed = None, False
    for k in range(len(idx)):
        if k < 26:
            continue
        if bar[k] < 0:
            trough = bar[k] if trough is None else min(trough, bar[k])
            if k >= 3 and bar[k] > bar[k - 1] > bar[k - 2] > bar[k - 3] and bar[k] >= 0.5 * trough:
                good[k] = True
                if not armed:
                    events.setdefault(idx[k], []).append("周线绿柱缩短过半")
                    armed = True
        else:
            good[k] = True
            if trough is not None and bar[k - 1] < 0:
                events.setdefault(idx[k], []).append("周线翻红")
            trough, armed = None, False
    ok = [False] * n
    k = -1
    for i in range(n):
        while k + 1 < len(idx) and idx[k + 1] <= i:
            k += 1
        ok[i] = k >= 0 and good[k]                       # the latest week completed on or before day i
    return events, ok


# ====================================================================== event studies

def next_exec(exec_ok, i):
    for j in range(i + 1, len(exec_ok)):
        if exec_ok[j]:
            return j
    return None


def study(closes, dates, exec_ok, days, direction, h):
    """days: signal days. Entry at the next exec day's close. Returns per-event rows (date, entry, r, adverse) and
    the independent subset (no overlap within h days)."""
    rows, last = [], -10 ** 9
    for i in days:
        e = next_exec(exec_ok, i)
        if e is None or e + h >= len(closes):
            continue
        r = closes[e + h] / closes[e] - 1
        path = closes[e:e + h + 1]
        adverse = (min(path) / closes[e] - 1) if direction > 0 else (max(path) / closes[e] - 1)
        rows.append({"i": i, "e": e, "date": dates[i], "r": r, "adv": adverse, "indep": e - last >= h})
        if e - last >= h:
            last = e
    return rows


def summarize(rows, direction, base):
    """rows of one signal (independent only) → stats; directional: buy wins when r > 0, sell wins when r < 0."""
    if not rows:
        return {"n": 0}
    dr = [direction * x["r"] for x in rows]
    wins = [x for x in dr if x > 0]
    losses = [-x for x in dr if x < 0]
    out = {"n": len(rows), "n_pre": sum(1 for x in rows if x["date"] < SPLIT), "n_post": sum(1 for x in rows if x["date"] >= SPLIT),
           "win": round(100 * len(wins) / len(dr), 1), "median": round(100 * statistics.median(dr), 2),
           "mean": round(100 * sum(dr) / len(dr), 2),
           "payoff": round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2) if wins and losses else None,
           "worst_adverse": round(100 * (min(x["adv"] for x in rows) if direction > 0 else max(x["adv"] for x in rows)), 2),
           "base_win": base["win"], "base_median": base["median"]}
    pre = [direction * x["r"] for x in rows if x["date"] < SPLIT]
    post = [direction * x["r"] for x in rows if x["date"] >= SPLIT]
    out["win_pre"] = round(100 * sum(1 for x in pre if x > 0) / len(pre), 1) if pre else None
    out["win_post"] = round(100 * sum(1 for x in post if x > 0) / len(post), 1) if post else None
    return out


def baseline(closes, exec_ok, start, direction, h):
    rs = []
    for i in range(start, len(closes)):
        e = next_exec(exec_ok, i)
        if e is None or e + h >= len(closes):
            break
        rs.append(direction * (closes[e + h] / closes[e] - 1))
    if not rs:
        return {"win": None, "median": None, "n": 0}
    return {"win": round(100 * sum(1 for x in rs if x > 0) / len(rs), 1), "median": round(100 * statistics.median(rs), 2), "n": len(rs)}


def signal_days(A, wk_events, n):
    """{signal name: (direction, [day indices])} for one index."""
    sig = {}
    def add(name, d, i):
        sig.setdefault(name, (d, []))[1].append(i)
    for i in range(n):
        for ev in A["events"][i]:
            if ev in BUY_EVENTS:
                add(ev, 1, i)
            elif ev in SELL_EVENTS:
                add(ev, -1, i)
        for s in A["swing"][i]:
            add(s, 1 if s[0] == "B" else -1, i)
        rg = A["regime"][i]
        if rg and (i == 0 or A["regime"][i - 1] != rg) and REGIME_DIR.get(rg):
            add("状态:" + rg, REGIME_DIR[rg], i)
        for c in A["v3"]["cause"][i]:
            for t, d in V3_TIERS.items():
                if t in c:
                    add("v3:" + t, d, i)
        lv = A["level"][i]
        if lv in LEVEL_DIR:
            add("明白话:" + fs.LEVELS[lv][1], LEVEL_DIR[lv], i)
        for ev in wk_events.get(i, []):
            add(ev, 1, i)
    return sig


# ====================================================================== grading (§8.12)

def grade(pooled, share_ok):
    """pooled: summary over all indices' independent events at 60 days (with costs in `median_net`).
    share_ok: fraction of indices (with ≥ 3 events) where win > base and median > base median."""
    n, pre, post = pooled.get("n", 0), pooled.get("n_pre", 0), pooled.get("n_post", 0)
    checks = []
    if n < 10 or pre < 5 or post < 5:
        return "样本不足", [f"独立事件 {n} 次（2016 前 {pre}，后 {post}）"]
    win, bw = pooled["win"], pooled["base_win"]
    med_gap = pooled["median_net"] - pooled["base_median"]
    dpre = None if pooled["win_pre"] is None else pooled["win_pre"] - pooled["base_win_pre"]
    dpost = None if pooled["win_post"] is None else pooled["win_post"] - pooled["base_win_post"]
    same_dir = dpre is not None and dpost is not None and (dpre > 0) == (dpost > 0)
    crit = [("独立事件 ≥ 20、两段各 ≥ 8", n >= 20 and pre >= 8 and post >= 8, f"{n} 次（{pre} / {post}）"),
            ("胜率 ≥ 65 % 且高出基准 ≥ 10 个百分点", win >= 65 and win - bw >= 10, f"胜率 {win}%（基准 {bw}%）"),
            ("中位收益（扣费）高出基准 ≥ 2 %", med_gap >= 2, f"中位 {pooled['median_net']:+.2f}%（基准 {pooled['base_median']:+.2f}%）"),
            ("盈亏比 ≥ 1.0", (pooled.get("payoff") or 0) >= 1.0, f"盈亏比 {pooled.get('payoff')}"),
            ("2016 前后方向一致", same_dir, f"胜率差 {dpre if dpre is None else round(dpre, 1)} / {dpost if dpost is None else round(dpost, 1)}"),
            ("≥ 60 % 的指数达标", share_ok >= 0.6, f"{round(share_ok * 100)}% 的指数")]
    checks = [f"{'✓' if ok else '✗'} {name}：{val}" for name, ok, val in crit]
    if all(ok for _, ok, _ in crit):
        return "已验证", checks
    if (not same_dir) or share_ok < 0.4 or win < bw - 5:
        return "不稳定 / 反效果", checks
    if abs(win - bw) <= 5 or med_gap < 1:
        return "无优势", checks
    if win > bw and med_gap > 0:
        return "有倾向", checks
    return "无优势", checks


def group_grade(pooled_grade, gg, checks):
    """A group's 已验证 counts only if the result over all indices points the same way (已验证 or 有倾向)."""
    if gg == "已验证" and pooled_grade not in ("已验证", "有倾向"):
        return "有倾向", checks + [f"✗ 只在这一组成立（合并结果：{pooled_grade}）"]
    return gg, checks


def pool(per_index, name, h=60, codes=None):
    """Pool the independent events of one signal over all indices, or over `codes` (each event keeps its own baseline)."""
    rows = []
    per_index = {c: R for c, R in per_index.items() if codes is None or c in codes}
    for code, R in per_index.items():
        x = R["events"].get(name)
        if not x:
            continue
        d, evs, base = x["dir"], x["rows"][h], R["base"][x["dir"]][h]
        for e in evs:
            if e["indep"]:
                rows.append((code, d, e, base))
    if not rows:
        return {"n": 0}, 0.0
    d = rows[0][1]
    dr = [d * e["r"] for _, _, e, _ in rows]
    wins, losses = [x for x in dr if x > 0], [-x for x in dr if x < 0]
    pre = [(dd * e["r"], b) for _, dd, e, b in rows if e["date"] < SPLIT]
    post = [(dd * e["r"], b) for _, dd, e, b in rows if e["date"] >= SPLIT]
    meanb = lambda xs, k: round(sum(b[k] for _, b in xs) / len(xs), 1) if xs else None
    out = {"n": len(dr), "n_pre": len(pre), "n_post": len(post), "dir": d,
           "win": round(100 * len(wins) / len(dr), 1), "median": round(100 * statistics.median(dr), 2),
           "median_net": round(100 * (statistics.median(dr) - ROUND_TRIP), 2),
           "payoff": round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2) if wins and losses else None,
           "base_win": round(sum(b["win"] for _, _, _, b in rows) / len(rows), 1),
           "base_median": round(sum(b["median"] for _, _, _, b in rows) / len(rows), 2),
           "win_pre": round(100 * sum(1 for x, _ in pre if x > 0) / len(pre), 1) if pre else None,
           "win_post": round(100 * sum(1 for x, _ in post if x > 0) / len(post), 1) if post else None,
           "base_win_pre": meanb(pre, "win"), "base_win_post": meanb(post, "win"), "indices": len({c for c, *_ in rows})}
    # Kelly: f = p − (1 − p) / b (on the 60-day result), half-Kelly shown as the reference
    p, b = out["win"] / 100, out["payoff"]
    out["kelly"] = round(max(0.0, p - (1 - p) / b) * 100, 1) if b else None
    out["half_kelly"] = None if out["kelly"] is None else round(out["kelly"] / 2, 1)
    ok = tot = 0
    for code, R in per_index.items():
        x = R["events"].get(name)
        if not x:
            continue
        st = x["stats"][h]
        if st.get("n", 0) >= 3:
            tot += 1
            ok += st["win"] > st["base_win"] and st["median"] > st["base_median"]
    return out, (ok / tot if tot else 0.0)


# ====================================================================== extra checks

def crashes(dates, closes, A, exec_ok):
    """Crash = close ≤ 75 % of the 250-day high, that high within the last 63 days. Bottom = lowest close until the
    price regains the top (or 750 days). Compares buying at +3 / +5.5 / +8 months with v3's first add after the start."""
    out, i, n = [], 250, len(closes)
    while i < n:
        w0 = i - 249
        hi_j = max(range(w0, i + 1), key=lambda j: (closes[j], j))
        if i - hi_j <= 63 and closes[i] <= 0.75 * closes[hi_j]:
            top = hi_j
            end = next((j for j in range(i, min(n, top + 750)) if closes[j] >= closes[top]), min(n, top + 750))
            bot = min(range(top, end), key=lambda j: closes[j])
            row = {"top": dates[top], "start": dates[i], "bottom": dates[bot], "drop": round((closes[bot] / closes[top] - 1) * 100, 1),
                   "months_to_bottom": round((fm.d_of(dates[bot]) - fm.d_of(dates[top])).days / 30.44, 1),
                   "recovered": end < n and closes[end] >= closes[top] if end < n else False}
            for label, months in (("m3", 3), ("m5_5", 5.5), ("m8", 8)):
                target = (fm.d_of(dates[top]) + timedelta(days=int(months * 30.44))).isoformat()
                j = bisect.bisect_left(dates, target)
                row[label] = None if j >= n else round((closes[j] / closes[bot] - 1) * 100, 1)
            v3j = next((j for j in range(i, end) if A["action"][j] > 0), None)
            row["v3_first_add"] = None if v3j is None else {"date": dates[v3j], "vs_bottom": round((closes[v3j] / closes[bot] - 1) * 100, 1)}
            out.append(row)
            i = end
        else:
            i += 1
    return out


FIB_RATIOS = (0.382, 0.5, 0.618)
FIB_CONTROL = (0.45, 0.55, 0.70)


def fib_days(dates, closes):
    """Up-moves: swing high H = the 250-day high (made today), swing low L = the lowest close of the 250 days before it,
    H ≥ 1.2 L. After a high, the first close at or below H − r (H − L) (before a new high) is an event for ratio r."""
    ev = {r: [] for r in FIB_RATIOS + FIB_CONTROL}
    n, cur, hit = len(closes), None, set()
    for i in range(250, n):
        if closes[i] >= max(closes[i - 249:i + 1]):
            lo = min(closes[max(0, i - 499):i - 249 + 1]) if i >= 499 else min(closes[:i - 249 + 1])
            lo = min(lo, min(closes[i - 249:i + 1]))
            cur = (closes[i], lo) if closes[i] >= 1.2 * lo else None
            hit = set()
            continue
        if cur:
            H, L = cur
            for r in ev:
                if r not in hit and closes[i] <= H - r * (H - L):
                    hit.add(r)
                    ev[r].append(i)
    return ev


# ====================================================================== one index

def evaluate(item, a_dates):
    dates, closes = item["dates"], item["closes"]
    n = len(dates)
    A = fs.analyze(dates, closes, item["amounts"], item["val_obs"], item["basis"])
    hk = item["market"] == "hk"
    aset = set(a_dates or [])
    exec_ok = [(d in aset) if (hk and aset) else True for d in dates]
    wk_events, wk_ok = weekly_macd(dates, closes)
    # v3 with the weekly MACD filter: tier 3 of adding only when the weekly bar has improved (Round 41)
    P3w = fs.plan_v3(A["v2"]["target_raw"], A["zone"], A["over"], A["trend"], A["events"], closes, A["ma"], A["sst"], A["swing"], A["sig"],
                     tier3_ok=wk_ok)
    starts = [i for i in range(n) if A["held"][i] is not None]
    start = starts[0] if starts else next(i for i in range(n) if A["trend"][i] is not None)
    dec = {"always": changes_only([1.0] * n, start),
           "cross": changes_only(cross_decisions(A, n, False), start), "cross_trend": changes_only(cross_decisions(A, n, True), start)}
    if starts:
        for key, held in (("v1", A["v1"]["held"]), ("v2", A["v2"]["held"]), ("v3", A["held"]), ("v3_weekly", P3w["held"])):
            dec[key] = changes_only([None if h is None else h / 100 for h in held], start)
    comp, comp_c, curves = {}, {}, {}
    for key, ds in dec.items():
        sim = simulate(dates, closes, ds, exec_ok, start, hk=hk)
        comp[key] = metrics(dates, sim, start)
        curves[key] = sim["value"]
        comp_c[key] = metrics(dates, simulate(dates, closes, ds, exec_ok, start, hk=hk, share="C"), start)
    base = {d: {h: baseline(closes, exec_ok, start, d, h) for h in HORIZONS} for d in (1, -1)}
    sig = signal_days(A, wk_events, n)
    events = {}
    for name, (d, days) in sig.items():
        days = [i for i in days if i >= start]
        rows = {h: study(closes, dates, exec_ok, days, d, h) for h in HORIZONS}
        events[name] = {"dir": d, "rows": rows,
                        "stats": {h: summarize([x for x in rows[h] if x["indep"]], d, base[d][h]) for h in HORIZONS}}
    return {"A": A, "start": start, "comp": comp, "comp_c": comp_c, "curves": curves, "base": base, "events": events,
            "crashes": crashes(dates, closes, A, exec_ok), "fib": fib_days(dates, closes), "exec_ok": exec_ok}


# ====================================================================== data (the signal monitor's universe)

def load_universe(today):
    items, errors = [], {}
    extra = [c for c in fm.read_index_codes(fm.INDICES_FILE) if c not in {b[0] for b in fs.BROAD}][:fs.MAX_EXTRA]
    for code, name, psrc, vsrc in fs.BROAD + [(c, "", "csi", "csi") for c in extra]:
        try:
            if psrc == "csi":
                nm, ds, cl, am, vo, _ = fs.fetch_csi_long(code, today)
                basis, name = "pe_only", name or nm or code
            else:
                ds, cl, am = fs.fetch_em(psrc.split(":", 1)[1])
                vo, basis = fs.fetch_danjuan(vsrc.split(":", 1)[1]), "avg"
            items.append({"code": code, "name": name, "group": "宽基" if any(code == b[0] for b in fs.BROAD) else "自选",
                          "market": "a", "dates": ds, "closes": cl, "amounts": am, "val_obs": vo, "basis": basis})
            items[-1]["egroup"] = "宽基" if items[-1]["group"] == "宽基" else "自选"
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"[:160]
        fm.pause()
    for code, name, secids, dj, basis in fs.HK:
        try:
            last = None
            for sid in secids:
                try:
                    ds, cl, am = fs.fetch_em(sid)
                    break
                except Exception as e:
                    last = e
            else:
                raise RuntimeError(f"no K-line ({last})")
            items.append({"code": code, "name": name, "group": "港股", "market": "hk", "dates": ds, "closes": cl, "amounts": am,
                          "val_obs": fs.fetch_danjuan(dj), "basis": basis, "egroup": "港股"})
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"[:160]
        fm.pause()
    sw, _ = fs.load_sw()
    for code, name, ds, cl, am, vo in sw:
        b = fs.SW_BASIS.get(code, "avg")
        items.append({"code": code, "name": name, "group": "行业", "market": "a", "dates": ds, "closes": cl, "amounts": am,
                      "val_obs": vo, "basis": b, "egroup": {"pb": "周期与金融", "pe": "稳定与成长"}.get(b, "其他行业")})
    for it in items:                                    # a hole in a history: the last continuous stretch only (as v1.1)
        k, _ = fs.last_segment(it["dates"])
        if k:
            it["dates"], it["closes"], it["amounts"] = it["dates"][k:], it["closes"][k:], it["amounts"][k:]
            it["val_obs"] = [o for o in it["val_obs"] if o[0] >= it["dates"][0]]
    return items, errors


def r1(x, nd=1):
    return None if x is None else round(x, nd)


def build(items, today_text):
    a_dates = next((it["dates"] for it in items if it["code"] == "000300"), None) or next((it["dates"] for it in items if it["market"] == "a"), [])
    per, details, crash_rows, fib_pool, errors = {}, {}, [], {r: [] for r in FIB_RATIOS + FIB_CONTROL}, {}
    for it in items:
        if time_left() < 60:
            errors[it["code"]] = "time budget used up"
            continue
        try:
            R = evaluate(it, a_dates)
        except Exception as e:
            errors[it["code"]] = f"{type(e).__name__}: {e}"[:160]
            log(f"  {it['code']}: failed ({errors[it['code']]})")
            continue
        per[it["code"]] = R
        for c in R["crashes"]:
            crash_rows.append({"code": it["code"], "name": it["name"], **c})
        base = R["base"][1][60]
        for r, days in R["fib"].items():
            for x in study(it["closes"], it["dates"], R["exec_ok"], [i for i in days if i >= R["start"]], 1, 60):
                if x["indep"]:
                    fib_pool[r].append((x["r"], base))
        comp = R["comp"]
        log(f"  {it['code']} {it['name']}: {comp['always']['from']}…{comp['always']['to']}; "
            + ", ".join(f"{k} {comp[k]['cagr']:+.1f}%/{comp[k]['mdd']:.0f}%" for k in STRATEGIES if comp.get(k)))
        # weekly curves for the app (step 3)
        dates = it["dates"]
        wk = [j for j in range(R["start"], len(dates)) if j == len(dates) - 1 or fm.d_of(dates[j]).isocalendar()[1] != fm.d_of(dates[j + 1]).isocalendar()[1]]
        details[it["code"]] = {"code": it["code"], "name": it["name"], "dates": [int(dates[j].replace("-", "")) for j in wk],
                               "close": [round(it["closes"][j], 2) for j in wk],
                               "curves": {k: [round(R["curves"][k][j], 4) for j in wk] for k in R["curves"]},
                               "held_v3": [R["A"]["held"][j] for j in wk]}
    # pooled grades per signal
    names = sorted({nm for R in per.values() for nm in R["events"]})
    signals = []
    for nm in names:
        pooled, share = pool(per, nm, 60)
        g, checks = grade(pooled, share) if pooled.get("n") else ("样本不足", ["没有事件"])
        by_index = {c: {h: {k: R["events"][nm]["stats"][h].get(k) for k in ("n", "win", "median", "base_win", "base_median")} for h in HORIZONS}
                    for c, R in per.items() if nm in R["events"]}
        groups = {}
        for gname in GROUPS:                             # per group; a group-only 已验证 needs the pooled result to agree
            codes = {it["code"] for it in items if it.get("egroup") == gname and it["code"] in per}
            if not codes:
                continue
            gp, gs = pool(per, nm, 60, codes)
            if not gp.get("n"):
                continue
            gg, gc = group_grade(g, *grade(gp, gs))
            groups[gname] = {"grade": gg, "checks": gc, "pooled": gp, "share_ok": round(gs, 2), "indices": len(codes)}
        signals.append({"name": nm, "dir": pooled.get("dir"), "grade": g, "checks": checks, "pooled": pooled, "share_ok": round(share, 2),
                        "groups": groups, "by_index": by_index})
    # rule comparison summary across indices
    def comp_summary(key, codes):
        out = {}
        for k in STRATEGIES:
            if k == "always":
                continue
            rows = [(per[c][key][k], per[c][key]["always"]) for c in codes if per[c][key].get(k) and per[c][key].get("always")]
            if rows:
                out[k] = {"indices": len(rows), "mdd_better": sum(1 for a, b in rows if a["mdd"] > b["mdd"]),
                          "cagr_better": sum(1 for a, b in rows if a["cagr"] > b["cagr"]),
                          "median_cagr_gap": round(statistics.median(a["cagr"] - b["cagr"] for a, b in rows), 2),
                          "median_mdd_gap": round(statistics.median(a["mdd"] - b["mdd"] for a, b in rows), 2),
                          "median_trades_per_year": statistics.median(a["trades_per_year"] for a, b in rows if a["trades_per_year"] is not None)}
        return out
    egroup = {it["code"]: it.get("egroup") for it in items}
    summary = {"A": comp_summary("comp", list(per)), "C": comp_summary("comp_c", list(per)),
               "groups": {g: comp_summary("comp", [c for c in per if egroup.get(c) == g]) for g in GROUPS}}
    fib = {}
    for r, xs in fib_pool.items():
        if xs:
            fib[str(r)] = {"n": len(xs), "win": round(100 * sum(1 for x, _ in xs if x > 0) / len(xs), 1),
                           "median": round(100 * statistics.median(x for x, _ in xs), 2),
                           "base_win": round(sum(b["win"] for _, b in xs) / len(xs), 1), "control": r in FIB_CONTROL}
    months = [c["months_to_bottom"] for c in crash_rows]
    crash_sum = {"n": len(crash_rows), "median_months": r1(statistics.median(months)) if months else None,
                 "p25": r1(sorted(months)[len(months) // 4]) if months else None, "p75": r1(sorted(months)[3 * len(months) // 4]) if months else None}
    for label in ("m3", "m5_5", "m8"):
        xs = [c[label] for c in crash_rows if c[label] is not None]
        crash_sum[label + "_vs_bottom_median"] = r1(statistics.median(xs)) if xs else None
    xs = [c["v3_first_add"]["vs_bottom"] for c in crash_rows if c["v3_first_add"]]
    crash_sum["v3_vs_bottom_median"] = r1(statistics.median(xs)) if xs else None
    indices = [{"code": c, "name": next(it["name"] for it in items if it["code"] == c), "group": next(it["group"] for it in items if it["code"] == c),
                "egroup": egroup.get(c), "comparison": R["comp"], "comparison_c": R["comp_c"],
                "base60": {"buy": R["base"][1][60], "sell": R["base"][-1][60]}} for c, R in per.items()]
    doc = {"schema": 1, "version": EVIDENCE_VERSION, "generated_beijing": today_text, "rules": fs.RULES,
           "costs": {"A": {"buy": BUY_FEE, "redeem": REDEEM}, "C": {"buy": 0, "redeem": C_REDEEM, "service_fee": C_SERVICE_FEE},
                     "cash_yield": CASH_YIELD, "hk_settle_days": HK_SETTLE_DAYS, "round_trip_for_grades": ROUND_TRIP}, "groups": GROUPS,
           "strategies": STRATEGY_NAMES, "summary": summary, "signals": signals, "indices": indices,
           "extra": {"crashes": crash_rows, "crash_summary": crash_sum, "fib": fib}, "errors": errors}
    return doc, details


def main():
    global STARTED
    STARTED = time.monotonic()
    log(f"fetch_evidence.py {EVIDENCE_VERSION} (rules {fs.RULES})")
    now = datetime.now(fm.BJ)
    if os.environ.get("MARKET_TODAY"):
        now = datetime.fromisoformat(os.environ["MARKET_TODAY"]).replace(tzinfo=fm.BJ)
    fm.NOW = now
    items, load_err = load_universe(now.date())
    log(f"Loaded {len(items)} indices ({len(load_err)} failed: {sorted(load_err)})")
    if not items:
        log("Nothing loaded; previous files kept.")
        sys.exit(1)
    doc, details = build(items, now.strftime("%Y-%m-%d %H:%M:%S"))
    doc["errors"].update(load_err)
    out_dir = os.path.join(fm.DATA_DIR, "evidence")
    written = sum(1 for c, d in details.items() if fm.write_if_changed(os.path.join(out_dir, f"{c}.json"), d, ignore=()))
    saved = fm.write_if_changed(os.path.join(fm.DATA_DIR, "evidence.json"), doc, ignore=("generated_beijing",))
    g = {}
    for s in doc["signals"]:
        g[s["grade"]] = g.get(s["grade"], 0) + 1
    log(f"Grades: {g}")
    for k, v in doc["summary"]["A"].items():
        log(f"  {STRATEGY_NAMES[k]}: smaller max drawdown than always-invested on {v['mdd_better']}/{v['indices']}, higher return on "
            f"{v['cagr_better']}/{v['indices']}; median gap {v['median_cagr_gap']:+.2f}%/yr, drawdown {v['median_mdd_gap']:+.1f} points; "
            f"{v['median_trades_per_year']} trades/yr")
    log(f"evidence.json {'saved' if saved else 'unchanged'}, {written} detail files written; took {time.monotonic() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
