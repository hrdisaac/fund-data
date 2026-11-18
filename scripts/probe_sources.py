#!/usr/bin/env python3
"""
Probe 15: the Hang Seng historical PE / dividend-yield spreadsheets found in the website's scripts (probe 14):
  /content/dam/wpb/hsil/index_operation/historical_pe/${E}/${t}/${t}.xlsx  and  …/historical_dy/…
Tries the likely values of E (language) and t (index code), and reads the first and last rows of every file found.
Read-only. Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
"""
import io
import re
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HSI = "https://www.hsi.com.hk"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": HSI + "/zh-hk/indexes/all-indexes/index-details/?seriesCode=hscgsi"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read(), r.headers.get("Content-Type", "")


def rows(xlsx):
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))
    out = []
    for row in ET.fromstring(z.read(sheets[0])).iter("{%s}row" % NS["m"]):
        vals = []
        for c in row.findall("m:c", NS):
            v = c.find("m:v", NS)
            x = v.text if v is not None else ""
            if c.get("t") == "s" and x:
                x = shared[int(x)]
            vals.append(x)
        out.append(vals)
    return sheets, out


def main():
    print(f"Probe 15 at {datetime.now(BJ).strftime('%Y-%m-%d %H:%M')} Beijing\n")
    hits = 0
    for kind in ("historical_pe", "historical_dy"):
        for code in ("hscgsi", "hshci", "hsi"):
            for lang in ("en", "tc", "sc", "zh_hk", "zh_cn", "zh-hk"):
                for t in (code, code.upper()):
                    url = f"{HSI}/content/dam/wpb/hsil/index_operation/{kind}/{lang}/{t}/{t}.xlsx"
                    try:
                        body, ctype = get(url)
                    except Exception as e:
                        if "404" not in str(e):
                            print(f"  {kind} {lang} {t}: ❌ {type(e).__name__}: {str(e)[:80]}")
                        time.sleep(0.3)
                        continue
                    hits += 1
                    print(f"  ✅ {kind} {lang} {t}: {len(body)} bytes {ctype!r}")
                    try:
                        sheets, r = rows(body)
                        data = [x for x in r if any(v.strip() for v in x)]
                        print(f"     sheets {sheets}; {len(data)} rows; first {data[:6]}; last {data[-3:]}")
                    except Exception as e:
                        print(f"     not readable as xlsx: {type(e).__name__}: {str(e)[:80]}; starts {body[:40]!r}")
                    time.sleep(0.4)
                    break
    print(f"\n{hits} files found.\nProbe 15 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
