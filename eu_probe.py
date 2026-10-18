#!/usr/bin/env python3
"""Prueba temporal: comprueba qué devuelven las fuentes europeas de resultados.
Se lanza a mano desde GitHub (tarea eu-probe) y el registro se copia para analizarlo. Se puede borrar después."""
import json
import os
import re

import requests
from bs4 import BeautifulSoup

UA = os.environ.get("SEC_USER_AGENT") or "earnings-alerts probe"
BROWSER = {"User-Agent": f"Mozilla/5.0 (compatible; earnings-alerts; {UA})",
           "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-GB,en;q=0.9"}


def show(title, r, n=1200):
    print(f"\n===== {title} =====")
    if isinstance(r, Exception):
        print(f"ERROR: {r}")
        return None
    print(f"HTTP {r.status_code} · {r.headers.get('content-type', '?')} · {len(r.content):,} bytes · {r.url}")
    print(r.text[:n].replace("\n\n", "\n"))
    return r


def get(url, **kw):
    try:
        return requests.get(url, headers=BROWSER, timeout=30, **kw)
    except Exception as e:
        return e


def links(r, pattern, limit=12):
    if isinstance(r, Exception) or r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "lxml")
    found = 0
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if re.search(pattern, a["href"] + " " + text, re.I) and text:
            print(f"  · {text[:110]} -> {a['href'][:160]}")
            found += 1
            if found >= limit:
                break
    print(f"  ({found} enlaces que encajan con /{pattern}/)")


# 1. Euronext: RSS general de notas de prensa de empresas
r = show("Euronext RSS", get("https://live.euronext.com/rss/company-pr-release"), 1500)
if not isinstance(r, Exception):
    print("items:", r.text.count("<item>"), "· LVMH:", r.text.count("LVMH"), "· Adyen:", r.text.count("ADYEN") + r.text.count("Adyen"))

# 2. Euronext: listado de notas de prensa por emisor (ISIN-mercado)
for name, code in (("LVMH", "FR0000121014-XPAR"), ("Adyen", "NL0012969182-XAMS")):
    r = show(f"Euronext listado {name}", get(f"https://live.euronext.com/en/listview/company-press-release/{code}"), 600)
    links(r, r"press-release|company-news|/node/|results|sales|revenue")
    r = show(f"Euronext ficha {name}", get(f"https://live.euronext.com/en/product/equities/{code}/company-information"), 300)
    links(r, r"press|news|release")

# 3. Londres: FCA National Storage Mechanism (API de búsqueda)
body = {"from": 0, "size": 5, "sort": "submitted_date", "sortorder": "desc",
        "criteriaObj": {"criteria": [], "dateCriteria": []}}   # últimas publicaciones, para ver qué campos devuelve
try:
    r = requests.post("https://api.data.fca.org.uk/search?index=fca-nsm-searchdata", json=body,
                      headers={**BROWSER, "Content-Type": "application/json"}, timeout=30)
except Exception as e:
    r = e
show("FCA NSM API (últimas publicaciones)", r, 1500)

# 4. Londres: Investegate (agregador de RNS)
r = show("Investegate LSEG", get("https://www.investegate.co.uk/company/LSEG"), 400)
links(r, r"announcement|result|trading|interim|half")

# 5. Londres: API de la web de la LSE
show("LSE API instrumento", get("https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/LSEG"), 800)

# 6. Webs de inversores (alternativa)
r = show("LVMH inversores", get("https://www.lvmh.com/en/investors"), 300)
links(r, r"result|revenue|financial|press")
r = show("Adyen inversores", get("https://investors.adyen.com/"), 300)
links(r, r"result|financial|shareholder|letter|report")
r = show("LSEG inversores", get("https://www.lseg.com/en/investor-relations"), 300)
links(r, r"result|financial|report|rns|regulatory")

print("\nFIN DE LA PRUEBA")
