#!/usr/bin/env python3
"""
BTC-DCA -> Telegram-Push
========================
Laeuft via GitHub Actions jeden Montag um 08:00 Uhr deutscher Zeit.

Stateless: Das Skript haelt keine Datenbank. Es rechnet die komplette
Historie bei jedem Lauf neu aus den oeffentlichen Binance-Kursen aus.
Das ist moeglich, weil der Sparplan deterministisch ist (jeder Montag
08:00 Europe/Berlin ab dem 08.06.2026).

Es wird KEIN API-Key benoetigt. Genutzt wird der oeffentliche
Market-Data-Endpoint data-api.binance.vision (funktioniert auch von
den US-basierten GitHub-Runnern aus, anders als api.binance.com).
"""

import os
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo
import urllib.request
import urllib.parse

# ----------------------------- Konfiguration -----------------------------
START_DATE = dt.date(2026, 6, 8)          # erster Spar-Montag
WEEKLY_EUR = 15.0                          # Einzahlung pro Woche
FEE_RATE = 0.004                           # 0,40 % Spread (Binance Convert)
NET_EUR = WEEKLY_EUR * (1 - FEE_RATE)      # 14,94 EUR fliessen in BTC
SYMBOL = "BTCEUR"
TZ = ZoneInfo("Europe/Berlin")

# Reihenfolge = Fallback-Reihenfolge
BINANCE_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

# Aus GitHub Secrets / Variables
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://DEIN-NUTZERNAME.github.io/btc-dca/")


# ------------------------------- Helfer ----------------------------------
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dca-bot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def binance_price_at(ts_ms):
    """Open-Preis der 1-Minuten-Kerze, die ts_ms enthaelt.
    Der Sparkauf liegt exakt um 08:00:00, also ist der Open-Preis der
    08:00-Kerze der Preis zu genau dieser Sekunde."""
    params = urllib.parse.urlencode(
        {"symbol": SYMBOL, "interval": "1m", "startTime": ts_ms, "limit": 1}
    )
    last_err = None
    for base in BINANCE_BASES:
        try:
            data = http_get_json(f"{base}/api/v3/klines?{params}")
            if data:
                return float(data[0][1])  # [openTime, OPEN, high, low, close, ...]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Binance-Kurs nicht abrufbar: {last_err}")


def mondays_until(today):
    """Alle Spar-Montage (Datum) von START_DATE bis einschliesslich heute."""
    out = []
    d = START_DATE
    while d <= today:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def monday_ts_ms(d):
    """UTC-Millisekunden fuer 08:00 deutscher Zeit am Datum d (DST-sicher)."""
    local = dt.datetime(d.year, d.month, d.day, 8, 0, 0, tzinfo=TZ)
    return int(local.timestamp() * 1000)


def de(x, decimals=2):
    """Zahl im deutschen Format (1.234,56)."""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def eur(x):
    return de(x, 2) + " EUR"


def btc(x):
    return f"{x:.8f}"


# -------------------------------- Hauptlauf -------------------------------
def main():
    now_local = dt.datetime.now(TZ)

    # DST-sichere Trigger-Logik:
    # GitHub-Cron feuert 06:00 UND 07:00 UTC (Montag). Nur der Lauf, der in
    # deutscher Zeit auf 08:xx faellt, sendet wirklich. Im Sommer ist das der
    # 06:00-UTC-Lauf, im Winter der 07:00-UTC-Lauf.
    force = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
    if not force and now_local.hour != 8:
        print(f"Kein 08:00-Lauf (aktuell {now_local:%H:%M} DE-Zeit) -> uebersprungen.")
        return

    today = now_local.date()
    mondays = mondays_until(today)
    if not mondays:
        print("Sparplan startet erst am 08.06.2026 - noch nichts zu senden.")
        return

    # Komplette Historie neu aufbauen
    tranches = []
    cum_eur = 0.0
    cum_btc = 0.0
    for d in mondays:
        price = binance_price_at(monday_ts_ms(d))
        amount_btc = NET_EUR / price
        cum_eur += WEEKLY_EUR
        cum_btc += amount_btc
        tranches.append({"date": d, "price": price, "btc": amount_btc})

    this_m = tranches[-1]
    last_m = tranches[-2] if len(tranches) >= 2 else None
    avg_price = cum_eur / cum_btc if cum_btc else 0.0

    if last_m:
        diff_pct = (this_m["price"] - last_m["price"]) / last_m["price"] * 100
        if diff_pct >= 0:
            cmp_line = f"\U0001F4C8 *{de(diff_pct)} %* teurer als letzten Montag"
        else:
            cmp_line = f"\U0001F4C9 *{de(diff_pct)} %* guenstiger als letzten Montag"
    else:
        cmp_line = "_Erster Kauf - kein Vergleich moeglich._"

    msg = (
        f"\u20BF *BTC-Sparplan - Montag {this_m['date']:%d.%m.%Y}*\n"
        f"\n"
        f"*Kurs heute:* {eur(this_m['price'])}\n"
        f"{cmp_line}\n"
        f"\n"
        f"*Gekauft:* {btc(this_m['btc'])} BTC\n"
        f"_(fuer {eur(NET_EUR)} netto, nach 0,40 % Spread)_\n"
        f"\n"
        f"*Gesamt investiert:* {eur(cum_eur)}\n"
        f"*BTC gesamt:* {btc(cum_btc)}\n"
        f"*\u00D8 Kaufpreis (inkl. Gebuehr):* {eur(avg_price)}\n"
        f"\n"
        f"[\U0001F4CA Dashboard oeffnen]({DASHBOARD_URL})"
    )

    send_telegram(msg)
    print(f"Nachricht fuer {this_m['date']} gesendet.")


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("FEHLER: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen.", file=sys.stderr)
        sys.exit(1)
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": "btc-dca-bot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        resp = json.loads(r.read().decode())
        if not resp.get("ok"):
            print(f"Telegram-Fehler: {resp}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
