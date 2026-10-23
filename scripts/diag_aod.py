"""Temporary CI diagnostic: which request shape gets Amazon's All Offers panel
from a GitHub Actions IP? Prints one line per attempt. Not part of the monitor;
delete with .github/workflows/diag.yml once the answer is in."""
import random
import time

import requests

ASINS = ["B0FTMQQ7L2", "B0FTMWWS8Y"]
HOST = "https://www.amazon.com.br"
UA_125 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
UA_141 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
UA_MOB = ("Mozilla/5.0 (Linux; Android 14; SM-S938B) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36")
HINTS = {
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "device-memory": "8", "dpr": "1", "viewport-width": "1536",
    "rtt": "50", "downlink": "10", "ect": "4g",
}
NAV = {"Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8"),
       "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
       "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
       "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
       "Upgrade-Insecure-Requests": "1"}


def xhr(asin, extra=None):
    h = {"Accept": "text/html,*/*", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
         "Referer": f"{HOST}/dp/{asin}", "X-Requested-With": "XMLHttpRequest"}
    h.update(extra or {})
    return h


AOD = HOST + "/gp/product/ajax/aodAjaxMain/ref=dp_aod_ALL_mbc?asin={a}&pc=dp&experienceId=aodAjaxMain"
AOD2 = HOST + "/gp/aod/ajax/ref=dp_aod_ALL_mbc?asin={a}&pc=dp&experienceId=aodAjaxMain"


def show(tag, r):
    t = r.text
    print(f"{tag:42} HTTP {r.status_code} len={len(t):7} "
          f"aod_offer={'aod-offer' in t} captcha={'validateCaptcha' in t} "
          f"title={'productTitle' in t}", flush=True)


def gap():
    time.sleep(random.uniform(8, 12))


def session(ua):
    s = requests.Session()
    s.headers["User-Agent"] = ua
    return s


def run():
    for asin in ASINS:
        print(f"== {asin}", flush=True)
        # v1: today's shape (old UA, warmed by a dp GET)
        s = session(UA_125)
        show("v1 dp warm (UA125)", s.get(f"{HOST}/dp/{asin}", headers=NAV, timeout=30)); gap()
        show("v1 aod (UA125, warmed)", s.get(AOD.format(a=asin), headers=xhr(asin), timeout=30)); gap()
        # v2: consistent Chrome 141 + client hints + fetch metadata, warmed
        s = session(UA_141)
        show("v2 dp warm (141+hints)", s.get(f"{HOST}/dp/{asin}", headers={**NAV, **HINTS}, timeout=30)); gap()
        meta = {**HINTS, "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin", "Origin": HOST}
        show("v2 aod (141+hints+cors, warmed)", s.get(AOD.format(a=asin), headers=xhr(asin, meta), timeout=30)); gap()
        show("v2b aod repeat same session", s.get(AOD.format(a=asin), headers=xhr(asin, meta), timeout=30)); gap()
        show("v2c aod2 endpoint", s.get(AOD2.format(a=asin), headers=xhr(asin, meta), timeout=30)); gap()
        # v3: product page with aod=1 (is the panel server-rendered?)
        r = s.get(f"{HOST}/dp/{asin}?aod=1", headers={**NAV, **HINTS}, timeout=30)
        show("v3 dp?aod=1", r); gap()
        # v4: fresh session, no warm-up, full hints
        s4 = session(UA_141)
        show("v4 aod cold (141+hints)", s4.get(AOD.format(a=asin), headers=xhr(asin, meta), timeout=30)); gap()
        # v5: mobile UA, warmed on mobile dp
        s5 = session(UA_MOB)
        mob = {**NAV, "sec-ch-ua-mobile": "?1", "sec-ch-ua-platform": '"Android"'}
        show("v5 dp warm (mobile)", s5.get(f"{HOST}/dp/{asin}", headers=mob, timeout=30)); gap()
        show("v5 aod (mobile)", s5.get(AOD.format(a=asin), headers=xhr(asin, {"sec-ch-ua-mobile": "?1"}), timeout=30)); gap()
        # v6: legacy offer-listing URL
        show("v6 offer-listing", s.get(f"{HOST}/gp/offer-listing/{asin}?condition=all",
                                       headers={**NAV, **HINTS}, timeout=30)); gap()


if __name__ == "__main__":
    run()
