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
import math
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_market as fm                                   # noqa: E402  (shared helpers: HTTP, 申万 history, JSON files)

SIGNALS_VERSION = "v1.4"
RULES = "第一期定稿（2026-11-18：v3 分档时机、按波动缩放、行业估值依据）"
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
PLAN_KNOTS = [(5, 90), (20, 75), (50, 55), (80, 25), (95, 10)]   # plan v2: valuation percentile → position (the band midpoints)
PLAN_EVENT_CAP = 20                      # plan v2: event steps move the target at most ±20 from the valuation level
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
# volatility scaling (agreed 2026-11-18): every price-percentage threshold is in units of the index's own monthly volatility
# σ_m = std of daily log returns over the last 750 trading days (at least 250) × √20; k = σ_m / SIGMA_REF. SIGMA_REF is
# 沪深300's σ_m on 2026-11-17 (18.1 % a year), frozen, so 沪深300 keeps the agreed numbers. Without σ_m: k = 1.
VOL_DAYS, VOL_MIN_DAYS, SIGMA_REF = 750, 250, 5.12
TRAIL_SIGMA, B2_SIGMA, S3_SIGMA = 1.5, 1.5, 0.5           # multiples of σ_m (not of k)
B1_BAND_PCT, B3_BREAK_PCT = 2.0, 3.0                       # × k
# plan v3 (agreed 2026-11-18): valuation sets the target (plan v2); confirmation tiers decide when and how much
V3_TIER_CAP = {1: 10, 2: 20, 3: 100}                       # tier 3 closes the whole gap
V3_CALM_DAYS, V3_LOWHIGH_DAYS = 10, 60
# valuation basis per 申万 industry (agreed 2026-11-17): PB for financials and strong cyclicals, PE for steady / asset-light
SW_BASIS = {**{c: "pb" for c in ("801780", "801790", "801180", "801040", "801950", "801050", "801030", "801960", "801710", "801010")},
            **{c: "pe" for c in ("801120", "801150", "801110", "801980", "801210", "801750", "801760", "801080", "801770", "801740")}}
HK = [("HKHSI", "恒生指数", ["100.HSI"], "HKHSI", "avg"), ("HKHSCEI", "国企指数", ["100.HSCEI", "124.HSCEI"], "HKHSCEI", "avg"),
      ("HKHSTECH", "恒生科技", ["124.HSTECH", "100.HSTECH"], "HKHSTECH", "pe")]

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


def combine_valuation(pe, pb, pe_pct, pb_pct, basis):
    """§8.5 ① + industry table: "avg" = mean of PE and PB percentiles (PE ≤ 0 → PB); "pe" = PE (loss → PB);
    "pb" = PB (no PB → PE); "pe_only" = sources without PB (CSI). Returns (percentile, label)."""
    pe_ok = pe is not None and pe > 0 and pe_pct is not None
    if basis == "pe_only":
        return (pe_pct, "PE") if pe_ok else (None, "")
    if basis == "pb":
        if pb_pct is not None:
            return pb_pct, "PB"
        return (pe_pct, "PE（无 PB）") if pe_ok else (None, "")
    if basis == "pe":
        if pe_ok:
            return pe_pct, "PE"
        return (pb_pct, "PB（亏损期只用 PB）") if pb_pct is not None else (None, "")
    if pe_ok and pb_pct is not None:
        return (pe_pct + pb_pct) / 2, "PE+PB"
    if pb_pct is not None:
        return pb_pct, "PB（亏损期只用 PB）" if pe is not None and pe <= 0 else "PB"
    return (None, "")


# ---------------------------------------------------------------- the engine (pure; tests re-compute it independently)

def monthly_vol(closes):
    """σ_m per day (%, no look-ahead): std of the last ≤ 750 daily log returns (≥ 250) × √20."""
    n = len(closes)
    out = [None] * n
    r = [None] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    s1 = s2 = 0.0
    for i in range(1, n):
        s1 += r[i]; s2 += r[i] * r[i]
        if i - VOL_DAYS >= 1:
            s1 -= r[i - VOL_DAYS]; s2 -= r[i - VOL_DAYS] * r[i - VOL_DAYS]
        m = min(i, VOL_DAYS)
        if m >= VOL_MIN_DAYS:
            var = max(0.0, (s2 - s1 * s1 / m) / (m - 1))
            out[i] = math.sqrt(var) * math.sqrt(20) * 100
    return out


def analyze(dates, closes, amounts, val_obs, basis):
    """dates/closes/amounts: daily price series (amounts may hold None). val_obs: [(date, pe, pb)].
    basis: "pe_only" | "avg" | "pe" | "pb" (True / False = "avg" / "pe_only"). Returns per-day arrays."""
    basis = "avg" if basis is True else "pe_only" if basis is False else basis
    n = len(closes)
    sig = monthly_vol(closes)
    kf = [x / SIGMA_REF if x else 1.0 for x in sig]          # scale factor per day
    ma = {k: moving_average(closes, k) for k in MA_LINES}
    slope = {}
    for k in (20, 51, 120, 250, 850):
        m = ma[k]
        slope[k] = [None if i < SLOPE_DAYS or m[i] is None or m[i - SLOPE_DAYS] is None else (m[i] / m[i - SLOPE_DAYS] - 1) * 100
                    for i in range(n)]
    sst = {k: [None if x is None else 1 if x > FLAT_PCT * kf[i] else -1 if x < -FLAT_PCT * kf[i] else 0
               for i, x in enumerate(v)] for k, v in slope.items()}
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
        v, b = combine_valuation(pe, pb, pcts[od][0], pcts[od][1], basis)
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
        if spread < GLUE_PCT * kf[i]:
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
            if CROSS_DAYS <= k <= CROSS_MAX_DAYS and abs(g) >= CROSS_MIN_GAP * kf[i]:
                events[i].append("金叉" if sign > 0 else "死叉")
                confirmed = True
        # 回踩: yesterday dipped below MA20 (within 3 %, not below MA51) in an MA51-up uptrend; today back above MA20
        if i >= 1 and gap[i - 1] is not None and gap[i - 1] > 0 and sst[51][i - 1] == 1:
            c1, m20, m51 = closes[i - 1], ma[20][i - 1], ma[51][i - 1]
            if c1 < m20 and c1 >= m20 * (1 - PULLBACK_PCT * kf[i - 1] / 100) and c1 >= m51 and closes[i] > ma[20][i]:
                events[i].append("回踩")
        if abs(g) <= CONVERGE_PCT * kf[i]:
            conv = True
        elif conv and abs(g) >= DIVERGE_PCT * kf[i]:
            events[i].append("发散向上" if g > 0 else "发散向下")
            conv = False

    P1 = plan_v1(zone, trend, over, events)
    P = plan_v2(zone, trend, over, vpct, events)

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
                and 0 <= closes[i] / ma[51][i] - 1 <= B1_BAND_PCT * kf[i] / 100 and rsi[i] < 45)
    def cond_b3(i):
        z = zone[i]
        if z is None or z >= 3 or i < 21 or any(spread4[j] is None or spread4[j] >= GLUE_PCT * kf[j] for j in range(i - 20, i)):
            return False
        top = max(ma[k][i] for k in (20, 51, 120, 250))
        m = amt_mean(i)
        return closes[i] > top * (1 + B3_BREAK_PCT * kf[i] / 100) and amt[i] is not None and m is not None and amt[i] >= 1.5 * m
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
            setup = any((j >= 20 and closes[j] / closes[j - 20] - 1 <= -(B2_SIGMA * sig[j] / 100 if sig[j] else 0.08))
                        or (rsi[j] is not None and rsi[j] <= 30)
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
        s3 = lambda j: 1 - (S3_SIGMA * sig[j] / 100 if sig[j] else 0.03)
        if z >= 2 and ma[250][i] is not None and closes[i] < ma[250][i] * s3(i) and sst[250][i] in (0, -1) \
                and any(sst[250][j] == 1 for j in range(max(0, i - 60), i)) and i - last_s.get("S3", -10 ** 9) >= SWING_COOLDOWN:
            prev_ok = ma[250][i - 1] is not None and closes[i - 1] < ma[250][i - 1] * s3(i - 1) and sst[250][i - 1] in (0, -1)
            if not prev_ok:
                swing_today[i].append("S3"); last_s["S3"] = i

    P3 = plan_v3(P["target_raw"], zone, over, trend, events, closes, ma, sst, swing_today, sig)
    target, held, action, cause, ev_step = P["target"], P3["held"], P3["action"], P3["cause"], P["ev_step"]

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
            "band": P["band"], "base": P["base"], "val_level": P["level"], "v1": P1, "v2": P, "v3": P3, "sig": sig, "kf": kf,
            "swing_exit": swing_exit, "open_swing": dict(open_tr), "level": level, "regime": regime, "hi60": hi60}


def plan_v1(zone, trend, over, events):
    """The plan as frozen on 2026-11-12 (§8.18, bands per zone). Kept unchanged: phase 2 reports it next to plan_v2."""
    n = len(zone)
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
    band = [BANDS[z] if z is not None else None for z in zone]
    return {"target": target, "held": held, "action": action, "cause": cause, "ev_step": ev_step, "band": band}


def val_level(p):
    """Plan v2: position for a valuation percentile, piecewise linear through PLAN_KNOTS (flat outside)."""
    if p <= PLAN_KNOTS[0][0]:
        return float(PLAN_KNOTS[0][1])
    for (x0, y0), (x1, y1) in zip(PLAN_KNOTS, PLAN_KNOTS[1:]):
        if p <= x1:
            return y0 + (y1 - y0) * (p - x0) / (x1 - x0)
    return float(PLAN_KNOTS[-1][1])


def round5(x):
    return 5 * math.floor(x / 5 + 0.5)


def plan_v2(zone, trend, over, vpct, events):
    """Plan v2 (option A, 2026-11-17): the target follows the valuation percentile smoothly instead of jumping
    between bands; trend ±5 (cheap: never negative, dear: never positive), 过热 −10, event steps ±10 kept within ±20;
    the suggested position moves only when the unrounded target is ≥ 10 points away (then to the target rounded to 5);
    rounding before the comparison would turn an 8-point wobble into a 10-point trade. No forced moves."""
    n = len(zone)
    target, held, action, cause = [None] * n, [None] * n, [0] * n, [[] for _ in range(n)]
    base_l, band_l, level_l, traw = [None] * n, [None] * n, [None] * n, [None] * n
    ev_step = [{} for _ in range(n)]
    off, H, snap, last_dir = 0, None, None, {1: -10 ** 9, -1: -10 ** 9}
    for i in range(n):
        z, tr, p = zone[i], trend[i], vpct[i]
        if z is None or tr is None or p is None:
            off, H, snap = 0, None, None
            continue
        g = zone_group(z)
        f = val_level(p)
        t = BASE_OFFSET[tr]
        t = max(t, 0) if g == "cheap" else min(t, 0) if g == "dear" else t
        base = f + t - (OVERHEAT_CUT if over[i] else 0)
        T = min(100.0, max(0.0, base + off))                 # unrounded target
        for ev in events[i]:
            if ev not in EVENT_DIR:
                continue
            st = EVENT_STEPS.get((g, tr), {}).get(ev, 0)
            note = ""
            if st and i - last_dir[EVENT_DIR[ev]] < SAME_DIR_GAP:
                st_eff, note = 0, "20 日内已响应过同方向事件"
            else:
                st_eff = st
            new_off = max(-PLAN_EVENT_CAP, min(PLAN_EVENT_CAP, off + STEP * st_eff))
            newT = min(100.0, max(0.0, base + new_off))
            if st_eff and abs(newT - T) < 1e-9:
                note = "已达事件调整上限" if st_eff > 0 else "已达事件调整下限"
            if abs(newT - T) > 1e-9:
                last_dir[EVENT_DIR[ev]] = i
            ev_step[i][ev] = (st, round(newT - T, 1), note)
            off, T = new_off, newT
        target[i], base_l[i], level_l[i], traw[i] = round5(T), base, f, T
        band_l[i] = (max(0, round5(f - PLAN_EVENT_CAP)), min(100, round5(f + PLAN_EVENT_CAP)))
        if H is None:
            H, snap = round5(T), (z, tr, over[i], p, off)
        elif abs(T - H) >= ACT_MIN:
            sz, stv, sov, sp, so = snap
            if round(sp) != round(p):
                cause[i].append(f"估值百分位 {sp:.0f}% → {p:.0f}%（估值对应仓位 {val_level(sp):.0f}% → {f:.0f}%）")
            if sz != z:
                cause[i].append(f"估值区 {ZONES[sz]} → {ZONES[z]}")
            if stv != tr:
                cause[i].append(f"趋势 {stv} → {tr}")
            if sov != over[i]:
                cause[i].append("出现过热" if over[i] else "过热解除")
            if so != off:
                cause[i].append(f"事件调整 {so:+g}% → {off:+g}%" + "".join(f"（今日{ev}）" for ev, x in ev_step[i].items() if x[1]))
            action[i], H, snap = round5(T) - H, round5(T), (z, tr, over[i], p, off)
        held[i] = H
    return {"target": target, "held": held, "action": action, "cause": cause, "ev_step": ev_step, "band": band_l,
            "base": base_l, "level": level_l, "target_raw": traw}


def plan_v3(T_raw, zone, over, trend, events, closes, ma, sst, swing, sig):
    """Plan v3 (agreed 2026-11-18): the target is plan v2's (valuation, trend term, 过热, events); the suggested position
    moves towards it only on confirmation tiers, each tier at most once per episode (an episode ends when the gap is
    < 10 points; buy tiers also restart on a new 60-day closing low, sell tiers on a new 60-day closing high).
      add (target ≥ 10 above): needs no new 60-day low in the last 10 trading days and close > MA20 (every tier includes
        the weaker one's condition), and then
        1 初步止跌 → ≤ 10;  2 止跌加强 MA20 向上 or 回踩 today → ≤ 20;
        3 趋势确认 金叉 today, or trend turned from 下降 to 转折/上升 today → the whole gap
      reduce (target ≥ 10 below):
        1 初步转弱 close < MA20 and MA20 走平 → ≤ 10;  2 转弱加强 死叉 today, or close < MA20 and MA20 向下 → ≤ 20;
        3 趋势破坏 close ≤ peak × (1 − 1.5 σ_m), where peak = the highest close after the lowest close of the last 60 days
          (the current up-move's peak, not a level from before a crash), or close < MA51 and MA51 not 向上, or S2 today with MA20 < MA51
          → the whole gap
      safety valve: 极高估 and 过热 → reduce 10 without confirmation, once per episode.
    A tier that fires uses up the tiers below it; the next move needs a stronger tier (or a restart)."""
    n = len(closes)
    held, action, cause, state = [None] * n, [0] * n, [[] for _ in range(n)], [None] * n
    W = V3_LOWHIGH_DAYS
    new_low = [i >= W - 1 and closes[i] < min(closes[i - W + 1:i]) for i in range(n)]
    new_high = [i >= W - 1 and closes[i] > max(closes[i - W + 1:i]) for i in range(n)]
    H, used_b, used_s, valve = None, set(), set(), False
    for i in range(n):
        T = T_raw[i]
        if T is None or ma[20][i] is None or ma[51][i] is None:
            H, used_b, used_s, valve = None, set(), set(), False
            continue
        if H is None:
            H = round5(T)
            held[i] = H
            continue
        if new_low[i]:
            used_b = set()
        if new_high[i]:
            used_s = set()
        gap = T - H
        if abs(gap) < ACT_MIN:
            used_b, used_s, valve = set(), set(), False
            held[i] = H
            continue
        c, m20, m51, s20, s51 = closes[i], ma[20][i], ma[51][i], sst[20][i], sst[51][i]
        tier, why = 0, ""
        if gap > 0:
            calm = not any(new_low[max(0, i - V3_CALM_DAYS + 1):i + 1])
            cand = []
            if calm and c > m20:
                cand.append((1, "初步止跌（10 天未创 60 日新低、站上 MA20）"))
            if calm and c > m20 and (s20 == 1 or "回踩" in events[i]):
                cand.append((2, "止跌加强（" + ("今日回踩" if "回踩" in events[i] else "MA20 向上") + "）"))
            if calm and c > m20 and ("金叉" in events[i] or (i > 0 and trend[i] in ("转折", "上升") and trend[i - 1] == "下降")):
                cand.append((3, "趋势确认（" + ("今日金叉" if "金叉" in events[i] else f"趋势转为{trend[i]}") + "）"))
            cand = [x for x in cand if x[0] not in used_b]
            if cand:
                tier, why = max(cand)
                step = min(V3_TIER_CAP[tier], round5(T) - H)
                if step > 0:
                    used_b |= set(range(1, tier + 1))
                    action[i], H = step, H + step
                    cause[i].append(f"估值目标 {round5(T):g}%，{why}")
            else:
                state[i] = ("add", "近 10 天内创过 60 日新低" if not calm else "收盘未站上 MA20" if c <= m20
                            else "这一轮已用过的确认档不再重复，需要更强一档（MA20 向上、回踩或金叉）")
        else:
            trail = 1 - (TRAIL_SIGMA * sig[i] / 100 if sig[i] else 0.08)
            w0 = max(0, i - W + 1)
            lo_j = min(range(w0, i + 1), key=lambda j: (closes[j], -j))     # the lowest close of the last 60 days (latest if tied)
            hi = max(closes[lo_j:i + 1])                                    # the peak of the move since then
            cand = []
            if c < m20 and s20 == 0:
                cand.append((1, "初步转弱（跌破 MA20、MA20 走平）"))
            if "死叉" in events[i] or (c < m20 and s20 == -1):
                cand.append((2, "转弱加强（" + ("今日死叉" if "死叉" in events[i] else "跌破 MA20、MA20 向下") + "）"))
            if c <= hi * trail or (c < m51 and s51 in (0, -1)) or ("S2" in swing[i] and m20 < m51):
                what = (f"比这一波最高收盘回落 {(1 - c / hi) * 100:.1f}%" if c <= hi * trail else
                        "跌破 MA51 且 MA51 不再向上" if c < m51 and s51 in (0, -1) else "顶背离且 MA20 在 MA51 下方")
                cand.append((3, f"趋势破坏（{what}）"))
            cand = [x for x in cand if x[0] not in used_s]
            if cand:
                tier, why = max(cand)
                step = max(-V3_TIER_CAP[tier], round5(T) - H)
                if step < 0:
                    used_s |= set(range(1, tier + 1))
                    action[i], H = step, H + step
                    cause[i].append(f"估值目标 {round5(T):g}%，{why}")
            elif zone[i] == 4 and over[i] and not valve:
                step = max(-10, round5(T) - H)
                if step < 0:
                    valve = True
                    action[i], H = step, H + step
                    cause[i].append(f"估值目标 {round5(T):g}%，安全阀（极高估且过热，不等确认先减一档）")
            else:
                state[i] = ("reduce", "收盘仍在 MA20 上方" if c >= m20 else "MA20 仍向上" if s20 == 1
                            else "这一轮已用过的确认档不再重复，需要更强一档（死叉、跌破 MA51 或大幅回落）")
        held[i] = H
    return {"held": held, "action": action, "cause": cause, "state": state}


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
    fill = None
    closes = {d: v[0] for d, v in out.items()}
    amounts = {d: v[1] for d, v in out.items() if v[1]}
    csi_last = max(out) if out else None
    try:
        added = fm.em_fill(code, closes, amounts)          # CSI behind (2026-11-12/13): close + amount from Eastmoney, never PE
        for d in added:
            out[d] = (closes[d], amounts.get(d), None)
        if added:
            fill = {"csi_last": csi_last, "from": added[0], "to": added[-1]}
    except Exception as e:
        log(f"  {code}: CSI ends {csi_last}; no Eastmoney fill ({type(e).__name__}: {str(e)[:100]})")
    ds = sorted(out)
    return (name, ds, [out[d][0] for d in ds], [out[d][1] for d in ds], [(d, out[d][2], None) for d in ds if out[d][2] is not None],
            fill)


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


def build_entry(code, name, group, source, dates, closes, amounts, val_obs, basis, val_source, price_fill=None):
    k, gap = last_segment(dates)
    history_note = ""
    if k:                                   # moving averages must not bridge a hole: use the last continuous stretch only
        history_note = f"价格历史在 {dates[k - 1]} 到 {dates[k]} 之间中断（{gap} 天），只用 {dates[k]} 以后的数据"
        dates, closes, amounts = dates[k:], closes[k:], amounts[k:]
        val_obs = [o for o in val_obs if o[0] >= dates[0]]
    A = analyze(dates, closes, amounts, val_obs, basis)
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
    if z is not None and A["held"][i] is not None:
        lo, hi = A["band"][i]
        base = round(A["base"][i], 1)
        last_act = next((j for j in range(i, -1, -1) if A["action"][j]), None)
        start = next(j for j in range(i, -1, -1) if j == 0 or A["held"][j - 1] is None)
        pos = {"band": [lo, hi], "base": base, "val_level": round(A["val_level"][i], 1), "target": A["target"][i],
               "held": A["held"][i], "action": A["action"][i],
               "causes": A["cause"][i] if A["action"][i] else [], "plan_from": dates[start],
               "last_action": None if last_act is None else {
                   "date": dates[last_act], "from": A["held"][last_act] - A["action"][last_act], "to": A["held"][last_act],
                   "level": A["level"][last_act], "causes": A["cause"][last_act]}}
        st = A["v3"]["state"][i]
        if st and not A["action"][i]:
            pos["pending"] = (f"估值要求加仓（目标 {A['target'][i]:g}%）；等待止跌确认：{st[1]}" if st[0] == "add" else
                              f"估值要求减仓（目标 {A['target'][i]:g}%）；等待转弱确认：{st[1]}")
            pos["waiting"] = st[0]
        elif not A["action"][i] and A["target"][i] != A["held"][i]:
            pos["pending"] = f"与目标相差不到 {ACT_MIN}%，暂不调整"
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
    kf, sg = A["kf"][i], A["sig"][i]
    thresholds = {"sigma_m": r2(sg), "k": r2(kf, 3), "flat": r2(FLAT_PCT * kf), "glue": r2(GLUE_PCT * kf), "cross": r2(CROSS_MIN_GAP * kf),
                  "pullback": r2(PULLBACK_PCT * kf), "converge": r2(CONVERGE_PCT * kf), "diverge": r2(DIVERGE_PCT * kf),
                  "b1": r2(B1_BAND_PCT * kf), "b3": r2(B3_BREAK_PCT * kf), "trail": r2(TRAIL_SIGMA * sg) if sg else 8.0,
                  "b2": r2(B2_SIGMA * sg) if sg else 8.0, "s3": r2(S3_SIGMA * sg) if sg else 3.0, "from_vol": sg is not None}
    lv = A["level"][i]
    summary = {"code": code, "name": name, "group": group, "source": source, "date": dates[i], "close": r2(closes[i]),
               "chg": r2((closes[i] / closes[i - 1] - 1) * 100), "history_from": dates[0], "history_note": history_note,
               "price_fill": price_fill,
               "level": lv, "words": LEVELS[lv][1], "rank": LEVELS[lv][0], "valuation": val, "trend": trend, "timing": timing,
               "regime": regime, "position": pos, "swing": swing, "ind": ind, "thresholds": thresholds,
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
            parts.append(f"建议仓位 {pos['held'] - pos['action']:g}% → {pos['held']:g}%（估值对应 {pos['val_level']:.0f}%）")
        else:
            parts.append(f"建议仓位 {pos['held']:g}%（估值对应 {pos['val_level']:.0f}%）")
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
    fm.NOW = now                                            # the Eastmoney fill decides which days have closed
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
                + (f" [{summary['history_note']}]" if summary.get('history_note') else "")
                + (f" [prices {summary['price_fill']['from']}…{summary['price_fill']['to']} from Eastmoney; CSI ends "
                   f"{summary['price_fill']['csi_last']}, valuation {summary['valuation']['date']}]" if summary.get('price_fill') else ""))
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
            fill = None
            if psrc == "csi":
                nm, ds, cl, am, vo, fill = fetch_csi_long(code, today)
                name2, src, vs, has_pb = name or nm or code, "中证指数官网（收盘、滚动市盈率、成交额）", "中证 滚动市盈率（无 PB）", "pe_only"
                if fill:
                    src += f"；{fill['from'][5:]}–{fill['to'][5:]} 的收盘和成交额来自东方财富（中证停在 {fill['csi_last'][5:]}）"
            else:
                ds, cl, am = fetch_em(psrc.split(":", 1)[1])
                vo = fetch_danjuan(vsrc.split(":", 1)[1])
                name2, src, vs, has_pb = name, "东方财富日 K（收盘、成交额）", "蛋卷 PE / PB（周度）", "avg"
            return build_entry(code, name2, group, src, ds, cl, am, vo, has_pb, vs, fill)
        finish(code, group, job)
        fm.pause()

    for code, name, secids, dj, basis in HK:                 # 港股: Eastmoney prices (HK trading days), 蛋卷 weekly PE / PB
        if time_left() < 30:
            errors[code] = "time budget used up"
            continue

        def job(code=code, name=name, secids=secids, dj=dj, basis=basis):
            last = None
            for sid in secids:
                try:
                    ds, cl, am = fetch_em(sid)
                    break
                except Exception as e:
                    last = e
            else:
                raise RuntimeError(f"Eastmoney K-line not available ({', '.join(secids)}): {last}")
            vo = fetch_danjuan(dj)
            return build_entry(code, name, "港股", f"东方财富日 K（{sid}，港股交易日）", ds, cl, am, vo, basis,
                               "蛋卷 PE / PB（周度）" + ("，以 PE 为主" if basis == "pe" else ""))
        finish(code, "港股", job)
        fm.pause()

    sw, waiting = load_sw()
    for code, name, ds, cl, am, vo in sw:
        def job(code=code, name=name, ds=ds, cl=cl, am=am, vo=vo):
            b = SW_BASIS.get(code, "avg")
            return build_entry(code, name, "行业", "申万宏源研究（收盘、成交额、市盈率、市净率）", ds, cl, am, vo, b,
                               "申万 " + {"pe": "PE 为主（亏损时 PB）", "pb": "PB 为主", "avg": "PE、PB 平均"}[b])
        finish(code, "行业", job)
    log(f"申万一级: {len(sw)} industries with prices" + (f"; {len(waiting)} still waiting for their price history" if waiting else ""))

    if not entries:
        log("Signals: nothing computed; previous files kept.")
        sys.exit(1)
    order = {"宽基": 0, "港股": 1, "行业": 2, "自选": 3}
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
           "params": {"zone_edges": ZONE_EDGES, "zone_buffer": ZONE_BUFFER, "plan_knots": PLAN_KNOTS, "plan_event_cap": PLAN_EVENT_CAP,
                      "sigma_ref": SIGMA_REF, "vol_days": [VOL_DAYS, VOL_MIN_DAYS], "sigma_multiples": {"trail": TRAIL_SIGMA, "b2": B2_SIGMA, "s3": S3_SIGMA},
                      "v3_tier_cap": V3_TIER_CAP, "sw_basis": SW_BASIS,
                      "bands_v1": BANDS, "base_offset": BASE_OFFSET,
                      "overheat": [OVERHEAT_ON, OVERHEAT_OFF], "step": STEP, "act_min": ACT_MIN, "same_dir_gap": SAME_DIR_GAP},
           "indices": entries}
    saved = fm.write_if_changed(out_path, doc)
    log(f"Signals: {len(entries)} indices ({len(errors)} failed), date {newest} (days {dict(sorted(by_date.items()))}), levels {counts}; "
        f"signals.json {'saved' if saved else 'unchanged'}, {written} detail files written; took {time.monotonic() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
