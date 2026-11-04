#!/usr/bin/env python3
"""
Fetch each fund's latest disclosed stock holdings (with weights) from Eastmoney's holdings page,
for the fund / ETF codes listed in funds.txt, and save them as data/holdings.json for the fund viewer.

Browsers are refused by that page, but a plain server-side request with ordinary headers is served,
which is why this runs in GitHub Actions. Standard library only.

funds.txt: one code per line (or comma/space separated); '#' starts a comment.
           The fund viewer's "⚙ 估值数据源" dialog shows the exact list to paste here.

Local test without network:  python fetch_holdings.py --from-dir samples/   (samples/<code>.txt = raw page bodies)
"""
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FUNDS_FILE = os.path.join(ROOT, "funds.txt")
OUT_PATH = os.path.join(ROOT, "data", "holdings.json")
BJ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_CODES = 400
PAUSE_SECONDS = 1.5


def log(msg):
    print(msg, flush=True)


def read_codes(path):
    if not os.path.exists(path):
        return []
    codes, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0]
            for tok in re.split(r"[\s,;，；]+", line):
                if re.fullmatch(r"\d{6}", tok) and tok not in seen:
                    seen.add(tok)
                    codes.append(tok)
    return codes[:MAX_CODES]


def fetch_page(code, year=""):
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}"
           f"&topline=1000&year={year}&month=&rt=0.{int(time.time())}")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": f"https://fundf10.eastmoney.com/ccmx_{code}.html",
        "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", errors="replace")


def split_apidata(body):
    """var apidata={ content:"...",arryear:[2026,2025],curyear:2026};  →  (content, years, curyear)"""
    m = re.search(r'content:"(.*)",arryear:(\[[^\]]*\]),curyear:(\d+)', body, re.S)
    if not m:
        return None, [], None
    content = m.group(1).replace('\\"', '"').replace("\\/", "/")
    try:
        years = [int(y) for y in json.loads(m.group(2))]
    except Exception:
        years = []
    return content, years, int(m.group(3))


class HoldingsHTML(html.parser.HTMLParser):
    """Collects, per <div class='boxitem'>: its text and its tables as rows of (text, href) cells."""

    def __init__(self):
        super().__init__()
        self.boxes = []
        self.box = None
        self.table = None
        self.row = None
        self.cell = None
        self.href = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and "boxitem" in (a.get("class") or ""):
            self.box = {"text": [], "tables": []}
            self.boxes.append(self.box)
        elif tag == "table" and self.box is not None:
            self.table = []
            self.box["tables"].append(self.table)
        elif tag == "tr" and self.table is not None:
            self.row = []
            self.table.append(self.row)
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"text": [], "href": None, "th": tag == "th"}
            self.row.append(self.cell)
        elif tag == "a" and self.cell is not None and self.cell["href"] is None:
            self.cell["href"] = a.get("href") or ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cell = None
        elif tag == "tr":
            self.row = None
        elif tag == "table":
            self.table = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)
        if self.box is not None:
            self.box["text"].append(data)


def stock_secid(code, href):
    m = re.search(r"unify/r/(\d+)\.([0-9A-Za-z]+)", href or "")
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    if re.fullmatch(r"\d{5}", code):
        return f"116.{code}"
    m = re.search(r"/(sh|sz|bj)(\d{6})", href or "", re.I)
    if m:
        return f"{1 if m.group(1).lower() == 'sh' else 0}.{m.group(2)}"
    if re.fullmatch(r"\d{6}", code):
        return f"1.{code}" if re.match(r"6|90", code) else f"0.{code}"
    return None


def parse_holdings(content):
    """Latest report in the page → {report_date, period, stocks:[[secid, code, name, weight]]} or None."""
    p = HoldingsHTML()
    p.feed(content)
    for box in p.boxes:
        for table in box["tables"]:
            heads = [re.sub(r"\s+", "", "".join(c["text"])) for c in (table[0] if table else []) if c["th"]]
            if not heads:
                continue
            i_code = next((i for i, h in enumerate(heads) if "股票代码" in h), -1)
            i_name = next((i for i, h in enumerate(heads) if "股票名称" in h), -1)
            i_w = next((i for i, h in enumerate(heads) if "占净值" in h), -1)
            if i_code < 0 or i_w < 0:
                continue
            text = re.sub(r"\s+", " ", "".join(box["text"]))
            dm = re.search(r"截止至[:：]?\s*(\d{4}-\d{2}-\d{2})", text)
            pm = re.search(r"(\d{4})年(\d)季度", text)
            stocks, seen = [], set()
            for row in table[1:]:
                cells = [c for c in row if not c["th"]]
                if len(cells) <= max(i_code, i_w):
                    continue
                code = "".join(cells[i_code]["text"]).strip()
                try:
                    w = float(re.sub(r"[%,\s]", "", "".join(cells[i_w]["text"])))
                except ValueError:
                    continue
                if not re.fullmatch(r"[0-9A-Za-z.]{1,10}", code) or not (0 < w <= 100):
                    continue
                href = cells[i_code]["href"] or (cells[i_name]["href"] if i_name >= 0 else None) or ""
                secid = stock_secid(code, href)
                key = secid or code
                if key in seen:
                    continue
                seen.add(key)
                name = ("".join(cells[i_name]["text"]).strip() if i_name >= 0 else code)[:20]
                stocks.append([secid, code, name, round(w, 4)])
            return {"report_date": dm.group(1) if dm else "", "period": f"{pm.group(1)}年{pm.group(2)}季度" if pm else "",
                    "stocks": stocks}
    return None


def fetch_holdings(code, loader):
    body = loader(code, "")
    content, years, cur = split_apidata(body)
    if content is None:
        raise RuntimeError("page did not contain apidata (blocked or format changed)")
    parsed = parse_holdings(content)
    if (parsed is None or not parsed["stocks"]) and years:
        cur = cur or datetime.now(BJ).year
        prev = max([y for y in years if y < cur], default=None)
        if prev:
            content2, _, _ = split_apidata(loader(code, str(prev)))
            parsed2 = parse_holdings(content2 or "")
            if parsed2 and parsed2["stocks"]:
                parsed = parsed2
    return parsed or {"report_date": "", "period": "", "stocks": []}


def main():
    sample_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--from-dir" else None
    codes = read_codes(FUNDS_FILE if not sample_dir else os.path.join(sample_dir, "funds.txt"))
    if not codes:
        log("funds.txt has no fund codes; nothing to do (the fund viewer's data-source dialog shows the list to paste).")
        return
    if sample_dir:
        def loader(code, year):
            name = f"{code}_{year}.txt" if year else f"{code}.txt"
            with open(os.path.join(sample_dir, name), encoding="utf-8") as f:
                return f.read()
    else:
        loader = fetch_page

    previous = {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            previous = json.load(f).get("funds", {})
    except (OSError, json.JSONDecodeError):
        pass

    funds, errors = {}, {}
    now = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    for i, code in enumerate(codes):
        try:
            h = fetch_holdings(code, loader)
            funds[code] = {"report_date": h["report_date"], "period": h["period"], "stocks": h["stocks"],
                           "full": len(h["stocks"]) > 10, "fetched_at": now}
            log(f"{code}: {len(h['stocks'])} stocks, {h['period'] or 'no period'}, as of {h['report_date'] or '--'}")
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"
            log(f"{code}: FAILED {errors[code]}")
            if code in previous:
                funds[code] = previous[code]   # keep the last good answer
        if not sample_dir and i < len(codes) - 1:
            time.sleep(PAUSE_SECONDS)

    if errors and len(errors) == len(codes):
        log("Every fetch failed; keeping the previous file.")
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    out = {"schema": 1, "source": "天天基金 基金持仓明细（个人使用）", "fetched_at_beijing": now,
           "count": len(funds), "funds": funds, "errors": errors}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Saved holdings for {len(funds)} codes to data/holdings.json ({len(errors)} failed).")


if __name__ == "__main__":
    main()
