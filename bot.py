#!/usr/bin/env python3
"""
BTC-DCA -> Telegram-Push
========================
Laeuft via GitHub Actions jeden Montag um ~08:45 Uhr deutscher Zeit
(also kurz nach dem echten Binance-Kauf um 08:39:54).

Stateless: Das Skript haelt keine Datenbank. Es rechnet die komplette
Historie bei jedem Lauf neu aus oeffentlichen Kursdaten aus.
Das ist moeglich, weil der Sparplan deterministisch ist (jeder Montag
08:40 Europe/Berlin ab dem 08.06.2026).

Es wird KEIN API-Key benoetigt. Kursquellen sind dieselben wie im
Dashboard: CryptoCompare (zuerst) und CoinGecko (Fallback). Dadurch
zeigen Telegram-Nachricht und Dashboard garantiert identische Zahlen.
"""

import os
import sys
import json
import random
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
STATE_FILE = os.path.join("state", "last_sent.txt")  # merkt sich den letzten Sendetag

# Aus GitHub Secrets / Variables. 'or' faengt auch einen leeren Wert ab
# (z.B. wenn die Repo-Variable DASHBOARD_URL gar nicht gesetzt ist).
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL") or "https://fab23059.github.io/btc-dca2/"


# ------------------------------- Helfer ----------------------------------
def already_sent(today_iso):
    """True, wenn fuer heute schon eine Nachricht raus ist (Dedup)."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip() == today_iso
    except Exception:  # Datei fehlt o.ae. -> noch nicht gesendet
        return False


def mark_sent(today_iso):
    """Haelt fest, dass heute gesendet wurde."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(today_iso)


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


# -------------------------- Broker-Kommentar ------------------------------
# Ein heruntergekommener Ex-Broker im Wolf-of-Wall-Street-Modus kommentiert
# deine Kaeufe. Manisch, halb unserioes, aber die echte Expertise blitzt durch.
BROKER_FIRST = [
    "Erste Tranche, Kid \u2013 genau so hat bei mir auch alles angefangen, bevor die Lambos kamen. Der Markt belohnt nicht die Schlauen, er belohnt die, die jeden Montag wiederkommen. Setz dich rein und lass den Zinseszins die Drecksarbeit machen.",
    "Willkommen im Spiel, Rookie. Ich hab Vermoegen kommen und gehen sehen, aber weisst du was bleibt? Disziplin \u2013 du kaufst stur weiter, egal was die Schlagzeilen schreien.",
    "Tranche eins steht. Klingt nach wenig? So fuehlt sich jeder Anfang an. Volatilitaet ist dein Freund, wenn du regelmaessig kaufst \u2013 merk dir den Satz, der war mal 50 Riesen Beratung wert.",
]
BROKER_UP = [
    "Teurer eingekauft, na und? Das heisst der Markt laeuft, nicht dass du zu spaet bist. Ich hab '17 auf den perfekten Dip gewartet und ihn nie gesehen \u2013 Trend is your friend, Baby.",
    "Hoeher als letzte Woche \u2013 relax, das ist ein Bullmarkt-Symptom, kein Fehler. Die Amateure warten auf Ruecksetzer und verpassen die Rallye, die Profis akkumulieren durch. Brust raus.",
    "Aufschlag bezahlt, ja \u2013 aber Momentum kostet nun mal Eintritt. Ein gruener Montag ist kein Grund zu heulen, sondern weiterzumachen. Stur. Bleiben.",
]
BROKER_DOWN = [
    "DIP! Hoerst du das Klingeln? Das ist die Kasse \u2013 dieselben Sats fuer weniger Fiat, das ist quasi geschenkt. Schwache Haende kotzen jetzt, du sammelst ein. Genau dafuer macht man DCA.",
    "Guenstiger als letzte Woche \u2013 das ist kein Crash, das ist ein SALE. Ich haette '18 fuer so einen Montag einiges gegeben. Rot ist die Farbe der Geduldigen, also laechel und kauf nach.",
    "Runtergekommen? Perfekt, mehr Bitcoin fuers gleiche Geld \u2013 Mathe luegt nicht, auch wenn ich's manchmal tue. Die Angst der anderen ist dein Rabatt. Plan durchziehen, irgendwann dankst du mir.",
]


def broker_take(week_no, diff_pct):
    """Liefert einen passenden, leicht groessenwahnsinnigen Spruch."""
    if week_no <= 1:
        pool = BROKER_FIRST
    elif diff_pct is not None and diff_pct < 0:
        pool = BROKER_DOWN
    else:
        pool = BROKER_UP
    return random.choice(pool)


# -------------------------------- Hauptlauf -------------------------------
def main():
    now_local = dt.datetime.now(TZ)
    today = now_local.date()
    today_iso = today.isoformat()
    force = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")

    # Robuste Trigger-Logik gegen GitHubs unzuverlaessigen Cron:
    # GitHub versucht es montagvormittags mehrfach. Gesendet wird beim ERSTEN
    # Lauf, der (a) montags, (b) nach 08:40 DE-Zeit liegt und (c) heute noch
    # nicht gesendet hat. Der Sendetag wird in state/last_sent.txt vermerkt,
    # damit es bei mehreren Laeufen trotzdem nur EINE Nachricht gibt.
    if not force:
        if today.weekday() != 0:  # 0 = Montag
            print(f"Kein Montag ({today_iso}) -> uebersprungen.")
            return
        mins = now_local.hour * 60 + now_local.minute
        if mins < BUY_HOUR * 60 + BUY_MIN:
            print(f"Vor 08:40 (aktuell {now_local:%H:%M}) -> warte auf den Kauf.")
            return
        if already_sent(today_iso):
            print(f"Fuer {today_iso} bereits gesendet -> uebersprungen.")
            return

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
    next_buy = this_m["date"] + dt.timedelta(days=7)

    diff_pct = None
    if last_m:
        diff_pct = (this_m["price"] - last_m["price"]) / last_m["price"] * 100
        if diff_pct >= 0:
            cmp_line = f"\U0001F4C8 <b>{de(diff_pct)} %</b> teurer als letzten Montag"
        else:
            cmp_line = f"\U0001F4C9 <b>{de(diff_pct)} %</b> guenstiger als letzten Montag"
    else:
        cmp_line = "\U0001F331 <i>Erster Kauf \u2013 kein Vergleich moeglich.</i>"

    def signed(x):
        return ("+" if x >= 0 else "") + eur(x)

    # Wert & G/V (falls Live-Kurs abrufbar) \u2013 gesamt + diese Woche
    cur = current_price()
    value_lines = ""
    if cur:
        value_now = cum_btc * cur
        pnl_total = value_now - cum_eur
        pnl_total_pct = (pnl_total / cum_eur * 100) if cum_eur else 0.0
        dot = "\U0001F7E2" if pnl_total >= 0 else "\U0001F534"
        pct_sign = "+" if pnl_total_pct >= 0 else ""
        value_lines = (
            f"\U0001F48E <b>Wert jetzt:</b> {eur(value_now)}\n"
            f"{dot} <b>G/V gesamt:</b> {signed(pnl_total)} ({pct_sign}{de(pnl_total_pct)} %)\n"
        )
        if last_m:
            prev_btc = cum_btc - this_m["btc"]
            value_prev = prev_btc * last_m["price"]
            pnl_week = pnl_total - (value_prev - (cum_eur - WEEKLY_EUR))
            value_lines += f"\U0001F4C5 <b>G/V diese Woche:</b> {signed(pnl_week)}\n"

    take = broker_take(week_no, diff_pct)

    msg = (
        f"\u20BF <b>BTC-Sparplan \u2013 Montag {this_m['date']:%d.%m.%Y}</b>\n"
        f"\U0001F5D3\uFE0F <i>Woche {week_no}</i>\n"
        f"\n"
        f"\U0001F4B0 <b>Kurs (08:40):</b> {eur(this_m['price'])}\n"
        f"{cmp_line}\n"
        f"\n"
        f"\U0001F6D2 <b>Gekauft:</b> {btc(this_m['btc'])} BTC\n"
        f"<i>(fuer {eur(NET_EUR)} netto)</i>\n"
        f"\n"
        f"\U0001FA99 <b>BTC gesamt:</b> {btc(cum_btc)}\n"
        f"\U0001F3AF <b>\u00D8 Kaufpreis:</b> {eur(avg_price)}\n"
        f"\U0001F4E6 <b>Investiert:</b> {eur(cum_eur)}\n"
        f"{value_lines}"
        f"\n"
        f"\U0001F399\uFE0F <i>\u201E{take}\u201C</i>\n"
        f"\n"
        f'\U0001F517 <a href="{DASHBOARD_URL}">Dashboard oeffnen</a>\n'
        f"\u23ED\uFE0F <i>N\u00e4chster Kauf: Montag {next_buy:%d.%m.%Y}</i>"
    )

    send_telegram(msg)
    if not force:
        mark_sent(today_iso)
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
    main()#!/usr/bin/env python3
"""
BTC-DCA -> Telegram-Push
========================
Laeuft via GitHub Actions jeden Montag um ~08:45 Uhr deutscher Zeit
(also kurz nach dem echten Binance-Kauf um 08:39:54).

Stateless: Das Skript haelt keine Datenbank. Es rechnet die komplette
Historie bei jedem Lauf neu aus oeffentlichen Kursdaten aus.
Das ist moeglich, weil der Sparplan deterministisch ist (jeder Montag
08:40 Europe/Berlin ab dem 08.06.2026).

Es wird KEIN API-Key benoetigt. Kursquellen sind dieselben wie im
Dashboard: CryptoCompare (zuerst) und CoinGecko (Fallback). Dadurch
zeigen Telegram-Nachricht und Dashboard garantiert identische Zahlen.
"""

import os
import sys
import json
import random
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

# Aus GitHub Secrets / Variables. 'or' faengt auch einen leeren Wert ab
# (z.B. wenn die Repo-Variable DASHBOARD_URL gar nicht gesetzt ist).
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL") or "https://fab23059.github.io/btc-dca2/"


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


# -------------------------- Broker-Kommentar ------------------------------
# Ein heruntergekommener Ex-Broker im Wolf-of-Wall-Street-Modus kommentiert
# deine Kaeufe. Manisch, halb unserioes, aber die echte Expertise blitzt durch.
BROKER_FIRST = [
    "Erste Tranche, Kid \u2013 genau so hat bei mir auch alles angefangen, bevor die Lambos kamen. Der Markt belohnt nicht die Schlauen, er belohnt die, die jeden Montag wiederkommen. Setz dich rein und lass den Zinseszins die Drecksarbeit machen.",
    "Willkommen im Spiel, Rookie. Ich hab Vermoegen kommen und gehen sehen, aber weisst du was bleibt? Disziplin \u2013 du kaufst stur weiter, egal was die Schlagzeilen schreien.",
    "Tranche eins steht. Klingt nach wenig? So fuehlt sich jeder Anfang an. Volatilitaet ist dein Freund, wenn du regelmaessig kaufst \u2013 merk dir den Satz, der war mal 50 Riesen Beratung wert.",
]
BROKER_UP = [
    "Teurer eingekauft, na und? Das heisst der Markt laeuft, nicht dass du zu spaet bist. Ich hab '17 auf den perfekten Dip gewartet und ihn nie gesehen \u2013 Trend is your friend, Baby.",
    "Hoeher als letzte Woche \u2013 relax, das ist ein Bullmarkt-Symptom, kein Fehler. Die Amateure warten auf Ruecksetzer und verpassen die Rallye, die Profis akkumulieren durch. Brust raus.",
    "Aufschlag bezahlt, ja \u2013 aber Momentum kostet nun mal Eintritt. Ein gruener Montag ist kein Grund zu heulen, sondern weiterzumachen. Stur. Bleiben.",
]
BROKER_DOWN = [
    "DIP! Hoerst du das Klingeln? Das ist die Kasse \u2013 dieselben Sats fuer weniger Fiat, das ist quasi geschenkt. Schwache Haende kotzen jetzt, du sammelst ein. Genau dafuer macht man DCA.",
    "Guenstiger als letzte Woche \u2013 das ist kein Crash, das ist ein SALE. Ich haette '18 fuer so einen Montag einiges gegeben. Rot ist die Farbe der Geduldigen, also laechel und kauf nach.",
    "Runtergekommen? Perfekt, mehr Bitcoin fuers gleiche Geld \u2013 Mathe luegt nicht, auch wenn ich's manchmal tue. Die Angst der anderen ist dein Rabatt. Plan durchziehen, irgendwann dankst du mir.",
]


def broker_take(week_no, diff_pct):
    """Liefert einen passenden, leicht groessenwahnsinnigen Spruch."""
    if week_no <= 1:
        pool = BROKER_FIRST
    elif diff_pct is not None and diff_pct < 0:
        pool = BROKER_DOWN
    else:
        pool = BROKER_UP
    return random.choice(pool)


# -------------------------------- Hauptlauf -------------------------------
def main():
    now_local = dt.datetime.now(TZ)

    # DST-sichere Trigger-Logik:
    # GitHub-Cron feuert 06:45 UND 07:45 UTC (Montag). Im Sommer ist der
    # 06:45-UTC-Lauf = 08:45 MESZ, im Winter der 07:45-UTC-Lauf = 08:45 MEZ.
    # Sende-Fenster: 08:40 bis 09:39 deutscher Zeit. Breit genug, dass auch ein
    # um bis zu ~55 Min verspaeteter GitHub-Cron noch durchkommt, aber so, dass
    # pro Montag nur EIN Lauf sendet (der zweite Cron um 09:45 faellt knapp raus).
    force = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
    mins = now_local.hour * 60 + now_local.minute
    start = BUY_HOUR * 60 + BUY_MIN          # 08:40
    if not force and not (start <= mins < start + 60):
        print(f"Ausserhalb Sende-Fenster 08:40-09:39 (aktuell {now_local:%H:%M} DE-Zeit) -> uebersprungen.")
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
    next_buy = this_m["date"] + dt.timedelta(days=7)

    diff_pct = None
    if last_m:
        diff_pct = (this_m["price"] - last_m["price"]) / last_m["price"] * 100
        if diff_pct >= 0:
            cmp_line = f"\U0001F4C8 <b>{de(diff_pct)} %</b> teurer als letzten Montag"
        else:
            cmp_line = f"\U0001F4C9 <b>{de(diff_pct)} %</b> guenstiger als letzten Montag"
    else:
        cmp_line = "\U0001F331 <i>Erster Kauf \u2013 kein Vergleich moeglich.</i>"

    def signed(x):
        return ("+" if x >= 0 else "") + eur(x)

    # Wert & G/V (falls Live-Kurs abrufbar) \u2013 gesamt + diese Woche
    cur = current_price()
    value_lines = ""
    if cur:
        value_now = cum_btc * cur
        pnl_total = value_now - cum_eur
        pnl_total_pct = (pnl_total / cum_eur * 100) if cum_eur else 0.0
        dot = "\U0001F7E2" if pnl_total >= 0 else "\U0001F534"
        pct_sign = "+" if pnl_total_pct >= 0 else ""
        value_lines = (
            f"\U0001F48E <b>Wert jetzt:</b> {eur(value_now)}\n"
            f"{dot} <b>G/V gesamt:</b> {signed(pnl_total)} ({pct_sign}{de(pnl_total_pct)} %)\n"
        )
        if last_m:
            prev_btc = cum_btc - this_m["btc"]
            value_prev = prev_btc * last_m["price"]
            pnl_week = pnl_total - (value_prev - (cum_eur - WEEKLY_EUR))
            value_lines += f"\U0001F4C5 <b>G/V diese Woche:</b> {signed(pnl_week)}\n"

    take = broker_take(week_no, diff_pct)

    msg = (
        f"\u20BF <b>BTC-Sparplan \u2013 Montag {this_m['date']:%d.%m.%Y}</b>\n"
        f"\U0001F5D3\uFE0F <i>Woche {week_no}</i>\n"
        f"\n"
        f"\U0001F4B0 <b>Kurs (08:40):</b> {eur(this_m['price'])}\n"
        f"{cmp_line}\n"
        f"\n"
        f"\U0001F6D2 <b>Gekauft:</b> {btc(this_m['btc'])} BTC\n"
        f"<i>(fuer {eur(NET_EUR)} netto)</i>\n"
        f"\n"
        f"\U0001FA99 <b>BTC gesamt:</b> {btc(cum_btc)}\n"
        f"\U0001F3AF <b>\u00D8 Kaufpreis:</b> {eur(avg_price)}\n"
        f"\U0001F4E6 <b>Investiert:</b> {eur(cum_eur)}\n"
        f"{value_lines}"
        f"\n"
        f"\U0001F399\uFE0F <i>\u201E{take}\u201C</i>\n"
        f"\n"
        f'\U0001F517 <a href="{DASHBOARD_URL}">Dashboard oeffnen</a>\n'
        f"\u23ED\uFE0F <i>N\u00e4chster Kauf: Montag {next_buy:%d.%m.%Y}</i>"
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
