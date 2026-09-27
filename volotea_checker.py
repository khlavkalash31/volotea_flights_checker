#!/usr/bin/env python3
"""Checker voli Volotea da Firenze.

Legge i prezzi dei voli di sola andata Volotea FLR -> destinazione e ritorno
per il prossimo anno, combina andata + ritorno con soggiorni di 5-10 giorni,
e invia per email le combinazioni più economiche.

Uso:
    python volotea_checker.py                 # scansiona e invia la mail
    python volotea_checker.py --dry-run       # scansiona e stampa il report, niente mail
    python volotea_checker.py --provider mock --dry-run   # prova con dati finti
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import os
import smtplib
import sys
import time
import tomllib
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
MAIL_TO = os.getenv("MAIL_TO", SMTP_USER)

APIFY_TOKEN = os.getenv("APIFY_TOKEN", None)

log = logging.getLogger("volotea")

WEEKDAYS_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


@dataclass
class Fare:
    """Il volo Volotea diretto più economico di un certo giorno su una tratta."""

    day: str  # YYYY-MM-DD
    price: float  # EUR, totale per tutti i passeggeri
    dep_time: str = ""
    arr_time: str = ""
    flight_no: str = ""


@dataclass
class Combo:
    dest_code: str
    dest_name: str
    outbound: Fare
    inbound: Fare

    @property
    def total(self) -> float:
        return round(self.outbound.price + self.inbound.price, 2)

    @property
    def nights(self) -> int:
        return (date.fromisoformat(self.inbound.day) - date.fromisoformat(self.outbound.day)).days


# --------------------------------------------------------------------------
# Fonti dei prezzi
# --------------------------------------------------------------------------


class Provider:
    """Interfaccia comune: restituisce, per ogni giorno richiesto, il volo più economico o None."""

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.passengers = int(cfg.get("passengers", 2))

    def fetch(self, frm: str, to: str, days: list[date]) -> dict[str, Fare | None]:
        raise NotImplementedError

    def booking_link(self, frm: str, to: str, out_day: str, ret_day: str) -> str:
        return "https://www.volotea.com/it/"


class _ConsentFetcher:
    """Fetcher per fast-flights che invia i cookie di consenso di Google.

    Dagli IP europei Google reindirizza a consent.google.com: senza questi cookie
    si riceve la pagina del consenso invece dei risultati.
    """

    COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg", "CONSENT": "PENDING+987"}

    def __init__(self):
        from primp import Client

        self.client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            cookie_store=True,
            cookies=self.COOKIES,
        )

    def fetch_html(self, q) -> str:
        params = q.params() if hasattr(q, "params") else {"q": q}
        res = self.client.get("https://www.google.com/travel/flights", params=params)
        if "consent.google" in str(res.url):
            raise RuntimeError("Google ha reindirizzato alla pagina del consenso")
        return res.text


class GoogleFlightsProvider(Provider):
    """Google Flights filtrato sulla compagnia Volotea (codice V7), solo voli diretti."""

    name = "google"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        try:
            import fast_flights  # noqa: F401
        except ImportError:
            sys.exit("Manca la libreria fast-flights: esegui  pip install -r requirements.txt")
        self.delay = float(cfg.get("request_delay_seconds", 1.0))
        self.fetcher = _ConsentFetcher()

    def _query(self, flights, trip: str):
        from fast_flights import Passengers, create_query

        return create_query(
            flights=flights,
            trip=trip,
            passengers=Passengers(adults=self.passengers),
            language="it",
            currency="EUR",
            max_stops=0,
        )

    def fetch(self, frm, to, days):
        from fast_flights import FlightQuery, FlightsNotFound, get_flights

        out: dict[str, Fare | None] = {}
        for i, d in enumerate(days):
            if i:
                time.sleep(self.delay)
            q = self._query([FlightQuery(date=d.isoformat(), from_airport=frm, to_airport=to, airlines=["V7"])], "one-way")
            try:
                results = get_flights(q, integration=self.fetcher)
            except FlightsNotFound:
                out[d.isoformat()] = None
                continue
            except TypeError:
                # fast-flights 3.1 va in errore su payload[3] = None quando quel giorno non ci sono voli
                out[d.isoformat()] = None
                continue
            except Exception as e:  # rete, blocco temporaneo, pagina cambiata...
                log.warning("  %s %s->%s: errore (%s), riprovo al prossimo giro", d, frm, to, e)
                continue  # non salvato in cache: verrà richiesto di nuovo
            best = None
            for f in results:
                is_volotea = any("volotea" in a.lower() or a.upper() == "V7" for a in f.airlines)
                if not is_volotea or len(f.flights) != 1 or not f.price:
                    continue
                if best is None or f.price < best.price:
                    leg = f.flights[0]
                    best = Fare(
                        day=d.isoformat(),
                        price=float(f.price),
                        dep_time="%02d:%02d" % leg.departure.time,
                        arr_time="%02d:%02d" % leg.arrival.time,
                    )
            out[d.isoformat()] = best
            log.debug("  %s %s->%s: %s", d, frm, to, best.price if best else "-")
        return out

    def booking_link(self, frm, to, out_day, ret_day):
        from fast_flights import FlightQuery

        q = self._query(
            [
                FlightQuery(date=out_day, from_airport=frm, to_airport=to, airlines=["V7"]),
                FlightQuery(date=ret_day, from_airport=to, to_airport=frm, airlines=["V7"]),
            ],
            "round-trip",
        )
        return q.url()


class ApifyProvider(Provider):
    """Scraper Volotea su Apify (legge dal sito Volotea con un browser reale). Serve APIFY_TOKEN."""

    name = "apify"
    ACTOR = "studio-amba~volotea-scraper"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.token = os.environ.get("APIFY_TOKEN")
        if not self.token:
            sys.exit("Imposta la variabile d'ambiente APIFY_TOKEN per usare il provider apify")

    def fetch(self, frm, to, days):
        if not days:
            return {}
        wanted = {d.isoformat() for d in days}
        payload = {
            "origin": frm,
            "destination": to,
            "dateFrom": min(days).isoformat(),
            "dateTo": max(days).isoformat(),
            "maxResults": 2000,
        }
        url = f"https://api.apify.com/v2/acts/{self.ACTOR}/run-sync-get-dataset-items?token={self.token}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=330) as r:
                items = json.load(r)
        except Exception as e:
            log.warning("  Apify %s->%s: errore (%s)", frm, to, e)
            return {}
        out: dict[str, Fare | None] = {d: None for d in wanted}
        for it in items:
            if it.get("origin") not in (None, frm) or it.get("destination") not in (None, to):
                continue
            dep = str(it.get("departureTime") or "")
            day, price = dep[:10], it.get("price")
            if day not in wanted or price is None:
                continue
            fare = Fare(
                day=day,
                price=float(price) * self.passengers,
                dep_time=dep[11:16],
                arr_time=str(it.get("arrivalTime") or "")[11:16],
                flight_no=str(it.get("flightNumber") or ""),
            )
            if out[day] is None or fare.price < out[day].price:
                out[day] = fare
        return out


class MockProvider(Provider):
    """Prezzi finti ma deterministici, per provare il programma senza rete."""

    name = "mock"

    def fetch(self, frm, to, days):
        out = {}
        for d in days:
            h = int(hashlib.sha256(f"{frm}{to}{d}".encode()).hexdigest(), 16)
            out[d.isoformat()] = Fare(day=d.isoformat(), price=float(19 + h % 120) * self.passengers, dep_time="%02d:%02d" % (6 + h % 14, h % 60))
        return out


PROVIDERS = {p.name: p for p in (GoogleFlightsProvider, ApifyProvider, MockProvider)}


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


class FareCache:
    def __init__(self, path: Path, max_age_hours: float):
        self.path = path
        self.max_age = timedelta(hours=max_age_hours)
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("Cache illeggibile, la ricreo")

    @staticmethod
    def key(provider, frm, to, day):
        return f"{provider}|{frm}|{to}|{day}"

    def get(self, provider, frm, to, day) -> tuple[bool, Fare | None]:
        entry = self.data.get(self.key(provider, frm, to, day))
        if not entry or datetime.now() - datetime.fromisoformat(entry["ts"]) > self.max_age:
            return False, None
        return True, Fare(**entry["fare"]) if entry["fare"] else None

    def put(self, provider, frm, to, day, fare: Fare | None):
        self.data[self.key(provider, frm, to, day)] = {"ts": datetime.now().isoformat(timespec="seconds"), "fare": asdict(fare) if fare else None}

    def save(self):
        today = date.today().isoformat()
        self.data = {k: v for k, v in self.data.items() if k.rsplit("|", 1)[1] >= today}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0))


# --------------------------------------------------------------------------
# Scansione e combinazioni
# --------------------------------------------------------------------------


def candidate_days(start: date, end: date, weekdays: list[int]) -> list[date]:
    days, d = [], start
    while d <= end:
        if not weekdays or d.weekday() in weekdays:
            days.append(d)
        d += timedelta(days=1)
    return days


def scan(cfg: dict, provider: Provider, cache: FareCache, today: date) -> dict[str, dict[str, dict[str, Fare]]]:
    """Ritorna {codice_destinazione: {"out": {giorno: Fare}, "in": {giorno: Fare}}}."""
    origin = cfg["origin"]
    start = today + timedelta(days=1)
    end = today + timedelta(days=int(cfg["horizon_days"]))
    result = {}
    for dest in cfg["destinations"]:
        code = dest["code"]
        days = candidate_days(start, end, dest.get("weekdays", []))
        result[code] = {}
        for direction, frm, to in (("out", origin, code), ("in", code, origin)):
            fares, missing = {}, []
            for d in days:
                hit, fare = cache.get(provider.name, frm, to, d.isoformat())
                if hit:
                    if fare:
                        fares[d.isoformat()] = fare
                else:
                    missing.append(d)
            log.info("%s -> %s: %d giorni da controllare (%d già in cache)", frm, to, len(missing), len(days) - len(missing))
            fetched = provider.fetch(frm, to, missing)
            for day, fare in fetched.items():
                cache.put(provider.name, frm, to, day, fare)
                if fare:
                    fares[day] = fare
            cache.save()  # salva spesso: se il programma si interrompe non si perde il lavoro
            result[code][direction] = fares
            log.info("   trovati %d voli", len(fares))
    return result


def all_combos(cfg: dict, fares: dict) -> list[Combo]:
    """Tutte le combinazioni andata+ritorno valide, dalla più economica."""
    names = {d["code"]: d.get("name", d["code"]) for d in cfg["destinations"]}
    lo, hi = int(cfg["min_stay_days"]), int(cfg["max_stay_days"])
    combos = []
    for code, f in fares.items():
        for out_day, out_fare in f.get("out", {}).items():
            od = date.fromisoformat(out_day)
            for stay in range(lo, hi + 1):
                ret = f.get("in", {}).get((od + timedelta(days=stay)).isoformat())
                if ret:
                    combos.append(Combo(code, names[code], out_fare, ret))
    combos.sort(key=lambda c: (c.total, c.outbound.day))
    return combos


def best_combos(cfg: dict, fares: dict) -> list[Combo]:
    combos = all_combos(cfg, fares)
    cap, top_n = int(cfg.get("max_per_destination", 0)), int(cfg["top_n"])
    picked, per_dest = [], {}
    for c in combos:
        if cap and per_dest.get(c.dest_code, 0) >= cap:
            continue
        picked.append(c)
        per_dest[c.dest_code] = per_dest.get(c.dest_code, 0) + 1
        if len(picked) >= top_n:
            break
    return picked


def cheapest_per_destination(cfg: dict, fares: dict) -> list[tuple[str, Combo | None]]:
    rows = []
    for d in cfg["destinations"]:
        one = best_combos({**cfg, "destinations": [d], "top_n": 1, "max_per_destination": 0}, {d["code"]: fares.get(d["code"], {})})
        rows.append((d.get("name", d["code"]), one[0] if one else None))
    return rows


# --------------------------------------------------------------------------
# Report ed email
# --------------------------------------------------------------------------


def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{WEEKDAYS_IT[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def fmt_eur(v: float) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


TH = "style='text-align:left;padding:6px;border-bottom:2px solid #ccc'"


def combos_table_html(cfg: dict, combos: list[Combo], provider: Provider) -> str:
    rows = []
    for i, c in enumerate(combos, 1):
        link = provider.booking_link(cfg["origin"], c.dest_code, c.outbound.day, c.inbound.day)
        rows.append(
            "<tr>"
            f"<td>{i}</td><td><b>{html.escape(c.dest_name)}</b></td>"
            f"<td>{fmt_day(c.outbound.day)}<br><small>{c.outbound.dep_time}</small></td>"
            f"<td>{fmt_day(c.inbound.day)}<br><small>{c.inbound.dep_time}</small></td>"
            f"<td>{c.nights}</td>"
            f"<td style='text-align:right'><b>{fmt_eur(c.total)}</b><br><small>{fmt_eur(c.outbound.price)} + {fmt_eur(c.inbound.price)}</small></td>"
            f"<td><a href='{html.escape(link)}'>vedi</a></td>"
            "</tr>"
        )
    return f"""<table style="border-collapse:collapse;font-size:14px" cellpadding="6">
<tr><th {TH}>#</th><th {TH}>Destinazione</th><th {TH}>Andata</th><th {TH}>Ritorno</th><th {TH}>Notti</th><th {TH}>Prezzo</th><th {TH}></th></tr>
{''.join(rows) or '<tr><td colspan=7>Nessuna combinazione trovata.</td></tr>'}
</table>"""


def build_report(cfg: dict, combos: list[Combo], per_dest, provider: Provider, today: date) -> tuple[str, str, str]:
    origin = cfg["origin"]
    pax = int(cfg.get("passengers", 1))
    subject = f"Voli Volotea da Firenze: migliori combinazioni al {today.strftime('%d/%m/%Y')}"
    if combos:
        subject += f" (da {fmt_eur(combos[0].total)})"
    pax_note = "a persona" if pax == 1 else f"totale per {pax} adulti"

    lines = [subject, "", f"Prezzi andata+ritorno ({pax_note}), soggiorni di {cfg['min_stay_days']}-{cfg['max_stay_days']} giorni, prossimi {cfg['horizon_days']} giorni.", ""]
    for i, c in enumerate(combos, 1):
        lines.append(
            f"{i:>2}. {c.dest_name:<18} {fmt_day(c.outbound.day)} -> {fmt_day(c.inbound.day)} "
            f"({c.nights} notti)  {fmt_eur(c.total)}  [and. {fmt_eur(c.outbound.price)} + rit. {fmt_eur(c.inbound.price)}]"
        )
    if not combos:
        lines.append("Nessuna combinazione trovata (controlla i log: forse la fonte dei prezzi non ha risposto).")

    lines += ["", "Miglior prezzo per destinazione:"]
    dest_html = []
    for name, c in per_dest:
        txt = f"{fmt_eur(c.total)} ({fmt_day(c.outbound.day)} -> {fmt_day(c.inbound.day)})" if c else "nessun volo trovato"
        lines.append(f"  - {name}: {txt}")
        dest_html.append(f"<li><b>{html.escape(name)}</b>: {html.escape(txt)}</li>")

    lines += ["", "In allegato le migliori combinazioni per ogni destinazione (CSV e HTML)."]
    body_html = f"""<html><body style="font-family:Arial,sans-serif;color:#222">
<h2 style="margin-bottom:4px">Voli Volotea da Firenze</h2>
<p style="margin-top:0;color:#555">Andata e ritorno ({pax_note}), soggiorni di {cfg['min_stay_days']}-{cfg['max_stay_days']} giorni,
prossimi {cfg['horizon_days']} giorni. Aggiornato al {today.strftime('%d/%m/%Y')}.</p>
{combos_table_html(cfg, combos, provider)}
<h3>Miglior prezzo per destinazione</h3><ul>{''.join(dest_html)}</ul>
<p>In allegato le migliori combinazioni per ogni destinazione: <b>combinazioni.csv</b> (per Excel) e <b>combinazioni.html</b>.</p>
<p style="color:#888;font-size:12px">Prezzi indicativi (fonte: {provider.name}), bagaglio in stiva escluso. Verifica sempre sul sito Volotea prima di prenotare.</p>
</body></html>"""
    return subject, "\n".join(lines), body_html


def build_attachments(cfg: dict, combos: list[Combo], provider: Provider, today: date) -> dict[str, str]:
    """Tutte le combinazioni: CSV (separatore ';' e virgola decimale, per Excel in italiano) e HTML per destinazione."""
    origin = cfg["origin"]
    num = lambda v: f"{v:.2f}".replace(".", ",")  # noqa: E731
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["destinazione", "codice", "andata", "giorno_andata", "ora_andata", "ritorno", "giorno_ritorno", "ora_ritorno",
                "notti", "prezzo_andata", "prezzo_ritorno", "totale", "link"])
    for c in combos:
        od, rd = date.fromisoformat(c.outbound.day), date.fromisoformat(c.inbound.day)
        w.writerow([c.dest_name, c.dest_code, c.outbound.day, WEEKDAYS_IT[od.weekday()], c.outbound.dep_time,
                    c.inbound.day, WEEKDAYS_IT[rd.weekday()], c.inbound.dep_time, c.nights,
                    num(c.outbound.price), num(c.inbound.price), num(c.total),
                    provider.booking_link(origin, c.dest_code, c.outbound.day, c.inbound.day)])

    pax = int(cfg.get("passengers", 1))
    pax_note = "a persona" if pax == 1 else f"totale per {pax} adulti"
    sections, index = [], []
    for d in cfg["destinations"]:
        mine = [c for c in combos if c.dest_code == d["code"]]
        name = html.escape(d.get("name", d["code"]))
        best = f" — da {fmt_eur(mine[0].total)}" if mine else " — nessun volo trovato"
        index.append(f"<li><a href='#{d['code']}'>{name}</a>{best} ({len(mine)} combinazioni)</li>")
        sections.append(f"<h2 id='{d['code']}'>{name}</h2>" + (combos_table_html(cfg, mine, provider) if mine else "<p>Nessuna combinazione trovata.</p>"))
    page = f"""<!doctype html><html lang="it"><head><meta charset="utf-8"><title>Voli Volotea da Firenze</title></head>
<body style="font-family:Arial,sans-serif;color:#222;max-width:900px;margin:auto;padding:16px">
<h1 style="margin-bottom:4px">Voli Volotea da Firenze — migliori combinazioni per destinazione</h1>
<p style="margin-top:0;color:#555">Andata e ritorno ({pax_note}), soggiorni di {cfg['min_stay_days']}-{cfg['max_stay_days']} giorni,
prossimi {cfg['horizon_days']} giorni. Aggiornato al {today.strftime('%d/%m/%Y')}. Ogni destinazione è ordinata dal prezzo più basso.</p>
<ul>{''.join(index)}</ul>
{''.join(sections)}
<p style="color:#888;font-size:12px">Prezzi indicativi (fonte: {provider.name}), bagaglio in stiva escluso.</p>
</body></html>"""
    return {"combinazioni.csv": "\ufeff" + buf.getvalue(), "combinazioni.html": page}


def send_email(subject: str, text: str, body_html: str, attachments: dict[str, str] | None = None):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to = os.getenv("MAIL_TO", user or "")
    sender = os.getenv("MAIL_FROM", user or "")
    if not (user and password and to):
        sys.exit("Configura SMTP_USER, SMTP_PASSWORD e MAIL_TO (vedi README)")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    for fname, content in (attachments or {}).items():
        subtype = "csv" if fname.endswith(".csv") else "html"
        msg.add_attachment(content.encode("utf-8"), maintype="text", subtype=subtype, filename=fname)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    log.info("Email inviata a %s", to)


# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="Migliori combinazioni voli Volotea da Firenze")
    ap.add_argument("--config", default=Path(__file__).with_name("config.toml"), type=Path)
    ap.add_argument("--provider", choices=sorted(PROVIDERS), help="sovrascrive il provider del file di config")
    ap.add_argument("--dry-run", action="store_true", help="non invia la mail, stampa il report e salva report.html")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg = tomllib.loads(args.config.read_text())
    if args.provider:
        cfg["provider"] = args.provider
    provider = PROVIDERS[cfg["provider"]](cfg)
    cache_path = Path(cfg.get("cache_file", "cache/fares.json"))
    if not cache_path.is_absolute():
        cache_path = args.config.parent / cache_path
    cache = FareCache(cache_path, float(cfg.get("cache_hours", 20)))

    today = date.today()
    fares = scan(cfg, provider, cache, today)
    combos = best_combos(cfg, fares)
    subject, text, body_html = build_report(cfg, combos, cheapest_per_destination(cfg, fares), provider, today)
    att_cap, per_dest_count, att_combos = int(cfg.get("attachment_max_per_destination", 0)), {}, []
    for c in all_combos(cfg, fares):
        per_dest_count[c.dest_code] = per_dest_count.get(c.dest_code, 0) + 1
        if not att_cap or per_dest_count[c.dest_code] <= att_cap:
            att_combos.append(c)
    attachments = build_attachments(cfg, att_combos, provider, today)

    if args.dry_run:
        print("\n" + text)
        out = args.config.parent / "report.html"
        out.write_text(body_html)
        log.info("Report HTML salvato in %s", out)
        for fname, content in attachments.items():
            (args.config.parent / fname).write_text(content, encoding="utf-8")
            log.info("Allegato salvato in %s", args.config.parent / fname)
    else:
        send_email(subject, text, body_html, attachments)


if __name__ == "__main__":
    main()
