"""
Litteraturhuset Dagsoversikt
Flask-backend med bakgrunns-caching for rask respons.

Optimalisert for Gunicorn i Docker (Coolify/Nixpacks):
  gunicorn app:app --bind 0.0.0.0:5000 --workers 1 --threads 4

VIKTIG: Bruk --workers 1 (ikke flere) fordi cache er in-memory per prosess.
IKKE bruk --preload — bakgrunnstråden må starte INNE i workeren, ikke i master.
"""

import copy
import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify

# ============================================
# KONFIGURASJON OG OPPSETT
# ============================================

load_dotenv()

# Logging — bruk logging-modulen i stedet for print()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dagsoversikt")

app = Flask(__name__)

# VenueOps API-legitimasjon (settes via miljøvariabler)
CLIENT_ID = os.environ.get("VENUEOPS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("VENUEOPS_CLIENT_SECRET", "")

# Sanity API (settes via miljøvariabler)
SANITY_PROJECT_ID = os.environ.get("SANITY_PROJECT_ID", "")
SANITY_DATASET = os.environ.get("SANITY_DATASET", "production")

# Oppdateringsintervall i sekunder (10 minutter)
UPDATE_INTERVAL = 600

# Norsk tidssone — CET (UTC+1) / CEST (UTC+2)
# Vi bruker fast UTC+1 her. For automatisk sommertid, se kommentar under.
# Hvis du vil ha automatisk sommertid, legg til 'pytz' eller 'zoneinfo' (Python 3.9+).
# Med zoneinfo: NORWAY_TZ = ZoneInfo("Europe/Oslo")
# Foreløpig bruker vi en enkel hjelpefunksjon.
def _norsk_naa():
    """Returner nåværende norsk tid (CET/CEST) som naive datetime.

    Bruker Pythons innebygde zoneinfo for korrekt sommertid-håndtering.
    Returnerer naive datetime (uten tzinfo) fordi resten av koden
    sammenligner med naive tider fra VenueOps.
    """
    try:
        from zoneinfo import ZoneInfo
        naa_utc = datetime.now(timezone.utc)
        naa_oslo = naa_utc.astimezone(ZoneInfo("Europe/Oslo"))
        # Returner som naive datetime (uten tzinfo) for enkel sammenligning
        return naa_oslo.replace(tzinfo=None)
    except ImportError:
        # Fallback: manuell UTC+1 (ingen sommertid)
        log.warning("zoneinfo ikke tilgjengelig, bruker fast UTC+1")
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


# ============================================
# GLOBAL CACHE — leses av API, skrives av bakgrunnstråd
# ============================================
_cache = {
    "token": None,
    "token_expires": None,
    "today": None,
    "today_updated": None,
    "tomorrow": None,
    "tomorrow_updated": None,
}
_cache_lock = threading.Lock()

# Beskyttelse mot samtidige refresh-kall
_refresh_in_progress = threading.Event()


# ============================================
# API-HJELPERE
# ============================================

def invalidate_token():
    """Tving ny token-henting ved neste kall."""
    with _cache_lock:
        _cache["token_expires"] = None
    log.info("Token invalidert — henter ny ved neste forsøk")


def get_access_token():
    """Hent access token fra VenueOps (med caching).

    Token caches i 50 minutter. Ved feil returneres gammel token
    hvis den fortsatt finnes.
    """
    with _cache_lock:
        if (_cache["token"]
                and _cache["token_expires"]
                and _norsk_naa() < _cache["token_expires"]):
            return _cache["token"]

    try:
        response = requests.post(
            "https://auth-api.eu-venueops.com/token",
            json={"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
            timeout=15,
        )
        response.raise_for_status()
        token = response.json().get("accessToken")

        if not token:
            log.error("Tomt token-svar fra VenueOps")
            with _cache_lock:
                return _cache.get("token")

        with _cache_lock:
            _cache["token"] = token
            _cache["token_expires"] = _norsk_naa() + timedelta(minutes=50)

        log.info("Ny access token hentet fra VenueOps")
        return token

    except requests.RequestException as e:
        log.error("Token-feil: %s", e)
        with _cache_lock:
            return _cache.get("token")  # Returner gammel token hvis vi har en


def normalize_title(title):
    """Normaliser tittel for sammenligning."""
    if not title:
        return ""
    normalized = title.replace(":", "")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def get_title_similarity(title1, title2):
    """Beregn likhet mellom to titler (0-100)."""
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)

    if not norm1 and not norm2:
        return 100.0
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 100.0

    return SequenceMatcher(None, norm1, norm2).ratio() * 100


def get_sanity_events(date):
    """Hent events fra Sanity for en gitt dato.

    Utvider tidsvinduet med +/- 2 timer for å fange tidssone-forskjeller.
    """
    start_of_day = datetime(date.year, date.month, date.day, 0, 0, 0) - timedelta(hours=2)
    end_of_day = datetime(date.year, date.month, date.day, 23, 59, 59) + timedelta(hours=2)

    start_str = start_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")

    groq = (
        f"*[_type == 'event' && defined(dates[0].eventStart) "
        f"&& dates[0].eventStart >= '{start_str}' "
        f"&& dates[0].eventStart <= '{end_str}'] "
        f"| order(dates[0].eventStart asc) {{"
        f"  'tittel': title.nb,"
        f"  'dato': dates[0].eventStart,"
        f"  'rom': venues[0].room->title,"
        f"  'eventId': _id,"
        f"  'slug': slug.current,"
        f"  'bildeUrl': image.asset->url,"
        f"  'ingress': description.nb,"
        f"  'arrangor': organizers[0]->title,"
        f"  'pris': admission.ticket.cost,"
        f"  'billettUrl': admission.ticket.url,"
        f"  'billettStatus': admission.status,"
        f"  'kategori': eventType->title,"
        f"  'varighet': dates[0].eventDuration"
        f"}}"
    )

    encoded_query = urllib.parse.quote(groq)
    url = (
        f"https://{SANITY_PROJECT_ID}.api.sanity.io/v2021-10-21"
        f"/data/query/{SANITY_DATASET}?query={encoded_query}&perspective=published"
    )

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        events = data.get("result", [])

        for e in events:
            log.debug(
                "  Sanity: '%s' - pris:%s billett:%s",
                e.get("tittel"), e.get("pris"), bool(e.get("billettUrl"))
            )

        return events

    except requests.RequestException as e:
        log.error("Sanity-feil: %s", e)
        return []


def match_sanity_event(event_name, event_time, sanity_events):
    """Finn beste match i Sanity basert på tittel og tid."""
    best_match = None
    best_score = 0

    for sanity in sanity_events:
        if not sanity.get("tittel"):
            continue

        # Beregn tittel-likhet
        title_sim = get_title_similarity(event_name, sanity["tittel"])

        # Tid-sjekk (innenfor 2 timer)
        try:
            sanity_time = datetime.fromisoformat(sanity["dato"].replace("Z", "+00:00"))
            # Konverter til norsk tid (fjern tzinfo for sammenligning)
            try:
                from zoneinfo import ZoneInfo
                sanity_time = sanity_time.astimezone(ZoneInfo("Europe/Oslo")).replace(tzinfo=None)
            except ImportError:
                sanity_time = sanity_time.replace(tzinfo=None) + timedelta(hours=1)

            time_diff = abs((event_time - sanity_time).total_seconds() / 60)

            if time_diff > 120:  # Mer enn 2 timer forskjell
                continue
        except (ValueError, TypeError):
            continue

        # Score: tittel-likhet er viktigst, tid som tiebreaker
        score = title_sim - (time_diff / 10)

        if score > best_score and title_sim >= 50:
            best_score = score
            best_match = sanity

    return best_match


def short_room(room):
    """Forkort romnavn."""
    if not room:
        return "—"
    room = room.replace("room-", "")
    room = re.sub(r"\s*\([^)]*\)", "", room)

    mappings = {
        "wergeland": "Werge",
        "skram": "Skram",
        "berner": "Berner",
        "nedjma": "Nedjma",
        "kverneland": "Kvern",
        "riverton": "River",
        "vestly": "Vestly",
    }

    return mappings.get(room.lower(), room[:10] if len(room) > 10 else room)


def parse_time(time_str):
    """Parse HH:MM-streng til datetime-objekt for i dag (norsk tid)."""
    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        naa = _norsk_naa()
        return naa.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
    except (ValueError, IndexError):
        return None


def is_active(start_time, end_time):
    """Sjekk om et tidspunkt er aktivt nå (norsk tid)."""
    naa = _norsk_naa()
    start = parse_time(start_time)
    end = parse_time(end_time)
    if start and end:
        return start <= naa <= end
    return False


def get_event_status(item, is_today=True):
    """Bestem status for arrangement basert på tid.

    Returnerer: 'active', 'rigg', 'getin', 'getout', 'finished', eller 'pending'.
    """
    if not is_today:
        return "pending"

    arr = item["arrangement"]
    rigg_info = item.get("rigg_info")
    getin_info = item.get("getin_info")
    getout_info = item.get("getout_info")

    # Get-out pågår (høy prioritet)
    if getout_info and is_active(getout_info["start"], getout_info["end"]):
        return "getout"

    # Arrangement pågår
    if is_active(arr["startTime"], arr["endTime"]):
        return "active"

    # Rigg pågår
    if rigg_info and is_active(rigg_info["start"], rigg_info["end"]):
        return "rigg"

    # Get-in pågår
    if getin_info and is_active(getin_info["start"], getin_info["end"]):
        return "getin"

    # Sjekk om helt ferdig (arrangement ferdig OG ingen aktiv get-out)
    arr_end = parse_time(arr["endTime"])
    getout_end = parse_time(getout_info["end"]) if getout_info else None

    naa = _norsk_naa()
    if arr_end and naa > arr_end:
        if getout_end:
            if naa > getout_end:
                return "finished"
            else:
                return "pending"  # Venter på get-out
        return "finished"

    return "pending"


def get_day_data(date, token):
    """Hent all data for en gitt dato fra VenueOps og Sanity.

    Denne funksjonen gjør flere API-kall og kan ta 5-15 sekunder.
    Kalles av bakgrunnstråden, ikke av request-handlers.
    """
    headers = {"Authorization": f"Bearer {token}"}
    date_str = date.strftime("%Y-%m-%d")

    # Sjekk om dette er dagens dato (norsk tid)
    is_today = date.date() == _norsk_naa().date()

    # Hent Sanity events for matching
    sanity_events = get_sanity_events(date)
    log.info("Sanity events funnet for %s: %d", date_str, len(sanity_events))

    # Hent alle funksjoner for dagen (med retry ved 401)
    response = requests.get(
        f"https://api.eu-venueops.com/v1/functions?startDate={date_str}&endDate={date_str}",
        headers=headers,
        timeout=15,
    )
    if response.status_code == 401:
        log.warning("VenueOps 401 — token utløpt, henter ny og prøver igjen")
        invalidate_token()
        new_token = get_access_token()
        if new_token:
            headers = {"Authorization": f"Bearer {new_token}"}
            response = requests.get(
                f"https://api.eu-venueops.com/v1/functions?startDate={date_str}&endDate={date_str}",
                headers=headers,
                timeout=15,
            )
    response.raise_for_status()
    functions = response.json()

    # Filtrer funksjonstyper (ekskluder Sjeherasad)
    arrangementer = [
        f for f in functions
        if f.get("functionTypeName") == "Arrangement"
        and f.get("functionStatus") != "canceled"
        and "sjeherasad" not in (f.get("roomName") or "").lower()
    ]
    rigg = [f for f in functions if f.get("functionTypeName") == "Rigg" and f.get("functionStatus") != "canceled"]
    getin = [f for f in functions if f.get("functionTypeName") == "Get-in" and f.get("functionStatus") != "canceled"]
    getout = [f for f in functions if f.get("functionTypeName") == "Get-out" and f.get("functionStatus") != "canceled"]
    verter = sorted(
        [f for f in functions if f.get("functionTypeName") == "Vert"],
        key=lambda x: x.get("startTime", ""),
    )
    teknikere = [
        f for f in functions
        if f.get("functionTypeName") == "Tekniker" and f.get("functionStatus") != "canceled"
    ]

    # Hent event-detaljer for arrangementer som mangler accountName
    event_cache = {}
    unique_event_ids = set(
        arr.get("eventId")
        for arr in arrangementer
        if arr.get("eventId") and not arr.get("accountName")
    )

    if unique_event_ids:
        log.info("Henter detaljer for %d events...", len(unique_event_ids))

        def fetch_event(event_id):
            try:
                resp = requests.get(
                    f"https://api.eu-venueops.com/v1/events/{event_id}",
                    headers=headers,
                    timeout=10,
                )
                resp.raise_for_status()
                return event_id, resp.json()
            except requests.RequestException as exc:
                log.warning("Kunne ikke hente event %s: %s", event_id, exc)
                return event_id, None

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_event, eid): eid for eid in unique_event_ids}
            for future in as_completed(futures):
                event_id, data = future.result()
                event_cache[event_id] = data

    # Bygg tekniker-mapping
    def get_technicians(event_name, room_id):
        techs = []
        for tek in teknikere:
            if tek.get("eventName") == event_name and tek.get("roomId") == room_id:
                if tek.get("staffAssignments"):
                    for staff in tek["staffAssignments"]:
                        name = staff.get("staffMemberName", "")
                        if name and "Trenger" not in name and "Trengs" not in name:
                            first_name = name.split()[0] if " " in name else name
                            full_name = name
                            if not any(t["fornavn"] == first_name for t in techs):
                                techs.append({
                                    "fornavn": first_name[:10],
                                    "navn": full_name,
                                    "bilde": f"/static/teknikere/{first_name.lower()}.jpg",
                                })
                elif tek.get("name"):
                    match = re.search(r"Tekniker\s+(.+)", tek["name"])
                    if match:
                        name = match.group(1).strip()
                        if "Trenger" not in name and "Trengs" not in name:
                            first_name = name.split()[0] if " " in name else name
                            if not any(t["fornavn"] == first_name for t in techs):
                                techs.append({
                                    "fornavn": first_name[:10],
                                    "navn": name,
                                    "bilde": f"/static/teknikere/{first_name.lower()}.jpg",
                                })
        return techs if techs else None

    # Bygg arrangement-liste med all info
    result = []
    for arr in arrangementer:
        event_name = arr.get("eventName")
        room_id = arr.get("roomId")

        # Finn tilhørende rigg — lagre ALLE riggetider
        rigg_funcs = [r for r in rigg if r.get("eventName") == event_name and r.get("roomId") == room_id]
        rigg_info = None
        rigg_tider = []
        if rigg_funcs:
            rigg_funcs_sorted = sorted(rigg_funcs, key=lambda x: x["startTime"])
            for rf in rigg_funcs_sorted:
                rigg_tider.append(f"{rf['startTime']}-{rf['endTime']}")

            starts = [r["startTime"] for r in rigg_funcs]
            ends = [r["endTime"] for r in rigg_funcs]
            rigg_info = {
                "start": min(starts),
                "end": max(ends),
                "count": len(rigg_funcs),
                "tider": rigg_tider,
            }

        # Finn tilhørende get-in
        getin_funcs = [g for g in getin if g.get("eventName") == event_name and g.get("roomId") == room_id]
        getin_info = None
        if getin_funcs:
            getin_info = {
                "start": min(g["startTime"] for g in getin_funcs),
                "end": max(g["endTime"] for g in getin_funcs),
            }

        # Finn tilhørende get-out
        getout_funcs = [g for g in getout if g.get("eventName") == event_name and g.get("roomId") == room_id]
        getout_info = None
        if getout_funcs:
            getout_info = {
                "start": min(g["startTime"] for g in getout_funcs),
                "end": max(g["endTime"] for g in getout_funcs),
            }

        # Hent arrangør — prøv fra arr først, så event_cache
        arrangor = arr.get("accountName")
        event_id = arr.get("eventId")
        if not arrangor and event_id and event_id in event_cache and event_cache[event_id]:
            arrangor = event_cache[event_id].get("accountName")

        # Parse arrangement-starttid for Sanity-matching
        try:
            arr_hour, arr_min = map(int, arr["startTime"].split(":"))
            arr_datetime = date.replace(hour=arr_hour, minute=arr_min, second=0, microsecond=0)
        except (ValueError, KeyError):
            arr_datetime = date

        # Match med Sanity
        sanity_match = match_sanity_event(event_name, arr_datetime, sanity_events)

        # Bygg Sanity-info
        sanity_info = None
        if sanity_match:
            log.debug(
                "  Match: '%s' -> '%s'",
                (event_name or "")[:30],
                (sanity_match.get("tittel") or "")[:30],
            )
            sanity_info = {
                "tittel": sanity_match.get("tittel"),
                "slug": sanity_match.get("slug"),
                "url": (
                    f"https://litteraturhuset.no/arrangement/{sanity_match.get('slug')}"
                    if sanity_match.get("slug") else None
                ),
                "bilde": sanity_match.get("bildeUrl"),
                "ingress": sanity_match.get("ingress"),
                "arrangor": sanity_match.get("arrangor"),
                "pris": sanity_match.get("pris"),
                "billettUrl": sanity_match.get("billettUrl"),
                "billettStatus": sanity_match.get("billettStatus"),
                "kategori": sanity_match.get("kategori"),
                "varighet": sanity_match.get("varighet"),
            }
        else:
            log.debug("  Ingen match: '%s'", (event_name or "")[:40])

        item = {
            "arrangement": arr,
            "rigg_info": rigg_info,
            "getin_info": getin_info,
            "getout_info": getout_info,
            "sort_time": rigg_info["start"] if rigg_info else arr["startTime"],
            "room": arr.get("roomName", ""),
            "room_full": arr.get("roomName", ""),
            "teknikere": get_technicians(event_name, room_id),
            "arrangor": arrangor,
            "event_id": event_id,
            "rigg_tider": rigg_tider if rigg_tider else None,
            "rigg_tid": f"{rigg_info['start']}-{rigg_info['end']}" if rigg_info else None,
            "arr_tid": f"{arr['startTime']}-{arr['endTime']}",
            "rigg_count": rigg_info["count"] if rigg_info else 0,
            "sanity": sanity_info,
        }
        item["status"] = get_event_status(item, is_today)
        result.append(item)

    # Sorter etter riggetid/arrangement-tid
    result.sort(key=lambda x: x["sort_time"])

    # Prosesser verter
    verter_processed = []
    for v in verter:
        name = v.get("name", "").strip()
        if name:
            parts = name.split()
            display_name = " ".join(parts[:4]) if parts else "(ukjent)"
            first_name = parts[0] if parts else "ukjent"
        else:
            display_name = "(ukjent)"
            first_name = "ukjent"

        verter_processed.append({
            "name": display_name,
            "fornavn": first_name,
            "bilde": f"/static/teknikere/{first_name.lower()}.jpg",
            "tid": f"{v.get('startTime', '?')}-{v.get('endTime', '?')}",
            "active": is_today and is_active(v.get("startTime"), v.get("endTime")),
        })

    data = {
        "arrangementer": result,
        "verter": verter_processed,
        "date": date_str,
        "date_display": date.strftime("%d.%m.%Y"),
        "weekday": [
            "mandag", "tirsdag", "onsdag", "torsdag",
            "fredag", "lørdag", "søndag",
        ][date.weekday()],
    }

    return data


# ============================================
# CACHE-OPPDATERING
# ============================================

def update_cache():
    """Oppdater cache med data for i dag og i morgen.

    Kalles av bakgrunnstråden hvert 10. minutt, og ved manuell refresh.
    Hele funksjonen er wrappet i try/except slik at bakgrunnstråden
    aldri dør uventet.
    """
    log.info("Oppdaterer cache...")

    try:
        token = get_access_token()
        if not token:
            log.error("Kunne ikke hente token — hopper over cache-oppdatering")
            return False

        # Hent data for i dag (norsk tid)
        today = _norsk_naa()
        today_data = get_day_data(today, token)

        with _cache_lock:
            _cache["today"] = today_data
            _cache["today_updated"] = _norsk_naa()
        log.info("I dag (%s): %d arrangementer", today.strftime("%d.%m"), len(today_data["arrangementer"]))

        # Hent data for i morgen
        tomorrow = _norsk_naa() + timedelta(days=1)
        tomorrow_data = get_day_data(tomorrow, token)

        with _cache_lock:
            _cache["tomorrow"] = tomorrow_data
            _cache["tomorrow_updated"] = _norsk_naa()
        log.info("I morgen (%s): %d arrangementer", tomorrow.strftime("%d.%m"), len(tomorrow_data["arrangementer"]))

        return True

    except Exception:
        log.exception("Feil under cache-oppdatering")
        return False


def _background_loop():
    """Bakgrunnstråd som oppdaterer cache med jevne mellomrom.

    Robust loop som ALDRI dør:
    - Fanger alle exceptions (inkludert uventede)
    - Ved feil: venter 60 sekunder, prøver igjen
    - Logger alt for feilsøking
    - Bruker kortere sleep-intervaller for raskere shutdown
    """
    log.info("Bakgrunnstråd startet (intervall: %d sek)", UPDATE_INTERVAL)

    consecutive_errors = 0

    while True:
        try:
            # Vent i UPDATE_INTERVAL sekunder, men sjekk hvert 10. sekund
            # slik at tråden kan avsluttes raskere ved shutdown
            for _ in range(UPDATE_INTERVAL // 10):
                time.sleep(10)

            success = update_cache()

            if success:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                log.warning(
                    "Cache-oppdatering feilet (%d påfølgende feil)",
                    consecutive_errors,
                )

        except Exception:
            consecutive_errors += 1
            log.exception(
                "Uventet feil i bakgrunnstråd (%d påfølgende feil)",
                consecutive_errors,
            )

            # Progressiv ventetid ved gjentatte feil (maks 5 min)
            wait_time = min(60 * consecutive_errors, 300)
            log.info("Venter %d sek før neste forsøk...", wait_time)
            time.sleep(wait_time)


# ============================================
# FLASK-RUTER
# ============================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data/<day>")
def get_data(day):
    """API-endepunkt — leser alltid fra cache (instant respons).

    Hvis cache ikke er klar (ved oppstart), henter data direkte.
    """
    if day not in ["today", "tomorrow"]:
        return jsonify({"error": "Invalid day"}), 400

    data = get_cached_data(day)

    if data is None:
        # Cache ikke klar ennå — hent data direkte (bare ved oppstart)
        log.warning("Cache tom for '%s' — henter direkte fra API", day)
        try:
            token = get_access_token()
            if token:
                if day == "today":
                    date = _norsk_naa()
                else:
                    date = _norsk_naa() + timedelta(days=1)
                data = get_day_data(date, token)
            else:
                return jsonify({"error": "Kunne ikke hente data — ingen token"}), 503
        except Exception:
            log.exception("Feil ved direkte datahenting for '%s'", day)
            return jsonify({"error": "Midlertidig feil — prøv igjen om litt"}), 503

    return jsonify(data)


def get_cached_data(day):
    """Hent data fra cache — oppdater status on-the-fly for i dag."""
    with _cache_lock:
        if day == "today":
            data = _cache.get("today")
            if data:
                # Lag en kopi og oppdater status (tidsbaserte statuser endres mellom cache-oppdateringer)
                data = copy.deepcopy(data)
                for item in data["arrangementer"]:
                    item["status"] = get_event_status(item, True)
                for v in data["verter"]:
                    tid_parts = v["tid"].split("-")
                    if len(tid_parts) == 2:
                        v["active"] = is_active(tid_parts[0], tid_parts[1])
            return data
        else:
            return _cache.get("tomorrow")


@app.route("/api/status")
def get_status():
    """Sjekk cache-status og helsetilstand."""
    with _cache_lock:
        today_updated = _cache["today_updated"]
        tomorrow_updated = _cache["tomorrow_updated"]

    return jsonify({
        "today_updated": today_updated.isoformat() if today_updated else None,
        "tomorrow_updated": tomorrow_updated.isoformat() if tomorrow_updated else None,
        "update_interval": UPDATE_INTERVAL,
        "server_time": _norsk_naa().isoformat(),
        "background_thread_alive": _updater_thread.is_alive() if _updater_thread else False,
    })


@app.route("/api/refresh", methods=["POST"])
def refresh_cache_endpoint():
    """Manuell oppdatering av cache.

    Beskyttet mot samtidige kall — hvis en oppdatering allerede pågår,
    returner umiddelbart med melding om det.
    """
    if _refresh_in_progress.is_set():
        log.info("Manuell refresh avvist — oppdatering pågår allerede")
        return jsonify({
            "status": "already_refreshing",
            "message": "En oppdatering pågår allerede",
        })

    log.info("Manuell refresh trigget")

    def do_refresh():
        _refresh_in_progress.set()
        try:
            update_cache()
        finally:
            _refresh_in_progress.clear()

    refresh_thread = threading.Thread(target=do_refresh, name="manual-refresh", daemon=True)
    refresh_thread.start()

    return jsonify({
        "status": "refreshing",
        "message": "Cache oppdateres i bakgrunnen",
    })


@app.route("/api/debug/sanity")
def debug_sanity():
    """Debug: Vis alle felt fra et Sanity event."""
    date = _norsk_naa()
    start_of_day = datetime(date.year, date.month, date.day, 0, 0, 0) - timedelta(hours=2)
    end_of_day = datetime(date.year, date.month, date.day, 23, 59, 59) + timedelta(hours=2)

    start_str = start_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")

    groq = (
        f"*[_type == 'event' && defined(dates[0].eventStart) "
        f"&& dates[0].eventStart >= '{start_str}' "
        f"&& dates[0].eventStart <= '{end_str}'][0]"
    )

    encoded_query = urllib.parse.quote(groq)
    url = (
        f"https://{SANITY_PROJECT_ID}.api.sanity.io/v2021-10-21"
        f"/data/query/{SANITY_DATASET}?query={encoded_query}&perspective=published"
    )

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return jsonify(data.get("result", {}))
    except requests.RequestException as e:
        log.error("Debug/sanity feil: %s", e)
        return jsonify({"error": str(e)})


# ============================================
# OPPSTART — Gunicorn post_fork hook eller direkte kjøring
# ============================================

_updater_thread = None
_updater_started = False


def start_background_updater():
    """Start bakgrunnstråd og kjør første cache-oppdatering.

    Sikker å kalle flere ganger — starter kun én gang.
    Denne funksjonen kalles ved modul-import, som betyr:
      - Med 'gunicorn app:app --workers 1': kjøres når workeren laster modulen
      - Med 'python app.py': kjøres ved oppstart
      - Med '--preload': kjøres i master FØR fork — tråden dør i fork!
        Derfor: IKKE bruk --preload med denne appen.
    """
    global _updater_started, _updater_thread

    if _updater_started:
        return
    _updater_started = True

    log.info("=" * 50)
    log.info("Litteraturhuset Dagsoversikt")
    log.info("Server-tid (norsk): %s", _norsk_naa().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Auto-oppdatering: hvert %d minutt", UPDATE_INTERVAL // 60)
    log.info("Manuell refresh: POST /api/refresh")
    log.info("=" * 50)

    # Første oppdatering — blokkerer til ferdig slik at cache er klar ved oppstart
    update_cache()

    # Start bakgrunnstråd for periodiske oppdateringer
    _updater_thread = threading.Thread(
        target=_background_loop,
        name="cache-updater",
        daemon=True,
    )
    _updater_thread.start()
    log.info("Bakgrunnstråd for cache-oppdatering startet (PID: %d)", os.getpid())


# Start ved modul-import (fungerer med Gunicorn uten --preload og Flask dev-server)
start_background_updater()


if __name__ == "__main__":
    # Kun for lokal utvikling
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
