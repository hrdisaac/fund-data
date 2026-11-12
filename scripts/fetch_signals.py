#!/usr/bin/env python3
"""
Signal monitor for the fund viewer (handoff §8, 第一期: today's states + plain-language signals, no evidence yet).
Standard library only; runs daily in GitHub Actions after fetch_market.py (it reads the 申万 history that script keeps).

For every index (broad indices, the 31 申万一级 industries, the CSI indices in indices.txt):
  ① valuation zone   PE / PB percentile within the trailing 10 years (≥ 3 years), no look-ahead
  ② trend            close vs MA250 and MA250's 20-day slope; MA850 position; 过热 (distance above MA850 vs own history)
  ③ timing           MA20 / MA51: confirmed 金叉 / 死叉, 回踩, 收敛再发散
  regime matrix (§8.7), position plan (§8.6 + the rules agreed on 2026-11-12), seven plain-language levels (§8.8),
  six swing signals (§8.9), monitor indicators (RSI, MACD, volume).
Output: data/signals.json (summary of every index) and data/signals/<code>.json (3-year chart series, read on demand).

Sources: CSI index-perf (close, PE, amount; one request per index from 2001), Eastmoney daily K-lines (创业板指),
蛋卷 weekly PE/PB (创业板指), 申万 history from data/history/sw (close, amount, PE, PB; fetch_market.py v15.2+).
Rules are fixed before any backtest (§8): do not tune them to history.
"""
import bisect
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_market as fm                                   # noqa: E402  (shared helpers: HTTP, 申万 history, JSON files)

SIGNALS_VERSION = "v1.1"
RULES = "第一期（2026-11-12 定稿）"
TIME_BUDGET = float(os.environ.get("SIGNALS_TIME_BUDGET", 8 * 60))
STARTED = time.monotonic()
CSI_START = "20010101"          # CSI adds a phantom row dated at startDate: 1 January is never a trading day → dropped
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg=19900101&end=20500101&lmt=10000")
EM_HEADERS = {"Referer": "https://quote.eastmoney.com/"}
DJ_URL = "https://danjuanfunds.com/djapi/index_eva/{kind}_history/{code}?day=all"
DJ_HEADERS = {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}
MAX_GAP_DAYS = 30               # a longer pause between two rows = a hole in the source's history (longest A-share closure ≈ 11 days)
MAX_EXTRA = 20                  # CSI indices from indices.txt beyond the broad list
CHART_DAYS = 750                # ≈ 3 years of daily points in each detail file

# (code, name, price source, valuation source)
BROAD = [("000001", "上证指数", "csi", "csi"), ("000300", "沪深300", "csi", "csi"), ("000905", "中证500", "csi", "csi"),
         ("000852", "中证1000", "csi", "csi"), ("932000", "中证2000", "csi", "csi"), ("000510", "中证A500", "csi", "csi"),
         ("000906", "中证800", "csi", "csi"), ("000985", "中证全指", "csi", "csi"), ("399006", "创业板指", "em:0.399006", "dj:SZ399006"),
         ("000688", "科创50", "csi", "csi")]

# ---------------------------------------------------------------- fixed rule parameters (§8; locked before backtests)
VAL_YEARS, VAL_MIN_YEARS = 10, 3.0
ZONES = ["极低估", "低估", "适中", "高估", "极高估"]
ZONE_EDGES = [10, 30, 70, 90]           # ≤10 极低估, ≤30 低估, <70 适中, <90 高估, else 极高估
ZONE_BUFFER = 3                          # hysteresis: a zone changes only 3 points past the boundary (agreed 2026-11-12)
BANDS = [(80, 100), (60, 90), (40, 70), (10, 40), (0, 20)]
TRENDS = ["下降", "转折", "上升"]
BASE_OFFSET = {"下降": -5, "转折": 0, "上升": 5}      # base = band midpoint ±5 (agreed 2026-11-12)
OVERHEAT_CUT = 10
STEP = 10                                # one 档 = 10 % position
ACT_MIN = 10                             # the plan changes only when the target moved ≥ 10 points (no 5-point fiddling)
SAME_DIR_GAP = 20                        # at most one same-direction event step per 20 trading days
SLOPE_DAYS, FLAT_PCT = 20, 0.5
GLUE_PCT = 2.0
OVERHEAT_ON, OVERHEAT_OFF, OVERHEAT_MIN_DAYS = 90, 85, 750
CROSS_MIN_GAP, CROSS_DAYS, CROSS_MAX_DAYS = 0.3, 3, 10
PULLBACK_PCT = 3.0
CONVERGE_PCT, DIVERGE_PCT = 0.5, 1.5
SWING_DAYS, SWING_COOLDOWN = 60, 20
MA_LINES = (20, 51, 120, 250, 850)

# event step table (§8.6 step 3): (zone group, trend) → {event: steps}; anything not listed = 0
EVENT_STEPS = {
    ("cheap", "下降"): {"金叉": 1}, ("cheap", "转折"): {"金叉": 2, "回踩": 1}, ("cheap", "上升"): {"金叉": 1, "回踩": 1},
    ("mid", "下降"): {"死叉": -1}, ("mid", "转折"): {"金叉": 1}, ("mid", "上升"): {"金叉": 1, "回踩": 1},
    ("dear", "下降"): {"死叉": -2}, ("dear", "转折"): {"死叉": -1}, ("dear", "上升"): {"死叉": -1},
}
EVENT_DIR = {"金叉": 1, "回踩": 1, "死叉": -1}
LEVELS = {  # key → (rank, words); rank: +3 strongest buy … −3 strongest sell
    "buy3": (3, "可考虑买入"), "buy2": (2, "可考虑分批加仓"), "buy1": (1, "可关注，暂不动"), "hold": (0, "持有，不操作"),
    "sell1": (-1, "注意风险"), "sell2": (-2, "可考虑减仓"), "sell3": (-3, "可考虑卖出"), "none": (0, "估值数据不足，不给建议")}
SWING_NAMES = {"B1": "上升趋势回踩", "B2": "低估超跌", "B3": "粘合突破", "S1": "高估滞涨", "S2": "顶背离", "S3": "趋势破位"}


def log(msg):
    print(msg, flush=True)


def time_left():
    return TIME_BUDGET - (time.monotonic() - STARTED)


def zone_group(z):
    return "cheap" if z <= 1 else "mid" if z == 2 else "dear"


def raw_zone(v):
    return 0 if v <= 10 else 1 if v <= 30 else 2 if v < 70 else 3 if v < 90 else 4


def next_zone(cur, v):
    """Zone with hysteresis: from `cur`, move only when v is ZONE_BUFFER points past the boundary."""
    r = raw_zone(v)
    if cur is None or r == cur:
        return r
    if r > cur:                                   # dearer: boundary between j-1 and j is ZONE_EDGES[j-1]
        best = cur
        for j in range(cur + 1, r + 1):
            if v >= ZONE_EDGES[j - 1] + ZONE_BUFFER:
                best = j
        return best
    best = cur
    for j in range(cur - 1, r - 1, -1):           # cheaper: boundary between j and j+1 is ZONE_EDGES[j]
        if v <= ZONE_EDGES[j] - ZONE_BUFFER:
            best = j
    return best


def slope_state(s):
    return None if s is None else 1 if s > FLAT_PCT else -1 if s < -FLAT_PCT else 0


DIR_TEXT = {1: "向上", 0: "走平", -1: "向下", None: "--"}


# ---------------------------------------------------------------- indicators (pure)

def moving_average(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_wilder(xs, n=14):
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    g = sum(max(0.0, xs[i] - xs[i - 1]) for i in range(1, n + 1)) / n
    l = sum(max(0.0, xs[i - 1] - xs[i]) for i in range(1, n + 1)) / n
    out[n] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(xs)):
        ch = xs[i] - xs[i - 1]
        g = (g * (n - 1) + max(0.0, ch)) / n
        l = (l * (n - 1) + max(0.0, -ch)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def ema(xs, n):
    a, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + a * (x - e)
        out.append(e)
    return out


def macd(xs):
    dif = [a - b for a, b in zip(ema(xs, 12), ema(xs, 26))]
    dea = ema(dif, 9)
    return dif, dea, [2 * (a - b) for a, b in zip(dif, dea)]       # bar = 2 × (DIF − DEA), as in Chinese software


def valuation_pcts(obs):
    """obs: [(date, pe, pb)] sorted (daily or weekly). → {date: (pe_pct, pb_pct)}; trailing 10 years, ≥ 3 years
    since the first observation, only positive values (same convention as 低估行业)."""
    out = {}
    if not obs:
        return out
    first = fm.d_of(obs[0][0])
    win_pe, win_pb, q = [], [], []
    head = 0
    for d, pe, pb in obs:
        cur = fm.d_of(d)
        start = fm.years_before(cur, VAL_YEARS).isoformat()
        while head < len(q) and q[head][0] <= start:
            _, ope, opb = q[head]
            head += 1
            if ope is not None and ope > 0:
                del win_pe[bisect.bisect_left(win_pe, ope)]
            if opb is not None and opb > 0:
                del win_pb[bisect.bisect_left(win_pb, opb)]
        q.append((d, pe, pb))
        if pe is not None and pe > 0:
            bisect.insort(win_pe, pe)
        if pb is not None and pb > 0:
            bisect.insort(win_pb, pb)
        if (cur - first).days / 365.25 < VAL_MIN_YEARS:
            continue
        out[d] = (fm.pct_below(win_pe, pe) if pe is not None and pe > 0 and win_pe else None,
                  fm.pct_below(win_pb, pb) if pb is not None and pb > 0 and win_pb else None)
    return out


def combine_valuation(pe, pb, pe_pct, pb_pct, has_pb):
    """§8.5 ①: PE > 0 → mean of PE and PB percentiles; PE ≤ 0 or missing → PB only; PE-only sources → PE."""
    if pe is not None and pe > 0 and pe_pct is not None and pb_pct is not None:
        return (pe_pct + pb_pct) / 2, "PE+PB"
    if pb_pct is not None and (pe is None or pe <= 0 or pe_pct is None):
        return pb_pct, "PB（亏损期只用 PB）" if pe is not None and pe <= 0 else "PB"
    if pe_pct is not None and not has_pb:
        return pe_pct, "PE"
    return None, ""


# ---------------------------------------------------------------- the engine (pure; tests re-compute it independently)

def analyze(dates, closes, amounts, val_obs, has_pb):
    """dates/closes/amounts: daily price series (amounts may hold None). val_obs: [(date, pe, pb)].
    Returns per-day arrays and today's detailed state."""
    n = len(closes)
    ma = {k: moving_average(closes, k) for k in MA_LINES}
    slope = {}
    for k in (20, 51, 120, 250, 850):
        m = ma[k]
        slope[k] = [None if i < SLOPE_DAYS or m[i] is None or m[i - SLOPE_DAYS] is None else (m[i] / m[i - SLOPE_DAYS] - 1) * 100
                    for i in range(n)]
    sst = {k: [slope_state(x) for x in v] for k, v in slope.items()}
    rsi = rsi_wilder(closes)
    dif, dea, hist = macd(closes)

    # valuation per day: latest observation on or before the day (≤ 14 calendar days old)
    pcts = valuation_pcts(val_obs)
    vdates = [o[0] for o in val_obs]
    vmap = {o[0]: o for o in val_obs}
    vpct, vbasis, vraw = [None] * n, [""] * n, [None] * n
    for i, d in enumerate(dates):
        j = bisect.bisect_right(vdates, d) - 1
        if j < 0 or (fm.d_of(d) - fm.d_of(vdates[j])).days > 14:
            continue
        od = vdates[j]
        if od not in pcts:
            continue
        _, pe, pb = vmap[od]
        v, b = combine_valuation(pe, pb, pcts[od][0], pcts[od][1], has_pb)
        vpct[i], vbasis[i], vraw[i] = v, b, (od, pe, pb, pcts[od][0], pcts[od][1])

    zone, z = [None] * n, None
    for i in range(n):
        if vpct[i] is None:
            z = None                                       # a gap in valuation restarts the zone
            continue
        z = next_zone(z, vpct[i])
        zone[i] = z

    trend = [None] * n
    for i in range(n):
        m, s = ma[250][i], sst[250][i]
        if m is None or s is None:
            continue
        trend[i] = "上升" if closes[i] > m and s == 1 else "下降" if closes[i] < m and s == -1 else "转折"

    # 过热: distance above MA850 vs all earlier distances (expanding), with hysteresis 90 on / 85 off
    over, over_pct, dist850 = [False] * n, [None] * n, [None] * n
    seen, on = [], False
    for i in range(n):
        m = ma[850][i]
        if m is None:
            continue
        d850 = closes[i] / m - 1
        dist850[i] = d850 * 100
        bisect.insort(seen, d850)
        if len(seen) < OVERHEAT_MIN_DAYS:
            continue
        p = fm.pct_below(seen, d850)
        over_pct[i] = p
        on = (p >= OVERHEAT_ON and d850 > 0) if not on else not (p < OVERHEAT_OFF or d850 <= 0)
        over[i] = on

    def align_at(i):
        vals = [ma[k][i] for k in (20, 51, 120, 250)]
        if any(v is None for v in vals):
            return None
        spread = (max(vals) / min(vals) - 1) * 100
        if spread < GLUE_PCT:
            return "粘合"
        if vals[0] > vals[1] > vals[2] > vals[3]:
            return "多头排列"
        if vals[0] < vals[1] < vals[2] < vals[3]:
            return "空头排列"
        return "交织"
    align = [align_at(i) for i in range(n)]
    spread4 = [None if align[i] is None else (max(ma[k][i] for k in (20, 51, 120, 250)) / min(ma[k][i] for k in (20, 51, 120, 250)) - 1) * 100
               for i in range(n)]

    # ③ timing events
    gap = [None if ma[20][i] is None or ma[51][i] is None else (ma[20][i] / ma[51][i] - 1) * 100 for i in range(n)]
    events = [[] for _ in range(n)]
    run_sign, run_start, confirmed, conv = 0, None, False, False
    for i in range(n):
        g = gap[i]
        if g is None:
            continue
        sign = 1 if g > 0 else -1 if g < 0 else 0
        prev = gap[i - 1] if i > 0 else None
        if sign != run_sign:
            # a run starts only on a real crossing (the previous day was on the other side or exactly on the line)
            run_start = i if (prev is not None and sign != 0) else None
            run_sign, confirmed = sign, False
        if run_start is not None and not confirmed and sign != 0:
            k = i - run_start + 1
            if CROSS_DAYS <= k <= CROSS_MAX_DAYS and abs(g) >= CROSS_MIN_GAP:
                events[i].append("金叉" if sign > 0 else "死叉")
                confirmed = True
        # 回踩: yesterday dipped below MA20 (within 3 %, not below MA51) in an MA51-up uptrend; today back above MA20
        if i >= 1 and gap[i - 1] is not None and gap[i - 1] > 0 and sst[51][i - 1] == 1:
            c1, m20, m51 = closes[i - 1], ma[20][i - 1], ma[51][i - 1]
            if c1 < m20 and c1 >= m20 * (1 - PULLBACK_PCT / 100) and c1 >= m51 and closes[i] > ma[20][i]:
                events[i].append("回踩")
        if abs(g) <= CONVERGE_PCT:
            conv = True
        elif conv and abs(g) >= DIVERGE_PCT:
            events[i].append("发散向上" if g > 0 else "发散向下")
            conv = False

    # position plan (§8.6) — base by zone and trend, event steps, hold band of 10 points, valuation discipline
    target, held, action, cause = [None] * n, [None] * n, [0] * n, [[] for _ in range(n)]
    ev_step = [{} for _ in range(n)]                       # event → (table step, effective points, note)
    off, H, last_dir = 0, None, {1: -10 ** 9, -1: -10 ** 9}
    prev_key = None
    for i in range(n):
        z, tr = zone[i], trend[i]
        if z is None or tr is None:
            off, H, prev_key = 0, None, None
            continue
        lo, hi = BANDS[z]
        base = (lo + hi) / 2 + BASE_OFFSET[tr] - (OVERHEAT_CUT if over[i] else 0)
        T = min(hi, max(lo, base + off))
        for ev in events[i]:
            if ev not in EVENT_DIR:
                continue
            st = EVENT_STEPS.get((zone_group(z), tr), {}).get(ev, 0)
            note = ""
            if st and i - last_dir[EVENT_DIR[ev]] < SAME_DIR_GAP:
                st_eff, note = 0, "20 日内已响应过同方向事件"
            else:
                st_eff = st
            newT = min(hi, max(lo, T + STEP * st_eff))
            if st_eff and newT == T:
                note = "已在区间" + ("上限" if st_eff > 0 else "下限")
            if newT != T:
                last_dir[EVENT_DIR[ev]] = i
            ev_step[i][ev] = (st, newT - T, note)
            T = newT
        off = T - base
        target[i] = T
        key = (z, tr, over[i])
        if H is None:
            H = T
        else:
            if prev_key is not None:
                pz, ptr, pov = prev_key
                if z != pz:
                    cause[i].append(f"估值区 {ZONES[pz]} → {ZONES[z]}")
                if tr != ptr:
                    cause[i].append(f"趋势 {ptr} → {tr}")
                if over[i] != pov:
                    cause[i].append("出现过热" if over[i] else "过热解除")
            for ev, (st, eff, note) in ev_step[i].items():
                if eff:
                    cause[i].append(f"{ev}（{'+' if eff > 0 else ''}{eff:g}%）")
            if H < lo or H > hi:
                cause[i].append(f"仓位必须回到 {lo}–{hi}% 区间")
                action[i], H = T - H, T
            elif abs(T - H) >= ACT_MIN:
                if zone_group(z) == "cheap" and T < H:
                    pass                                   # cheap: never reduce on weakening trend (valuation protects)
                elif zone_group(z) == "dear" and T > H:
                    pass                                   # dear: never add on strengthening trend (no chasing)
                else:
                    action[i], H = T - H, T
        held[i] = H
        prev_key = key

    # swing signals (§8.9)
    hi60 = [None] * n
    for i in range(59, n):
        hi60[i] = max(closes[i - 59:i + 1])
    amt = [a if a is not None and a > 0 else None for a in amounts]
    def amt_mean(i, k=20):
        xs = amt[max(0, i - k):i]
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if len(xs) >= k * 0.8 else None
    swing_today = [[] for _ in range(n)]
    swing_exit = [[] for _ in range(n)]
    open_tr, last_s = {}, {}
    pos_runs, cur_peak, prev_peak = [], None, None
    def cond_b1(i):
        z = zone[i]
        return (z is not None and z <= 2 and trend[i] == "上升" and ma[51][i] is not None and rsi[i] is not None
                and 0 <= closes[i] / ma[51][i] - 1 <= 0.02 and rsi[i] < 45)
    def cond_b3(i):
        z = zone[i]
        if z is None or z >= 3 or i < 21 or any(spread4[j] is None or spread4[j] >= GLUE_PCT for j in range(i - 20, i)):
            return False
        top = max(ma[k][i] for k in (20, 51, 120, 250))
        m = amt_mean(i)
        return closes[i] > top * 1.03 and amt[i] is not None and m is not None and amt[i] >= 1.5 * m
    for i in range(n):
        h = hist[i]
        # MACD positive-run peaks (S2): the previous completed run's peak vs the current one
        if h > 0:
            cur_peak = h if cur_peak is None else max(cur_peak, h)
        else:
            if cur_peak is not None:
                prev_peak, cur_peak = cur_peak, None
        # exits first
        for sid in list(open_tr):
            t0 = open_tr[sid]
            days = i - t0
            reason = None
            if sid == "B1" and ma[120][i] is not None and closes[i] < ma[120][i]:
                reason = "跌破 MA120（止损）"
            elif sid == "B2" and ma[51][i] is not None and rsi[i] is not None and closes[i] > ma[51][i] and rsi[i] > 60:
                reason = "回到 MA51 上方且 RSI > 60（止盈）"
            elif sid == "B3" and closes[i] < max(ma[k][i] for k in (20, 51, 120, 250)):
                reason = "跌回均线群（假突破止损）"
            if reason is None and days >= SWING_DAYS:
                reason = f"持有 {SWING_DAYS} 个交易日到期"
            if reason:
                swing_exit[i].append((sid, t0, reason))
                del open_tr[sid]
        z = zone[i]
        if z is None or i < 1:
            continue
        if "B1" not in open_tr and cond_b1(i) and not cond_b1(i - 1):
            swing_today[i].append("B1"); open_tr["B1"] = i
        if "B2" not in open_tr and z <= 1 and ma[20][i] is not None and ma[20][i - 1] is not None \
                and closes[i] > ma[20][i] and closes[i - 1] <= ma[20][i - 1]:
            setup = any((j >= 20 and closes[j] / closes[j - 20] - 1 <= -0.08) or (rsi[j] is not None and rsi[j] <= 30)
                        for j in range(max(0, i - 19), i + 1))
            if setup:
                swing_today[i].append("B2"); open_tr["B2"] = i
        if "B3" not in open_tr and cond_b3(i) and not cond_b3(i - 1):
            swing_today[i].append("B3"); open_tr["B3"] = i
        if z >= 3 and over[i] and ma[20][i] is not None and ma[20][i - 1] is not None and closes[i] < ma[20][i] \
                and closes[i - 1] >= ma[20][i - 1] and i - last_s.get("S1", -10 ** 9) >= SWING_COOLDOWN:
            if any(hi60[j] is not None and closes[j] >= hi60[j] for j in range(max(0, i - 10), i)):
                swing_today[i].append("S1"); last_s["S1"] = i
        if z >= 2 and hi60[i] is not None and closes[i] >= hi60[i] and prev_peak is not None and prev_peak > 0 \
                and i - last_s.get("S2", -10 ** 9) >= SWING_COOLDOWN:
            cur = cur_peak if h > 0 else h
            if cur is not None and cur < prev_peak:
                swing_today[i].append("S2"); last_s["S2"] = i
        if z >= 2 and ma[250][i] is not None and closes[i] < ma[250][i] * 0.97 and sst[250][i] in (0, -1) \
                and any(sst[250][j] == 1 for j in range(max(0, i - 60), i)) and i - last_s.get("S3", -10 ** 9) >= SWING_COOLDOWN:
            prev_ok = ma[250][i - 1] is not None and closes[i - 1] < ma[250][i - 1] * 0.97 and sst[250][i - 1] in (0, -1)
            if not prev_ok:
                swing_today[i].append("S3"); last_s["S3"] = i

    # plain-language level per day (§8.8, words follow what the plan does — agreed 2026-11-12)
    level = ["none"] * n
    for i in range(n):
        if zone[i] is None or trend[i] is None:
            continue
        a = action[i]
        if a >= 20:
            level[i] = "buy3"
        elif a > 0:
            level[i] = "buy2"
        elif a <= -20:
            level[i] = "sell3"
        elif a < 0:
            level[i] = "sell2"
        elif swing_today[i]:
            ss = swing_today[i]
            if any(s.startswith("B") for s in ss) and not any(s.startswith("S") for s in ss):
                level[i] = "buy2"
            elif any(s in ("S1", "S2") for s in ss) and zone[i] == 4:
                level[i] = "sell3"
            elif any(s.startswith("S") for s in ss):
                level[i] = "sell2"
            else:
                level[i] = "hold"
        elif any(EVENT_DIR.get(ev) == 1 for ev in ev_step[i]):
            level[i] = "buy1"
        elif any(EVENT_DIR.get(ev) == -1 for ev in ev_step[i]):
            level[i] = "sell1"
        elif over[i] and i > 0 and not over[i - 1]:
            level[i] = "sell1"
        else:
            level[i] = "hold"

    regime = [regime_at(i, closes, ma, sst, align, over, zone) for i in range(n)]
    return {"ma": ma, "slope": slope, "sst": sst, "rsi": rsi, "dif": dif, "dea": dea, "hist": hist, "vpct": vpct,
            "vbasis": vbasis, "vraw": vraw, "zone": zone, "trend": trend, "over": over, "over_pct": over_pct,
            "dist850": dist850, "align": align, "spread4": spread4, "gap": gap, "events": events, "ev_step": ev_step,
            "target": target, "held": held, "action": action, "cause": cause, "swing": swing_today,
            "swing_exit": swing_exit, "open_swing": dict(open_tr), "level": level, "regime": regime, "hi60": hi60}


REGIMES = ["过热 / 牛市末期", "牛市中期", "牛市初期", "底部反转初期", "熊市末期 / 筑底", "熊市", "震荡"]


def regime_checks(i, closes, ma, sst, align, over, zone):
    """§8.7 (confirmed by the user): conditions of every state in order; the first satisfied one wins."""
    c, m = closes[i], {k: ma[k][i] for k in MA_LINES}
    s = {k: sst[k][i] for k in (20, 51, 120, 250, 850)}
    z = zone[i]
    if m[250] is None or s[250] is None:
        return None
    up250_recent = s[250] == 1 and any(sst[250][j] in (0, -1) for j in range(max(0, i - 60), i))
    dear_bull = z is not None and z >= 3 and align[i] == "多头排列"
    chk = [
        (REGIMES[0], [("过热标记成立", over[i]), ("或：估值 ≥ 高估 且 多头排列", dear_bull)], over[i] or dear_bull),
        (REGIMES[1], [("多头排列", align[i] == "多头排列"), ("MA51 向上", s[51] == 1), ("MA120 向上", s[120] == 1),
                      ("MA250 向上", s[250] == 1), ("未过热", not over[i])], None),
        (REGIMES[2], [("收盘 > MA250", c > m[250]), ("MA250 近 60 日内由下降/走平转为向上", up250_recent)], None),
        (REGIMES[3], [("MA20 > MA51", m[20] is not None and m[51] is not None and m[20] > m[51]),
                      ("MA51 走平或向上", s[51] in (0, 1)), ("MA120 下降或走平", s[120] in (0, -1)),
                      ("MA250 下降或走平", s[250] in (0, -1)), ("收盘 > MA120", m[120] is not None and c > m[120])], None),
        (REGIMES[4], [("收盘 < MA250", c < m[250]), ("MA120 走平或向上，或四线粘合", s[120] in (0, 1) or align[i] == "粘合"),
                      ("估值 ≤ 低估" + ("" if z is not None else "（无估值）"), z is not None and z <= 1)], None),
        (REGIMES[5], [("收盘 < MA250", c < m[250]), ("MA250 向下", s[250] == -1),
                      ("MA120 < MA250", m[120] is not None and m[120] < m[250])], None),
        (REGIMES[6], [("以上都不满足", True)], True),
    ]
    return [(name, [(t, bool(ok)) for t, ok in conds], bool(all(ok for _, ok in conds)) if any_ is None else bool(any_))
            for name, conds, any_ in chk]


def regime_at(i, closes, ma, sst, align, over, zone):
    chk = regime_checks(i, closes, ma, sst, align, over, zone)
    if chk is None:
        return None
    return next(name for name, _, ok in chk if ok)


# ---------------------------------------------------------------- data sources

def fetch_csi_long(code, today):
    url = fm.CSI_URL.format(code=code, start=CSI_START, end=today.strftime("%Y%m%d"))
    j = fm.get_json(url, fm.CSI_HEADERS, attempts=3, timeout=60)
    rows = j.get("data") if isinstance(j, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("CSI returned no rows")
    out, name = {}, ""
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = fm.ymd(r.get("tradeDate"))
        c = fm.num(r.get("close"))
        if not d or d[5:] == "01-01" or c is None or c <= 0:          # 1 January: CSI's phantom first row
            continue
        name = r.get("indexNameCn") or name
        a = fm.num(r.get("tradingValue"))
        out[d] = (c, a if a and a > 0 else None, fm.num(r.get("peg")))
    ds = sorted(out)
    return name, ds, [out[d][0] for d in ds], [out[d][1] for d in ds], [(d, out[d][2], None) for d in ds if out[d][2] is not None]


def fetch_em(secid):
    j = fm.get_json(EM_KLINE.format(secid=secid), EM_HEADERS, attempts=3, timeout=40)
    k = ((j or {}).get("data") or {}).get("klines") or []
    out = {}
    for line in k:
        p = str(line).split(",")
        if len(p) < 7:
            continue
        d, c, a = fm.ymd(p[0]), fm.num(p[2]), fm.num(p[6])
        if d and c and c > 0:
            out[d] = (c, a / 1e8 if a and a > 0 else None)              # amount in 亿元, like CSI
    if len(out) < 300:
        raise RuntimeError(f"Eastmoney returned {len(out)} bars")
    ds = sorted(out)
    return ds, [out[d][0] for d in ds], [out[d][1] for d in ds]


def fetch_danjuan(code):
    vals = {}
    for kind in ("pe", "pb"):
        j = fm.get_json(DJ_URL.format(kind=kind, code=code), DJ_HEADERS, attempts=2, timeout=30)
        d = j.get("data") if isinstance(j, dict) else None
        lst = next((v for v in (d or {}).values() if isinstance(v, list) and v and isinstance(v[0], dict)), None) if isinstance(d, dict) else None
        if not lst:
            raise RuntimeError(f"蛋卷 {kind}_history {code}: no list")
        for x in lst:
            ts, v = x.get("ts"), fm.num(x.get(kind))
            if ts and v is not None:
                day = datetime.fromtimestamp(int(ts) / 1000, fm.BJ).date().isoformat()
                vals.setdefault(day, [None, None])[0 if kind == "pe" else 1] = v
    return [(d, v[0], v[1]) for d, v in sorted(vals.items())]


# ---------------------------------------------------------------- output

def r2(x, nd=2):
    return None if x is None else round(x, nd)


def last_segment(dates):
    """Index where the last continuous stretch of the price history starts (and the gap before it, in days)."""
    for k in range(len(dates) - 1, 0, -1):
        gap = (fm.d_of(dates[k]) - fm.d_of(dates[k - 1])).days
        if gap > MAX_GAP_DAYS:
            return k, gap
    return 0, 0


def build_entry(code, name, group, source, dates, closes, amounts, val_obs, has_pb, val_source):
    k, gap = last_segment(dates)
    history_note = ""
    if k:                                   # moving averages must not bridge a hole: use the last continuous stretch only
        history_note = f"价格历史在 {dates[k - 1]} 到 {dates[k]} 之间中断（{gap} 天），只用 {dates[k]} 以后的数据"
        dates, closes, amounts = dates[k:], closes[k:], amounts[k:]
        val_obs = [o for o in val_obs if o[0] >= dates[0]]
    A = analyze(dates, closes, amounts, val_obs, has_pb)
    i = len(dates) - 1
    if i < 270 or A["trend"][i] is None:
        raise RuntimeError(f"history too short ({len(dates)} days)")
    ma, sst = A["ma"], A["sst"]
    z = A["zone"][i]

    def since(arr, i):
        v, j = arr[i], i
        while j > 0 and arr[j - 1] == v:
            j -= 1
        return dates[j]
    raw = A["vraw"][i]
    val = {"source": val_source, "basis": A["vbasis"][i], "pct": r2(A["vpct"][i], 1), "zone": ZONES[z] if z is not None else None,
           "zone_raw": ZONES[raw_zone(A["vpct"][i])] if A["vpct"][i] is not None else None,
           "zone_since": since(A["zone"], i) if z is not None else None,
           "date": raw[0] if raw else None, "pe": r2(raw[1]) if raw else None, "pb": r2(raw[2], 3) if raw else None,
           "pe_pct": r2(raw[3], 1) if raw else None, "pb_pct": r2(raw[4], 1) if raw else None,
           "from": val_obs[0][0] if val_obs else None}
    if z is None:
        short = bool(val_obs) and (fm.d_of(dates[i]) - fm.d_of(val_obs[0][0])).days / 365.25 < VAL_MIN_YEARS
        val["note"] = "估值历史不足 3 年" if short else "估值数据中断" if val_obs else "没有估值数据"
        if short:
            val["ready_from"] = (fm.d_of(val_obs[0][0]) + timedelta(days=int(VAL_MIN_YEARS * 365.25) + 1)).isoformat()
    if z is not None:
        # how far the percentile must move for a zone change (hysteresis made visible)
        val["next"] = {"cheaper": ZONE_EDGES[z - 1] - ZONE_BUFFER if z > 0 else None,
                       "dearer": ZONE_EDGES[z] + ZONE_BUFFER if z < 4 else None}
    trend = {"state": A["trend"][i], "since": since(A["trend"], i), "align": A["align"][i], "spread": r2(A["spread4"][i]),
             "ma": {str(k): r2(ma[k][i]) for k in MA_LINES}, "slope": {str(k): r2(A["slope"][k][i]) for k in (20, 51, 120, 250, 850)},
             "dir": {str(k): DIR_TEXT[sst[k][i]] for k in (20, 51, 120, 250, 850)},
             "above850": None if ma[850][i] is None else closes[i] > ma[850][i], "dist850": r2(A["dist850"][i]),
             "overheat": A["over"][i], "over_pct": r2(A["over_pct"][i], 1)}
    ev_i = A["ev_step"][i]
    today_events = [{"event": ev, "step": ev_i[ev][0] if ev in ev_i else 0, "effect": ev_i[ev][1] if ev in ev_i else 0,
                     "note": ev_i[ev][2] if ev in ev_i else ""} for ev in A["events"][i]]
    last_cross = next(((dates[j], ev) for j in range(i, -1, -1) for ev in A["events"][j] if ev in ("金叉", "死叉")), None)
    timing = {"gap": r2(A["gap"][i]), "above": None if A["gap"][i] is None else A["gap"][i] > 0,
              "dir20": DIR_TEXT[sst[20][i]], "dir51": DIR_TEXT[sst[51][i]], "events": today_events,
              "last_cross": {"date": last_cross[0], "event": last_cross[1]} if last_cross else None}
    reg = regime_checks(i, closes, ma, sst, A["align"], A["over"], A["zone"])
    regime = {"state": A["regime"][i], "since": since(A["regime"], i),
              "checks": [{"state": nm, "ok": ok, "conds": [[t, c] for t, c in conds]} for nm, conds, ok in reg]}
    pos = None
    if z is not None:
        lo, hi = BANDS[z]
        base = (lo + hi) / 2 + BASE_OFFSET[A["trend"][i]] - (OVERHEAT_CUT if A["over"][i] else 0)
        last_act = next((j for j in range(i, -1, -1) if A["action"][j]), None)
        start = next(j for j in range(i, -1, -1) if j == 0 or A["held"][j - 1] is None)
        pos = {"band": [lo, hi], "base": base, "target": A["target"][i], "held": A["held"][i], "action": A["action"][i],
               "causes": A["cause"][i] if A["action"][i] else [], "plan_from": dates[start],
               "last_action": None if last_act is None else {
                   "date": dates[last_act], "from": A["held"][last_act] - A["action"][last_act], "to": A["held"][last_act],
                   "level": A["level"][last_act], "causes": A["cause"][last_act]}}
        if not A["action"][i] and A["target"][i] != A["held"][i]:
            pos["pending"] = ("低估区不因趋势转弱减仓" if zone_group(z) == "cheap" and A["target"][i] < A["held"][i] else
                              "高估区不因趋势转强加仓" if zone_group(z) == "dear" and A["target"][i] > A["held"][i] else
                              f"与目标相差不到 {ACT_MIN}%，暂不调整")
    swing = {"today": [{"id": s, "name": SWING_NAMES[s]} for s in A["swing"][i]],
             "open": [{"id": s, "name": SWING_NAMES[s], "since": dates[t0], "days": i - t0} for s, t0 in sorted(A["open_swing"].items())],
             "exits": [{"id": s, "name": SWING_NAMES[s], "since": dates[t0], "reason": r} for s, t0, r in A["swing_exit"][i]]}
    amt = amounts
    prev = [a for a in amt[max(0, i - 20):i] if a]
    win = sorted(a for a in amt[max(0, i - 249):i + 1] if a)
    ind = {"amount": r2(amt[i], 1), "rsi14": r2(A["rsi"][i], 1), "macd": {"dif": r2(A["dif"][i], 3), "dea": r2(A["dea"][i], 3), "bar": r2(A["hist"][i], 3)},
           "vol_ratio": r2(amt[i] / (sum(prev) / len(prev))) if amt[i] and len(prev) >= 16 else None,
           "vol_pct": fm.pct_below(win, amt[i]) if amt[i] and len(win) >= 200 else None,
           "hi250": r2(max(closes[max(0, i - 249):i + 1])), "lo250": r2(min(closes[max(0, i - 249):i + 1]))}
    lv = A["level"][i]
    summary = {"code": code, "name": name, "group": group, "source": source, "date": dates[i], "close": r2(closes[i]),
               "chg": r2((closes[i] / closes[i - 1] - 1) * 100), "history_from": dates[0], "history_note": history_note,
               "level": lv, "words": LEVELS[lv][1], "rank": LEVELS[lv][0], "valuation": val, "trend": trend, "timing": timing,
               "regime": regime, "position": pos, "swing": swing, "ind": ind,
               "reason": reason_text(name, val, trend, timing, pos, swing, lv)}
    # detail file: about three years of daily points
    k0 = max(0, len(dates) - CHART_DAYS)
    ev3 = [[dates[j], ev, A["ev_step"][j].get(ev, (0, 0, ""))[1]] for j in range(k0, len(dates)) for ev in A["events"][j]]
    acts = [[dates[j], A["held"][j] - A["action"][j], A["held"][j], A["level"][j]] for j in range(k0, len(dates)) if A["action"][j]]
    sw3 = [[dates[j], s] for j in range(k0, len(dates)) for s in A["swing"][j]]
    regs = [[dates[j], A["regime"][j]] for j in range(k0, len(dates)) if A["regime"][j] and (j == k0 or A["regime"][j] != A["regime"][j - 1])]
    detail = {"code": code, "name": name, "date": dates[i], "rules": RULES,
              "dates": [int(d.replace("-", "")) for d in dates[k0:]], "close": [r2(x) for x in closes[k0:]],
              "ma": {str(k): [r2(x) for x in ma[k][k0:]] for k in MA_LINES},
              "held": A["held"][k0:], "zone": A["zone"][k0:], "events": ev3, "actions": acts, "swing": sw3, "regimes": regs}
    return summary, detail


def reason_text(name, val, trend, timing, pos, swing, lv):
    parts = []
    if val["zone"]:
        parts.append(f"{val['zone']}（{val['basis']} 百分位 {val['pct']:.0f}%）")
    else:
        parts.append(val.get("note") or "无估值")
    parts.append(f"趋势{trend['state']}（年线{trend['dir']['250']}）")
    if trend["overheat"]:
        parts.append("过热")
    evs = [e["event"] for e in timing["events"]]
    if evs:
        parts.append("今日" + "、".join(evs))
    if swing["today"]:
        parts.append("波段 " + "、".join(f"{s['id']} {s['name']}" for s in swing["today"]))
    if pos:
        if pos["action"]:
            parts.append(f"建议仓位 {pos['held'] - pos['action']:g}% → {pos['held']:g}%（区间 {pos['band'][0]}–{pos['band'][1]}%）")
        else:
            parts.append(f"建议仓位 {pos['held']:g}%（区间 {pos['band'][0]}–{pos['band'][1]}%）")
    return f"{name} · {LEVELS[lv][1]} — " + " · ".join(parts)


def load_sw():
    """申万 industries with full price history (fetch_market.py v15.2+). → [(code, name, dates, closes, amounts, val_obs)]"""
    hist, inds = fm.sw_load()
    out, waiting = [], []
    for code in sorted(inds):
        info = inds[code]
        days = hist.get(code) or {}
        if not info.get("prices") or not days:
            waiting.append(code)
            continue
        ds = sorted(d for d, v in days.items() if v[3] is not None and v[3] > 0)
        out.append((code, info.get("name") or code, ds, [days[d][3] for d in ds], [days[d][4] for d in ds],
                    [(d, days[d][0], days[d][1]) for d in sorted(days) if days[d][0] is not None or days[d][1] is not None]))
    return out, waiting


def signals_dir():
    return os.path.join(fm.DATA_DIR, "signals")


def main():
    global STARTED
    STARTED = time.monotonic()
    log(f"fetch_signals.py {SIGNALS_VERSION} (rules {RULES})")
    now = datetime.now(fm.BJ)
    if os.environ.get("MARKET_TODAY"):
        now = datetime.fromisoformat(os.environ["MARKET_TODAY"]).replace(tzinfo=fm.BJ)
    today = now.date()
    out_path = os.path.join(fm.DATA_DIR, "signals.json")
    previous = fm.read_json(out_path, {}) or {}
    prev_by_code = {e.get("code"): e for e in previous.get("indices", []) if isinstance(e, dict)}
    entries, errors, written = [], {}, 0

    def finish(code, group, fn):
        nonlocal written
        try:
            summary, detail = fn()
            entries.append(summary)
            log(f"  {code} {summary['name']}: {summary['date']} {summary['words']} — "
                f"{summary['valuation']['zone'] or summary['valuation'].get('note')} · {summary['trend']['state']} · "
                f"{summary['regime']['state']}" + (f" · 仓位 {summary['position']['held']:g}%" if summary['position'] else "")
                + (f" [{summary['history_note']}]" if summary.get('history_note') else ""))
            if fm.write_if_changed(os.path.join(signals_dir(), f"{code}.json"), detail, ignore=()):
                written += 1
            return summary
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:200]
            errors[code] = msg
            log(f"  {code}: failed ({msg})")
            old = prev_by_code.get(code)
            if old:
                entries.append({**old, "stale": True})
            return None

    extra = [c for c in fm.read_index_codes(fm.INDICES_FILE) if c not in {b[0] for b in BROAD}][:MAX_EXTRA]
    for code, name, psrc, vsrc in BROAD + [(c, "", "csi", "csi") for c in extra]:
        if time_left() < 30:
            errors[code] = "time budget used up"
            continue
        group = "宽基" if any(code == b[0] for b in BROAD) else "自选"

        def job(code=code, name=name, psrc=psrc, vsrc=vsrc, group=group):
            if psrc == "csi":
                nm, ds, cl, am, vo = fetch_csi_long(code, today)
                name2, src, vs, has_pb = name or nm or code, "中证指数官网（收盘、滚动市盈率、成交额）", "中证 滚动市盈率（无 PB）", False
            else:
                ds, cl, am = fetch_em(psrc.split(":", 1)[1])
                vo = fetch_danjuan(vsrc.split(":", 1)[1])
                name2, src, vs, has_pb = name, "东方财富日 K（收盘、成交额）", "蛋卷 PE / PB（周度）", True
            return build_entry(code, name2, group, src, ds, cl, am, vo, has_pb, vs)
        finish(code, group, job)
        fm.pause()

    sw, waiting = load_sw()
    for code, name, ds, cl, am, vo in sw:
        def job(code=code, name=name, ds=ds, cl=cl, am=am, vo=vo):
            return build_entry(code, name, "行业", "申万宏源研究（收盘、成交额、市盈率、市净率）", ds, cl, am, vo, True, "申万 PE / PB")
        finish(code, "行业", job)
    log(f"申万一级: {len(sw)} industries with prices" + (f"; {len(waiting)} still waiting for their price history" if waiting else ""))

    if not entries:
        log("Signals: nothing computed; previous files kept.")
        sys.exit(1)
    order = {"宽基": 0, "行业": 1, "自选": 2}
    entries.sort(key=lambda e: (order.get(e.get("group"), 9), e.get("code")))
    # sources publish at different times (Eastmoney can be a day ahead of CSI / 申万): the summary date is the date most
    # indices have; levels are counted for every fresh index at that date or later (older ones are shown, not counted)
    fresh = [e for e in entries if not e.get("stale")]
    by_date = {}
    for e in fresh:
        by_date[e["date"]] = by_date.get(e["date"], 0) + 1
    newest = max(by_date, key=lambda d: (by_date[d], d)) if by_date else max(e["date"] for e in entries)
    counts = {}
    for e in fresh:
        if e["date"] >= newest:
            counts[e["level"]] = counts.get(e["level"], 0) + 1
    doc = {"schema": 1, "version": SIGNALS_VERSION, "rules": RULES, "fetched_at_beijing": now.strftime("%Y-%m-%d %H:%M:%S"),
           "date": newest, "dates": dict(sorted(by_date.items())), "counts": counts, "sw_waiting": len(waiting), "errors": errors,
           "params": {"zone_edges": ZONE_EDGES, "zone_buffer": ZONE_BUFFER, "bands": BANDS, "base_offset": BASE_OFFSET,
                      "overheat": [OVERHEAT_ON, OVERHEAT_OFF], "step": STEP, "act_min": ACT_MIN, "same_dir_gap": SAME_DIR_GAP},
           "indices": entries}
    saved = fm.write_if_changed(out_path, doc)
    log(f"Signals: {len(entries)} indices ({len(errors)} failed), date {newest} (days {dict(sorted(by_date.items()))}), levels {counts}; "
        f"signals.json {'saved' if saved else 'unchanged'}, {written} detail files written; took {time.monotonic() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
