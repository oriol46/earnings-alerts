#!/usr/bin/env python3
"""
Earnings Alerts — resultados del S&P 500 por Telegram.

Detecta los comunicados de resultados que las empresas del S&P 500 presentan a la
SEC (formulario 8-K, item 2.02), los analiza con Claude y te envía un resumen
diario por Telegram agrupado por sector.

Uso:
  python earnings_alerts.py               # revisa resultados nuevos y los analiza
  python earnings_alerts.py --summary     # envía el resumen de lo pendiente
  python earnings_alerts.py --ticker MSFT # analiza el último resultado de una empresa y te lo envía
  python earnings_alerts.py --test        # mensaje de prueba a Telegram
  Añade --dry-run para imprimir en pantalla en lugar de enviar.
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests

# ---------------------------------------------------------------- configuración
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")

# Empresas con aviso inmediato de resultados y análisis de la call (separadas por comas)
WATCHLIST = {t.strip().upper() for t in os.environ.get("WATCHLIST", "UBER,NFLX,META,MSFT,SPGI,V").split(",")
             if t.strip()}
CALL_MIN_HOURS = 2          # espera mínima desde los resultados antes de buscar la transcripción
CALL_RETRY_HOURS = 3        # cada cuánto se reintenta si aún no está publicada
CALL_MAX_TRIES = 8          # intentos por empresa (~1 día de búsqueda en horario de mercado)
AV_DAILY_LIMIT = int(os.environ.get("AV_DAILY_LIMIT", "20"))  # el plan gratis de Alpha Vantage es muy limitado
MAX_TRANSCRIPT_CHARS = 120_000

MODEL_FAST = os.environ.get("CLAUDE_MODEL_FAST", "claude-haiku-5-5")    # análisis de cada empresa
MODEL_SMART = os.environ.get("CLAUDE_MODEL_SMART", "claude-sonnet-5-5")  # lectura del día

DB_PATH = os.environ.get("EARNINGS_DB", "earnings.db")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))  # días hacia atrás que revisa
MAX_DOC_CHARS = 60_000      # recorta comunicados muy largos (ahorra coste)
MAX_ATTEMPTS = 3            # reintentos de análisis por filing
SEC_DELAY = 0.12            # la SEC permite 10 peticiones/segundo
TELEGRAM_LIMIT = 3900       # Telegram corta en 4096 caracteres

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SECTOR_ES = {
    "Information Technology": "Tecnología",
    "Health Care": "Salud",
    "Financials": "Financieras",
    "Consumer Discretionary": "Consumo discrecional",
    "Consumer Staples": "Consumo básico",
    "Communication Services": "Comunicaciones",
    "Industrials": "Industriales",
    "Energy": "Energía",
    "Materials": "Materiales",
    "Real Estate": "Inmobiliario",
    "Utilities": "Utilities",
}

SESSION = requests.Session()


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- base de datos
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS filings (
        accession TEXT PRIMARY KEY, cik INTEGER, ticker TEXT, name TEXT, sector TEXT,
        filing_date TEXT, acceptance TEXT, url TEXT, analysis TEXT,
        status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        summarized INTEGER DEFAULT 0, created_at TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(filings)")}
    for column, kind in (("notified", "INTEGER DEFAULT 0"), ("call_status", "TEXT DEFAULT 'pending'"),
                         ("call_tries", "INTEGER DEFAULT 0"), ("call_last_try", "TEXT"), ("call_analysis", "TEXT")):
        if column not in existing:
            conn.execute(f"ALTER TABLE filings ADD COLUMN {column} {kind}")
    conn.commit()
    return conn


# ---------------------------------------------------------------- red
def sec_get(url, retries=4):
    headers = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    for attempt in range(retries):
        time.sleep(SEC_DELAY)
        try:
            r = SESSION.get(url, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"La SEC no responde: {url}")


# ---------------------------------------------------------------- S&P 500
def load_sp500(conn):
    """Lista del S&P 500 desde Wikipedia (ticker, nombre, sector, CIK). Se refresca cada 7 días."""
    row = conn.execute("SELECT value, updated FROM meta WHERE key='sp500'").fetchone()
    if row:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row[1])
        if age < timedelta(days=7):
            return json.loads(row[0])
    try:
        import pandas as pd
        page = SESSION.get(WIKI_SP500, headers={"User-Agent": SEC_USER_AGENT or "earnings-alerts"}, timeout=30)
        page.raise_for_status()
        df = pd.read_html(StringIO(page.text), attrs={"id": "constituents"})[0]
        companies, seen = [], set()
        for _, r in df.iterrows():
            cik = int(r["CIK"])
            if cik in seen:  # GOOGL/GOOG, FOX/FOXA… comparten CIK
                continue
            seen.add(cik)
            companies.append({
                "ticker": str(r["Symbol"]).strip(),
                "name": str(r["Security"]).strip(),
                "sector": SECTOR_ES.get(str(r["GICS Sector"]).strip(), str(r["GICS Sector"]).strip()),
                "cik": cik,
            })
        if len(companies) < 400:
            raise RuntimeError(f"Lista sospechosa: {len(companies)} empresas")
        conn.execute("REPLACE INTO meta VALUES ('sp500', ?, ?)",
                     (json.dumps(companies), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        log(f"Lista del S&P 500 actualizada: {len(companies)} empresas")
        return companies
    except Exception as e:
        if row:
            log(f"No se pudo actualizar la lista ({e}); uso la guardada")
            return json.loads(row[0])
        raise


# ---------------------------------------------------------------- SEC: detectar resultados
def earnings_filings(company, since_date):
    """8-K con item 2.02 (Results of Operations) desde since_date (YYYY-MM-DD)."""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{company['cik']:010d}.json").json()
    rec = data["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form != "8-K" or rec["filingDate"][i] < since_date:
            continue
        if not re.search(r"(^|,)\s*2\.02\s*(,|$)", rec["items"][i] or ""):
            continue
        out.append({
            "accession": rec["accessionNumber"][i],
            "filing_date": rec["filingDate"][i],
            "acceptance": rec["acceptanceDateTime"][i],
            "primary": rec["primaryDocument"][i],
        })
    return out


def html_to_text(raw_html):
    """Convierte el HTML del comunicado en texto, conservando las tablas como filas 'a | b | c'."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c not in ("$", "%", ")", "(")]
        tr.replace_with(soup.new_string(" | ".join(cells) + "\n"))
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0\u200b]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def fetch_press_release(cik, filing):
    """Descarga el comunicado (anexo EX-99.x). Si no hay anexo, usa el 8-K principal."""
    from bs4 import BeautifulSoup
    acc = filing["accession"]
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    docs = []
    try:
        index = sec_get(f"{base}{acc}-index.htm").text
        soup = BeautifulSoup(index, "lxml")
        for tr in soup.select("table.tableFile tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            link, doc_type = tds[2].find("a"), tds[3].get_text(strip=True).upper()
            if link and doc_type.startswith("EX-99"):
                href = link["href"].replace("/ix?doc=", "")
                if href.lower().endswith((".htm", ".html", ".txt")):
                    docs.append((doc_type, "https://www.sec.gov" + href if href.startswith("/") else base + href))
    except Exception as e:
        log(f"  índice no disponible ({e})")
    docs.sort(key=lambda d: d[0])  # EX-99.1 primero
    if not docs:
        docs = [("8-K", base + filing["primary"])]

    parts, total = [], 0
    for doc_type, url in docs[:3]:
        text = html_to_text(sec_get(url).text)
        parts.append(f"=== {doc_type} ===\n{text}")
        total += len(text)
        if total >= MAX_DOC_CHARS:
            break
    return "\n\n".join(parts)[:MAX_DOC_CHARS], docs[0][1]


# ---------------------------------------------------------------- consenso (gratis, Yahoo)
def eps_consensus(ticker, filing_date):
    """BPA estimado por el consenso para ese trimestre (Yahoo Finance). None si no lo encuentra."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker.replace(".", "-")).get_earnings_dates(limit=8)
        if df is None or df.empty or "EPS Estimate" not in df.columns:
            return None
        target = datetime.fromisoformat(filing_date).date()
        for ts, r in df.iterrows():
            if abs((ts.date() - target).days) <= 3 and r["EPS Estimate"] == r["EPS Estimate"]:
                return float(r["EPS Estimate"])
    except Exception as e:
        log(f"  consenso no disponible para {ticker} ({e})")
    return None


# ---------------------------------------------------------------- Claude
ANALYSIS_PROMPT = """Eres un analista de renta variable. Analiza el comunicado de resultados de {name} ({ticker}, sector {sector}), presentado a la SEC el {date}.
{consensus_line}

Registra tu análisis llamando a la herramienta "registrar_resultado". Si no puedes usarla, devuelve SOLO un JSON válido con esta estructura:
{{
  "periodo": "trimestre y año fiscal, p. ej. '3T FY2026'",
  "ingresos_musd": número: ingresos totales del trimestre en MILLONES de dólares (p. ej. 94930.0 para 94,93 mil M$), o null,
  "crec_ingresos_pct": número: variación interanual de esos ingresos en % (p. ej. 8.2 o -3.1), o null,
  "bpa": número: BPA diluido del trimestre en $ (ajustado si la empresa lo da; si no, GAAP), o null,
  "bpa_tipo": "ajustado" | "GAAP",
  "vs_consenso": "bate" | "en linea" | "decepciona" | "sin consenso",
  "guidance": "sube" | "mantiene" | "baja" | "inicia" | "retira" | "no da",
  "guidance_detalle": "una frase con las cifras, o null",
  "destacados": ["exactamente 3 puntos, máximo 20 palabras cada uno, con cifras: segmentos, márgenes, KPIs clave del sector"],
  "alertas": ["normalmente lista VACÍA; ver reglas"],
  "tono": "positivo" | "neutro" | "negativo",
  "resumen": "una frase de máximo 20 palabras con la LECTURA del trimestre (qué lo explica y qué implica), sin repetir las cifras de ingresos ni BPA"
}}

Reglas:
- Español con formato numérico español en los textos. Usa solo datos del documento (y el consenso indicado); no inventes; si un dato no aparece, null.
- Ingresos: una sola cifra, la de ingresos totales que la empresa destaca (en bancos, ingresos netos totales; en aseguradoras, ingresos totales). Revisa las unidades de las tablas ("in millions", "in thousands") antes de convertir a millones.
- "vs_consenso": compara el BPA ajustado con el consenso indicado ("en linea" si la diferencia es menor del 2%); "sin consenso" si no se te ha dado.
- "tono" refleja la calidad del trimestre y del guidance, no el lenguaje de la empresa.
- "alertas" es SOLO para hechos excepcionales que un gestor deba conocer aunque no siga la empresa: recorte o suspensión del dividendo, subida del dividendo superior al 20%, programa de recompra NUEVO de más del 5% de la capitalización, salida o cambio de CEO/CFO, deterioros o provisiones extraordinarias, reformulación de cuentas, adquisición o desinversión relevante anunciada, reestructuración con despidos, investigación regulatoria o litigio material. NO son alertas: recompras o dividendos ordinarios, provisiones habituales del negocio, caídas de un segmento, variaciones normales de márgenes. Máximo 2. En la gran mayoría de trimestres la lista debe estar vacía.

DOCUMENTO:
{doc}"""

DAILY_PROMPT = """Eres un gestor de renta variable. Estos son los resultados del S&P 500 publicados en las últimas horas (JSON por empresa):

{data}

Escribe la "lectura del día" en español: entre 3 y 5 viñetas breves (máximo 30 palabras cada una) sobre tendencias por sector, sorpresas relevantes y cambios de guidance que merezcan atención. Cita los tickers. Sin introducción ni conclusión. Usa '• ' al inicio de cada viñeta y no uses markdown."""

_client = None


_nullable = lambda t: {"type": [t, "null"]}
ANALYSIS_TOOL = {
    "name": "registrar_resultado",
    "description": "Registra el análisis estructurado del comunicado de resultados.",
    "input_schema": {
        "type": "object",
        "properties": {
            "periodo": _nullable("string"),
            "ingresos_musd": {"type": ["number", "null"], "description": "Ingresos totales en millones de $"},
            "crec_ingresos_pct": {"type": ["number", "null"], "description": "Variación interanual en %"},
            "bpa": {"type": ["number", "null"], "description": "BPA diluido en $"},
            "bpa_tipo": {"type": "string", "enum": ["ajustado", "GAAP"]},
            "vs_consenso": {"type": "string", "enum": ["bate", "en linea", "decepciona", "sin consenso"]},
            "guidance": {"type": "string", "enum": ["sube", "mantiene", "baja", "inicia", "retira", "no da"]},
            "guidance_detalle": _nullable("string"),
            "destacados": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "alertas": {"type": "array", "items": {"type": "string"}, "maxItems": 2,
                        "description": "Solo hechos excepcionales según las reglas; normalmente vacía."},
            "tono": {"type": "string", "enum": ["positivo", "neutro", "negativo"]},
            "resumen": {"type": "string"},
        },
        "required": ["periodo", "ingresos_musd", "bpa", "bpa_tipo", "guidance", "destacados", "alertas", "tono",
                     "resumen"],
    },
}


def claude_message(prompt, model, max_tokens, tools=None):
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if tools:
        kwargs["tools"] = tools
    return _client.messages.create(**kwargs)


def claude(prompt, model, max_tokens):
    msg = claude_message(prompt, model, max_tokens)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def extract_analysis(msg):
    """Saca el análisis de la respuesta: primero de la herramienta, si no, de un JSON en el texto."""
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use" and block.name == ANALYSIS_TOOL["name"]:
            return dict(block.input)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    kinds = ", ".join(getattr(b, "type", "?") for b in msg.content) or "vacía"
    raise ValueError(f"Claude no devolvió el análisis (stop_reason={msg.stop_reason}; bloques: {kinds}; "
                     f"texto: {text[:200]!r})")


def analyze(company, filing, doc):
    consensus = eps_consensus(company["ticker"], filing["filing_date"])
    consensus_line = (f"Consenso de BPA para este trimestre (Yahoo Finance, normalmente ajustado): {consensus:.2f} $."
                      if consensus is not None else "No hay dato de consenso disponible.")
    prompt = ANALYSIS_PROMPT.format(name=company["name"], ticker=company["ticker"], sector=company["sector"],
                                    date=filing["filing_date"], consensus_line=consensus_line, doc=doc)
    msg = claude_message(prompt, MODEL_FAST, 4000, tools=[ANALYSIS_TOOL])
    data = extract_analysis(msg)
    data["consenso_bpa"] = consensus
    return data, msg.usage.input_tokens, msg.usage.output_tokens


# ---------------------------------------------------------------- conference call (Alpha Vantage)
class AlphaVantageLimit(Exception):
    pass


def _av_budget_ok(conn):
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute("SELECT value, updated FROM meta WHERE key='av_calls'").fetchone()
    used = int(row[0]) if row and row[1] == today else 0
    return used < AV_DAILY_LIMIT


def _av_count(conn):
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute("SELECT value, updated FROM meta WHERE key='av_calls'").fetchone()
    used = int(row[0]) if row and row[1] == today else 0
    conn.execute("REPLACE INTO meta VALUES ('av_calls', ?, ?)", (str(used + 1), today))
    conn.commit()


def quarter_candidates(periodo, filing_date):
    """Trimestres a probar en Alpha Vantage: el fiscal que indica el comunicado y el natural anterior a la fecha."""
    out = []
    p = periodo or ""
    q = re.search(r"([1-4])\s*T|Q\s*([1-4])|([1-4])Q", p, re.I)
    y = re.search(r"(20\d\d)", p) or re.search(r"FY\s*'?(\d\d)\b", p, re.I)
    if q and y:
        year = y.group(1) if len(y.group(1)) == 4 else "20" + y.group(1)
        out.append(f"{year}Q{next(g for g in q.groups() if g)}")
    d = datetime.fromisoformat(filing_date).date()
    cal_q, cal_y = (d.month - 1) // 3, d.year     # trimestre natural anterior a la publicación
    if cal_q == 0:
        cal_q, cal_y = 4, cal_y - 1
    if f"{cal_y}Q{cal_q}" not in out:
        out.append(f"{cal_y}Q{cal_q}")
    return out


def fetch_transcript(conn, ticker, quarter):
    """Transcripción de la call desde Alpha Vantage. None si todavía no está."""
    if not _av_budget_ok(conn):
        raise AlphaVantageLimit("límite diario propio alcanzado")
    r = SESSION.get("https://www.alphavantage.co/query",
                    params={"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker.replace(".", "-"),
                            "quarter": quarter, "apikey": ALPHAVANTAGE_API_KEY}, timeout=60)
    _av_count(conn)
    r.raise_for_status()
    data = r.json()
    if "Information" in data or "Note" in data:
        raise AlphaVantageLimit(str(data.get("Information") or data.get("Note"))[:200])
    if "Error Message" in data:
        raise RuntimeError(data["Error Message"][:200])
    turns = data.get("transcript") or []
    if not turns:
        return None
    text = "\n\n".join(f"{t.get('speaker', '?')} ({t.get('title', '')}): {t.get('content', '')}" for t in turns)
    return text[:MAX_TRANSCRIPT_CHARS]


CALL_PROMPT = """Eres un gestor de renta variable fundamental. Tienes el análisis del comunicado de resultados de {name} ({ticker}) del trimestre {periodo}, publicado el {date}, y la transcripción de una conference call.

ANÁLISIS DEL COMUNICADO:
{analysis}

Analiza la call y registra el resultado con la herramienta "registrar_call". Céntrate en lo que la call AÑADE al comunicado: matices de la directiva, explicaciones, cambios de mensaje y el turno de preguntas. Escribe en español, frases cortas y concretas con cifras cuando las haya. No inventes nada que no esté en la transcripción.

Primero comprueba si la transcripción corresponde a ESE trimestre (por las cifras y las fechas que se mencionan). Si es de otro trimestre, marca "corresponde": false y rellena el resto con lo mínimo.

TRANSCRIPCIÓN:
{transcript}"""

CALL_TOOL = {
    "name": "registrar_call",
    "description": "Registra el análisis de la conference call de resultados.",
    "input_schema": {
        "type": "object",
        "properties": {
            "corresponde": {"type": "boolean", "description": "La transcripción es del trimestre indicado"},
            "tono": {"type": "string", "enum": ["confiado", "neutral", "prudente", "defensivo"]},
            "tono_explicacion": {"type": "string", "description": "Máximo 25 palabras"},
            "mensajes_clave": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                               "description": "Lo nuevo que no estaba en el comunicado; máximo 25 palabras cada uno"},
            "guidance_call": {"type": ["string", "null"],
                              "description": "Matices sobre el guidance dados en la call, o null"},
            "preguntas": {"type": "array", "maxItems": 3, "items": {
                "type": "object",
                "properties": {"tema": {"type": "string"},
                               "respuesta": {"type": "string", "enum": ["clara", "vaga", "evasiva"]},
                               "detalle": {"type": "string", "description": "Máximo 25 palabras"}},
                "required": ["tema", "respuesta", "detalle"]},
                "description": "Las preguntas de analistas más relevantes y cómo respondió la directiva"},
            "riesgos": {"type": "array", "items": {"type": "string"}, "maxItems": 2,
                        "description": "Riesgos o señales de alerta mencionados; vacío si no hay"},
            "impacto": {"type": "string", "enum": ["mejora", "confirma", "empeora"],
                        "description": "Si la call mejora, confirma o empeora la lectura del comunicado"},
            "balance": {"type": "string", "description": "Una frase de máximo 25 palabras"},
        },
        "required": ["corresponde", "tono", "tono_explicacion", "mensajes_clave", "preguntas", "riesgos",
                     "impacto", "balance"],
    },
}


def analyze_call(row, transcript):
    a = {k: v for k, v in row["analysis"].items() if k != "consenso_bpa"}
    prompt = CALL_PROMPT.format(name=row["name"], ticker=row["ticker"], periodo=a.get("periodo"),
                                date=row["filing_date"], analysis=json.dumps(a, ensure_ascii=False),
                                transcript=transcript)
    msg = claude_message(prompt, MODEL_SMART, 4000, tools=[CALL_TOOL])
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use" and block.name == CALL_TOOL["name"]:
            return dict(block.input)
    raise ValueError(f"Claude no devolvió el análisis de la call (stop_reason={msg.stop_reason})")


# ---------------------------------------------------------------- Telegram
def send_telegram(text, dry_run=False):
    if dry_run:
        print("\n" + "-" * 60 + "\n" + text + "\n" + "-" * 60)
        return
    r = SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                     data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"}, timeout=30)
    if r.status_code != 200:
        log(f"Error de Telegram: {r.text[:300]}")
    time.sleep(1)


def send_blocks(header, blocks, dry_run=False):
    """Envía bloques completos sin partir ninguno, en mensajes de menos de 4096 caracteres."""
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > TELEGRAM_LIMIT:
            send_telegram(current, dry_run)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        send_telegram(current, dry_run)


# ---------------------------------------------------------------- formato
TONE = {"positivo": "🟢", "neutro": "🟡", "negativo": "🔴"}
VS = {"bate": " ✅", "en linea": " ➖", "decepciona": " ❌"}
GUIDE = {"sube": "Guía ⬆️", "mantiene": "Guía ➡️", "baja": "Guía ⬇️", "inicia": "Guía 🆕", "retira": "Guía ⛔"}


def esc(value):
    return html.escape(str(value)) if value not in (None, "") else ""


VS_TXT = {"bate": "✅ bate", "en linea": "➖ en línea", "decepciona": "❌ decepciona"}
GUIDE_TXT = {"sube": "⬆️ sube", "mantiene": "➡️ mantiene", "baja": "⬇️ baja",
             "inicia": "🆕 inicia", "retira": "⛔ retira", "no da": "— no da"}


def _es(x, decimals):
    """Número con formato español: 12.412,5"""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _money(x):
    return f"{_es(x, 2)} $"


def _name(row):
    return re.sub(r"\s*\(The\)$", "", row["name"])


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _revenue(a):
    musd = a.get("ingresos_musd")
    if _num(musd):
        rev = f"{_es(musd / 1000, 1)} mil M$" if abs(musd) >= 10000 else f"{_es(musd, 0)} M$"
    else:
        rev = esc(a.get("ingresos")) or "—"  # formato antiguo
    pct = a.get("crec_ingresos_pct")
    if _num(pct):
        rev += f" ({'+' if pct > 0 else ''}{_es(pct, 1)}% a/a)"
    elif a.get("crec_ingresos"):
        rev += f" ({esc(a['crec_ingresos'])} a/a)"
    return rev


def _eps_value(a):
    if _num(a.get("bpa")):
        return _money(a["bpa"]) + (" (GAAP)" if a.get("bpa_tipo") == "GAAP" else "")
    return esc(a.get("bpa")) or "—"


def _eps(a, with_consensus=True):
    eps = _eps_value(a)
    if with_consensus and a.get("consenso_bpa") is not None:
        eps += f" vs {_money(a['consenso_bpa'])} est."
    if a.get("vs_consenso") in VS:
        eps += VS[a["vs_consenso"]]
    return eps


def company_line(row):
    """Bloque compacto para el resumen diario."""
    a = row["analysis"]
    lines = [f"{TONE.get(a.get('tono'), '⚪')} <a href=\"{esc(row['url'])}\"><b>{esc(row['ticker'])}</b></a>"
             f" · {esc(_name(row))} · {esc(a.get('periodo'))}",
             f"Ingresos {_revenue(a)}",
             f"BPA {_eps(a)} · Guía {GUIDE.get(a.get('guidance'), '—').replace('Guía ', '')}"]
    if a.get("resumen"):
        lines.append(f"↳ <i>{esc(a['resumen'])}</i>")
    for alert in a.get("alertas") or []:
        lines.append(f"⚠️ {esc(alert)}")
    return "\n".join(lines)


def company_detail(row):
    """Mensaje completo de una empresa, por bloques."""
    a = row["analysis"]
    date = row.get("filing_date") or ""
    if date:
        date = f" · publicado {date[8:10]}/{date[5:7]}"
    out = [f"{TONE.get(a.get('tono'), '⚪')} <b>{esc(row['ticker'])} · {esc(_name(row))}</b>",
           f"{esc(row.get('sector', ''))} · {esc(a.get('periodo'))}{date}"]

    figures = [f"Ingresos: {_revenue(a)}", f"BPA: {_eps_value(a)}"]
    if a.get("consenso_bpa") is not None:
        verdict = VS_TXT.get(a.get("vs_consenso"))
        figures.append(f"Consenso BPA: {_money(a['consenso_bpa'])}" + (f" → {verdict}" if verdict else ""))
    guide = GUIDE_TXT.get(a.get("guidance"), "—")
    if a.get("guidance_detalle"):
        guide += f" · {esc(a['guidance_detalle'])}"
    figures.append(f"Guidance: {guide}")
    out.append("\n📊 <b>Cifras</b>\n" + "\n".join(figures))

    if a.get("resumen"):
        out.append(f"\n💬 <b>Lectura</b>\n<i>{esc(a['resumen'])}</i>")
    if a.get("destacados"):
        out.append("\n🔎 <b>Destacados</b>\n" + "\n".join(f"• {esc(d)}" for d in a["destacados"]))
    if a.get("alertas"):
        out.append("\n⚠️ <b>Alertas</b>\n" + "\n".join(f"• {esc(x)}" for x in a["alertas"]))
    if row.get("url"):
        out.append(f"\n🔗 <a href=\"{esc(row['url'])}\">Comunicado en la SEC</a>")
    return "\n".join(out)


CALL_TONE = {"confiado": "💪 confiado", "neutral": "😐 neutral", "prudente": "🤔 prudente", "defensivo": "🛡️ defensivo"}
ANSWER = {"clara": "🟢", "vaga": "🟡", "evasiva": "🔴"}
IMPACT = {"mejora": "⬆️ Mejora", "confirma": "➡️ Confirma", "empeora": "⬇️ Empeora"}


def call_section(c):
    out = [f"🎙️ <b>La call</b> · tono {CALL_TONE.get(c.get('tono'), esc(c.get('tono')))}"]
    if c.get("tono_explicacion"):
        out.append(f"<i>{esc(c['tono_explicacion'])}</i>")
    if c.get("mensajes_clave"):
        out.append("\n<b>Mensajes clave</b>\n" + "\n".join(f"• {esc(m)}" for m in c["mensajes_clave"]))
    if c.get("guidance_call"):
        out.append(f"\n<b>Guidance en la call:</b> {esc(c['guidance_call'])}")
    if c.get("preguntas"):
        out.append("\n<b>Preguntas de analistas</b>\n" + "\n".join(
            f"{ANSWER.get(p.get('respuesta'), '⚪')} <b>{esc(p.get('tema'))}</b>: {esc(p.get('detalle'))}"
            for p in c["preguntas"]))
    if c.get("riesgos"):
        out.append("\n<b>Riesgos</b>\n" + "\n".join(f"• {esc(r)}" for r in c["riesgos"]))
    out.append(f"\n📌 <b>{IMPACT.get(c.get('impacto'), 'Balance')}</b> la lectura del comunicado: "
               f"{esc(c.get('balance'))}")
    return "\n".join(out)


def send_full_update(row, dry_run=False):
    """Mensaje completo de resultados + sección de la call, sin partir bloques."""
    header = "🎙️ <b>Actualización tras la call</b>"
    send_blocks(header, [company_detail(row), call_section(row["call"])], dry_run)


# ---------------------------------------------------------------- flujos principales
def process(conn, company, filing):
    """Descarga y analiza un filing; guarda el resultado. Devuelve la fila o None."""
    acc = filing["accession"]
    row = conn.execute("SELECT status, attempts FROM filings WHERE accession=?", (acc,)).fetchone()
    if row and (row[0] == "ok" or row[1] >= MAX_ATTEMPTS):
        return None
    if not row:
        conn.execute("""INSERT INTO filings (accession, cik, ticker, name, sector, filing_date, acceptance, created_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (acc, company["cik"], company["ticker"], company["name"], company["sector"],
                      filing["filing_date"], filing["acceptance"], datetime.now(timezone.utc).isoformat()))
        conn.commit()
    try:
        log(f"Analizando {company['ticker']} ({filing['filing_date']})")
        doc, url = fetch_press_release(company["cik"], filing)
        analysis, tin, tout = analyze(company, filing, doc)
        conn.execute("""UPDATE filings SET url=?, analysis=?, status='ok', attempts=attempts+1,
                        input_tokens=?, output_tokens=? WHERE accession=?""",
                     (url, json.dumps(analysis, ensure_ascii=False), tin, tout, acc))
        conn.commit()
        return {**company, "filing_date": filing["filing_date"], "url": url, "analysis": analysis,
                "tin": tin, "tout": tout}
    except Exception as e:
        global LAST_ERROR
        LAST_ERROR = str(e)[:300]
        log(f"  error con {company['ticker']}: {e}")
        conn.execute("UPDATE filings SET status='error', attempts=attempts+1 WHERE accession=?", (acc,))
        conn.commit()
        return None


LAST_ERROR = ""


def run_check(conn):
    companies = load_sp500(conn)
    since = (datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    log(f"Revisando {len(companies)} empresas (8-K item 2.02 desde {since})")
    done, tin, tout, errors = 0, 0, 0, []
    for company in companies:
        try:
            filings = earnings_filings(company, since)
        except Exception as e:
            log(f"  {company['ticker']}: {e}")
            continue
        for filing in filings:
            before = conn.execute("SELECT status, attempts FROM filings WHERE accession=?",
                                  (filing["accession"],)).fetchone()
            if before and (before[0] == "ok" or before[1] >= MAX_ATTEMPTS):
                continue
            result = process(conn, company, filing)
            if result:
                done, tin, tout = done + 1, tin + result["tin"], tout + result["tout"]
                if company["ticker"].upper() in WATCHLIST:
                    send_blocks("📢 <b>Resultados publicados</b>", [company_detail(result)])
                    conn.execute("UPDATE filings SET notified=1 WHERE accession=?", (filing["accession"],))
                    conn.commit()
            else:
                errors.append(f"{company['ticker']}: {LAST_ERROR}")
    log(f"Hecho: {done} resultados analizados · tokens entrada {tin:,} · salida {tout:,}")
    if errors:
        lines = "\n".join(f"• {esc(e[:150])}" for e in errors[:10])
        more = f"\n… y {len(errors) - 10} más" if len(errors) > 10 else ""
        send_telegram(f"⚠️ <b>Earnings Alerts</b>: {len(errors)} análisis fallidos (se reintentan solos)\n{lines}{more}")
    if ALPHAVANTAGE_API_KEY:
        run_calls(conn)


def _load_row(conn, accession):
    r = conn.execute("""SELECT accession, ticker, name, sector, filing_date, acceptance, url, analysis,
                               call_tries FROM filings WHERE accession=?""", (accession,)).fetchone()
    return {"accession": r[0], "ticker": r[1], "name": r[2], "sector": r[3], "filing_date": r[4],
            "acceptance": r[5], "url": r[6], "analysis": json.loads(r[7]), "call_tries": r[8] or 0}


def try_call(conn, row, dry_run=False):
    """Busca la transcripción del trimestre, la analiza y envía el mensaje completo. True si lo consigue."""
    for quarter in quarter_candidates(row["analysis"].get("periodo"), row["filing_date"]):
        log(f"  {row['ticker']}: buscando transcripción {quarter}")
        transcript = fetch_transcript(conn, row["ticker"], quarter)
        if not transcript:
            continue
        call = analyze_call(row, transcript)
        if not call.get("corresponde", True):
            log(f"  {row['ticker']}: la transcripción {quarter} es de otro trimestre")
            continue
        conn.execute("UPDATE filings SET call_status='done', call_analysis=? WHERE accession=?",
                     (json.dumps(call, ensure_ascii=False), row["accession"]))
        conn.commit()
        send_full_update({**row, "call": call}, dry_run)
        return True
    return False


def run_calls(conn, dry_run=False):
    """Para la watchlist: busca la transcripción de la call de los resultados recientes."""
    if not WATCHLIST:
        return
    now = datetime.now(timezone.utc)
    since = (now.date() - timedelta(days=5)).isoformat()
    marks = ",".join("?" * len(WATCHLIST))
    pending = conn.execute(f"""SELECT accession, acceptance, call_last_try FROM filings
                               WHERE status='ok' AND call_status='pending' AND filing_date >= ?
                               AND call_tries < ? AND UPPER(ticker) IN ({marks})""",
                           (since, CALL_MAX_TRIES, *sorted(WATCHLIST))).fetchall()
    for accession, acceptance, last_try in pending:
        try:
            published = datetime.fromisoformat(acceptance.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (AttributeError, ValueError):
            published = now - timedelta(hours=CALL_MIN_HOURS)
        if now - published < timedelta(hours=CALL_MIN_HOURS):
            continue
        if last_try and now - datetime.fromisoformat(last_try) < timedelta(hours=CALL_RETRY_HOURS):
            continue
        row = _load_row(conn, accession)
        try:
            found = try_call(conn, row, dry_run)
        except AlphaVantageLimit as e:
            log(f"Alpha Vantage sin cupo por hoy ({e}); se reintenta más tarde")
            return
        except Exception as e:
            log(f"  error con la call de {row['ticker']}: {e}")
            found = False
        if found:
            continue
        tries = row["call_tries"] + 1
        status = "pending" if tries < CALL_MAX_TRIES else "not_found"
        conn.execute("UPDATE filings SET call_tries=?, call_last_try=?, call_status=? WHERE accession=?",
                     (tries, now.isoformat(), status, accession))
        conn.commit()
        if status == "not_found":
            send_telegram(f"🎙️ No he encontrado la transcripción de la call de <b>{esc(row['ticker'])}</b> "
                          f"({esc(row['analysis'].get('periodo'))}). Puedes analizarla a mano más adelante.", dry_run)


def run_summary(conn, dry_run=False):
    rows = conn.execute("""SELECT accession, ticker, name, sector, filing_date, url, analysis FROM filings
                           WHERE status='ok' AND summarized=0 ORDER BY sector, ticker""").fetchall()
    if not rows:
        log("No hay resultados nuevos para el resumen")
        return
    items = [{"accession": r[0], "ticker": r[1], "name": r[2], "sector": r[3], "filing_date": r[4],
              "url": r[5], "analysis": json.loads(r[6])} for r in rows]

    tones = [i["analysis"].get("tono") for i in items]
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    header = (f"📊 <b>Resultados S&amp;P 500 · {today}</b>\n"
              f"{len(items)} empresas · 🟢 {tones.count('positivo')} · 🟡 {tones.count('neutro')} · "
              f"🔴 {tones.count('negativo')}")

    blocks = []
    if len(items) >= 3:
        try:
            compact = [{"ticker": i["ticker"], "sector": i["sector"],
                        **{k: i["analysis"].get(k) for k in
                           ("ingresos_musd", "crec_ingresos_pct", "crec_ingresos", "vs_consenso", "guidance", "guidance_detalle", "tono", "resumen", "alertas")}}
                       for i in items]
            reading, _, _ = claude(DAILY_PROMPT.format(data=json.dumps(compact, ensure_ascii=False)), MODEL_SMART, 800)
            blocks.append("🧭 <b>Lectura del día</b>\n" + esc(reading.strip()))
        except Exception as e:
            log(f"Sin lectura del día: {e}")

    by_sector = {}
    for i in items:
        by_sector.setdefault(i["sector"], []).append(i)
    for sector, group in sorted(by_sector.items(), key=lambda kv: -len(kv[1])):
        blocks.append(f"<b>— {esc(sector)} ({len(group)}) —</b>")
        blocks.extend(company_line(i) for i in group)

    send_blocks(header, blocks, dry_run)
    if not dry_run:
        conn.executemany("UPDATE filings SET summarized=1 WHERE accession=?", [(i["accession"],) for i in items])
        conn.commit()
    log(f"Resumen enviado: {len(items)} empresas")


def run_ticker(conn, ticker, dry_run=False):
    """Prueba: analiza el último resultado (últimos 120 días) de una empresa y lo envía."""
    companies = load_sp500(conn)
    company = next((c for c in companies if c["ticker"].upper() == ticker.upper()), None)
    if not company:
        sys.exit(f"{ticker} no está en el S&P 500")
    since = (datetime.now(timezone.utc).date() - timedelta(days=120)).isoformat()
    filings = earnings_filings(company, since)
    if not filings:
        sys.exit(f"No encuentro resultados de {ticker} en los últimos 120 días")
    filing = max(filings, key=lambda f: f["filing_date"])
    conn.execute("DELETE FROM filings WHERE accession=?", (filing["accession"],))  # fuerza el reanálisis
    result = process(conn, company, filing)
    if result:
        send_telegram(company_detail(result), dry_run)
    else:
        send_telegram(f"⚠️ No se pudo analizar <b>{esc(ticker)}</b>: {esc(LAST_ERROR)}", dry_run)
    return filing if result else None


def run_call_now(conn, ticker, dry_run=False):
    """Prueba: resultados + call del último trimestre de una empresa, sin esperar."""
    if not ALPHAVANTAGE_API_KEY:
        sys.exit("Falta ALPHAVANTAGE_API_KEY")
    filing = run_ticker(conn, ticker, dry_run)
    if not filing:
        return
    row = _load_row(conn, filing["accession"])
    try:
        if not try_call(conn, row, dry_run):
            send_telegram(f"🎙️ Alpha Vantage aún no tiene la transcripción de <b>{esc(ticker)}</b> "
                          f"({esc(row['analysis'].get('periodo'))}).", dry_run)
    except Exception as e:
        send_telegram(f"⚠️ Error con la call de <b>{esc(ticker)}</b>: {esc(str(e)[:300])}", dry_run)


def main():
    p = argparse.ArgumentParser(description="Resultados del S&P 500 por Telegram")
    p.add_argument("--summary", action="store_true", help="envía el resumen de resultados pendientes")
    p.add_argument("--ticker", help="analiza el último resultado de una empresa y lo envía")
    p.add_argument("--call", help="analiza el último resultado de una empresa junto con su call")
    p.add_argument("--test", action="store_true", help="mensaje de prueba a Telegram")
    p.add_argument("--dry-run", action="store_true", help="imprime en pantalla en lugar de enviar")
    args = p.parse_args()

    if args.test:
        send_telegram("✅ <b>Earnings Alerts</b> funcionando. Te llegará el resumen de resultados del S&amp;P 500 cada mañana.",
                      args.dry_run)
        return
    missing = [n for n, v in (("SEC_USER_AGENT", SEC_USER_AGENT), ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)) if not v]
    if not args.summary and missing:
        sys.exit(f"Faltan variables: {', '.join(missing)}")

    conn = db_connect()
    if args.call:
        run_call_now(conn, args.call, args.dry_run)
    elif args.ticker:
        run_ticker(conn, args.ticker, args.dry_run)
    elif args.summary:
        run_summary(conn, args.dry_run)
    else:
        run_check(conn)


if __name__ == "__main__":
    main()
