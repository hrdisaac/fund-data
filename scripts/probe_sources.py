#!/usr/bin/env python3
"""
Probe 12: does the 同花顺 estimate API give the SAME answer for the fund code as for its hqcode, for every fund in
funds.txt? Read-only. Paste into scripts/probe_sources.py, run "Probe data sources", paste the whole log back.
"""
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CODES = ['014530', '004788', '001051', '217027', '007593', '022424', '004194', '007474', '011612', '006909', '000071', '012348', '006327', '501311', '014519', '021142', '017610', '021049', '006195', '009051', '021550', '020602', '161725', '160222', '005235', '008928', '000248', '018201', '217017', '519714', '110022', '005827', '110011', '519002', '260108', '004424', '011309', '002982', '002978', '012323', '005064', '501009', '008284', '014424', '013416', '003095', '001717', '000727', '012159', '002974', '014855', '008888', '008282', '570008', '004070', '000251', '003634', '004224', '001475', '005609', '009068', '019164', '110025', '008702', '270002', '163406', '001938', '161005', '006435', '001410', '290011', '004616', '006624', '010381', '000326', '001257', '159601', '159967', '512770', '513050', '563180', '513910', '513830', '513060', '159883', '159939', '512760', '159806', '159652']


def get(url, ref):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def estimate(name, day):
    body = get(f"https://gz-fund.10jqka.com.cn/?module=api&controller=index&action=chart&info=vm_fd_{name}&start={day}&_={int(time.time() * 1000)}",
               "https://fund.10jqka.com.cn/")
    m = re.match(r"\s*vm_fd_[^=]+=\s*'(.*)'\s*;?\s*$", body, re.S)
    return m.group(1) if m else None


def main():
    now = datetime.now(BJ)
    print(f"Probe 12 at {now.strftime('%Y-%m-%d %H:%M')} Beijing; {len(CODES)} codes\n")
    hq = {}
    for k in range(0, len(CODES), 10):
        part = CODES[k:k + 10]
        try:
            body = get("https://fund.10jqka.com.cn/data/client/myfund/" + ",".join(part), "https://fund.10jqka.com.cn/")
            for c, h in re.findall(r'"code":"(\d{6})".*?"hqcode":"([^"]+)"', body):
                hq[c] = h
        except Exception as e:
            print(f"  lookup {part[0]}…: ❌ {type(e).__name__}: {str(e)[:120]}")
        time.sleep(0.6)
    print(f"hqcode found for {len(hq)} of {len(CODES)} codes; equal to the code itself: {sorted(c for c, h in hq.items() if h == c)}\n")
    day = now.strftime("%m%d")
    stats = {"same": [], "different": [], "code_empty_hq_has": [], "code_has_hq_empty": [], "both_empty": [], "no_hqcode": [], "error": []}
    for c in CODES:
        try:
            a = estimate(c, day)
            h = hq.get(c)
            if not h:
                last = a.split("|")[-1] if a else ""
                stats["no_hqcode"].append((c, "data" if "~" in last and last.split("~", 1)[1].strip("~") else "empty"))
                continue
            b = a if h == c else estimate(h, day)
            has = lambda s: bool(s) and "~" in s.split("|")[-1] and bool(s.split("|")[-1].split("~", 1)[1].strip("~"))
            if has(a) and has(b):
                stats["same" if a == b else "different"].append(c if a == b else (c, h, a[:60], b[:60]))
            elif has(b):
                stats["code_empty_hq_has"].append((c, h))
            elif has(a):
                stats["code_has_hq_empty"].append((c, h))
            else:
                stats["both_empty"].append((c, h))
        except Exception as e:
            stats["error"].append((c, f"{type(e).__name__}: {str(e)[:80]}"))
        time.sleep(0.4)
    for k, v in stats.items():
        print(f"{k}: {len(v)}")
        if v and k != "same":
            for x in v[:40]:
                print(f"    {x}")
    print("\nProbe 12 done. Please paste this whole log into the chat.")


if __name__ == "__main__":
    main()
