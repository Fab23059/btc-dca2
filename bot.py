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
BUY_HOUR = 8                               # Ausfuehrungszeit lt. Binance: 08:39:54
BUY_MIN = 40                               # -> wir runden auf 08:40
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
    """BTC/EUR-Preis zum Zeitpunkt ts_ms (Montag 08:40 DE-Zeit).
    Dieselbe Logik wie im Dashboard, auf die Minute genau:
    1) CryptoCompare Minutenkerze (exakt, ~7 Tage zurueck)
    2) CoinGecko enges Zeitfenster -> 5-Min-Daten (jede Vergangenheit)
    3) CryptoCompare Stundenkerze (grob, letzter Fallback)."""
    sec = ts_ms // 1000
    # 1) Minutengenau (nur die letzten ~7 Tage verfuegbar)
    try:
        j = http_get_json(
            "https://min-api.cryptocompare.com/data/v2/histominute"
            f"?fsym=BTC&tsym=EUR&limit=1&toTs={sec}"
        )
        if j.get("Response") == "Success":
            data = j.get("Data", {}).get("Data", [])
            if data and data[-1].get("close"):
                return float(data[-1]["close"])
    except Exception:  # noqa: BLE001
        pass
    # 2) CoinGecko: enges Fenster (<= 1 Tag) liefert 5-Min-Aufloesung
    try:
        j = http_get_json(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart/range"
            f"?vs_currency=eur&from={sec - 3600}&to={sec + 3600}"
        )
        prices = j.get("prices") or []
        if prices:
            best = min(prices, key=lambda p: abs(p[0] - ts_ms))
            return float(best[1])
    except Exception:  # noqa: BLE001
        pass
    # 3) Stundenkerze als grober Fallback
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
    raise RuntimeError("Kurs nicht abrufbar (CryptoCompare & CoinGecko)")


def current_price():
    """Aktueller BTC/EUR-Kurs (fuer Gesamtwert und G/V)."""
    try:
        j = http_get_json("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=EUR")
        if j.get("EUR"):
            return float(j["EUR"])
    except Exception:  # noqa: BLE001
        pass
    try:
        j = http_get_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur"
        )
        return float(j["bitcoin"]["eur"])
    except Exception:  # noqa: BLE001
        return None


def mondays_until(today):
    """Alle Spar-Montage (Datum) von START_DATE bis einschliesslich heute."""
    out = []
    d = START_DATE
    while d <= today:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def monday_ts_ms(d):
    """UTC-Millisekunden fuer 08:40 deutscher Zeit am Datum d (DST-sicher)."""
    local = dt.datetime(d.year, d.month, d.day, BUY_HOUR, BUY_MIN, 0, tzinfo=TZ)
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
    # GitHub-Cron feuert 06:45 UND 07:45 UTC (Montag). Nur der Lauf, der in
    # deutscher Zeit auf 08:40-08:59 faellt, sendet wirklich (also NACH dem
    # echten Binance-Kauf um 08:39:54). Im Sommer ist das der 06:45-UTC-Lauf
    # (= 08:45 MESZ), im Winter der 07:45-UTC-Lauf (= 08:45 MEZ).
    force = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
    if not force and not (now_local.hour == BUY_HOUR and now_local.minute >= BUY_MIN):
        print(f"Kein 08:40-Lauf (aktuell {now_local:%H:%M} DE-Zeit) -> uebersprungen.")
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
    week_no = len(tranches)
    fees_total = cum_eur * FEE_RATE
    next_buy = this_m["date"] + dt.timedelta(days=7)

    if last_m:
        diff_pct = (this_m["price"] - last_m["price"]) / last_m["price"] * 100
        if diff_pct >= 0:
            cmp_line = f"\U0001F4C8 <b>{de(diff_pct)} %</b> teurer als letzten Montag"
        else:
            cmp_line = f"\U0001F4C9 <b>{de(diff_pct)} %</b> guenstiger als letzten Montag"
    else:
        cmp_line = "<i>Erster Kauf \u2013 kein Vergleich moeglich.</i>"

    # Aktueller Wert & G/V (falls Live-Kurs abrufbar)
    cur = current_price()
    if cur:
        value_now = cum_btc * cur
        pnl = value_now - cum_eur
        pnl_pct = (pnl / cum_eur * 100) if cum_eur else 0.0
        arrow = "\U0001F7E2" if pnl >= 0 else "\U0001F534"
        sign = "+" if pnl >= 0 else ""
        value_block = (
            f"<b>Wert jetzt:</b> {eur(value_now)}\n"
            f"{arrow} <b>G/V:</b> {sign}{eur(pnl)} ({sign}{de(pnl_pct)} %)\n"
        )
    else:
        value_block = ""

    msg = (
        f"\u20BF <b>BTC-Sparplan \u2013 Montag {this_m['date']:%d.%m.%Y}</b>\n"
        f"<i>Woche {week_no}</i>\n"
        f"\n"
        f"<b>Kurs (08:40):</b> {eur(this_m['price'])}\n"
        f"{cmp_line}\n"
        f"\n"
        f"<b>Gekauft:</b> {btc(this_m['btc'])} BTC\n"
        f"<i>(fuer {eur(NET_EUR)} netto, nach 0,40 % Spread)</i>\n"
        f"\n"
        f"\u2014 <b>Gesamt</b> \u2014\n"
        f"<b>Investiert:</b> {eur(cum_eur)}\n"
        f"<b>BTC:</b> {btc(cum_btc)}\n"
        f"<b>\u00D8 Kaufpreis:</b> {eur(avg_price)}\n"
        f"{value_block}"
        f"<b>Gebuehren gesamt:</b> {eur(fees_total)}\n"
        f"\n"
        f'<a href="{https://fab23059.github.io/btc-dca2/}">\U0001F4CA Dashboard oeffnen</a>\n'
        f"<i>N\u00e4chster Kauf: Montag {next_buy:%d.%m.%Y}</i>"
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
