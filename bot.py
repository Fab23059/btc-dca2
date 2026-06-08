#!/usr/bin/env python3
"""
BTC-DCA -> Telegram-Push
========================
Laeuft via GitHub Actions jeden Montag um 08:00 Uhr deutscher Zeit.

Stateless: Das Skript haelt keine Datenbank. Es rechnet die komplette
Historie bei jedem Lauf neu aus oeffentlichen Kursdaten aus.
Das ist moeglich, weil der Sparplan deterministisch ist (jeder Montag
08:00 Europe/Berlin ab dem 08.06.2026).

Es wird KEIN API-Key benoetigt. Kursquellen sind dieselben wie im
Dashboard: CryptoCompare (zuerst) und CoinGecko (Fallback). Dadurch
zeigen Telegram-Nachricht und Dashboard garantiert identische Zahlen.
"""

import os
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo
import urllib.request
import urllib.parse
import urllib.error

# ----------------------------- Konfiguration -----------------------------
START_DATE = dt.date(2026, 6, 8)          # erster Spar-Montag
WEEKLY_EUR = 15.0                          # Einzahlung pro Woche
FEE_RATE = 0.004                           # 0,40 % Spread (Binance Convert)
NET_EUR = WEEKLY_EUR * (1 - FEE_RATE)      # 14,94 EUR fliessen in BTC
TZ = ZoneInfo("Europe/Berlin")

# Aus GitHub Secrets / Variables
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://fab23059.github.io/btc-dca2/")


# ------------------------------- Helfer ----------------------------------
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-dca-bot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def price_at(ts_ms):
    """BTC/EUR-Preis zum Zeitpunkt ts_ms (Montag 08:00 DE-Zeit).
    Exakt dieselbe Logik wie im Dashboard: CryptoCompare zuerst,
    CoinGecko als Fallback -> beide zeigen identische Zahlen."""
    sec = ts_ms // 1000
    # 1) CryptoCompare: Stundenkerze bis sec -> close
    try:
        j = http_get_json(
            "https://min-api.cryptocompare.com/data/v2/histohour"
            f"?fsym=BTC&tsym=EUR&limit=1&toTs={sec}"
        )
        if j.get("Response") == "Success":
            data = j.get("Data", {}).get("Data", [])
            if data and data[-1].get("close"):
                return float(data[-1]["close"])
    except Exception:  # noqa: BLE001
        pass
    # 2) CoinGecko: Zeitfenster um sec -> naechster Punkt zu ts
    try:
        j = http_get_json(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart/range"
            f"?vs_currency=eur&from={sec - 5400}&to={sec + 5400}"
        )
        prices = j.get("prices") or []
        if prices:
            best = min(prices, key=lambda p: abs(p[0] - ts_ms))
            return float(best[1])
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("Kurs nicht abrufbar (CryptoCompare & CoinGecko)")


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
        price = price_at(monday_ts_ms(d))
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
            cmp_line = f"\U0001F4C8 <b>{de(diff_pct)} %</b> teurer als letzten Montag"
        else:
            cmp_line = f"\U0001F4C9 <b>{de(diff_pct)} %</b> guenstiger als letzten Montag"
    else:
        cmp_line = "<i>Erster Kauf \u2013 kein Vergleich moeglich.</i>"

    msg = (
        f"\u20BF <b>BTC-Sparplan \u2013 Montag {this_m['date']:%d.%m.%Y}</b>\n"
        f"\n"
        f"<b>Kurs heute:</b> {eur(this_m['price'])}\n"
        f"{cmp_line}\n"
        f"\n"
        f"<b>Gekauft:</b> {btc(this_m['btc'])} BTC\n"
        f"<i>(fuer {eur(NET_EUR)} netto, nach 0,40 % Spread)</i>\n"
        f"\n"
        f"<b>Gesamt investiert:</b> {eur(cum_eur)}\n"
        f"<b>BTC gesamt:</b> {btc(cum_btc)}\n"
        f"<b>\u00D8 Kaufpreis (inkl. Gebuehr):</b> {eur(avg_price)}\n"
        f"\n"
        f'<a href="{DASHBOARD_URL}">\U0001F4CA Dashboard oeffnen</a>'
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
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": "btc-dca-bot"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"Telegram-Fehler HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    if not resp.get("ok"):
        print(f"Telegram-Fehler: {resp}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
