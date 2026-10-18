# Earnings Alerts — resultados del S&P 500 por Telegram

Revisa cada 30 minutos (en horario de mercado de EE. UU.) los comunicados de resultados que las empresas del S&P 500 presentan a la SEC (8-K, item 2.02). Cada uno lo analiza Claude Haiku: ventas, crecimiento, BPA frente al consenso, guidance, destacados y alertas (dividendo, recompras, cambio de CEO…).

Cada mañana de martes a sábado te llega **un resumen por Telegram** con:
- Cabecera con el número de empresas y el reparto 🟢 / 🟡 / 🔴.
- **Lectura del día** escrita por Claude Sonnet: tendencias por sector y sorpresas.
- Una línea por empresa, agrupadas por sector. El ticker es un enlace al comunicado original en la SEC.

Leyenda: ✅ bate el consenso de BPA · ➖ en línea · ❌ decepciona · Guía ⬆️ ➡️ ⬇️ 🆕 ⛔ · ⚠️ hecho excepcional.

---

## Puesta en marcha (todo desde el navegador)

Es igual que el bot de insiders. Puedes reutilizar el **mismo bot y chat de Telegram**.

### Paso 1 — Crear el repositorio
1. En **github.com**, arriba a la derecha, pulsa **+** → **New repository**.
2. Nombre: `earnings-alerts`. Marca **Public** (minutos gratis ilimitados; las claves van aparte como secretos y no se ven).
3. Pulsa **Create repository**.

### Paso 2 — Subir los archivos
1. Pulsa el enlace **"uploading an existing file"**.
2. Arrastra `earnings_alerts.py`, `requirements.txt` y `README.md`.
3. Pulsa **Commit changes**.

### Paso 3 — Crear la tarea automática
1. **Add file** → **Create new file**.
2. Nombre del archivo, exactamente: `.github/workflows/earnings-alerts.yml`
3. Pega el contenido del archivo `earnings-alerts.yml` que te he pasado.
4. **Commit changes** (dos veces).

### Paso 4 — Guardar los secretos
**Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Crea estos cuatro (sin comillas):

| Name | Secret |
|---|---|
| `TELEGRAM_BOT_TOKEN` | el mismo token que usa el bot de insiders |
| `TELEGRAM_CHAT_ID` | el mismo chat_id |
| `SEC_USER_AGENT` | el mismo que en el bot de insiders (nombre y email) |
| `ANTHROPIC_API_KEY` | tu clave de la API de Claude (la de tu plataforma de inversión) |

### Paso 5 — Probarlo
1. Pestaña **Actions** → a la izquierda **earnings-alerts** → **Run workflow**.
2. En **mode** escribe `test` → **Run workflow**. Te llegará un mensaje de prueba.
3. Vuelve a lanzarlo con **mode** = `ticker` y **ticker** = `MSFT` (o cualquier empresa del S&P 500). Te llegará el análisis completo de su último resultado. Es la mejor forma de comprobar la calidad.
4. Por último, lánzalo con **mode** = `summary`: revisará los resultados de los últimos 2 días y te enviará el primer resumen. La primera vez tarda más (descarga la lista del S&P 500 y analiza todo lo pendiente).

A partir de ahí funciona solo.

---

## Coste
- GitHub Actions: gratis (repositorio público).
- API de Claude: Haiku para cada empresa y Sonnet solo para la lectura del día. En los registros de cada ejecución (**Actions** → la ejecución → *Revisar resultados nuevos*) verás la línea `tokens entrada … · salida …` para calcular el coste real.

## Ajustes opcionales
Se cambian como variables en el archivo `.yml`, dentro de `env:`:
- `CLAUDE_MODEL_FAST` / `CLAUDE_MODEL_SMART`: modelos de Claude usados.
- `LOOKBACK_DAYS`: días hacia atrás que revisa (por defecto 2).
- Hora del resumen: la línea `cron: "37 6 * * 2-6"` está en hora UTC. Si la cambias, cambia también las dos condiciones `github.event.schedule == '37 6 * * 2-6'` para que coincidan.

## Limitaciones de esta primera versión
- El consenso es solo el **BPA** (gratis, vía Yahoo Finance) y a veces no está disponible; en ese caso no se marca ✅/❌.
- Algunas empresas publican el comunicado en el 8-K un poco después de la nota de prensa; puede haber unos minutos de retraso.
- Solo cubre EE. UU. Europa irá en una fase posterior.

## Probar en local (opcional, PowerShell)
```powershell
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."
$env:SEC_USER_AGENT="Tu Nombre tu@email.com"; $env:ANTHROPIC_API_KEY="..."
python earnings_alerts.py --ticker MSFT --dry-run   # imprime en pantalla sin enviar
```
