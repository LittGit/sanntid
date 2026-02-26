"""
Litteraturhuset Dagsoversikt
Flask-backend med bakgrunns-caching for rask respons
"""

from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta
import requests
import os
import re
from difflib import SequenceMatcher
import threading
import time

app = Flask(__name__)

# VenueOps API credentials
CLIENT_ID = "7ufkl81437snu2kg7qmbtc09gf"
CLIENT_SECRET = "8bci9o6t3ahqelc6oibbfss4sulmi8tt1tsu6ldnu6b48q48g7r"

# Sanity API
SANITY_PROJECT_ID = "4bjarlop"
SANITY_DATASET = "production"

# ============================================
# GLOBAL CACHE - leses av API, skrives av bakgrunnstråd
# ============================================
_cache = {
    'token': None,
    'token_expires': None,
    'today': None,
    'today_updated': None,
    'tomorrow': None,
    'tomorrow_updated': None,
    'lock': threading.Lock()
}

# Oppdateringsintervall (sekunder) - 10 minutter
UPDATE_INTERVAL = 600

def get_access_token():
    """Hent access token fra VenueOps (med caching)"""
    with _cache['lock']:
        if _cache['token'] and _cache['token_expires'] and datetime.now() < _cache['token_expires']:
            return _cache['token']
    
    try:
        response = requests.post(
            'https://auth-api.eu-venueops.com/token',
            json={'clientId': CLIENT_ID, 'clientSecret': CLIENT_SECRET},
            timeout=10
        )
        token = response.json().get('accessToken')
        
        with _cache['lock']:
            _cache['token'] = token
            _cache['token_expires'] = datetime.now() + timedelta(minutes=50)
        
        return token
    except Exception as e:
        print(f"Token error: {e}")
        return _cache.get('token')  # Returner gammel token hvis vi har en

def normalize_title(title):
    """Normaliser tittel for sammenligning"""
    if not title:
        return ""
    normalized = title.replace(':', '')
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip().lower()

def get_title_similarity(title1, title2):
    """Beregn likhet mellom to titler (0-100)"""
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
    """Hent events fra Sanity for en gitt dato"""
    # Start og slutt for dagen (UTC) - utvid litt for å fange opp tidssone-forskjeller
    start_of_day = datetime(date.year, date.month, date.day, 0, 0, 0) - timedelta(hours=2)
    end_of_day = datetime(date.year, date.month, date.day, 23, 59, 59) + timedelta(hours=2)
    
    start_str = start_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Oppdatert query med riktige feltnavn
    groq = f"""*[_type == 'event' && defined(dates[0].eventStart) && dates[0].eventStart >= '{start_str}' && dates[0].eventStart <= '{end_str}'] | order(dates[0].eventStart asc) {{
      'tittel': title.nb,
      'dato': dates[0].eventStart,
      'rom': venues[0].room->title,
      'eventId': _id,
      'slug': slug.current,
      'bildeUrl': image.asset->url,
      'ingress': description.nb,
      'arrangor': organizers[0]->title,
      'pris': admission.ticket.cost,
      'billettUrl': admission.ticket.url,
      'billettStatus': admission.status,
      'kategori': eventType->title,
      'varighet': dates[0].eventDuration
    }}"""
    
    import urllib.parse
    encoded_query = urllib.parse.quote(groq)
    url = f"https://{SANITY_PROJECT_ID}.api.sanity.io/v2021-10-21/data/query/{SANITY_DATASET}?query={encoded_query}&perspective=published"
    
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        events = data.get('result', [])
        
        # Debug: vis hva vi fikk
        for e in events:
            print(f"  Sanity: '{e.get('tittel')}' - pris:{e.get('pris')} billett:{bool(e.get('billettUrl'))}")
        
        return events
    except Exception as e:
        print(f"Sanity error: {e}")
        return []

def match_sanity_event(event_name, event_time, sanity_events):
    """Finn beste match i Sanity basert på tittel og tid"""
    best_match = None
    best_score = 0
    
    for sanity in sanity_events:
        if not sanity.get('tittel'):
            continue
            
        # Beregn tittel-likhet
        title_sim = get_title_similarity(event_name, sanity['tittel'])
        
        # Tid-sjekk (innenfor 2 timer)
        try:
            sanity_time = datetime.fromisoformat(sanity['dato'].replace('Z', '+00:00'))
            # Konverter til lokal tid (legg til 1-2 timer for norsk tid)
            sanity_time = sanity_time.replace(tzinfo=None) + timedelta(hours=1)
            
            time_diff = abs((event_time - sanity_time).total_seconds() / 60)
            
            if time_diff > 120:  # Mer enn 2 timer forskjell
                continue
        except:
            continue
        
        # Score: tittel-likhet er viktigst, tid som tiebreaker
        score = title_sim - (time_diff / 10)  # Trekk fra litt for tidsavvik
        
        if score > best_score and title_sim >= 50:  # Minimum 50% tittel-likhet
            best_score = score
            best_match = sanity
    
    return best_match

def short_room(room):
    """Forkort romnavn"""
    if not room:
        return "—"
    room = room.replace("room-", "")
    # Fjern parenteser og innhold
    import re
    room = re.sub(r'\s*\([^)]*\)', '', room)
    
    mappings = {
        'wergeland': 'Werge',
        'skram': 'Skram',
        'berner': 'Berner',
        'nedjma': 'Nedjma',
        'kverneland': 'Kvern',
        'riverton': 'River',
        'vestly': 'Vestly'
    }
    
    return mappings.get(room.lower(), room[:10] if len(room) > 10 else room)

def parse_time(time_str):
    """Parse HH:MM string til datetime objekt for i dag"""
    if not time_str:
        return None
    parts = time_str.split(':')
    now = datetime.now()
    return now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)

def is_active(start_time, end_time):
    """Sjekk om tidspunkt er aktivt nå"""
    now = datetime.now()
    start = parse_time(start_time)
    end = parse_time(end_time)
    if start and end:
        return start <= now <= end
    return False

def get_event_status(item, is_today=True):
    """Bestem status for arrangement basert på tid"""
    # Hvis ikke i dag, vis alt som pending (ingen aktive statuser)
    if not is_today:
        return 'pending'
    
    arr = item['arrangement']
    rigg_info = item.get('rigg_info')
    getin_info = item.get('getin_info')
    getout_info = item.get('getout_info')
    
    # Get-out pågår (høy prioritet - vis dette selv om arr er "ferdig")
    if getout_info and is_active(getout_info['start'], getout_info['end']):
        return 'getout'
    
    # Arrangement pågår
    if is_active(arr['startTime'], arr['endTime']):
        return 'active'
    
    # Rigg pågår
    if rigg_info and is_active(rigg_info['start'], rigg_info['end']):
        return 'rigg'
    
    # Get-in pågår
    if getin_info and is_active(getin_info['start'], getin_info['end']):
        return 'getin'
    
    # Sjekk om helt ferdig (arr ferdig OG ingen aktiv get-out)
    arr_end = parse_time(arr['endTime'])
    getout_end = parse_time(getout_info['end']) if getout_info else None
    
    if arr_end and datetime.now() > arr_end:
        # Hvis get-out finnes, sjekk om den også er ferdig
        if getout_end:
            if datetime.now() > getout_end:
                return 'finished'
            else:
                return 'pending'  # Venter på get-out
        return 'finished'
    
    return 'pending'

def get_day_data(date, token):
    """Hent all data for en gitt dato (intern funksjon, kalles av bakgrunnstråd)"""
    headers = {'Authorization': f'Bearer {token}'}
    date_str = date.strftime('%Y-%m-%d')
    
    # Sjekk om dette er dagens dato
    is_today = date.date() == datetime.now().date()
    
    # Hent Sanity events for matching
    sanity_events = get_sanity_events(date)
    print(f"Sanity events funnet: {len(sanity_events)}")
    
    # Hent alle funksjoner for dagen
    response = requests.get(
        f'https://api.eu-venueops.com/v1/functions?startDate={date_str}&endDate={date_str}',
        headers=headers,
        timeout=10
    )
    functions = response.json()
    
    # Filtrer funksjonstyper (ekskluder Sjeherasad)
    arrangementer = [f for f in functions if f.get('functionTypeName') == 'Arrangement' and f.get('functionStatus') != 'canceled' and 'sjeherasad' not in (f.get('roomName') or '').lower()]
    rigg = [f for f in functions if f.get('functionTypeName') == 'Rigg' and f.get('functionStatus') != 'canceled']
    getin = [f for f in functions if f.get('functionTypeName') == 'Get-in' and f.get('functionStatus') != 'canceled']
    getout = [f for f in functions if f.get('functionTypeName') == 'Get-out' and f.get('functionStatus') != 'canceled']
    verter = sorted([f for f in functions if f.get('functionTypeName') == 'Vert'], key=lambda x: x.get('startTime', ''))
    teknikere = [f for f in functions if f.get('functionTypeName') == 'Tekniker' and f.get('functionStatus') != 'canceled']
    
    # Bruk accountName direkte fra functions hvis tilgjengelig (raskere!)
    # Hvis ikke, hent event-detaljer (men bare for unike eventIds)
    event_cache = {}
    unique_event_ids = set(arr.get('eventId') for arr in arrangementer if arr.get('eventId') and not arr.get('accountName'))
    
    # Hent bare events som mangler accountName
    if unique_event_ids:
        print(f"Henter detaljer for {len(unique_event_ids)} events...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def fetch_event(event_id):
            try:
                resp = requests.get(
                    f'https://api.eu-venueops.com/v1/events/{event_id}',
                    headers=headers,
                    timeout=5
                )
                return event_id, resp.json()
            except:
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
            if tek.get('eventName') == event_name and tek.get('roomId') == room_id:
                # Prøv staffAssignments først
                if tek.get('staffAssignments'):
                    for staff in tek['staffAssignments']:
                        name = staff.get('staffMemberName', '')
                        if name and 'Trenger' not in name and 'Trengs' not in name:
                            first_name = name.split()[0] if ' ' in name else name
                            full_name = name
                            # Sjekk om vi allerede har denne teknikeren
                            if not any(t['fornavn'] == first_name for t in techs):
                                techs.append({
                                    'fornavn': first_name[:10],
                                    'navn': full_name,
                                    'bilde': f"/static/teknikere/{first_name.lower()}.jpg"
                                })
                # Fallback til navn-parsing
                elif tek.get('name'):
                    match = re.search(r'Tekniker\s+(.+)', tek['name'])
                    if match:
                        name = match.group(1).strip()
                        if 'Trenger' not in name and 'Trengs' not in name:
                            first_name = name.split()[0] if ' ' in name else name
                            if not any(t['fornavn'] == first_name for t in techs):
                                techs.append({
                                    'fornavn': first_name[:10],
                                    'navn': name,
                                    'bilde': f"/static/teknikere/{first_name.lower()}.jpg"
                                })
        return techs if techs else None
    
    # Bygg arrangement-liste med all info
    result = []
    for arr in arrangementer:
        event_name = arr.get('eventName')
        room_id = arr.get('roomId')
        
        # Finn tilhørende rigg - lagre ALLE riggetider
        rigg_funcs = [r for r in rigg if r.get('eventName') == event_name and r.get('roomId') == room_id]
        rigg_info = None
        rigg_tider = []  # Liste med alle individuelle riggetider
        if rigg_funcs:
            # Sorter etter starttid
            rigg_funcs_sorted = sorted(rigg_funcs, key=lambda x: x['startTime'])
            for rf in rigg_funcs_sorted:
                rigg_tider.append(f"{rf['startTime']}-{rf['endTime']}")
            
            starts = [r['startTime'] for r in rigg_funcs]
            ends = [r['endTime'] for r in rigg_funcs]
            rigg_info = {
                'start': min(starts),
                'end': max(ends),
                'count': len(rigg_funcs),
                'tider': rigg_tider  # Alle individuelle tider
            }
        
        # Finn tilhørende get-in
        getin_funcs = [g for g in getin if g.get('eventName') == event_name and g.get('roomId') == room_id]
        getin_info = None
        if getin_funcs:
            getin_info = {
                'start': min(g['startTime'] for g in getin_funcs),
                'end': max(g['endTime'] for g in getin_funcs)
            }
        
        # Finn tilhørende get-out
        getout_funcs = [g for g in getout if g.get('eventName') == event_name and g.get('roomId') == room_id]
        getout_info = None
        if getout_funcs:
            getout_info = {
                'start': min(g['startTime'] for g in getout_funcs),
                'end': max(g['endTime'] for g in getout_funcs)
            }
        
        # Hent arrangør - prøv fra arr først, så event_cache
        arrangor = arr.get('accountName')
        event_id = arr.get('eventId')
        if not arrangor and event_id and event_id in event_cache and event_cache[event_id]:
            arrangor = event_cache[event_id].get('accountName')
        
        # Parse arrangement starttid for Sanity-matching
        try:
            arr_hour, arr_min = map(int, arr['startTime'].split(':'))
            arr_datetime = date.replace(hour=arr_hour, minute=arr_min, second=0, microsecond=0)
        except:
            arr_datetime = date
        
        # Match med Sanity
        sanity_match = match_sanity_event(event_name, arr_datetime, sanity_events)
        
        # Bygg Sanity-info
        sanity_info = None
        if sanity_match:
            print(f"  ✓ Match: '{event_name[:30]}' -> '{sanity_match.get('tittel', '')[:30]}' (pris:{sanity_match.get('pris')} billett:{bool(sanity_match.get('billettUrl'))})")
            
            sanity_info = {
                'tittel': sanity_match.get('tittel'),
                'slug': sanity_match.get('slug'),
                'url': f"https://litteraturhuset.no/arrangement/{sanity_match.get('slug')}" if sanity_match.get('slug') else None,
                'bilde': sanity_match.get('bildeUrl'),
                'ingress': sanity_match.get('ingress'),
                'arrangor': sanity_match.get('arrangor'),
                'pris': sanity_match.get('pris'),
                'billettUrl': sanity_match.get('billettUrl'),
                'billettStatus': sanity_match.get('billettStatus'),
                'kategori': sanity_match.get('kategori'),
                'varighet': sanity_match.get('varighet')
            }
        else:
            print(f"  ✗ Ingen match: '{event_name[:40]}'")
        
        item = {
            'arrangement': arr,
            'rigg_info': rigg_info,
            'getin_info': getin_info,
            'getout_info': getout_info,
            'sort_time': rigg_info['start'] if rigg_info else arr['startTime'],
            'room': arr.get('roomName', ''),
            'room_full': arr.get('roomName', ''),
            'teknikere': get_technicians(event_name, room_id),
            'arrangor': arrangor,
            'event_id': event_id,
            'rigg_tider': rigg_tider if rigg_tider else None,  # Alle individuelle riggetider
            'rigg_tid': f"{rigg_info['start']}-{rigg_info['end']}" if rigg_info else None,
            'arr_tid': f"{arr['startTime']}-{arr['endTime']}",
            'rigg_count': rigg_info['count'] if rigg_info else 0,
            'sanity': sanity_info  # Sanity-match info
        }
        item['status'] = get_event_status(item, is_today)
        result.append(item)
    
    # Sorter etter riggetid/arrangement-tid
    result.sort(key=lambda x: x['sort_time'])
    
    # Prosesser verter
    verter_processed = []
    for v in verter:
        name = v.get('name', '').strip()
        if name:
            # Ta med opptil 4 ord fra navnet
            parts = name.split()
            display_name = ' '.join(parts[:4]) if parts else '(ukjent)'
            # Hent fornavn for bilde-matching
            first_name = parts[0] if parts else 'ukjent'
        else:
            display_name = '(ukjent)'
            first_name = 'ukjent'
        
        verter_processed.append({
            'name': display_name,
            'fornavn': first_name,
            'bilde': f"/static/teknikere/{first_name.lower()}.jpg",
            'tid': f"{v.get('startTime', '?')}-{v.get('endTime', '?')}",
            'active': is_today and is_active(v.get('startTime'), v.get('endTime'))
        })
    
    data = {
        'arrangementer': result,
        'verter': verter_processed,
        'date': date_str,
        'date_display': date.strftime('%d.%m.%Y'),
        'weekday': ['mandag', 'tirsdag', 'onsdag', 'torsdag', 'fredag', 'lørdag', 'søndag'][date.weekday()]
    }
    
    return data

# ============================================
# BAKGRUNNS-OPPDATERING
# ============================================
def update_cache():
    """Oppdater cache med data for i dag og i morgen"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Oppdaterer cache...")
    
    try:
        token = get_access_token()
        if not token:
            print("  Kunne ikke hente token!")
            return
        
        # Hent data for i dag
        today = datetime.now()
        today_data = get_day_data(today, token)
        
        with _cache['lock']:
            _cache['today'] = today_data
            _cache['today_updated'] = datetime.now()
        print(f"  ✓ I dag: {len(today_data['arrangementer'])} arr")
        
        # Hent data for i morgen
        tomorrow = datetime.now() + timedelta(days=1)
        tomorrow_data = get_day_data(tomorrow, token)
        
        with _cache['lock']:
            _cache['tomorrow'] = tomorrow_data
            _cache['tomorrow_updated'] = datetime.now()
        print(f"  ✓ I morgen: {len(tomorrow_data['arrangementer'])} arr")
        
    except Exception as e:
        print(f"  Cache update error: {e}")

def background_updater():
    """Bakgrunnstråd som oppdaterer cache hvert 10. minutt"""
    while True:
        time.sleep(UPDATE_INTERVAL)
        update_cache()

def get_cached_data(day):
    """Hent data fra cache - oppdater status on-the-fly for i dag"""
    with _cache['lock']:
        if day == 'today':
            data = _cache.get('today')
            if data:
                # Lag en kopi og oppdater status
                import copy
                data = copy.deepcopy(data)
                for item in data['arrangementer']:
                    item['status'] = get_event_status(item, True)
                for v in data['verter']:
                    tid_parts = v['tid'].split('-')
                    if len(tid_parts) == 2:
                        v['active'] = is_active(tid_parts[0], tid_parts[1])
            return data
        else:
            return _cache.get('tomorrow')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data/<day>')
def get_data(day):
    """API endpoint - leser alltid fra cache (instant respons)"""
    if day not in ['today', 'tomorrow']:
        return jsonify({'error': 'Invalid day'}), 400
    
    data = get_cached_data(day)
    
    if data is None:
        # Cache ikke klar ennå - hent data direkte (bare ved oppstart)
        token = get_access_token()
        if day == 'today':
            date = datetime.now()
        else:
            date = datetime.now() + timedelta(days=1)
        data = get_day_data(date, token)
    
    return jsonify(data)

@app.route('/api/status')
def get_status():
    """Sjekk cache-status"""
    with _cache['lock']:
        return jsonify({
            'today_updated': _cache['today_updated'].isoformat() if _cache['today_updated'] else None,
            'tomorrow_updated': _cache['tomorrow_updated'].isoformat() if _cache['tomorrow_updated'] else None,
            'update_interval': UPDATE_INTERVAL
        })

@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    """Manuell oppdatering av cache"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Manuell refresh trigget!")
    
    # Kjør oppdatering i egen tråd så vi ikke blokkerer
    def do_refresh():
        update_cache()
    
    refresh_thread = threading.Thread(target=do_refresh)
    refresh_thread.start()
    
    return jsonify({
        'status': 'refreshing',
        'message': 'Cache oppdateres i bakgrunnen'
    })

@app.route('/api/debug/sanity')
def debug_sanity():
    """Debug: Vis alle felt fra et Sanity event"""
    date = datetime.now()
    start_of_day = datetime(date.year, date.month, date.day, 0, 0, 0) - timedelta(hours=2)
    end_of_day = datetime(date.year, date.month, date.day, 23, 59, 59) + timedelta(hours=2)
    
    start_str = start_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Hent ALLE felt for å se hva som finnes
    groq = f"""*[_type == 'event' && defined(dates[0].eventStart) && dates[0].eventStart >= '{start_str}' && dates[0].eventStart <= '{end_str}'][0]"""
    
    import urllib.parse
    encoded_query = urllib.parse.quote(groq)
    url = f"https://{SANITY_PROJECT_ID}.api.sanity.io/v2021-10-21/data/query/{SANITY_DATASET}?query={encoded_query}&perspective=published"
    
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        return jsonify(data.get('result', {}))
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    # Start bakgrunns-oppdatering
    print("=" * 50)
    print("Litteraturhuset Dagsoversikt")
    print(f"Auto-oppdatering: hvert {UPDATE_INTERVAL // 60} minutt")
    print("Manuell refresh: POST /api/refresh")
    print("=" * 50)
    
    # Første oppdatering - synkront for å ha data klar
    update_cache()
    
    # Start bakgrunnstråd
    updater_thread = threading.Thread(target=background_updater, daemon=True)
    updater_thread.start()
    
    # Start Flask
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
