#!/usr/bin/env python3
"""
One-off probe: which historical valuation data sources can the GitHub relay read?
Run it once from GitHub Actions (workflow "Probe data sources") and paste the log into the chat.
It only reads; it writes nothing to the repository.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
TODAY = datetime.now(BJ).strftime("%Y%m%d")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CANDIDATES = [
    ("CSI 指数日行情 JSON（看有没有 PE 字段）",
     "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=000300&startDate=20160101&endDate=" + TODAY,
     {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"}),
    ("CSI 指数基本信息（当前 PE/股息率）",
     "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info?indexCode=000300",
     {"Referer": "https://www.csindex.com.cn/", "Accept": "application/json"}),
    ("CSI 估值文件 indicator.xls（历史 PE1/PE2/股息率）",
     "https://csi-web-dev.oss-cn-shanghai-finance-1-pub.aliyuncs.com/static/html/csindex/public/uploads/file/autofile/indicator/000300indicator.xls",
     {}),
    ("CSI 估值文件（光伏产业 931151）",
     "https://csi-web-dev.oss-cn-shanghai-finance-1-pub.aliyuncs.com/static/html/csindex/public/uploads/file/autofile/indicator/931151indicator.xls",
     {}),
    ("蛋卷 指数 PE 历史（沪深300）",
     "https://danjuanfunds.com/djapi/index_eva/pe_history/SH000300?day=all",
     {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}),
    ("蛋卷 指数 PB 历史（沪深300）",
     "https://danjuanfunds.com/djapi/index_eva/pb_history/SH000300?day=all",
     {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}),
    ("蛋卷 指数详情（中证800）",
     "https://danjuanfunds.com/djapi/index_eva/detail/SH000906",
     {"Referer": "https://danjuanfunds.com/djmodule/value-center", "Accept": "application/json"}),
    ("东财数据中心 行业估值（中证行业市盈率）",
     "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEINDUSTRY_DET&columns=ALL&pageSize=5&sortColumns=TRADE_DATE&sortTypes=-1",
     {"Referer": "https://data.eastmoney.com/"}),
    ("东财数据中心 市场估值（沪深两市整体 PE）",
     "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEMARKET&columns=ALL&pageSize=5&sortColumns=TRADE_DATE&sortTypes=-1",
     {"Referer": "https://data.eastmoney.com/"}),
    ("东财 国债收益率（10 年期历史）",
     "https://datacenter.eastmoney.com/api/data/get?type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1&p=1&ps=5",
     {"Referer": "https://data.eastmoney.com/"}),
    ("东财 融资融券 历史汇总（杠杆率用）",
     "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_RZRQ_LSHJ&columns=ALL&pageSize=5&sortColumns=DIM_DATE&sortTypes=-1",
     {"Referer": "https://data.eastmoney.com/"}),
    ("东财 持仓明细页（服务器端能否读到）",
     "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=001407&topline=10&year=&month=",
     {"Referer": "https://fundf10.eastmoney.com/ccmx_001407.html"}),
]


def fetch(url, extra):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    headers.update(extra)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def describe_json(body):
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    out = []

    def walk(obj, depth=0):
        if depth > 3:
            return
        if isinstance(obj, dict):
            out.append("  " * depth + f"dict keys: {list(obj.keys())[:15]}")
            for k in list(obj.keys())[:4]:
                if isinstance(obj[k], (dict, list)):
                    out.append("  " * depth + f"[{k}]")
                    walk(obj[k], depth + 1)
        elif isinstance(obj, list):
            out.append("  " * depth + f"list of {len(obj)}; first: {json.dumps(obj[0], ensure_ascii=False)[:300] if obj else '[]'}")
            if obj and len(obj) > 1:
                out.append("  " * depth + f"last: {json.dumps(obj[-1], ensure_ascii=False)[:200]}")
    walk(j)
    return "\n".join(out)


def describe_xls(body):
    try:
        import xlrd  # installed by the workflow
    except ImportError:
        return "xlrd not installed; raw size only"
    try:
        book = xlrd.open_workbook(file_contents=body)
        sh = book.sheet_by_index(0)
        head = [str(c.value) for c in sh.row(0)]
        first = [str(c.value) for c in sh.row(1)] if sh.nrows > 1 else []
        last = [str(c.value) for c in sh.row(sh.nrows - 1)] if sh.nrows > 2 else []
        return f"rows {sh.nrows}; columns {head}; first {first}; last {last}"
    except Exception as e:
        return f"not a readable xls: {e}"


def main():
    print(f"Probe run at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    for name, url, extra in CANDIDATES:
        print("=" * 100)
        print(name)
        print(url)
        try:
            status, ctype, body = fetch(url, extra)
        except urllib.error.HTTPError as e:
            print(f"  ❌ HTTP {e.code}")
            continue
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {e}")
            continue
        print(f"  HTTP {status}, {ctype}, {len(body)} bytes")
        text_head = body[:160].decode("utf-8", errors="replace").replace("\n", " ")
        print(f"  starts with: {text_head!r}")
        if url.endswith(".xls"):
            print("  " + describe_xls(body))
        else:
            d = describe_json(body)
            if d:
                print(d)
            elif b"var apidata" in body[:50]:
                m = re.search(rb"content:\"(.{0,200})", body)
                print(f"  holdings page readable server-side; content starts: {m.group(1)[:200] if m else b''!r}")
    print("=" * 100)
    print("Done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
