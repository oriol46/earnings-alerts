#!/usr/bin/env python3
"""Prueba temporal (2ª ronda): estructura interna de Investegate, Euronext, LVMH y Adyen.
Se lanza a mano desde GitHub (tarea eu-probe) y el registro se copia para analizarlo. Se puede borrar después."""
import os
import re

import requests
from bs4 import BeautifulSoup

UA = os.environ.get("SEC_USER_AGENT") or "earnings-alerts probe"
H = {"User-Agent": f"Mozilla/5.0 (compatible; earnings-alerts; {UA})",
     "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
     "Accept-Language": "en-GB,en;q=0.9"}


def get(url):
    try:
        r = requests.get(url, headers=H, timeout=30)
        print(f"\n===== {url}\nHTTP {r.status_code} · {r.headers.get('content-type', '?')} · {len(r.content):,} bytes")
        return r
    except Exception as e:
        print(f"\n===== {url}\nERROR {e}")
        return None


def snippet(text, pattern, width=700, limit=2):
    for i, m in enumerate(re.finditer(pattern, text, re.I)):
        if i >= limit:
            break
        s = max(0, m.start() - 200)
        print(f"--- contexto de /{pattern}/ en {m.start()}:\n" + re.sub(r"\s+", " ", text[s:s + width]))


# 1. Investegate: estructura de una fila del listado y de un anuncio
r = get("https://www.investegate.co.uk/company/LSEG")
if r is not None and r.ok:
    soup = BeautifulSoup(r.text, "lxml")
    a = soup.find("a", href=re.compile(r"/announcement/rns/"))
    if a:
        row = a.find_parent("tr") or a.find_parent("div")
        print("FILA:", re.sub(r"\s+", " ", str(row))[:900])
        ann = a["href"]
        r2 = get(ann if ann.startswith("http") else "https://www.investegate.co.uk" + ann)
        if r2 is not None and r2.ok:
            s2 = BeautifulSoup(r2.text, "lxml")
            for tag in s2.find_all(["div", "article", "section"], class_=True):
                txt = tag.get_text(" ", strip=True)
                if len(txt) > 400:
                    print(f"  bloque <{tag.name} class='{' '.join(tag['class'])}'> {len(txt)} car.: {txt[:160]}")
            snippet(r2.text, r"Released|Date|RNS Number", 400, 2)
    print("filas con 'Results':", len(re.findall(r"Results|Trading Update|Interim", r.text)))
r = get("https://www.investegate.co.uk/company/LSEG?page=2")

# 2. Euronext: ¿de dónde carga la lista de notas de prensa?
r = get("https://live.euronext.com/en/listview/company-press-release/FR0000121014-XPAR")
if r is not None and r.ok:
    t = r.text
    print("filas <tr>:", t.count("<tr"), "· 'views-row':", t.count("views-row"), "· 'LVMH':", t.count("LVMH"))
    for u in sorted(set(re.findall(r'["\'](/[a-z]{2}/ajax/[^"\']+|/[^"\']*views/ajax[^"\']*|/[^"\']*listview[^"\']*)["\']', t)))[:25]:
        print("  url:", u)
    snippet(t, r"view_name|view_display_id|ajaxViews", 600, 3)
    snippet(t, r"<table", 900, 2)
for u in ("https://live.euronext.com/en/products/equities/company-news?search_api_fulltext=LVMH",
          "https://live.euronext.com/en/ajax/getCompanyPressRelease/FR0000121014-XPAR",
          "https://live.euronext.com/en/listview/company-press-release/FR0000121014-XPAR?page=0&_wrapper_format=drupal_ajax"):
    r = get(u)
    if r is not None and r.ok:
        print("'LVMH':", r.text.count("LVMH"), "· <tr:", r.text.count("<tr"))
        snippet(r.text, r"LVMH", 600, 2)

# 3. LVMH: publicaciones (web Next.js, puede llevar los datos en __NEXT_DATA__)
r = get("https://www.lvmh.com/en/publications?tag=Press%20releases")
if r is not None and r.ok:
    print("__NEXT_DATA__:", "__NEXT_DATA__" in r.text, "· 'revenue':", len(re.findall("revenue", r.text, re.I)),
          "· pdfs:", len(re.findall(r"\.pdf", r.text)))
    for u in sorted(set(re.findall(r'https://[^"\']+\.pdf', r.text)))[:10]:
        print("  pdf:", u)
    snippet(r.text, r"Q3 2026|third quarter|nine months|revenue for", 600, 3)
r = get("https://www.lvmh.com/en/financial-calendar/2026-q3-revenue")
if r is not None and r.ok:
    for u in sorted(set(re.findall(r'https://[^"\']+\.pdf', r.text)))[:10]:
        print("  pdf:", u)

# 4. Adyen: página de resultados
r = get("https://investors.adyen.com/financials")
if r is not None and r.ok:
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.find_all("a", href=True):
        if re.search(r"financials/|\.pdf|letter|update", a["href"], re.I):
            print("  ·", a.get_text(" ", strip=True)[:90], "->", a["href"][:140])
    snippet(r.text, r"Q3 2026|business update", 500, 2)

print("\nFIN DE LA PRUEBA 2")
