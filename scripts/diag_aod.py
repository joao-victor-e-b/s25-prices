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


import re


def smid_probe(s, asin, smid, label):
    r = s.get(f"{HOST}/dp/{asin}?smid={smid}&psc=1", headers={**NAV, **HINTS}, timeout=30)
    t = r.text
    mids = re.findall(r'name="merchantID"[^>]*value="([A-Z0-9]+)"|id="merchantID"[^>]*value="([A-Z0-9]+)"', t)
    mids = sorted({a or b for a, b in mids})
    ingress = re.search(r'id="aod-ingress-link".{0,600}', t, re.S)
    ing = re.sub(r"<[^>]+>", " ", ingress.group(0)) if ingress else ""
    ing = re.sub(r"\s+", " ", ing)[:200]
    parc = re.findall(r"total parcelado\s*R\$\s*([\d.]+,\d{2})", t)
    acc = len(re.findall(r'id="newAccordionRow_\d+"', t))
    print(f"{label:34} HTTP {r.status_code} merchantIDs={mids} accordion_rows={acc} "
          f"parcelado={parc[:3]} ingress='{ing}'", flush=True)


def run():
    s = session(UA_141)
    s.get(f"{HOST}/dp/B0FTMQQ7L2", headers={**NAV, **HINTS}, timeout=30); gap()
    smid_probe(s, "B0FTMQQ7L2", "A1708Z2OZTF5YE", "256 smid Trocafone (non-buybox)"); gap()
    smid_probe(s, "B0FTMQQ7L2", "A2TO0KKQTM1BAB", "256 smid CellularStore (buybox)"); gap()
    smid_probe(s, "B0FTMQQ7L2", "A3SPPFNXUZXE4P", "256 smid Assurant (absent)"); gap()
    smid_probe(s, "B0FTMWWS8Y", "A3SPPFNXUZXE4P", "512 smid Assurant"); gap()
    smid_probe(s, "B0DSYJCY45", "A1ZZFT5FULY4LN", "new256 smid Amazon.com.br"); gap()
    for _ in range(3):   # does the panel ever come back with the warmed session?
        r = s.get(AOD.format(a="B0FTMQQ7L2"), headers=xhr("B0FTMQQ7L2"), timeout=30)
        show("aod retry", r); gap()


if __name__ == "__main__":
    run()
