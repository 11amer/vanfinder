#!/usr/bin/env python3
"""Rebuild BrightDrop-Van-Finder.html (single self-contained offline file) from the CSVs.

Usage:
  python3 build_app.py                      # listings.csv (+ listings_400.csv) + close matches -> BrightDrop-Van-Finder.html
  python3 build_app.py --latest-snapshot    # use newest snapshots/YYYY-MM-DD_listings.csv as the main list
  python3 build_app.py --listings X.csv --close Y.csv --out Z.html --updated "Oct 5, 2026"
  python3 build_app.py --no-publish         # build the local plaintext file only, leave the public site alone

Publishing: every build (unless --no-publish) also writes a password-protected copy of the app to
/workspace/vanfinder-site/index.html (the publicly tunneled site). The copy is encrypted client-side
with StatiCrypt (AES-256 + PBKDF2-SHA256, 600k iterations; node_modules/.bin/staticrypt) using the
password in .site_password (chmod 600, never printed). The salt lives in .staticrypt.json and must stay
stable, otherwise every visitor's "Remember me" is invalidated. The encrypted file is round-trip
decrypted and checked for leaked plaintext before it atomically replaces the live file; if anything
fails, the previous live file is kept, plaintext is never published, and the build exits with status 3.

No network access. Building needs only the Python 3 standard library; publishing needs Node + the local
staticrypt package (cd /workspace/brightdrop600 && npm ci).
Recall info comes from the CSV recall columns (added by recalls/add_recalls.py) plus campaign details in
recalls/recalls_nhtsa.json (made by recalls/fetch_recalls.py). CSVs without the columns (e.g. daily
snapshots) get recall info by model year from that JSON.
The user's notes/status/quotes/favorites live in the browser's localStorage keyed by VIN AND are synced to the
server (GET/POST /api/notes -> user_state.json, merged per VIN by last-modified time), so they survive URL/origin
changes. API_BASE is the absolute tunnel URL used when the app is opened from another origin (e.g. GitHub Pages).

Discovery provenance (row field dsc) is only copied from an explicit inventory column
(Discovery source, Original source, Found on, or Source). It is never inferred from the
listing URL. Rows with no such evidence are Unknown. The current listing-link host is stored
separately as lhs and labeled "Listing site" in the app. Same VIN at a different seller is a
different listing and is kept. Login/password protection is unchanged.

Per-VIN recall checks: confirmed NHTSA results in recalls/vin_checks/results.csv override the model-year status
(badge, filters); links pasted in the app's "Recall check" box (recalls/vin_checks/pending_links.csv, written by
server.py) show as "Recall check queued". The API token in .api_token is embedded in the app so it only ever leaves
the box inside the encrypted payload (the plaintext output file is chmod 600). Builds are serialized with .build.lock.
"""
import argparse, base64, csv, datetime, glob, gzip, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = '/workspace/vanfinder-site'                    # served by http.server :8765 + cloudflared tunnel
SITE_INDEX = os.path.join(SITE_DIR, 'index.html')
PASSWORD_FILE = os.path.join(HERE, '.site_password')
STATICRYPT = os.path.join(HERE, 'node_modules', '.bin', 'staticrypt')
STATICRYPT_CONFIG = os.path.join(HERE, '.staticrypt.json')  # holds the (non-secret) salt; keep it stable
STATICRYPT_TEMPLATE = os.path.join(HERE, 'staticrypt_template.html')
SITE_TITLE = 'BrightDrop Van Finder'
REMEMBER_DAYS = 365
sys.path.insert(0, HERE)
import recall_data
from vf_common import distance_from_home
from vf_battery import battery_type as _bat_type, drivetrain as _bd_drive   # Oct 12 2026: Battery filter
# Truck Scout categories (besides the BrightDrop 600 list): Toyota Tundra + step vans + transit vans, made by tundra/build_tundra.py,
# stepvans/build_stepvans.py, and transitvans/build_transitvans.py. They ship gzip+base64-compressed inside the encrypted payload ("x") to keep the page small.
NETWORKS_DIR = os.path.join(HERE, 'networks')   # dealer networks (import_network.py -> networks/<id>/inventory.json)
TUNDRA_CSV = os.path.join(HERE, 'tundra', 'tundra_listings.csv')
STEPVAN_CSV = os.path.join(HERE, 'stepvans', 'stepvans_listings.csv')
STEPVAN_CLOSE_CSV = os.path.join(HERE, 'stepvans', 'stepvans_close_matches.csv')
TRANSITVAN_CSV = os.path.join(HERE, 'transitvans', 'transitvans_listings.csv')
TRANSITVAN_CLOSE_CSV = os.path.join(HERE, 'transitvans', 'transitvans_close_matches.csv')
# Ford E-Transit with an aftermarket AWD/4x4 conversion (etransit/build_etransit.py). Ford builds the E-Transit RWD only,
# so this tab is often empty; etransit_meta.json carries the note shown in the empty tab.
ETRANSIT_CSV = os.path.join(HERE, 'etransit', 'etransit_listings.csv')
ETRANSIT_CLOSE_CSV = os.path.join(HERE, 'etransit', 'etransit_close_matches.csv')
ETRANSIT_META = os.path.join(HERE, 'etransit', 'etransit_meta.json')
# Rivian commercial vans (Rivian 500 / 700, ex-EDV / RCV), any price, new + used (rivian/build_rivian.py). Rivian rows that
# also sit in the step-van CSV are shown only under this tab (like 400 VINs only under the 400 tab).
RIVIAN_CSV = os.path.join(HERE, 'rivian', 'rivian_listings.csv')
RIVIAN_CLOSE_CSV = os.path.join(HERE, 'rivian', 'rivian_close_matches.csv')
# BrightDrop / Zevo AUCTION lots (daily_check_auctions.py). Kept apart from the dealer lists; closed lots stay as close matches.
AUCTION_CSV = os.path.join(HERE, 'listings_auctions.csv')
AUCTION_CLOSE_CSV = os.path.join(HERE, 'listings_auctions_close_matches.csv')
BD400_CSV = os.path.join(HERE, 'listings_400.csv')
BD400_CLOSE_CSV = os.path.join(HERE, 'listings_400_close_matches.csv')
BD400_NEW_CSV = os.path.join(HERE, 'listings_400_new.csv')   # brand-new (unused) dealer stock, shown as its own group
KEY_ALPHA = 'ABCDEFGHJKLMNPRSTUVWXYZ0123456789'   # VIN alphabet (no I, O, Q)
try:
    RDB = recall_data.load_db()
except (OSError, ValueError) as e:
    print(f'warning: no recall data ({e}); building without recalls', file=sys.stderr)
    RDB = None
RFIELDS = ('rc', 'rs', 'rk', 'dnd', 'rsu', 'vc', 'pl')  # recall fields (kept out of the data-version stamp)
API_TOKEN_FILE = os.path.join(HERE, '.api_token')   # made by server.py/start_server.sh; embedded only in the encrypted app
API_ENDPOINT = '/api/recall'
NOTES_ENDPOINT = '/api/notes'
API_BASE = os.environ.get('VF_API_BASE', 'https://gary-parliamentary-relates-binary.trycloudflare.com')  # update if the tunnel URL changes
BUILD_LOCK = os.path.join(HERE, '.build.lock')     # serializes builds (server.py rebuilds, apply_result.py, manual runs)
VINRES = recall_data.load_vin_results()             # confirmed per-VIN NHTSA results (recalls/vin_checks/results.csv)
PENDING = {}                                        # VIN -> recall links pasted in the app (pending_links.csv)
for _p in recall_data.load_pending():
    PENDING.setdefault(_p['vin'].upper(), []).append(_p)


def vin_check(vin):
    """Per-VIN NHTSA result for the app: o=open count, c=campaign text, k=campaign ids, d=date, s=source, u=link."""
    x = VINRES.get(vin)
    if not x:
        return None
    ids = [i for i in recall_data.campaign_ids(x['campaigns']) if x['open']]
    return {'o': x['open'], 'c': x['campaigns'] if x['open'] else '', 'k': ids, 'd': x['checked'], 's': x['source'],
            'u': x['url'], 'dnd': 1 if any(RDB and i in RDB['campaigns'] and (RDB['campaigns'][i].get('parkIt') or
                                                                       RDB['campaigns'][i].get('parkOutSide')) for i in ids) else 0}


def pending_links(vin):
    """Last few pasted NHTSA links for a VIN: u=url, ts=time ET, st=pending|done|dismissed."""
    return [{'u': p['url'], 'ts': p['timestamp_et'], 'st': p['status'] or 'pending'} for p in PENDING.get(vin, [])[-5:]]


def recall_fields(r, vin):
    """Recall info for one row: from the CSV recall columns when present, else derived by model year."""
    if RDB is None:
        return {}
    st = (r.get('Recall status') or '').strip()
    if (r.get('Recall count') or '').strip() != '' and not st.startswith(('Clear', 'Open')):  # per-VIN rows: model-year info below
        ids = [m.replace('-', '') + '000' for m in re.findall(r'\b(\d\dV-\d{3})\b', r.get('Recall campaigns') or '')]
        vals = {k: (r.get(k) or '').strip() for k in recall_data.COLS}
    else:
        cs, _ = recall_data.recalls_for(RDB, r.get('Year'), r.get('Model'), vin)
        ids = [c['id'] for c in cs]
        vals = recall_data.columns(RDB, r.get('Year'), r.get('Model'), vin)
    return {'rc': to_int(vals['Recall count']) or 0, 'rs': vals['Recall status'] or 'unknown',
            'rk': [i for i in ids if i in RDB['campaigns']], 'dnd': 1 if vals['Do-not-drive warning'].lower() == 'yes' else 0,
            'rsu': vals['Recall source URL']}
STATES = {'AL':'Alabama','AK':'Alaska','AZ':'Arizona','AR':'Arkansas','CA':'California','CO':'Colorado','CT':'Connecticut',
 'DE':'Delaware','DC':'District of Columbia','FL':'Florida','GA':'Georgia','HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana',
 'IA':'Iowa','KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland','MA':'Massachusetts','MI':'Michigan',
 'MN':'Minnesota','MS':'Mississippi','MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire',
 'NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina','ND':'North Dakota','OH':'Ohio','OK':'Oklahoma',
 'OR':'Oregon','PA':'Pennsylvania','RI':'Rhode Island','SC':'South Carolina','SD':'South Dakota','TN':'Tennessee','TX':'Texas',
 'UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington','WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming'}
# Explicit columns only. Order matters: a dedicated discovery column beats the older Source column.
# Listing URL / Dealer page URL / Verification notes are intentionally not here.
_DISCOVERY_FIELDS = ('Discovery source', 'Original source', 'Found on', 'Source')
_UNKNOWN = 'Unknown'
_STATE_BLANK = {'', 'unknown', 'n/a', 'na', 'none', '-', '—', 'tbd', 'null', 'n.a.', 'not listed', 'not recorded'}
_STATE_BY_NAME = {name.casefold(): code for code, name in STATES.items()}
_DC_ALIASES = {'dc', 'd.c.', 'd.c', 'district of columbia', 'washington dc', 'washington d.c.', 'washington, dc', 'washington, d.c.'}


def _collapse(s):
    return re.sub(r'\s+', ' ', (s or '').replace('\u00a0', ' ')).strip()


def normalize_seller(s):
    """Seller as recorded, with whitespace collapsed. Blank becomes Unknown (not a guessed dealer)."""
    s = _collapse(s)
    return s if s else _UNKNOWN


def normalize_discovery(s):
    s = _collapse(s)
    if not s or s.casefold() in _STATE_BLANK:
        return _UNKNOWN
    return s


def discovery_from_row(row):
    """Where the listing was originally found, or Unknown.

    Only an explicit column counts as evidence. The current listing URL is never read here,
    so a CarStory/Cars.com/dealer link cannot become the discovery source.
    """
    if not row:
        return _UNKNOWN
    for key in _DISCOVERY_FIELDS:
        if key in row and _collapse(row.get(key)):
            return normalize_discovery(row.get(key))
    return _UNKNOWN


def listing_site(url):
    """Hostname of the current listing link. This is not a discovery source."""
    u = _collapse(url)
    if not u:
        return _UNKNOWN
    if '://' not in u and not u.startswith('//'):
        u = '//' + u
    try:
        host = (urlparse(u).hostname or '').lower().strip('.')
    except ValueError:
        return _UNKNOWN
    if host.startswith('www.'):
        host = host[4:]
    # A real website host only. Garbage, blank, and non-URLs stay Unknown.
    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host or ''):
        return _UNKNOWN
    return host


def normalize_state(raw):
    """Return (postal code or '', display name or 'Unknown').

    Accepts USPS codes and full state names already in the source data.
    Anything else, including a blank, stays Unknown — no guessed state.
    """
    s = _collapse(raw).strip(' .')
    s = re.sub(r'\s+\d{5}(?:-\d{4})?$', '', s).strip(' .')
    if not s or s.casefold() in _STATE_BLANK:
        return '', _UNKNOWN
    if s.casefold() in _DC_ALIASES:
        return 'DC', STATES['DC']
    letters = re.sub(r'[^A-Za-z]', '', s)
    if len(letters) == 2 and letters.upper() in STATES:
        code = letters.upper()
        return code, STATES[code]
    name = s.casefold()
    if name in _STATE_BY_NAME:
        code = _STATE_BY_NAME[name]
        return code, STATES[code]
    return '', _UNKNOWN


def split_city_state(loc):
    """Split a 'City, ST' cell. A bare 2-letter code is a state; a bare city is not a state."""
    loc = _collapse(loc)
    if not loc:
        return '', ''
    if ',' in loc:
        city, _, st = loc.rpartition(',')
        return _collapse(city), _collapse(st)
    code, _name = normalize_state(loc)
    if code and re.fullmatch(r'[A-Za-z]{2}|D\.?C\.?', loc.strip()):
        return '', loc
    return loc, ''


def stamp_provenance(rec, row, city, state_raw, seller, url):
    """Attach normalized place, seller, discovery source, and listing site. Does not drop the row."""
    code, name = normalize_state(state_raw)
    rec['city'] = city
    rec['st'] = code
    rec['stn'] = name if code else ''
    rec['sel'] = normalize_seller(seller)
    rec['dsc'] = discovery_from_row(row)   # never the listing host
    rec['lhs'] = listing_site(url)         # current link host, labeled Listing site in the UI
    return rec


def listing_key(r):
    """VIN + seller. Same VIN at a different seller is a different listing."""
    return ((r.get('v') or '').upper(), _collapse(r.get('sel')).casefold())


def merge_new_sellers(primary, incoming):
    """Append incoming listings whose VIN+seller pair is not already present.

    Does not remove or rewrite primary. Same VIN with a different seller is kept.
    """
    seen = {listing_key(r) for r in primary}
    out = []
    for r in incoming:
        k = listing_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def drop_tab_vins(rows, vins):
    """Hide a VIN from this tab because another tab already owns it. Seller is not consulted."""
    if not vins:
        return list(rows)
    return [r for r in rows if r.get('v') not in vins]


def to_int(s):
    s = re.sub(r'[^\d]', '', s or '')
    return int(s) if s else None


def short_battery(b):
    m = re.search(r'GM-est\s*(~?[\d\-]+)\s*mi', b or '')
    rng = (m.group(1).replace('-', '\u2013') + ' mi') if m else ''
    if 'Standard' in (b or ''):
        kind = 'Std Range'
    elif 'Max Range' in (b or ''):
        kind = 'Max Range'
    elif '20-module' in (b or ''):
        kind = '20-module'
    else:
        kind = (b or '').split('(')[0].strip()
    return (kind + ' ' + rng).strip()


def close_label(why):
    # Make the "what doesn't match" text clearer, e.g. "400 (shorter body)" -> "Model 400 (shorter body)"
    w = (why or '').strip()
    w = re.sub(r'(^|,\s*)400 \(shorter body\)', r'\1Model 400 (shorter body)', w)
    return w or 'Close match'


def load(path, close, cat='bd'):
    out = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            vin = (r.get('VIN') or '').strip().upper()
            if not vin:
                continue
            city, state_raw = split_city_state(r.get('City/State'))
            phone = (r.get('Phone') or '').strip()
            digits = re.sub(r'\D', '', phone)
            if len(digits) == 11 and digits.startswith('1'):
                digits = digits[1:]
            ver = (r.get('Verified') or '').strip()
            rec = {
                'v': vin,
                'p': to_int(r.get('Price')),
                'y': to_int(r.get('Year')),
                'mdl': (r.get('Model') or '').strip(),
                'mi': to_int(r.get('Miles')),
                'dt': (r.get('Drivetrain') or '').strip(),
                'bat': (r.get('Battery/Range') or '').strip(),
                'bs': short_battery(r.get('Battery/Range')),
                'ph': phone,
                'tel': ('+1' + digits) if len(digits) == 10 else digits,
                'url': (r.get('Listing URL') or '').strip(),
                'dlr': (r.get('Dealer page URL') or '').strip(),
                'ver': ver,
                'vk': 'warn' if ver.lower().startswith('unconfirmed') else ('dealer' if 'dealer site' in ver.lower() else 'ok'),
                'vn': (r.get('Verification notes') or '').strip(),
                'cm': 1 if close else 0,
                'c': cat, 'mk': 'Chevrolet' if (r.get('Model') or '').strip().lower().startswith('chevrolet') else 'BrightDrop',
                'fu': 'EV', 'cond': 'used', 'bt': 'cargo van',
            }
            rec['bk'] = (r.get('Battery type') or '').strip() or _bat_type(vin, r.get('Battery/Range'), r.get('Year'))
            rec['dt'] = _bd_drive(vin, rec['dt'])
            stamp_provenance(rec, r, city, state_raw, r.get('Seller'), r.get('Listing URL'))
            dist = distance_from_home('', rec['city'], rec['st'])
            if dist != '':
                rec['d'] = int(dist)
            if close:
                rec['why'] = close_label(r.get('Why not a match'))
            rec.update(recall_fields(r, vin))
            vc, pl = vin_check(vin), pending_links(vin)
            if vc:
                rec['vc'] = vc
            if pl:
                rec['pl'] = pl
            out.append(rec)
    return out


def synth_key(url):
    """Stable 17-char key in the VIN alphabet for listings without a VIN (notes sync needs /^[A-HJ-NPR-Z0-9]{17}$/).
    'ZZ' prefix = never a real VIN (no such WMI)."""
    h = hashlib.sha256(url.encode('utf-8')).digest()
    return 'ZZ' + ''.join(KEY_ALPHA[b % len(KEY_ALPHA)] for b in h[:15])


def phone_tel(phone):
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return ('+1' + digits) if len(digits) == 10 else ''


def load_extra(path, cat, close):
    """Unified Truck Scout CSV (vf_common.COLUMNS) -> compact rows. cat: 'tu' (Tundra), 'sv' (step vans), 'tv' (transit vans),
    'et' (E-Transit AWD/4x4 conversions), 'rv' (Rivian 500 / 700 vans)."""
    out = []
    if not os.path.exists(path):
        print(f'note: {os.path.relpath(path, HERE)} not found - {cat} category left empty', file=sys.stderr)
        return out
    with open(path, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            g = lambda k: (r.get(k) or '').strip()
            vin, url = g('VIN').upper(), g('Listing URL')
            if not vin and not url:
                continue
            flags = [x.strip() for x in g('Flags').split(';') if x.strip()]
            cfp = 1 if ('call for price' in flags or not g('Price')) else 0
            flags = [x for x in flags if x != 'call for price']
            cond = g('Condition').lower() or 'used'
            if 'Certified pre-owned' in flags:
                cond, flags = 'certified', [x for x in flags if x != 'Certified pre-owned']
            rec = {'c': cat, 'v': vin or synth_key(url), 'p': to_int(g('Price')), 'y': to_int(g('Year')), 'mk': g('Make'),
                   'mdl': g('Model'), 'mi': to_int(g('Miles')), 'dt': g('Drivetrain'), 'fu': g('Fuel/Powertrain'), 'cond': cond,
                   'sty': g('Seller type'),
                   'ph': g('Phone'), 'tel': phone_tel(g('Phone')), 'url': url, 'ds': g('Date seen'), 'src': g('Source'),
                   'cm': 1 if close else 0}
            # dsc is discovery (explicit Source / Discovery source column). ds stays "date seen".
            # lhs is the current listing-link host and is not copied into dsc.
            stamp_provenance(rec, r, g('City'), g('State'), g('Seller'), url)
            for k, col in (('tr', 'Trim'), ('bt', 'Body type'), ('bb', 'Body builder'), ('bl', 'Body length'), ('cb', 'Cab/Bed'),
                           ('eng', 'Engine'), ('gv', 'GVWR')):
                if g(col):
                    rec[k] = g(col)
            if not vin:
                rec['nov'] = 1
            if cfp:
                rec['cfp'] = 1
            if flags:
                rec['fl'] = flags
            d = to_int(g('Distance mi'))
            if d is not None:
                rec['d'] = d
            notes = g('Notes')
            if cat in ('tu', 'tv'):
                rec['w4'] = 'vpic' if 'vPIC' in notes else 'listing'   # how AWD/4WD was verified
                if cat == 'tv' and notes:
                    rec['nt'] = notes[:600] + ('…' if len(notes) > 600 else '')
            elif notes:
                rec['nt'] = notes[:600] + ('…' if len(notes) > 600 else '')
            if cat == 'au':   # auction lot: current bid in p, close time ET, buyer's premium, title status; never call-for-price
                rec.pop('cfp', None)
                for k, col in (('ce', 'Close time ET'), ('ls', 'Lot status'), ('bp', "Buyer's premium"), ('ts', 'Title status'),
                               ('lot', 'Lot #'), ('pv', 'Partial VIN'), ('bat', 'Battery'), ('dmg', 'Damage')):
                    if g(col):
                        rec[k] = g(col)
                if g('Lot status') == 'closed' and not close:
                    close = True; rec['cm'] = 1; rec['why'] = 'Auction closed'
            if close:
                rec['why'] = rec.get('why') or g('Why not a match') or 'Close match'
            out.append(rec)
    return out


def fmt_dt(ts):
    d = datetime.datetime.fromtimestamp(ts)  # box clock = America/New_York
    return d.strftime('%b %-d, %Y %-I:%M %p ET'), d.strftime('%Y-%m-%dT%H:%M')



NW_COLS = ['v', 'y', 'mk', 'md', 'mds', 'tr', 'ec', 'ic', 'gr', 'vda', 'mi', 'p', 'loc', 'lt', 'c', 'st', 'z', 'd', 'av', 'note', 'dt', 'dts', 'dsrc',
           'gx', 'nd', 'dtc', 'dtv', 'bt', 'bc', 'fu', 'en', 'tx', 'dr', 'pl', 'gv', 'vm', 'vt', 'vs', 'fl', 'hm', 'fs', 'nw', 'pp', 'pd', 'rm']


def load_networks():
    """Dealer networks (Hertz Dealer Network ...): networks/<id>/inventory.json made by import_network.py.
    Returns (payload_dict_or_None, [ {'v': vin, 'nwo': 1} ... ] for the leak check). Rows are arrays in NW_COLS order."""
    mp = os.path.join(NETWORKS_DIR, 'networks_meta.json')
    if not os.path.exists(mp):
        return None, []
    meta = json.load(open(mp))
    nets, leak = [], []
    for nid in sorted(meta):
        ip = os.path.join(NETWORKS_DIR, nid, 'inventory.json')
        if not os.path.exists(ip):
            continue
        inv = json.load(open(ip))
        rows = [[(x.get(c) if x.get(c) not in ('', []) else None) for c in NW_COLS] for x in inv]
        leak += [{'v': x['v'], 'nwo': 1} for x in inv]
        m = meta[nid]
        nets.append({'id': nid, 'name': m.get('name', nid), 'n': sum(1 for x in inv if not x.get('rm')),
                     'meta': {k: m.get(k) for k in ('updated', 'imported', 'src', 'new', 'chg', 'removed', 'dts', 'vfail', 'vwarn', 'vmis')},
                     'rows': rows})
    if not nets:
        return None, []
    return {'cols': NW_COLS, 'nets': nets}, leak

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--listings', default=os.path.join(HERE, 'listings.csv'))
    ap.add_argument('--close', default=os.path.join(HERE, 'listings_close_matches.csv'),
                    help='close-match CSV (pass "" to omit)')
    ap.add_argument('--latest-snapshot', action='store_true',
                    help='use newest snapshots/YYYY-MM-DD_listings.csv as the main list')
    ap.add_argument('--out', default=os.path.join(HERE, 'BrightDrop-Van-Finder.html'))
    ap.add_argument('--updated', help='override the "Data updated" label (default: newest CSV mtime)')
    ap.add_argument('--no-publish', action='store_true',
                    help=f'do not write the encrypted copy to {SITE_INDEX}')
    ap.add_argument('--site-out', default=SITE_INDEX, help='where to publish the encrypted copy (default: %(default)s)')
    a = ap.parse_args()

    if a.latest_snapshot:
        snaps = sorted(glob.glob(os.path.join(HERE, 'snapshots', '????-??-??_listings.csv')))
        if not snaps:
            sys.exit('no snapshots/YYYY-MM-DD_listings.csv found')
        a.listings = snaps[-1]

    rows = load(a.listings, False, cat='bd')
    close = []
    if a.close and os.path.exists(a.close):
        # Same VIN + same seller is already in the match list. A different seller is kept.
        close = merge_new_sellers(rows, load(a.close, True, cat='bd'))
    elif a.close:
        print(f'warning: close-match file not found: {a.close}', file=sys.stderr)

    # BrightDrop 400 tab (same filters as 600: used US AWD Max Range). 400 VINs that also sit in the
    # 600 close-match CSV stay in that CSV on disk, but in the app they live under the 400 tab only
    # so the two series never mix in one list.
    b4_rows, b4_close = [], []
    if os.path.exists(BD400_CSV):
        b4_rows = load(BD400_CSV, False, cat='b4')
    else:
        print('note: listings_400.csv not found - BrightDrop 400 category left empty', file=sys.stderr)
    if os.path.exists(BD400_CLOSE_CSV):
        b4_close = merge_new_sellers(b4_rows, load(BD400_CLOSE_CSV, True, cat='b4'))
    # NEW (unused) dealer-stock 400s: separate file + separate group in the app (r.nu); never counted as used matches.
    # Same VIN kept when the seller differs; the used row wins only for the same seller.
    b4_new = []
    if os.path.exists(BD400_NEW_CSV):
        for r in merge_new_sellers(b4_rows + b4_close, load(BD400_NEW_CSV, False, cat='b4')):
            r['nu'], r['cond'] = 1, 'new'
            b4_new.append(r)
    # Tab boundary: a VIN that belongs to the 400 list is not also shown under 600.
    close = drop_tab_vins(close, {r['v'] for r in b4_rows + b4_close + b4_new})

    # Truck Scout categories (deduped by key across files; BrightDrop VINs never repeated)
    extra, ex_meta = [], {}
    # VINs already placed on a BrightDrop tab stay there. Within a later tab, same VIN + different seller is kept.
    # Auction lots may share a VIN with a dealer row and are always shown.
    owned = {r['v'] for r in rows + close + b4_rows + b4_close + b4_new}
    for cat, paths in (('tu', [(TUNDRA_CSV, False)]), ('sv', [(STEPVAN_CSV, False), (STEPVAN_CLOSE_CSV, True)]),
                       ('tv', [(TRANSITVAN_CSV, False), (TRANSITVAN_CLOSE_CSV, True)]),
                       ('et', [(ETRANSIT_CSV, False), (ETRANSIT_CLOSE_CSV, True)]),
                       ('rv', [(RIVIAN_CSV, False), (RIVIAN_CLOSE_CSV, True)]),
                       ('au', [(AUCTION_CSV, False), (AUCTION_CLOSE_CSV, True)])):
        cr, seen_keys = [], set()
        for pth, cl in paths:
            for r in load_extra(pth, cat, cl):
                if cat == 'sv' and (r.get('mk') or '').lower() == 'rivian' and os.path.exists(RIVIAN_CSV):
                    continue   # Rivian vans live in the Rivian Vans tab
                if cat != 'au' and r['v'] in owned:
                    continue
                k = listing_key(r)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                cr.append(r)
        if cat != 'au':
            owned.update(r['v'] for r in cr)
        if cat == 'au':   # Oct 12 2026 (Amer): all battery types + drivetrains are matches; Battery filter field instead
            for r in cr:
                r['bk'] = _bat_type(r['v'], (r.get('bat') or '') + ' ' + (r.get('why') or '') + ' ' + (r.get('tr') or ''), r.get('y'))
                r['dt'] = _bd_drive(r['v'], r.get('dt') or '')
                if r.get('cm') and r.get('ls') != 'closed' and re.fullmatch(r'(?i)[\s,;]*((Standard Range( battery| \(ETC\))?|FWD( \(not AWD\))?|not AWD Max Range|400 \(shorter body\))[\s,;]*)+', r.get('why') or ''):
                    r['cm'] = 0; r.pop('why', None)
        pr = [r['p'] for r in cr if r['p'] and not r['cm']]
        ex_meta[cat] = {'n': sum(1 for r in cr if not r['cm'] and not r.get('cfp')), 'nc': sum(1 for r in cr if r['cm']),
                        'ncfp': sum(1 for r in cr if r.get('cfp') and not r['cm']), 'min': min(pr) if pr else None,
                        'updated': fmt_dt(max(os.path.getmtime(p) for p, _ in paths if os.path.exists(p)))[0] if cr else ''}
        extra += cr
    try:   # E-Transit tab note ('no qualifying vans as of ...') + date, from etransit/build_etransit.py
        _em = json.load(open(ETRANSIT_META))
        ex_meta['et']['note'] = _em.get('note', '')
        ex_meta['et']['updated'] = ex_meta['et']['updated'] or fmt_dt(os.path.getmtime(ETRANSIT_META))[0]
    except (OSError, ValueError, KeyError):
        pass
    mt_files = [a.listings] + ([a.close] if os.path.exists(a.close or '') else []) + ([BD400_CSV] if b4_rows else []) + ([BD400_CLOSE_CSV] if b4_close else []) + ([BD400_NEW_CSV] if b4_new else [])
    mt = max(os.path.getmtime(p) for p in mt_files if p and os.path.exists(p))
    label, stamp = fmt_dt(mt)
    if a.updated:
        label = a.updated
    prices = [r['p'] for r in rows if r['p']]
    meta = {
        'updated': label, 'mtime': stamp,
        # data version: changes only when the listing data changes (drives 'New' / price-change badges)
        'stamp': hashlib.sha1(json.dumps([{k: v for k, v in r.items() if k not in RFIELDS} for r in rows + close + b4_rows + b4_close + b4_new + extra],
                                         sort_keys=True).encode()).hexdigest()[:12],
        'built': datetime.datetime.now().strftime('%b %-d, %Y %-I:%M %p ET'),
        'n': len(rows), 'nc': len(close),
        'min': min(prices) if prices else None,
        'src': os.path.basename(a.listings),
        'srcc': os.path.basename(a.close) if close else '',
        'cats': ex_meta,
        'b4': {'n': len(b4_rows), 'nc': len(b4_close),
               'min': min([r['p'] for r in b4_rows if r['p']], default=None),
               'src': os.path.basename(BD400_CSV) if b4_rows else '',
               'srcc': os.path.basename(BD400_CLOSE_CSV) if b4_close else '',
               'nnu': len(b4_new), 'minnu': min([r['p'] for r in b4_new if r['p']], default=None),
               'srcnu': os.path.basename(BD400_NEW_CSV) if b4_new else '',
               'updated': fmt_dt(os.path.getmtime(BD400_CSV))[0] if b4_rows and os.path.exists(BD400_CSV) else ''},
    }
    rec = None
    if RDB is not None:
        used = sorted({c for r in rows + close + b4_rows + b4_close + b4_new for c in r.get('rk', [])})
        keep = ('short', 'label', 'date', 'component', 'units', 'summary', 'consequence', 'remedy', 'what', 'scope',
                'interim', 'parkIt', 'parkOutSide', 'vehicles', 'page', 'doc')
        rec = {'fetched': RDB.get('fetched', ''), 'source': RDB.get('source', ''), 'perVin': RDB.get('per_vin', {}),
               'c': {c: {k: RDB['campaigns'][c].get(k) for k in keep} for c in used}}
    tok = _read_api_token()
    payload = {'meta': meta, 'rows': rows + close + b4_rows + b4_close + b4_new, 'recalls': rec,
               'api': {'ep': API_ENDPOINT, 'ne': NOTES_ENDPOINT, 'base': API_BASE, 't': tok, 'r': _relay_cfg()} if tok else None}
    if extra:   # gzip (mtime=0 -> deterministic, so an unchanged build is not re-pushed) + base64; inflated in the browser
        xs = json.dumps(extra, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        payload['x'] = base64.b64encode(gzip.compress(xs, 9, mtime=0)).decode('ascii')
    nw_payload, nw_leak = load_networks()
    if nw_payload:   # gzip+base64 like 'x'; unpacked lazily when the Dealer Networks tab opens
        nws = json.dumps(nw_payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        payload['nw'] = base64.b64encode(gzip.compress(nws, 9, mtime=0)).decode('ascii')
        print(f'Dealer networks: ' + ', '.join(f"{n['name']} {n['n']:,} cars" for n in nw_payload['nets']))
    js = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    js = js.replace('</', '<\\/').replace('<!--', '<\\!--')  # safe inside <script>
    html = TEMPLATE.replace('/*__DATA__*/', js)

    tmp = a.out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html)
    # self-check before replacing the old file
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    chk = json.loads(m.group(1))
    assert len(chk['rows']) == len(rows) + len(close) + len(b4_rows) + len(b4_close) + len(b4_new)
    if extra:
        assert len(json.loads(gzip.decompress(base64.b64decode(chk['x'])))) == len(extra)
    os.chmod(tmp, 0o600)   # holds the API token in plaintext; only the encrypted copy is published
    os.replace(tmp, a.out)
    print(f'wrote {a.out} ({os.path.getsize(a.out)//1024} KB): {len(rows)} BrightDrop 600 matches from {meta["src"]}, '
          f'{len(close)} close matches, cheapest ${meta["min"]:,}, data updated {label}')
    if b4_rows or b4_close:
        b4min = meta.get('b4', {}).get('min')
        print(f'BrightDrop 400: {len(b4_rows)} vans (all battery types) + {len(b4_close)} close matches'
              + (f', cheapest ${b4min:,}' if b4min else ''))
    if b4_new:
        nmin = meta['b4'].get('minnu')
        print(f'BrightDrop 400 NEW (unused) stock: {len(b4_new)} AWD Max Range units in a separate group'
              + (f', cheapest ${nmin:,}' if nmin else ''))
    if rec:
        all_bd = rows + close + b4_rows + b4_close + b4_new
        nr = sum(1 for r in all_bd if r.get('rc'))
        print(f'recalls: {nr} of {len(all_bd)} vans have NHTSA recalls listed for their model year '
              f'({", ".join(v["short"] for v in rec["c"].values())}); do-not-drive: {sum(r.get("dnd", 0) for r in all_bd)}')
    for cat, lab in (('tu', 'Toyota Tundra'), ('sv', 'Step vans'), ('tv', 'Transit Vans'), ('et', 'Ford E-Transit (AWD/4x4 conversion)'),
                     ('rv', 'Rivian Vans'), ('au', 'Auctions (BrightDrop)')):
        m = ex_meta[cat]
        print(f'{lab}: {m["n"]} priced + {m["ncfp"]} call for price' + (f' + {m["nc"]} close matches' if m['nc'] else '') +
              (f', cheapest ${m["min"]:,}' if m['min'] else ''))

    if a.no_publish:
        print('publish: skipped (--no-publish); public site left unchanged')
        return
    try:
        publish_encrypted(a.out, html, rows + close + b4_rows + b4_close + b4_new + extra + nw_leak, a.site_out, secrets=[tok] if tok else [])
    except Exception as e:  # never fall back to plaintext
        print(f'PUBLISH FAILED: {e}\n  -> kept the previous encrypted {a.site_out}; nothing plaintext was published.',
              file=sys.stderr)
        sys.exit(3)
    # GitHub Pages copy (11amer/vanfinder): push the new encrypted page if it changed. Never fatal for the build.
    if os.path.abspath(a.site_out) == SITE_INDEX and not os.environ.get('VF_NO_PAGES'):
        try:
            p = subprocess.run([os.path.join(HERE, 'publish_pages.sh')], capture_output=True, text=True, timeout=180,
                               env=dict(os.environ, VF_PLAIN_SHA=hashlib.sha256(html.encode('utf-8')).hexdigest()))
            out = (p.stdout + p.stderr).strip().replace('\n', ' | ')
            print(f'pages: {out}' if p.returncode == 0 else f'pages: PUSH FAILED (exit {p.returncode}): {out[-400:]}')
        except Exception as e:
            print(f'pages: PUSH FAILED: {e!r}')


def _relay_cfg():   # notes relay (relay.py): secret ntfy topic + AES key, embedded ONLY inside the encrypted app
    with open(os.path.join(HERE, '.relay_config.json'), encoding='utf-8') as f:
        c = json.load(f)
    return {'n': 'https://ntfy.sh/' + c['topic'], 'k': c['key'],
            's': 'https://raw.githubusercontent.com/11amer/vanfinder/main/notes.enc'}


def _read_api_token():
    try:
        with open(API_TOKEN_FILE, encoding='utf-8') as f:
            t = f.read().strip()
    except FileNotFoundError:
        print(f'note: {API_TOKEN_FILE} not found - the recall-link Save will only store links in the browser', file=sys.stderr)
        return ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', t):
        raise RuntimeError(f'{API_TOKEN_FILE} does not look like a token')
    return t


def _read_password():
    st = os.stat(PASSWORD_FILE)
    if st.st_mode & 0o077:
        print(f'warning: {PASSWORD_FILE} is readable by group/others; run chmod 600 on it', file=sys.stderr)
    with open(PASSWORD_FILE, encoding='utf-8') as f:
        pw = f.read().strip('\r\n')
    if not pw.strip():
        raise RuntimeError(f'{PASSWORD_FILE} is empty')
    return pw


def _run_staticrypt(args, pw, cwd):
    # staticrypt prefixes the -c path with './', so it must be relative to cwd
    args = [os.path.relpath(x, cwd) if x == STATICRYPT_CONFIG else x for x in args]
    env = dict(os.environ, STATICRYPT_PASSWORD=pw)  # password via env, never on the command line (ps)
    p = subprocess.run([STATICRYPT] + args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=300)
    out = (p.stdout + p.stderr).replace(pw, '***')
    if p.returncode != 0:
        raise RuntimeError(f'staticrypt exited {p.returncode}: {out.strip()[-500:]}')
    return out


def publish_encrypted(plain_path, plain_html, rows, site_out=SITE_INDEX, secrets=()):
    """Encrypt plain_path with StatiCrypt and atomically replace site_out. Raises on any problem
    (the caller keeps the old live file). Plaintext never touches the site directory."""
    for need in (STATICRYPT, STATICRYPT_TEMPLATE, PASSWORD_FILE):
        if not os.path.exists(need):
            raise RuntimeError(f'missing {need}' + (' (run: npm ci)' if need == STATICRYPT else ''))
    if not os.path.exists(STATICRYPT_CONFIG):
        print(f'note: {STATICRYPT_CONFIG} not found - a new salt will be generated '
              f'(existing "Remember me" logins will have to re-enter the password)', file=sys.stderr)
    pw = _read_password()
    site_dir = os.path.dirname(os.path.abspath(site_out))
    work = tempfile.mkdtemp(prefix='.publish-', dir=HERE)  # 0700, outside the served directory
    try:
        src = os.path.join(work, 'index.html')
        shutil.copyfile(plain_path, src)
        _run_staticrypt([src, '-d', os.path.join(work, 'enc'), '-c', STATICRYPT_CONFIG, '--short',
                         '--remember', str(REMEMBER_DAYS), '-t', STATICRYPT_TEMPLATE,
                         '--template-title', SITE_TITLE,
                         '--template-instructions', 'Enter the password to open the van list.',
                         '--template-placeholder', 'Password',
                         '--template-button', 'Unlock',
                         '--template-remember', f'Remember me on this device for {REMEMBER_DAYS} days',
                         '--template-error', 'Wrong password - please try again.',
                         '--template-color-primary', '#0a66e8', '--template-color-secondary', '#f3f5f8'], pw, work)
        enc_path = os.path.join(work, 'enc', 'index.html')
        with open(enc_path, encoding='utf-8') as f:
            enc = f.read()
        # --- sanity checks: it is a StatiCrypt page and nothing from the plaintext leaked ---
        if '"staticryptEncryptedMsgUniqueVariableName"' not in enc or SITE_TITLE not in enc:
            raise RuntimeError('encrypted output does not look like a StatiCrypt page')
        if len(enc) < len(plain_html):
            raise RuntimeError('encrypted output is smaller than the plaintext - refusing to publish')
        m = re.search(r'"staticryptEncryptedMsgUniqueVariableName":\s*"([^"]*)"', enc)
        if not m or not re.fullmatch(r'[0-9a-f]{64,}', m.group(1)):
            raise RuntimeError('encrypted payload is missing or not hex')
        shell = enc[:m.start(1)] + enc[m.end(1):]  # the page minus the ciphertext must hold no listing data
        leaks = ['<script id="data"', 'bd600finder.', '"rows":', '"meta":', '/api/', 'api/recall', 'api/notes', 'user_state',
                 'X-Api-Token', 'localStorage.setItem("bd', '"user":', '"state":'] + [x for x in secrets if x]
        for r in rows:
            if r.get('nwo'):   # dealer-network VINs: full VIN only (24k six-char tails would false-positive on the page shell)
                leaks.append(r['v'])
                continue
            leaks += [r['v'], r['v'][-6:]] + [x for x in (r.get('ph'), r.get('sel'), r.get('url')) if x and len(x) >= 6]
        hit = next((x for x in leaks if x in shell), None)
        if hit is not None:
            raise RuntimeError('encrypted output contains plaintext listing data - refusing to publish')
        # --- round trip: decrypt with the same password and compare byte-for-byte ---
        os.makedirs(os.path.join(work, 'chk'))
        _run_staticrypt([enc_path, '--decrypt', '-d', os.path.join(work, 'dec'), '-c', STATICRYPT_CONFIG],
                        pw, os.path.join(work, 'chk'))
        with open(os.path.join(work, 'dec', 'index.html'), encoding='utf-8') as f:
            if f.read() != plain_html:
                raise RuntimeError('round-trip decryption did not reproduce the app - refusing to publish')
        # --- atomic replace (rename within the same filesystem; replaces a symlink itself, not its target) ---
        os.chmod(enc_path, 0o644)
        try:
            os.replace(enc_path, site_out)
        except OSError:  # different filesystem: stage next to the target, then rename
            fd, stage = tempfile.mkstemp(prefix='.index.', suffix='.tmp', dir=site_dir)
            os.close(fd)
            try:
                shutil.copyfile(enc_path, stage)
                os.chmod(stage, 0o644)
                os.replace(stage, site_out)
            finally:
                if os.path.exists(stage):
                    os.remove(stage)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if os.path.exists(STATICRYPT_CONFIG):
        os.chmod(STATICRYPT_CONFIG, 0o600)
    extra = sorted(n for n in os.listdir(site_dir) if n != os.path.basename(site_out))
    if extra:
        print(f'WARNING: {site_dir} contains other files that are publicly served: {extra}', file=sys.stderr)
    if os.path.islink(site_out):
        raise RuntimeError(f'{site_out} is still a symlink')
    print(f'published encrypted {site_out} ({os.path.getsize(site_out)//1024} KB, StatiCrypt AES, '
          f'remember-me {REMEMBER_DAYS} days)')


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#0b1f3a">
<link rel="icon" href="data:,">
<title>BrightDrop Van Finder</title>
<style>
:root{--bg:#f3f5f8;--card:#fff;--text:#111827;--muted:#5b6474;--line:#e3e7ee;--accent:#0a66e8;--accent-ink:#fff;
--good:#0d7a43;--good-bg:#e5f6ec;--warn:#9a5b00;--warn-bg:#fff3d6;--close:#6d28d9;--close-bg:#f1eaff;--new:#c2410c;--new-bg:#ffedd5;
--bad:#b42318;--bad-bg:#fee4e2;--hdr:#0b1f3a;--hdr-ink:#fff;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08);--r:14px}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--text:#e8ebf0;--muted:#9aa3b2;--line:#2a313c;--accent:#4c94ff;
--good:#5fd39a;--good-bg:#12301f;--warn:#f2c46b;--warn-bg:#3a2b0c;--close:#c4a8ff;--close-bg:#2a1f45;--new:#ffb27a;--new-bg:#3d2312;--bad:#ff8a80;--bad-bg:#3d1512;--hdr:#0a1526;--shadow:none}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
button,select,input,textarea{font:inherit;color:inherit}
a{color:var(--accent)}
header{background:var(--hdr);color:var(--hdr-ink);padding:16px 16px 14px;padding-top:max(16px,env(safe-area-inset-top))}
.wrap{max-width:1280px;margin:0 auto}
h1{margin:0;font-size:20px;letter-spacing:-.01em}
h1 small{display:block;font-weight:500;opacity:.7;font-size:13px;margin-top:2px}
.stats{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;font-size:14px;opacity:.92}
.stats b{font-size:15px}
.stats .upd{opacity:.75}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
.searchrow{display:flex;gap:8px}
#q,#nwq{flex:1;min-width:0;height:52px;border-radius:12px;border:1.5px solid var(--line);background:var(--card);padding:0 16px;font-size:18px;outline:none}
#q:focus,#nwq:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:44px;padding:0 14px;border-radius:10px;border:1.5px solid var(--line);
background:var(--card);cursor:pointer;text-decoration:none;color:var(--text);font-weight:600;font-size:15px;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.ghost{background:transparent}
#ftoggle{height:52px}
#ftoggle .cnt,#nwft .cnt{background:var(--accent);color:#fff;border-radius:99px;font-size:12px;padding:1px 7px}
.filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.filters.collapsed{display:none}
.f{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.f select,.f input:not([type=checkbox]){height:44px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);padding:0 10px;font-size:16px;text-transform:none;letter-spacing:0;font-weight:500;color:var(--text)}
.tog{display:flex;align-items:center;gap:10px;line-height:1.2;min-height:44px;padding:0 12px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);
font-size:15px;font-weight:600;cursor:pointer;user-select:none;color:var(--text)}
.tog input{width:20px;height:20px;accent-color:var(--accent);margin:0}
.tog.on{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--card))}
.span2{grid-column:span 2}
.sortrow{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}
.sf{display:flex;align-items:center;gap:6px;min-width:0;height:42px;padding:0 4px 0 10px;border-radius:10px;border:1.5px solid var(--line);background:var(--card)}
.sf>span{flex:none;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.sf select{flex:1;min-width:0;width:100%;height:38px;border:0;background:transparent;color:var(--text);font-size:15px;font-weight:600;padding:0 2px;outline:none}
.sf:focus-within{border-color:var(--accent)}
.chips{display:flex;gap:6px;margin-top:8px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;padding-bottom:1px}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;min-height:34px;padding:0 11px;border-radius:99px;border:1.5px solid var(--line);background:var(--card);color:var(--text);font:inherit;font-size:13.5px;font-weight:600;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.chip[aria-pressed=true]::before{content:'✓ '}
.chip.rr{border-color:#16a34a;color:#166534}.chip.rr[aria-pressed=true]{background:#16a34a;border-color:#16a34a;color:#fff}
.facets{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:flex-end}
.facets .f{flex:1 1 9.5rem;min-width:0}
.facets .f.sel{flex:1.6 1 13rem}
.facets .f.ms.open{grid-column:auto}
.facetnote{margin:6px 0 0;color:var(--muted);font-size:12.5px;line-height:1.35;font-weight:500}
.asum{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
.asum:empty{display:none}
.asum .n{font-size:12.5px;font-weight:700;color:var(--muted);margin-right:2px}
.achip{display:inline-flex;align-items:center;gap:6px;min-height:32px;max-width:100%;padding:0 8px 0 10px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--text);font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.achip .x{color:var(--muted);font-weight:700}
.asum .clr{min-height:32px;font-size:13px;margin-left:auto}
.facets details.msd summary:focus-visible,.achip:focus-visible,.btn:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.b-prov{background:var(--bg);color:var(--muted);border:1px solid var(--line);font-weight:600}
@media (max-width:699px){
  .facets .f,.facets .f.sel{flex:1 1 calc(50% - 8px);max-width:calc(50% - 4px)}
}
.b-rr{background:#dcfce7;color:#166534}.b-av{background:#e0f2fe;color:#075985}.b-dmg{background:#fee2e2;color:#991b1b}
#nwrrinfo{font-size:12.5px;color:var(--muted);margin:6px 2px 0}
@media (max-width:420px){.chips{gap:5px}.chip{padding:0 9px;font-size:13px}.sf>span{font-size:10px}}
.b-tv{background:var(--good);color:#fff}
.vr{display:block;font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
.t th .sa{font-size:11px;color:var(--accent);margin-left:3px;letter-spacing:0}
.t th.sorted{color:var(--text)}
.resline{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;font-size:14px;color:var(--muted)}
.resline b{color:var(--text)}
.views{display:none;gap:0}
.views .btn{min-height:34px;border-radius:0;font-size:13px;padding:0 12px}
.views .btn:first-child{border-radius:8px 0 0 8px}.views .btn:last-child{border-radius:0 8px 8px 0;margin-left:-1.5px}
.views .btn.on{background:var(--text);color:var(--bg);border-color:var(--text)}
main{padding:12px 16px 40px}
#list{display:grid;grid-template-columns:1fr;gap:12px}
.card{background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);border:1px solid var(--line);border-left:5px solid var(--line);padding:14px 14px 12px;min-width:0}
.card.is-close{border-left-color:var(--close)}
.s-called{border-left-color:#3b82f6!important}.s-waiting{border-left-color:#f59e0b!important}.s-offer-made{border-left-color:#10b981!important}
.s-passed{opacity:.55}
.ctop{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.price{font-size:30px;font-weight:800;letter-spacing:-.02em;line-height:1.1}
.price.na{font-size:20px;color:var(--muted);font-weight:700;padding-top:6px}
.quoted{display:block;font-size:14px;font-weight:700;color:var(--good);letter-spacing:0;margin-top:2px}
.star{width:48px;height:48px;flex:none;border-radius:12px;border:1.5px solid var(--line);background:var(--card);font-size:26px;line-height:1;cursor:pointer;color:var(--muted);-webkit-tap-highlight-color:transparent}
.star.on{color:#f5a400;border-color:#f5c451;background:color-mix(in srgb,#f5a400 10%,var(--card))}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 6px}
.b{display:inline-block;font-size:12.5px;font-weight:700;padding:3px 9px;border-radius:99px;line-height:1.35}
.b-ok{background:var(--good-bg);color:var(--good)}
.b-warn{background:var(--warn-bg);color:var(--warn)}
.b-close{background:var(--close-bg);color:var(--close)}
.b-new{background:var(--new-bg);color:var(--new)}
.b-pc{background:var(--good-bg);color:var(--good)}.b-pc.up{background:var(--warn-bg);color:var(--warn)}
.b-rc{background:var(--warn-bg);color:var(--warn);border:0;cursor:pointer;font-family:inherit}
.b-rc.open{background:var(--bad-bg);color:var(--bad)}
.b-rc0{background:var(--bg);color:var(--muted);border:1px solid var(--line);cursor:pointer;font-family:inherit}
.b-dnd{background:var(--bad);color:#fff}
.b-rok{background:var(--good-bg);color:var(--good);border:1px solid color-mix(in srgb,var(--good) 45%,transparent);cursor:pointer;font-family:inherit}
.b-rq{background:var(--warn-bg);color:var(--warn);border:1px dashed color-mix(in srgb,var(--warn) 60%,transparent)}
.rst.clear{background:var(--good-bg);color:var(--good)}
.rcv{margin:8px 0;padding:8px 10px;border-radius:8px;font-size:13.5px;word-break:break-word}
.rcv.ok{background:var(--good-bg);color:var(--good)}.rcv.bad{background:var(--bad-bg);color:var(--bad)}.rcv a{color:inherit}
.rbox{margin-top:12px;padding:10px;border:1.5px solid var(--line);border-radius:12px;background:var(--bg)}
.rbh{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;font-size:13.5px;margin-bottom:8px}
.rbh>b{font-size:15px}
.rbs{color:var(--muted);font-weight:600}.rbs.ok{color:var(--good)}.rbs.bad{color:var(--bad)}.rbs.q{color:var(--warn)}
.rbs a{color:inherit}
.rbt{color:var(--muted);font-size:12.5px;margin:-4px 0 8px}
.rbox input{width:100%;height:46px;border:1.5px solid var(--line);background:var(--card);border-radius:10px;padding:0 10px;font-size:16px;outline:none}
.rbox input:focus{border-color:var(--accent)}.rbox input.bad{border-color:var(--bad)}
.rbb{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:8px;margin-top:8px}
.rbb .btn{min-height:44px;font-size:15px;white-space:normal;text-align:center;line-height:1.15;padding:4px 10px}
.rbm{font-size:13.5px;font-weight:600;margin-top:6px}.rbm:empty{display:none}
.rbm.ok{color:var(--good)}.rbm.err{color:var(--bad)}.rbm.busy{color:var(--muted)}
.rbm .btn{min-height:34px;font-size:13px;margin-left:6px;padding:0 10px}
.t tr.x .rbox{max-width:760px}
.vwrow{grid-column:1/-1;display:flex;align-items:center;gap:8px;min-width:0;height:42px;padding:0 10px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);font-size:13px}
.vwrow .vt{flex:none;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.vwrow input{flex:1;min-width:50px;margin:0;accent-color:var(--accent);height:28px}
.vwrow .vl{flex:none;white-space:nowrap;font-weight:600;font-variant-numeric:tabular-nums}
.updbar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:21;display:flex;align-items:center;gap:10px;background:var(--hdr);color:#fff;
padding:8px 8px 8px 14px;border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.25);font-weight:600;font-size:14px;max-width:calc(100vw - 24px)}
.updbar .btn{min-height:36px;font-size:14px}
details.rcd{margin-top:8px;font-size:13.5px;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:var(--bg)}
details.rcd summary{cursor:pointer;padding:8px 0;font-weight:700;color:var(--warn)}
details.rcd.none summary{color:var(--muted);font-weight:600}
.rci{padding:8px 0;border-top:1px solid var(--line)}
.rci h4{margin:0 0 3px;font-size:14px}.rci h4 small{font-weight:600;color:var(--muted)}
.rci p{margin:3px 0;word-break:break-word}.rci .k{font-weight:700}
.rst{display:inline-block;font-size:12px;font-weight:700;padding:1px 7px;border-radius:99px;background:var(--warn-bg);color:var(--warn)}
.rst.open{background:var(--bad-bg);color:var(--bad)}.rst.remedied{background:var(--good-bg);color:var(--good)}
.rcn{color:var(--muted);font-size:12.5px;margin:6px 0 8px}
.specs{font-size:17px}
.specs b{font-weight:700}
.bat{font-size:14px;color:var(--muted);margin-top:2px}
.loc{margin-top:6px;font-size:15px}
.loc .city{font-weight:600}
.vin{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.vin6{font-size:22px;font-weight:800;letter-spacing:.06em;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:1px 8px}
.vinfull{font-size:13px;color:var(--muted);letter-spacing:.02em;word-break:break-all}
.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}
.actions .btn{min-height:48px;font-size:16px}
.actions .btn small{font-weight:500;opacity:.85;font-size:13px}
.nophone{grid-column:span 2;font-size:13px;color:var(--muted);text-align:center;padding:2px 0}
.mine{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:8px;margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
.mine select,.mine input,.mine textarea{width:100%;border:1.5px solid var(--line);background:var(--bg);border-radius:10px;font-size:16px}
.mine select,.mine input{height:46px;padding:0 10px}
.qwrap{position:relative}
.qwrap span{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.qwrap input{padding-left:22px}
.mine textarea{grid-column:span 2;min-height:46px;padding:10px;resize:vertical;line-height:1.35}
details.vn{margin-top:8px;font-size:13.5px;color:var(--muted)}
details.vn summary{cursor:pointer;padding:6px 0;font-weight:600;list-style-position:inside}
details.vn p{margin:4px 0 0;word-break:break-word}
.empty{text-align:center;color:var(--muted);padding:48px 16px;background:var(--card);border-radius:var(--r);border:1px dashed var(--line)}
footer{padding:0 16px 40px;color:var(--muted);font-size:13px}
footer .btn{min-height:38px;font-size:14px}
footer .row{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
#sync{font-size:12px;font-weight:600;color:var(--muted);margin-left:6px;white-space:nowrap}#sync.ok{color:var(--good)}#sync.bad{color:var(--warn)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(30px);background:#111827;color:#fff;padding:12px 18px;border-radius:12px;
font-weight:600;opacity:0;transition:.2s;pointer-events:none;z-index:20;box-shadow:0 6px 24px rgba(0,0,0,.25)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.warnbar{background:var(--warn-bg);color:var(--warn);padding:8px 16px;font-size:14px;font-weight:600}
/* table */
table.t{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;font-size:14px}
.t th{position:sticky;top:0;background:var(--card);text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);padding:10px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.t th[data-sort]{cursor:pointer}.t th[data-sort]:hover{color:var(--text)}
.t td{padding:7px 7px;border-bottom:1px solid var(--line);vertical-align:middle}
.t tr.r:hover td{background:color-mix(in srgb,var(--accent) 4%,var(--card))}
.t tr.r.is-close td:first-child{box-shadow:inset 4px 0 0 var(--close)}
.t tr.r.s-called td:first-child{box-shadow:inset 4px 0 0 #3b82f6}.t tr.r.s-waiting td:first-child{box-shadow:inset 4px 0 0 #f59e0b}.t tr.r.s-offer-made td:first-child{box-shadow:inset 4px 0 0 #10b981}
.t .tp{font-weight:800;font-size:16px;white-space:nowrap}
.t .tq{display:block;font-size:12px;color:var(--good);font-weight:700}
.t .num{white-space:nowrap;text-align:right}
.t .v6{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:800;font-size:15px;letter-spacing:.04em}
.t .sub{display:block;color:var(--muted);font-size:12px;white-space:nowrap}
.t .nw{white-space:nowrap}
.t .star{width:36px;height:36px;font-size:20px;border-radius:9px}
.t select{height:36px;border-radius:8px;border:1.5px solid var(--line);background:var(--bg);font-size:14px;padding:0 6px}
.t .acts{display:flex;gap:4px;white-space:nowrap}
.t .acts .btn{min-height:34px;padding:0 9px;font-size:13px;border-radius:8px}
.t .b{font-size:11.5px;padding:2px 7px;white-space:nowrap}
.t .b-close{white-space:normal;margin-top:3px}
.t.wc td.vc{min-width:190px}
.t td.lc{max-width:230px}.t td.bc{white-space:nowrap}.t.wc td.bc{max-width:120px;white-space:normal}.t td.lc .sub{white-space:normal}
.tscroll{overflow-x:auto;border-radius:var(--r)}
.t tr.x td{background:var(--bg);padding:10px 12px}
.t tr.x .mine{border-top:0;margin-top:0;padding-top:0;grid-template-columns:180px 1fr;align-items:start}
.t tr.x .mine textarea{grid-column:auto}
.t tr.x .vnp{margin:8px 0 0;color:var(--muted);font-size:13px}
.t tr.rx td{background:var(--bg);padding:4px 12px 10px}
.t tr.rx .rci{max-width:1100px}
.t .b-rc,.t .b-rc0{margin-top:3px}
.t .b-tv{display:inline-block;margin-top:3px}
.t .b-rok,.t .b-rq{white-space:normal;margin-top:3px;text-align:left}
/* Truck Scout: category tabs, multi-select filters, call-for-price group */
.cats{display:flex;gap:6px;margin-bottom:8px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.cats::-webkit-scrollbar{display:none}
.cat{flex:none;min-height:40px;padding:0 12px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);color:var(--text);font:inherit;font-weight:700;font-size:14.5px;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.cat[aria-selected=true]{background:var(--hdr);border-color:var(--hdr);color:#fff}
.cat .n{font-weight:600;opacity:.75;font-size:12.5px;margin-left:5px}
.f.ms{position:relative}
.f.ms.open{grid-column:1/-1}
details.msd summary{list-style:none;display:flex;align-items:center;justify-content:space-between;gap:6px;height:44px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);padding:0 10px;
font-size:16px;font-weight:500;color:var(--text);text-transform:none;letter-spacing:0;cursor:pointer;-webkit-tap-highlight-color:transparent}
details.msd summary::-webkit-details-marker{display:none}
details.msd summary::after{content:'▾';color:var(--muted);flex:none}
details.msd summary.on{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--card))}
details.msd .msv{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.msb{margin-top:4px;border:1.5px solid var(--line);border-radius:10px;background:var(--card);max-height:280px;overflow:auto;padding:4px}
.msb label{display:flex;align-items:center;gap:10px;min-height:42px;padding:0 8px;font-size:15px;text-transform:none;letter-spacing:0;font-weight:500;color:var(--text);cursor:pointer;border-radius:8px}
.msb label.z{opacity:.5}
.msb input{width:20px;height:20px;accent-color:var(--accent);margin:0;flex:none}
.msb .c{margin-left:auto;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.msb .msx{width:100%;min-height:36px;margin-top:2px;font-size:13px}
.cfph{grid-column:1/-1;margin:22px 0 2px;font-size:16px;line-height:1.3;padding:10px 12px;border-radius:12px;background:var(--warn-bg);color:var(--warn)}
.cfph small{display:block;font-weight:500;font-size:13px;margin-top:2px}
.card.is-cfp{border-left-color:var(--warn)}
.b-unused{background:#1d4ed8;color:#fff;letter-spacing:.03em}
.nuh{grid-column:1/-1;margin:22px 0 2px;font-size:16px;line-height:1.3;padding:10px 12px;border-radius:12px;background:#1d4ed8;color:#fff}
.nuh small{display:block;font-weight:500;font-size:13px;margin-top:2px;opacity:.92}
.nutease{display:block;width:100%;text-align:left;border:0;cursor:pointer;font-family:inherit;font-weight:700}
.nulnk{border:0;background:none;padding:0;font:inherit;color:#1d4ed8;font-weight:700;text-decoration:underline;cursor:pointer}
.card.is-nu{border-left-color:#1d4ed8}
tr.r.is-nu td:first-child{box-shadow:inset 4px 0 0 #1d4ed8}
.b-cat{background:var(--hdr);color:#fff}
.b-cond{background:var(--bg);color:var(--text);border:1px solid var(--line)}
.b-flag{background:var(--warn-bg);color:var(--warn)}
.b-bad{background:var(--bad-bg);color:var(--bad)}
.b-rnc{background:var(--bg);color:var(--muted);border:1px dashed var(--line)}
.dist{color:var(--muted)}
.ttl{font-size:15px;font-weight:600;margin-top:2px}
.dtv{font-weight:700}
#xwarn{background:var(--warn-bg);color:var(--warn)}
@media (max-width:899px){.filters:not(.collapsed){max-height:62vh;overflow:auto}}
@media (min-width:700px){
  #list{grid-template-columns:repeat(2,minmax(0,1fr))}
  .filters{grid-template-columns:repeat(4,minmax(0,1fr))}.span2{grid-column:auto}
  .sortrow{display:flex;flex-wrap:wrap}.sf{flex:0 1 330px}.vwrow{flex:0 1 400px}
}
@media (min-width:900px){
  .bar{position:static}   /* the full filter panel is tall on desktop; the table header stays sticky */
  h1{font-size:24px}
  #ftoggle{display:none}
  h1 small{display:inline;margin-left:8px;font-size:14px}
  .filters,.filters.collapsed{display:flex;flex-wrap:wrap;align-items:flex-end}
  .f select,.f input:not([type=checkbox]){min-width:104px}
  .f.ms{min-width:150px;max-width:230px}.f.ms.open{grid-column:auto}
  .f.ms .msb{position:absolute;z-index:8;left:0;min-width:240px;box-shadow:0 8px 28px rgba(0,0,0,.18)}
  .filters{gap:8px 8px}
  .views{display:flex}
  .tog{white-space:nowrap}
  .actions .btn{min-height:44px;font-size:15px}
}
@media (min-width:1100px){#list{grid-template-columns:repeat(3,minmax(0,1fr))}}
/* Dealer Networks tab (Hertz Dealer Network etc.) */
.catn{flex:none;min-height:40px;padding:0 12px;border-radius:10px;border:1.5px solid var(--line);background:var(--card);color:var(--text);font:inherit;font-weight:700;font-size:14.5px;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.catn[aria-selected=true]{background:var(--hdr);border-color:var(--hdr);color:#fff}
.catn .n{font-weight:600;opacity:.75;font-size:12.5px;margin-left:5px}
body:not(.nwm) #nwbar,body:not(.nwm) #nwmain,body:not(.nwm) #nwinfo{display:none}
body.nwm .bar .wrap>*:not(#cats):not(#nwbar){display:none!important}
body.nwm main:not(#nwmain){display:none}
body.nwm .bar{position:static}
body.nwm #srcinfo,body.nwm #recinfo{display:none}
#nwlist{display:grid;grid-template-columns:1fr;gap:12px}
#nwbar .sortrow{margin-top:8px}
#nwbar .nwnet{grid-column:1/-1}
#nwbar .nwnet select{font-size:16px;font-weight:700}
.nwc{background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);border:1px solid var(--line);border-left:5px solid var(--good);padding:14px 14px 12px;min-width:0}
.nwc.mis{border-left-color:var(--bad)}.nwc.mn{border-left-color:var(--warn)}.nwc.gone{opacity:.6;border-left-color:var(--line)}
.nwc h3{margin:4px 0 0;font-size:18px;line-height:1.25;letter-spacing:-.01em}
.nwc .sub{color:var(--muted);font-size:14px;margin-top:2px}
.nwc .ln{font-size:14.5px;margin-top:6px}
.nwc .ln b{font-weight:700}
.nwc .dot{display:inline-block;width:12px;height:12px;border-radius:50%;border:1px solid rgba(128,128,128,.6);vertical-align:-1px;margin-right:4px}
.b-dt{background:var(--accent);color:#fff}.b-dt.two{background:var(--bg);color:var(--text);border:1px solid var(--line)}
.b-bad{background:var(--bad-bg);color:var(--bad)}.b-flag{background:var(--warn-bg);color:var(--warn)}
.nwc details.nwd{margin-top:10px;border-top:1px solid var(--line);padding-top:6px}
.nwc details.nwd summary{cursor:pointer;font-weight:700;font-size:14.5px;min-height:40px;display:flex;align-items:center;color:var(--accent)}
.nwc .kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--line);font-size:14px}
.nwc .kv:last-child{border-bottom:0}
.nwc .kv span{color:var(--muted);flex:none}.nwc .kv b{font-weight:600;text-align:right;min-width:0;overflow-wrap:anywhere}
.nwc .nwh{margin:10px 0 2px;font-size:12px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.nwc .nwfl{margin:8px 0 0;padding:8px 10px;border-radius:8px;background:var(--warn-bg);color:var(--warn);font-size:13.5px}
.nwc .nwfl.bad{background:var(--bad-bg);color:var(--bad)}
.nwc textarea{width:100%;min-height:60px;border:1.5px solid var(--line);border-radius:10px;background:var(--card);padding:8px 10px;font-size:15px;margin-top:8px}
.nwmore{grid-column:1/-1;text-align:center;margin-top:6px}
#nwinfo{padding:6px 16px 0;font-size:13px;color:var(--muted)}
.nwbusy{padding:30px 10px;text-align:center;color:var(--muted);font-size:16px}
.msb .msq{width:100%;height:40px;border:1.5px solid var(--line);border-radius:8px;background:var(--bg);padding:0 10px;margin:0 0 4px;font-size:15px;position:sticky;top:0}
@media (min-width:900px){#nwft{display:none}#nwbar .filters,#nwbar .filters.collapsed{display:flex;flex-wrap:wrap;align-items:flex-end}}
@media (min-width:1100px){#nwlist{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (min-width:700px) and (max-width:1099px){#nwlist{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<div id="xwarn" class="warnbar" hidden></div>
<div id="lswarn" class="warnbar" hidden>This browser is blocking local storage, so notes/status won't be kept after closing. Try opening the file in Chrome or Safari.</div>
<header><div class="wrap">
  <h1>BrightDrop Van Finder <small id="hsub">used · AWD · long range</small> <span id="sync" role="status" aria-live="polite" hidden></span></h1>
  <div class="stats" id="stats"></div>
</div></header>

<div class="bar"><div class="wrap">
  <div class="cats" id="cats" role="tablist" aria-label="Category">
    <button type="button" class="catn" id="nwtab" role="tab" data-nwtab="1" aria-selected="false" title="Dealer-only networks (Hertz Dealer Network): search every car by make, model, miles, drivetrain, color, VIN-checked">Dealer Networks<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="bd" aria-selected="true">BrightDrop 600<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="b4" aria-selected="false">BrightDrop 400<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="tu" aria-selected="false">Toyota Tundra<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="sv" aria-selected="false">Step vans<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="tv" aria-selected="false">Transit Vans<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="et" aria-selected="false" title="Ford E-Transit with an aftermarket AWD/4x4 conversion, up to $50k">E-Transit AWD/4x4<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="rv" aria-selected="false" title="Rivian Commercial Van (ex-EDV / RCV) 500 and 700, new &amp; used, any price">Rivian Vans<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="au" aria-selected="false" title="BrightDrop / Zevo auction lots (AWD Max Range, open), soonest closing first – bid prices, not asking prices">Auctions<span class="n"></span></button>
    <button type="button" class="cat" role="tab" data-cat="all" aria-selected="false">All<span class="n"></span></button>
  </div>
  <div class="searchrow">
    <input id="q" type="search" placeholder="Search seller, city, state, VIN, year, notes…" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search" aria-label="Search">
    <button id="ftoggle" class="btn" type="button" aria-expanded="false">Filters <span class="cnt" id="fcnt" hidden>0</span></button>
  </div>
  <div class="sortrow">
    <label class="sf"><span>Sort by</span><select id="sort" aria-label="Sort by"></select></label>
    <label class="sf"><span>Then by</span><select id="sort2" aria-label="Then by"></select></label>
    <div class="vwrow" id="vwrow" title="How much price vs. miles counts in the Best value sort and the Top value badges">
      <label class="vt" for="vw">Best value</label><span class="vl">Price <b id="vwp">60%</b></span>
      <input type="range" id="vw" min="0" max="100" step="5" value="60" aria-label="Best value weighting: price weight in percent (the rest is miles)">
      <span class="vl">Miles <b id="vwm">40%</b></span></div>
  </div>
  <div class="chips" id="chips" role="group" aria-label="Quick price and mileage limits">
    <button type="button" class="chip" data-chip="maxp" data-val="30000" aria-pressed="false">Under $30k</button>
    <button type="button" class="chip" data-chip="maxp" data-val="35000" aria-pressed="false">Under $35k</button>
    <button type="button" class="chip" data-chip="maxm" data-val="10000" aria-pressed="false">Under 10k mi</button>
    <button type="button" class="chip" data-chip="maxm" data-val="25000" aria-pressed="false">Under 25k mi</button>
    <button type="button" class="chip" data-chip="maxd" data-val="300" aria-pressed="false">Within 300 mi</button>
  </div>
  <div class="facets" id="facets" role="group" aria-label="State, dealer, and discovery">
    <div class="f ms" data-k="st"><span>State</span><details class="msd" id="ms-st"><summary aria-label="Dealer state"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms sel" data-k="sel"><span>Dealer</span><details class="msd" id="ms-sel"><summary aria-label="Dealer or seller"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="dsc"><span>Discovered</span><details class="msd" id="ms-dsc"><summary aria-label="Discovery source" title="Where the inventory file says this listing was first found. Unknown if that was not recorded. Never taken from the current listing link."><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="lhs"><span>Listing site</span><details class="msd" id="ms-lhs"><summary aria-label="Listing site" title="Website of the current listing link. This is not the original discovery source."><span class="msv">Any</span></summary><div class="msb"></div></details></div>
  </div>
  <p class="facetnote">Discovered is the recorded first source. It stays Unknown when the file does not say. Listing site is only the current link.</p>
  <div class="asum" id="asum" aria-live="polite"></div>
  <div class="filters collapsed" id="filters">
    <div class="f ms" data-k="mk"><span>Make</span><details class="msd" id="ms-mk"><summary aria-label="Make"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="mdl"><span>Model</span><details class="msd" id="ms-mdl"><summary aria-label="Model"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="tr"><span>Trim</span><details class="msd" id="ms-tr"><summary aria-label="Trim"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="cond"><span>Condition</span><details class="msd" id="ms-cond"><summary aria-label="Condition"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="bk"><span>Battery</span><details class="msd" id="ms-bk"><summary aria-label="Battery"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="dt"><span>Drivetrain</span><details class="msd" id="ms-dt"><summary aria-label="Drivetrain"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="fu"><span>Fuel</span><details class="msd" id="ms-fu"><summary aria-label="Fuel"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="bt"><span>Body type</span><details class="msd" id="ms-bt"><summary aria-label="Body type"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="bl"><span>Body / length</span><details class="msd" id="ms-bl"><summary aria-label="Body / length"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <div class="f ms" data-k="sty"><span>Seller type</span><details class="msd" id="ms-sty"><summary aria-label="Seller type"><span class="msv">Any</span></summary><div class="msb"></div></details></div>
    <label class="f">Year from<select id="ymin"></select></label>
    <label class="f">Year to<select id="ymax"></select></label>
    <label class="f">Min price<select id="minp"><option value="">Any</option></select></label>
    <label class="f">Max price<select id="maxp"><option value="">Any</option></select></label>
    <label class="f">Min miles<select id="minm"><option value="">Any</option></select></label>
    <label class="f">Max miles<select id="maxm"><option value="">Any</option></select></label>
    <label class="f">Distance (07004)<select id="maxd"><option value="">Any distance</option></select></label>
    <label class="f">My status<select id="status"></select></label>
    <label class="f">Recalls<select id="rec"><option value="">All vans</option><option value="open">Hide vans with open recalls</option>
      <option value="any">Hide open or possible recalls</option><option value="only">Only vans with recalls</option>
      <option value="clear">Only recall-free confirmed</option></select></label>
    <label class="tog" id="favtog"><input type="checkbox" id="fav"> ★ Favorites</label>
    <label class="tog" id="closetog"><input type="checkbox" id="close"> Include close matches</label>
    <label class="tog" id="cfptog"><input type="checkbox" id="cfp"> Show call-for-price</label>
    <label class="tog" id="nutog"><input type="checkbox" id="nu"> Show NEW (unused) stock</label>
    <label class="tog" id="rdettog"><input type="checkbox" id="rdet"> Show recall details</label>
  </div>
  <div class="resline"><span id="res"></span>
    <span style="display:flex;gap:8px;align-items:center">
      <button class="btn ghost" id="reset" type="button" style="min-height:34px;font-size:13px" hidden>Reset</button>
      <span class="views" id="views"><button class="btn" data-view="cards" type="button">Cards</button><button class="btn" data-view="table" type="button">Table</button></span>
    </span>
  </div>
  <div id="nwbar">
    <div class="sortrow">
      <label class="sf nwnet"><span>Network</span><select id="nwnet" aria-label="Network"></select></label>
    </div>
    <div class="searchrow" style="margin-top:8px">
      <input id="nwq" type="search" placeholder="Search make, model, color, VIN, city…" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search" aria-label="Search the network">
      <button id="nwft" class="btn" type="button" aria-expanded="false" style="height:52px">Filters <span class="cnt" id="nwfcnt" hidden>0</span></button>
    </div>
    <div class="sortrow"><label class="sf"><span>Sort by</span><select id="nwsort" aria-label="Sort by"></select></label>
      <label class="sf"><span>Drive</span><select id="nwdt" aria-label="Quick drivetrain"><option value="">Any drivetrain</option><option value="custom" hidden>Custom (see Filters)</option><option value="awd4">AWD or 4WD</option><option value="awd">AWD only</option><option value="4wd">4WD only</option><option value="fwd">FWD only</option><option value="rwd">RWD only</option><option value="2wd">2WD (FWD / RWD)</option></select></label></div>
    <div class="chips" id="nwchips" role="group" aria-label="Quick limits"></div>
    <div id="nwrrinfo" hidden></div>
    <div class="filters collapsed" id="nwfilters"></div>
    <div class="resline"><span id="nwres"></span><button class="btn ghost" id="nwreset" type="button" style="min-height:34px;font-size:13px" hidden>Reset</button></div>
  </div>
</div></div>

<main><div class="wrap"><div id="list"></div></div></main>
<main id="nwmain"><div class="wrap"><div id="nwlist"></div></div></main>
<div id="nwinfo"></div>

<footer><div class="wrap">
  <div id="srcinfo"></div>
  <div>Your notes, status, quotes and stars are saved in this browser (by VIN) and synced to the van-list server, so they come back on any device or link. Export a copy now and then as a backup.</div>
  <div class="row">
    <button class="btn" id="exp" type="button">Export notes</button>
    <label class="btn" for="imp">Import notes</label><input type="file" id="imp" accept="application/json,.json" hidden>
  </div>
  <div>Prices are as advertised and usually exclude tax, title and dealer fees. Always confirm availability, price and battery with the seller.</div>
  <div id="recinfo" style="margin-top:8px"></div>
</div></footer>
<div id="toast" role="status" aria-live="polite"></div>
<div id="updbar" class="updbar" hidden role="status">Updated van data is ready <button class="btn primary" type="button" id="updgo">Reload</button></div>

<script id="data" type="application/json">/*__DATA__*/</script>
<script>
(function(){
'use strict';
var DATA = JSON.parse(document.getElementById('data').textContent);
/* Toyota Tundra + step-van + transit-van rows ride gzip+base64-compressed in DATA.x (keeps the encrypted page small) */
function inflate(b64){
  if (!window.DecompressionStream || !window.Response || !window.Blob) return Promise.reject(new Error('no DecompressionStream'));
  var bin = atob(b64), u = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return new Response(new Blob([u]).stream().pipeThrough(new DecompressionStream('gzip'))).text().then(JSON.parse);
}
if (DATA.x) inflate(DATA.x).then(function(x){ start(x, ''); }, function(){ start([], 'This browser could not unpack the Toyota Tundra / step-van / transit-van listings (update Chrome/Safari). Showing BrightDrop vans only.'); });
else start([], '');
function start(EXTRA, XERR){
var META = DATA.meta, ROWS = DATA.rows.concat(EXTRA || []), REC = DATA.recalls || null, RC = (REC && REC.c) || {}, API = DATA.api || null;
ROWS.forEach(function(r){ if (!r.c) r.c = 'bd'; });
var CATS = {bd: {n: 'BrightDrop 600', u: ['van', 'vans'], sub: 'used · all batteries (Max / Standard Range) · AWD + FWD · use the Battery filter'},
  b4: {n: 'BrightDrop 400', u: ['van', 'vans'], sub: 'used · all batteries + drivetrains · shorter body (+ NEW unused stock, shown separately) · use the Battery filter'},
  tu: {n: 'Toyota Tundra', u: ['truck', 'trucks'], sub: '2024–2026 · 4WD only · up to $50k · new & used'},
  sv: {n: 'Step vans', u: ['van', 'vans'], sub: 'any make, year & drivetrain · up to $50k'},
  tv: {n: 'Transit Vans', u: ['van', 'vans'], sub: 'cargo · AWD/4WD only · ≤$50k · new & used'},
  et: {n: 'Ford E-Transit (AWD/4x4 conversion)', u: ['van', 'vans'], sub: 'aftermarket AWD/4x4 conversions only · ≤$50k · Ford builds the E-Transit RWD only'},
  rv: {n: 'Rivian Vans', u: ['van', 'vans'], sub: 'Rivian 500 / 700 (ex-EDV / RCV) · new & used · no price cap · auctions in call-for-price'},
  au: {n: 'Auctions (BrightDrop)', u: ['lot', 'lots'], sub: 'open auction lots · AWD Max Range 600/400 · sorted by close time (ET) · price = current bid, before buyer\u2019s premium'},
  all: {n: 'All', u: ['listing', 'listings'], sub: 'BrightDrop 600 · BrightDrop 400 · Toyota Tundra · step vans · transit vans · E-Transit AWD/4x4 · Rivian vans · auctions'}};
var STATUSES = ['Not called','Called','Waiting','Offer made','Passed'];
var K = {user:'bd600finder.user.v1', ui:'bd600finder.ui.v1', track:'bd600finder.track.v1', rq:'bd600finder.recall.v1'};
var $ = function(id){ return document.getElementById(id); };

/* ---------- storage ---------- */
var mem = {}, lsOK = true;
try { var t='__bd_t'; localStorage.setItem(t,'1'); localStorage.removeItem(t); } catch(e){ lsOK=false; $('lswarn').hidden=false; }
function load(k, d){ try { var v = lsOK ? localStorage.getItem(k) : mem[k]; return v ? JSON.parse(v) : d; } catch(e){ return d; } }
function save(k, v){ var s = JSON.stringify(v); if (lsOK) { try { localStorage.setItem(k, s); } catch(e){ toast('Could not save (storage full?)'); } } else mem[k]=s; }
var USER = load(K.user, {});   // vin -> {s,n,q,f,t}
var UI = load(K.ui, {});
var VW = (typeof UI.vw === 'number' && UI.vw >= 0 && UI.vw <= 100) ? UI.vw : 60;   // Best value: price weight in %, rest = miles
function U(v){ return USER[v] || {}; }
function setU(v, key, val){
  var o = USER[v] || (USER[v] = {});
  if (val === '' || val == null || val === false || (key==='s' && val==='Not called')) delete o[key]; else o[key] = val;
  o.t = Date.now();   // an entry holding only t is a deletion tombstone (so a clear syncs to other devices)
  save(K.user, USER); syncSoon();
}

/* ---------- notes sync with the server (newer t per VIN wins) ---------- */
var SYNC = {on: !!(API && API.ne) && location.protocol !== 'file:', timer: null, busy: false, again: false, fails: 0};
function apiUrl(p){   // same server when opened from the tunnel or locally; otherwise (GitHub Pages copy) the absolute tunnel URL
  var b = (API.base || '').replace(/\/$/, ''), local = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
  return (b && !local && location.origin !== b ? b : '') + p; }
function syncShow(cls, txt){ var e = $('sync'); if (!e) return; e.hidden = false; e.className = cls; e.textContent = txt; }
function mergeIn(st){   // server -> local; returns number of VINs taken from the server
  var n = 0; if (!st || typeof st !== 'object') return 0;
  Object.keys(st).forEach(function(v){
    var e = st[v]; if (!/^[A-HJ-NPR-Z0-9]{17}$/.test(v) || !e || typeof e !== 'object') return;
    var cur = USER[v]; if (!cur || (e.t||0) > (cur.t||0)) { USER[v] = e; n++; }
  });
  if (n) save(K.user, USER);
  return n;
}
function localNewer(st){ return Object.keys(USER).some(function(v){ var e = st && st[v]; return !e || (USER[v].t||0) > (e.t||0); }); }
/* Sync goes through a free relay (ntfy.sh, encrypted with a key that exists only inside this locked page) + an
   encrypted snapshot in the GitHub repo. No box URL needed, so nothing breaks when the box's connection changes. */
var RL = API && API.r, RKEY = null, PUSHED_K = 'bd600finder.pushed.v1';
function b64e(u){ var s = ''; for (var i = 0; i < u.length; i += 8192) s += String.fromCharCode.apply(null, u.subarray(i, i + 8192)); return btoa(s); }
function b64d(s){ var b = atob(s.trim()), u = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; }
function rkey(){ return RKEY || (RKEY = crypto.subtle.importKey('raw', b64d(RL.k), 'AES-GCM', false, ['encrypt', 'decrypt'])); }
function renc(o){ var iv = crypto.getRandomValues(new Uint8Array(12));
  return rkey().then(function(k){ return crypto.subtle.encrypt({name: 'AES-GCM', iv: iv}, k, new TextEncoder().encode(JSON.stringify(o))); })
    .then(function(ct){ var u = new Uint8Array(12 + ct.byteLength); u.set(iv); u.set(new Uint8Array(ct), 12); return b64e(u); }); }
function rdec(s){ var u = b64d(s);
  return rkey().then(function(k){ return crypto.subtle.decrypt({name: 'AES-GCM', iv: u.subarray(0, 12)}, k, u.subarray(12)); })
    .then(function(pt){ return JSON.parse(new TextDecoder().decode(pt)); }); }
function tfetch(url, o){ var ctl = window.AbortController ? new AbortController() : null, to = setTimeout(function(){ ctl && ctl.abort(); }, 20000);
  o = o || {}; o.cache = 'no-store'; if (ctl) o.signal = ctl.signal;
  return fetch(url, o).then(function(r){ clearTimeout(to); if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); }, function(e){ clearTimeout(to); throw e; }); }
function relaySend(obj){ return renc(obj).then(function(b){ return tfetch(RL.n, {method: 'POST', body: b, headers: {'Content-Type': 'text/plain'}}); }); }
function remoteState(){   // encrypted snapshot (box-made) + the last 12 h of relay messages, newest t per VIN wins
  var st = {};
  function add(s){ if (!s) return; Object.keys(s).forEach(function(v){ var e = s[v]; if (e && (!st[v] || (e.t||0) > (st[v].t||0))) st[v] = e; }); }
  var snap = tfetch(RL.s + '?_=' + Date.now()).then(rdec).then(function(d){ add(d.state); }, function(){ return null; });
  var msgs = tfetch(RL.n + '/json?poll=1&since=12h').then(function(t){
    return Promise.all(t.split('\n').filter(function(l){ return l.trim(); }).map(function(l){
      var m; try { m = JSON.parse(l); } catch (e) { return null; }
      if (m.event !== 'message') return null;
      var p = m.attachment && m.attachment.url ? tfetch(m.attachment.url) : Promise.resolve(m.message || '');
      return p.then(rdec).then(function(d){ if (d.k === 'n') add(d.state); }, function(){ return null; });
    })); });
  return Promise.all([snap, msgs]).then(function(){ return st; });
}
function chunks(st){   // keep each relay message small (ntfy text limit ~4 KB)
  var out = [], cur = {}, size = 0;
  Object.keys(st).forEach(function(v){ var n = JSON.stringify(st[v]).length + 24;
    if (size && size + n > 2400) { out.push(cur); cur = {}; size = 0; } cur[v] = st[v]; size += n; });
  if (size) out.push(cur); return out;
}
function apiCall(method, body){
  if (method === 'GET') return remoteState().then(function(st){ return {ok: true, state: st}; });
  var pushed = +(localStorage.getItem(PUSHED_K) || 0), all = body.state || {}, delta = {}, maxT = pushed;
  Object.keys(all).forEach(function(v){ var t = all[v].t || 0; if (t > pushed || body.full) { delta[v] = all[v]; if (t > maxT) maxT = t; } });
  var parts = chunks(delta);
  return parts.reduce(function(p, c){ return p.then(function(){ return relaySend({k: 'n', state: c}); }); }, Promise.resolve())
    .then(function(){ localStorage.setItem(PUSHED_K, String(maxT)); return {ok: true, state: null}; });
}
function syncFail(){
  SYNC.busy = false; SYNC.fails++;
  syncShow('bad', '⚠ Not synced – saved on phone only');
  clearTimeout(SYNC.timer); SYNC.timer = setTimeout(syncNow, Math.min(300000, 5000 * Math.pow(2, SYNC.fails - 1)));   // retry with backoff
}
function syncOk(j){
  SYNC.busy = false; SYNC.fails = 0;
  if (mergeIn(j.state) && typeof render === 'function') render(true);
  if (SYNC.again) { SYNC.again = false; return syncSoon(); }
  syncShow('ok', '✓ Synced');
}
function syncNow(full){
  clearTimeout(SYNC.timer); SYNC.timer = null;
  if (!SYNC.on) return;
  if (SYNC.busy) { SYNC.again = true; return; }
  SYNC.busy = true; syncShow('', 'Syncing…');
  apiCall('POST', {state: USER, full: full === true}).then(syncOk, syncFail);
}
function syncSoon(){ if (!SYNC.on) return; syncShow('', 'Syncing…'); clearTimeout(SYNC.timer); SYNC.timer = setTimeout(syncNow, 1200); }
function syncStart(){
  if (!SYNC.on || !RL || !window.crypto || !crypto.subtle) { SYNC.on = false; syncShow('bad', '⚠ Not synced – saved on this device only'); return; }
  SYNC.busy = true; syncShow('', 'Syncing…');
  apiCall('GET').then(function(j){
    SYNC.busy = false;
    var newer = localNewer(j.state);
    if (mergeIn(j.state) && typeof render === 'function') render(true);
    if (newer) syncNow(true); else { SYNC.fails = 0; syncShow('ok', '✓ Synced'); }
  }, syncFail);
  window.addEventListener('online', function(){ if (SYNC.fails) syncNow(); });
  document.addEventListener('visibilitychange', function(){ if (document.visibilityState === 'visible' && SYNC.fails) syncNow(); });
}

/* ---------- new / price-change tracking (per browser) ---------- */
var TR = load(K.track, null), first = !TR;
if (!TR) TR = {first: META.stamp, v: {}};
TR.cats = TR.cats || {bd: 1};   // categories this browser has already seen (a newly added category is not all 'New')
var NEW = {}, PC = {};
ROWS.forEach(function(r){
  var x = TR.v[r.v];
  if (!x) { x = TR.v[r.v] = {d: (first || TR.cats[r.c]) ? META.stamp : '0', p: r.p, fs: Date.now()}; }
  else if (r.p != null && x.p != null && r.p !== x.p) { x.prev = x.p; x.p = r.p; x.cd = META.stamp; x.cts = Date.now(); }
  else if (x.p == null) x.p = r.p;
  if (!first && x.d === META.stamp && TR.first !== META.stamp) NEW[r.v] = 1;
  if (x.prev != null && x.prev !== r.p) PC[r.v] = r.p - x.prev;
  // recency for the 'Newest listed / price drop' sort: when this browser first saw the van, or its last price drop
  r._rn = Math.max(x.fs || 0, (PC[r.v] < 0 ? x.cts || 0 : 0));
});
ROWS.forEach(function(r){ TR.cats[r.c] = 1; });
save(K.track, TR);

/* ---------- helpers ---------- */
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function money(n){ return '$' + Number(n).toLocaleString('en-US'); }
function miles(n){ return n == null ? 'Miles not listed' : Number(n).toLocaleString('en-US') + ' mi'; }
function slug(s){ return String(s||'Not called').toLowerCase().replace(/\s+/g,'-'); }
function link(r){ return r.dlr || r.url; }
var toastT;
function toast(msg){ var t=$('toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(toastT); toastT=setTimeout(function(){ t.classList.remove('show'); }, 1800); }
function copy(text){
  function fallback(){
    var ta=document.createElement('textarea'); ta.value=text; ta.setAttribute('readonly',''); ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, 99); var ok=false; try{ ok=document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta); toast(ok ? 'VIN copied: ' + text : 'VIN: ' + text);
  }
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function(){ toast('VIN copied: ' + text); }, fallback);
  else fallback();
}
ROWS.forEach(function(r){
  r.rk = r.rk || []; r.rc = r.rc || 0;
  r._h = [r.sel, r.city, r.st, r.stn, r.dsc, r.lhs, r.nov ? '' : r.v, r.y, r.vn, r.mk, r.mdl, r.tr, r.cond, r.dt, r.fu, r.bt, r.bb, r.bl, r.cb, r.eng, (r.fl || []).join(' '),
    r.cfp ? 'call for price' : '', r.src, r.nt, r.bat, r.why||'', r.ph, r.ver,
    r.rk.map(function(c){ return RC[c] ? RC[c].label + ' recall' : ''; }).join(' '),
    r.vc ? (r.vc.o ? 'open recall ' + r.vc.c : 'recall-free confirmed') : ''].join(' ').toLowerCase();
});
var VMAP = {}; ROWS.forEach(function(r){ VMAP[r.v] = r; });

/* ---------- per-VIN recall checks: pasted NHTSA links ----------
   Server data: r.vc = confirmed NHTSA result {o: open count, c, k: campaign ids, d: date, s: source, u: link, dnd},
   r.pl = pasted links [{u, ts, st: pending|done|dismissed}]. Local fallback RQ (bd600finder.recall.v1):
   vin -> {u, t, st: sending|queued|failed, e}; dropped once the server data includes that link. */
var RQ = load(K.rq, {});
(function(){
  var ch = false;
  Object.keys(RQ).forEach(function(v){
    var x = RQ[v], r = VMAP[v];
    if (!r || !x || !x.u || (r.pl || []).some(function(p){ return p.u === x.u; }) || Date.now() - (x.t || 0) > 30 * 864e5) { delete RQ[v]; ch = true; }
  });
  if (ch) save(K.rq, RQ);
})();
function queued(r){   // a pasted link waiting to be read: local (not yet in server data) first, then server pending
  var x = RQ[r.v]; if (x) return {u: x.u, st: x.st, e: x.e, local: 1};
  var p = (r.pl || []).filter(function(p){ return p.st === 'pending'; }).pop();
  return p ? {u: p.u, st: 'pending', ts: p.ts} : null;
}
function isOpenR(r){ return r.vc ? r.vc.o > 0 : r.rs === 'open'; }
function nhtsaUrl(v){ return 'https://www.nhtsa.gov/recalls?vymm=' + encodeURIComponent(v); }

/* ---------- header ---------- */
var CNT = {};   // per category: n = main (priced or BrightDrop), nc = close matches, cfp = call for price, min = cheapest, nw = new
['bd', 'b4', 'tu', 'sv', 'tv', 'et', 'rv', 'au', 'all'].forEach(function(c){ CNT[c] = {n: 0, nc: 0, cfp: 0, nu: 0, min: null, nw: 0}; });
ROWS.forEach(function(r){
  [r.c, 'all'].forEach(function(c){
    var k = CNT[c];
    if (r.cm) k.nc++; else if (r.nu) k.nu++; else if (r.cfp) k.cfp++; else { k.n++; if (r.p != null && (k.min == null || r.p < k.min)) k.min = r.p; }
    if (NEW[r.v]) k.nw++;
  });
});
function catUpdated(c){ if (c === 'b4') return ((META.b4 || {}).updated) || META.updated; return c === 'bd' || c === 'all' ? META.updated : ((META.cats || {})[c] || {}).updated || META.updated; }
function stats(){
  var c = CAT, k = CNT[c], u = CATS[c].u;
  $('stats').innerHTML =
    '<span><b>' + k.n.toLocaleString('en-US') + '</b> ' + (c === 'bd' || c === 'b4' ? 'matching vans' : 'matching ' + u[1]) + '</span>' +
    (k.nc ? '<span><b>' + k.nc + '</b> close matches</span>' : '') +
    (k.cfp ? '<span><b>' + k.cfp.toLocaleString('en-US') + '</b> call for price</span>' : '') +
    (k.nu ? '<span><b>' + k.nu + '</b> NEW (unused) – separate</span>' : '') +
    (k.min ? '<span>Cheapest <b>' + money(k.min) + '</b></span>' : '') +
    (k.nw ? '<span><b>' + k.nw + '</b> new since last refresh</span>' : '') +
    '<span class="upd">Data updated ' + esc(catUpdated(c)) + '</span>';
  $('hsub').textContent = CATS[c].sub;
  document.querySelectorAll('#cats .cat').forEach(function(b){
    var cc = b.getAttribute('data-cat'); b.setAttribute('aria-selected', String(cc === c));
    b.querySelector('.n').textContent = CNT[cc].n.toLocaleString('en-US');
    b.hidden = cc !== 'bd' && cc !== 'b4' && cc !== 'et' && cc !== 'au' && cc !== c && !CNT[cc].n && !CNT[cc].cfp && !CNT[cc].nc;
  });
}
if (REC) $('recinfo').innerHTML = 'Recalls: NHTSA recall database by model year (fetched ' + esc(REC.fetched) + '). ' +
  (REC.perVin && REC.perVin.ok ? '' : 'Whether a recall is still open on a specific van could not be checked automatically (NHTSA and GM VIN lookups block scripts), so recalls are shown as “may apply” – ask the seller for the VIN recall printout, or check the VIN at <a href="https://www.nhtsa.gov/recalls" target="_blank" rel="noopener noreferrer">nhtsa.gov/recalls</a>.');
$('srcinfo').textContent = 'Data: ' + META.n + ' AWD long-range BrightDrop 600 listings (' + META.src + ')' +
  (META.nc ? ' + ' + META.nc + ' close matches (' + META.srcc + ')' : '') + '. Updated ' + META.updated + '; file built ' + META.built + '.' +
  (META.b4 && META.b4.n ? ' BrightDrop 400: ' + META.b4.n + ' vans, all battery types (' + (META.b4.src || 'listings_400.csv') + ')' +
    (META.b4.nc ? ' + ' + META.b4.nc + ' close matches' : '') + (META.b4.nnu ? ' + ' + META.b4.nnu + ' NEW (unused) dealer-stock units shown separately (' + (META.b4.srcnu || 'listings_400_new.csv') + ')' : '') + ', updated ' + catUpdated('b4') + '.' : '') +
  (CNT.tu.n + CNT.tu.cfp ? ' Toyota Tundra: ' + CNT.tu.n + ' priced + ' + CNT.tu.cfp + ' call-for-price 2024–2026 4WD listings up to $50k (CarStory + Cars.com; 4WD checked by NHTSA VIN decode), updated ' + catUpdated('tu') + '.' : '') +
  (CNT.sv.n + CNT.sv.cfp + CNT.sv.nc ? ' Step vans: ' + CNT.sv.n + ' priced + ' + CNT.sv.cfp + ' call for price + ' + CNT.sv.nc + ' close matches up to $50k (Penske, Ryder, CarStory, Craigslist, GovDeals), updated ' + catUpdated('sv') + '.' : '') +
  (CNT.tv.n + CNT.tv.cfp + CNT.tv.nc ? ' Transit Vans: ' + CNT.tv.n + ' priced + ' + CNT.tv.cfp + ' call-for-price AWD/4WD cargo vans up to $50k (CarStory + Cars.com; drivetrain checked by NHTSA VIN decode)' + (CNT.tv.nc ? ' + ' + CNT.tv.nc + ' close matches' : '') + ', updated ' + catUpdated('tv') + '.' : '') +
  ' Ford E-Transit (AWD/4x4 conversion): ' + CNT.et.n + ' matches' + (CNT.et.nc ? ' + ' + CNT.et.nc + ' close matches' : '') + ' (aftermarket AWD/4x4 conversions up to $50k; Ford builds the E-Transit RWD only), updated ' + catUpdated('et') + '.' +
  (CNT.rv.n + CNT.rv.cfp + CNT.rv.nc ? ' Rivian Vans: ' + CNT.rv.n + ' priced + ' + CNT.rv.cfp + ' call-for-price Rivian 500 / 700 commercial vans, any price (dealer sites, TruckPaper, Commercial Truck Trader, KBB/Autotrader, CarGurus, Craigslist, Copart/IAA auctions; 500 vs 700 from NHTSA VIN decode where a VIN is shown)' + (CNT.rv.nc ? ' + ' + CNT.rv.nc + ' close matches' : '') + ', updated ' + catUpdated('rv') + '. Rivian vans are listed here only (not under Step vans).' : '') +
  ' Distances are straight-line miles from the 07004 zip centroid. Recalls were only checked for BrightDrop vans. ' +
  'Discovery source stays Unknown unless the inventory file records where a listing was first found. Listing site is the current link only.';

/* ---------- filter controls ---------- */
[5000,10000,15000,20000,25000,30000,32000,34000,35000,36000,38000,40000,45000,50000,60000,70000,80000,100000].forEach(function(v){ $('maxp').insertAdjacentHTML('beforeend','<option value="'+v+'">'+money(v)+'</option>'); });
[5000,10000,15000,20000,25000,30000,35000,40000,45000].forEach(function(v){ $('minp').insertAdjacentHTML('beforeend','<option value="'+v+'">'+money(v)+'+</option>'); });
[5000,10000,15000,20000,25000,30000,40000,50000,75000,100000,150000,200000].forEach(function(v){ $('maxm').insertAdjacentHTML('beforeend','<option value="'+v+'">'+Number(v).toLocaleString('en-US')+' mi</option>'); });
[1000,5000,10000,25000,50000,100000].forEach(function(v){ $('minm').insertAdjacentHTML('beforeend','<option value="'+v+'">'+Number(v).toLocaleString('en-US')+'+ mi</option>'); });
[25,50,100,150,200,300,500,750,1000,1500].forEach(function(v){ $('maxd').insertAdjacentHTML('beforeend','<option value="'+v+'">Within '+v.toLocaleString('en-US')+' mi</option>'); });
/* multi-select filters: key -> value getter (unknowns grouped as 'Not listed') */
function lenKey(r){
  if (r.c === 'tu') return r.cb || 'Cab/bed not listed';
  if (r.c === 'b4') return '400 (shorter body)';
  if (r.c === 'bd') return /\b400\b/.test(r.mdl || '') ? '400 (shorter body)' : '600';
  var m = /(\d+)\s*ft/.exec(r.bl || ''); if (!m) return 'Length not listed';
  var n = +m[1]; return n <= 12 ? '12 ft or less' : n <= 16 ? '13–16 ft' : n <= 20 ? '17–20 ft' : '21 ft or more';
}
var MSK = {mk: function(r){ return r.mk || 'Not listed'; }, mdl: function(r){ return r.mdl || 'Not listed'; }, tr: function(r){ return r.tr || 'Not listed'; },
  cond: function(r){ return r.cond === 'certified' ? 'Certified pre-owned' : r.cond === 'new' ? 'New' : 'Used'; },
  dt: function(r){ return String(r.dt || '').replace(/\s*\(per listing[^)]*\)\s*$/, '') || 'Not listed'; },
  fu: function(r){ var f = String(r.fu || '').replace(/\s*\(per listing\)\s*$/, ''); return f ? f.charAt(0).toUpperCase() + f.slice(1) : 'Not listed'; },
  bt: function(r){ var b = r.bt || (r.cm ? 'Body not confirmed' : 'Not listed'); return b.charAt(0).toUpperCase() + b.slice(1); },
  bk: function(r){ return r.bk || (r.c === 'bd' || r.c === 'b4' || r.c === 'au' ? 'Unknown' : 'n/a (not a BrightDrop)'); },
  bl: lenKey,
  st: function(r){ return r.st || 'Unknown'; },
  sel: function(r){ return r.sel || 'Unknown'; },
  dsc: function(r){ return r.dsc || 'Unknown'; },   // recorded discovery only; never r.lhs or the listing URL
  lhs: function(r){ return r.lhs || 'Unknown'; },
  sty: function(r){ return r.sty ? r.sty.charAt(0).toUpperCase() + r.sty.slice(1) : 'Not recorded'; }};
var MSKEYS = Object.keys(MSK);
var STNAME = {};
ROWS.forEach(function(r){ if (r.st && r.stn) STNAME[r.st] = r.stn; });
var FLAB = {st:'State', sel:'Dealer', dsc:'Discovered', lhs:'Listing site', mk:'Make', mdl:'Model', tr:'Trim', cond:'Condition',
  bk:'Battery', dt:'Drivetrain', fu:'Fuel', bt:'Body', bl:'Length', sty:'Seller type'};
function msLabel(k, v){
  if (k === 'st' && v && v !== 'Unknown') return STNAME[v] ? v + ' · ' + STNAME[v] : v;
  return v;
}
function isBlankOpt(v){ return v === 'Unknown' || v === 'Not listed' || v === 'Not recorded' || /not listed$/i.test(v); }
$('status').innerHTML = '<option value="">Any status</option>' + STATUSES.map(function(s){ return '<option>'+s+'</option>'; }).join('') +
  '<option value="_active">Hide passed</option><option value="_notes">Has my notes/quote</option>' + (Object.keys(NEW).length ? '<option value="_new">New since last refresh</option>' : '');

var SORTS = [['v','Best value (price + miles)','Best value'],['p','Price: low → high','Price ↑'],['-p','Price: high → low','Price ↓'],
  ['mi','Miles: low → high','Miles ↑'],['-mi','Miles: high → low','Miles ↓'],['-y','Year: newest','Year: newest'],['y','Year: oldest','Year: oldest'],
  ['d','Distance: nearest first','Nearest'],['n','Newest listed / price drop','New / price drop'],['q','My quote: low → high','My quote ↑']];
var NARROW = window.innerWidth < 600;   // short option labels so the selected sort fits on phones
var SORTV = 2;  // bump when the default sort changes, so old saved defaults don't hide the new one
function defaults(){ return {q:'', maxp:'', minp:'', maxm:'', minm:'', ymin:'', ymax:'', maxd:'', status:'', sort:'v', sort2:'p', fav:false, close:false, cfp:false, nu:false, rec:'', rdet:false,
  mk:[], mdl:[], tr:[], cond:[], bk:[], dt:[], fu:[], bt:[], bl:[], st:[], sel:[], dsc:[], lhs:[], sty:[]}; }
['sort','sort2'].forEach(function(id){ $(id).innerHTML = SORTS.map(function(o){ return '<option value="'+o[0]+'" title="'+o[1]+'">'+(NARROW ? o[2] : o[1])+'</option>'; }).join(''); });
function okSort(s){ return SORTS.some(function(o){ return o[0] === s; }); }
/* Each category keeps its own filters + sort (bd600finder.ui.v1: f = BrightDrop set as before, fs = all sets, cat = last tab) */
function fromSaved(saved){
  var f = defaults(); saved = saved || {};
  Object.keys(f).forEach(function(k){ if (k !== 'q' && saved[k] != null && Array.isArray(f[k]) === Array.isArray(saved[k])) f[k] = saved[k]; });
  if (saved.state && !saved.st) f.st = [saved.state];             // old single-select State
  if (saved.year && !saved.ymin && !saved.ymax) f.ymin = f.ymax = String(saved.year);   // old single Year
  if (Array.isArray(f.st)) f.st = f.st.map(function(v){ return v === 'Not listed' ? 'Unknown' : v; });
  if (UI.sv !== SORTV) { f.sort = 'v'; f.sort2 = 'p'; }   // first load after the 'Best value' update
  if (!okSort(f.sort)) f.sort = 'v'; if (!okSort(f.sort2)) f.sort2 = 'p';
  return f;
}
var FS = {}, UFS = UI.fs || {};
['bd', 'b4', 'tu', 'sv', 'tv', 'et', 'rv', 'au', 'all'].forEach(function(c){ FS[c] = fromSaved(c === 'bd' ? (UFS.bd || UI.f) : UFS[c]); });
var CAT = CATS[UI.cat] ? UI.cat : 'bd';
var F = FS[CAT];
function inCat(r){ return CAT === 'all' || r.c === CAT; }
var view = UI.view || (window.innerWidth >= 1000 ? 'table' : 'cards');

function baseRow(r){ return inCat(r) && (F.close || !r.cm) && (F.cfp || !r.cfp) && (F.nu || !r.nu); }
function passScalars(r){
  var mp = +F.maxp || 0, mnp = +F.minp || 0, mm = +F.maxm || 0, mnm = +F.minm || 0, y1 = +F.ymin || 0, y2 = +F.ymax || 0, md = +F.maxd || 0;
  if (mp && r.p != null && r.p > mp) return false;
  if (mnp && r.p != null && r.p < mnp) return false;
  if (mm && r.mi != null && r.mi > mm) return false;
  if (mnm && r.mi != null && r.mi < mnm) return false;
  if (y1 && r.y && r.y < y1) return false;
  if (y2 && r.y && r.y > y2) return false;
  if (md && r.d != null && r.d > md) return false;
  return true;
}
function inScope(r){ return inCat(r) && (F.close || !r.cm); }   // new stock and call-for-price stay selectable before their groups are opened
function facetPass(r, except){
  if (!inScope(r) || !passScalars(r)) return false;
  for (var i = 0; i < MSKEYS.length; i++){
    var k = MSKEYS[i];
    if (k === except) continue;
    if (F[k] && F[k].length && F[k].indexOf(MSK[k](r)) < 0) return false;
  }
  return true;
}
function fillMS(){   // options = values still available with the other filters applied; a chosen state narrows dealers
  var cnt = {}; MSKEYS.forEach(function(k){ cnt[k] = {}; });
  ROWS.forEach(function(r){
    MSKEYS.forEach(function(k){ if (!facetPass(r, k)) return; var v = MSK[k](r); cnt[k][v] = (cnt[k][v] || 0) + 1; });
  });
  MSKEYS.forEach(function(k){
    var c = cnt[k], sel = F[k] || [], d = $('ms-' + k), opts = Object.keys(c);
    if (!d) return;
    var prev = d.querySelector('[data-msq]'); var keepQ = prev ? prev.value : '';
    sel.forEach(function(v){ if (opts.indexOf(v) < 0) opts.push(v); });
    var alpha = k === 'st' || k === 'sel' || k === 'dsc' || k === 'lhs' || k === 'tr' || k === 'mdl' || k === 'mk';
    opts.sort(function(a, b){ var na = isBlankOpt(a), nb = isBlankOpt(b); return na - nb || (alpha ? a.localeCompare(b) : (c[b] || 0) - (c[a] || 0) || a.localeCompare(b)); });
    var search = (k === 'st' || k === 'sel' || k === 'dsc' || k === 'lhs') && opts.length > 8
      ? '<input class="msq" type="search" data-msq="' + k + '" placeholder="Type to narrow" aria-label="Narrow ' + esc(FLAB[k] || k) + ' options" autocomplete="off" value="' + esc(keepQ) + '">' : '';
    d.querySelector('.msb').innerHTML = search + opts.map(function(v){
      var hide = keepQ && msLabel(k, v).toLowerCase().indexOf(keepQ.toLowerCase()) < 0;
      return '<label' + (c[v] ? '' : ' class="z"') + (hide ? ' hidden' : '') + '><input type="checkbox" data-ms="' + k + '" value="' + esc(v) + '"' + (sel.indexOf(v) >= 0 ? ' checked' : '') + '> ' + esc(msLabel(k, v)) + '<span class="c">' + (c[v] || 0) + '</span></label>';
    }).join('') + (sel.length ? '<button type="button" class="btn ghost msx" data-msx="' + k + '">Clear</button>' : '');
    var sm = d.querySelector('summary'); sm.classList.toggle('on', sel.length > 0);
    d.querySelector('.msv').textContent = !sel.length ? 'Any' : sel.length === 1 ? msLabel(k, sel[0]) : sel.length + ' selected';
  });
  var ys = {}; ROWS.forEach(function(r){ if (inCat(r) && r.y) ys[r.y] = 1; });
  var yl = Object.keys(ys).map(Number).sort(function(a, b){ return b - a; });
  [F.ymin, F.ymax].forEach(function(y){ if (y && yl.indexOf(+y) < 0) yl.push(+y); });
  $('ymin').innerHTML = '<option value="">Any</option>' + yl.map(function(y){ return '<option value="' + y + '">' + y + '</option>'; }).join('');
  $('ymax').innerHTML = $('ymin').innerHTML;
  $('ymin').value = F.ymin; $('ymax').value = F.ymax;
}
function syncControls(){
  $('maxp').value=F.maxp; $('minp').value=F.minp; $('maxm').value=F.maxm; $('minm').value=F.minm; $('maxd').value=F.maxd; $('status').value=F.status; $('sort').value=F.sort; $('sort2').value=F.sort2;
  document.querySelectorAll('#chips .chip').forEach(function(c){ c.setAttribute('aria-pressed', String(String(F[c.getAttribute('data-chip')]) === c.getAttribute('data-val'))); });
  if ($('status').value !== F.status) { F.status=''; $('status').value=''; }
  $('fav').checked=F.fav; $('close').checked=F.close; $('cfp').checked=F.cfp; $('nu').checked=!!F.nu; $('nutog').hidden = !CNT[CAT].nu && !F.nu; $('nutog').classList.toggle('on', !!F.nu); $('rec').value=F.rec; $('rdet').checked=F.rdet;
  $('cfptog').hidden = !CNT[CAT].cfp && !F.cfp; $('closetog').hidden = !CNT[CAT].nc && !F.close;
  if ($('rec').value !== F.rec) { F.rec=''; $('rec').value=''; }
  $('favtog').classList.toggle('on', F.fav); $('closetog').classList.toggle('on', F.close); $('cfptog').classList.toggle('on', F.cfp); $('rdettog').classList.toggle('on', F.rdet);
  fillMS(); stats();
  $('vw').value = VW; $('vwp').textContent = VW + '%'; $('vwm').textContent = (100 - VW) + '%'; $('vwrow').title = vwTitle();
  var n = ['maxp','minp','maxm','minm','ymin','ymax','maxd','status','rec'].filter(function(k){ return F[k]; }).length + (F.fav?1:0) + (F.close?1:0) + (F.cfp?1:0) + (F.nu?1:0) +
    MSKEYS.filter(function(k){ return F[k] && F[k].length; }).length;
  $('fcnt').hidden = !n; $('fcnt').textContent = n;
  $('reset').hidden = !(n || F.q || F.sort !== 'v' || F.sort2 !== 'p');
  renderActive(n);
  Array.prototype.forEach.call(document.querySelectorAll('[data-view]'), function(b){ b.classList.toggle('on', b.getAttribute('data-view') === view); });
}

function renderActive(n){
  var bits = [];
  MSKEYS.forEach(function(k){ (F[k] || []).forEach(function(v){ bits.push({k:k, v:v, t:(FLAB[k] || k) + ': ' + msLabel(k, v)}); }); });
  [['maxp','Max price'],['minp','Min price'],['maxm','Max miles'],['minm','Min miles'],['ymin','Year from'],['ymax','Year to'],['maxd','Distance'],['status','Status'],['rec','Recalls']].forEach(function(pair){
    if (F[pair[0]]) bits.push({k:pair[0], v:F[pair[0]], t:pair[1]});
  });
  if (F.q) bits.push({k:'q', v:F.q, t:'Search: ' + F.q});
  if (F.fav) bits.push({k:'fav', t:'Favorites'});
  if (F.close) bits.push({k:'close', t:'Close matches'});
  if (F.cfp) bits.push({k:'cfp', t:'Call for price'});
  if (F.nu) bits.push({k:'nu', t:'NEW stock'});
  var el = $('asum');
  if (!bits.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<span class="n">' + bits.length + ' active</span>' + bits.map(function(b){
    return '<button type="button" class="achip" data-clear="' + esc(b.k) + '"' + (b.v != null ? ' data-val="' + esc(b.v) + '"' : '') + ' aria-label="Remove ' + esc(b.t) + '">' + esc(b.t) + '<span class="x" aria-hidden="true">×</span></button>';
  }).join('') + '<button type="button" class="btn ghost clr" data-clear="*">Clear all</button>';
}
function filtered(){
  var terms = F.q.toLowerCase().split(/\s+/).filter(Boolean);
  var msk = MSKEYS.filter(function(k){ return F[k] && F[k].length; }), cf = [], nl = []; CFH = 0; NUH = 0;
  var out = ROWS.filter(function(r){
    if (!inCat(r)) return false;
    if (r.cm && !F.close) return false;
    if (!passScalars(r)) return false;
    for (var j = 0; j < msk.length; j++) if (F[msk[j]].indexOf(MSK[msk[j]](r)) < 0) return false;
    // a confirmed per-VIN NHTSA result (r.vc) overrides the model-year 'may apply' status
    if (F.rec === 'open' && isOpenR(r)) return false;
    if (F.rec === 'any' && (r.vc ? r.vc.o > 0 : r.rc && r.rs !== 'remedied')) return false;
    if (F.rec === 'only' && !(r.vc ? r.vc.o > 0 : r.rc)) return false;
    if (F.rec === 'clear' && !(r.vc && r.vc.o === 0)) return false;
    var u = U(r.v);
    if (F.fav && !u.f) return false;
    if (F.status) {
      var s = u.s || 'Not called';
      if (F.status === '_active') { if (s === 'Passed') return false; }
      else if (F.status === '_notes') { if (!u.n && !u.q) return false; }
      else if (F.status === '_new') { if (!NEW[r.v]) return false; }
      else if (s !== F.status) return false;
    }
    if (terms.length) {
      var h = r._h + ' ' + (u.n || '').toLowerCase() + ' ' + (u.s || '').toLowerCase();
      for (var i = 0; i < terms.length; i++) if (h.indexOf(terms[i]) < 0) return false;
    }
    if (r.nu) { if (F.nu) nl.push(r); else NUH++; return false; }   // NEW (unused) stock: own group, never mixed with used matches
    if (r.cfp) { if (F.cfp) cf.push(r); else CFH++; return false; }   // call for price: own group, never in the main list
    return true;
  });
  valueScores(out);
  var c1 = cmpKey(F.sort), c2 = F.sort2 && F.sort2 !== F.sort ? cmpKey(F.sort2) : null;
  var cv = cmpKey('v'), cp = cmpKey('p'), cmi = cmpKey('mi');
  var cau = function(a,b){ return CAT === 'au' ? ((a.ce || '9999') < (b.ce || '9999') ? -1 : (a.ce || '9999') > (b.ce || '9999') ? 1 : 0) : 0; };   // auctions: soonest close first
  var cmp = function(a,b){ return cau(a,b) || c1(a,b) || (c2 ? c2(a,b) : 0) || cv(a,b) || cp(a,b) || cmi(a,b) || (a.v < b.v ? -1 : a.v > b.v ? 1 : 0); };
  out.sort(cmp); cf.sort(cmp); nl.sort(cmp); CFL = cf; NUL = nl;
  return out;
}
var CFL = [], CFH = 0, NUL = [], NUH = 0;   // NUL/NUH = NEW (unused) rows shown / hidden   // call-for-price rows shown (separate group) / hidden count
/* Best value: w x percentile rank of price + (1-w) x percentile rank of miles (w = 'Best value' slider, default 60%), within the shown set (lower = better).
   Vans missing price or miles get no score and sort last. VS = vin -> score, VR = vin -> rank (1 = best). */
var VS = {}, VR = {}, VN = 0, VNC = {};
function pctRank(vals){   // value -> percentile rank in [0,1], ties share the mid rank
  var s = vals.slice().sort(function(a,b){ return a-b; }), n = s.length, m = {};
  for (var i = 0; i < n; ) { var j = i; while (j + 1 < n && s[j+1] === s[i]) j++; m[s[i]] = n > 1 ? ((i + j) / 2) / (n - 1) : 0; i = j + 1; }
  return m;
}
function valueScores(list){   // ranked within each category (the All tab never compares a Tundra with a van)
  VS = {}; VR = {}; VN = 0; VNC = {};
  var g = {}; list.forEach(function(r){ (g[r.c] || (g[r.c] = [])).push(r); });
  Object.keys(g).forEach(function(c){ valueGroup(g[c]); VNC[c] = VN; });
}
function valueGroup(list){
  var sc = list.filter(function(r){ return r.p != null && r.mi != null; });
  var pp = pctRank(sc.map(function(r){ return r.p; })), pm = pctRank(sc.map(function(r){ return r.mi; }));
  var wp = VW / 100, wm = 1 - wp;
  sc.forEach(function(r){ VS[r.v] = Math.round((wp * pp[r.p] + wm * pm[r.mi]) * 1e9) / 1e9; });   // rounded so equal scores tie exactly -> 'Then by' decides
  sc.sort(function(a,b){ return VS[a.v] - VS[b.v] || a.p - b.p || a.mi - b.mi || (a.v < b.v ? -1 : 1); });
  sc.forEach(function(r, i){ VR[r.v] = i + 1; });
  VN = sc.length;
}
function sval(r, k){
  if (k === 'v') return VS[r.v] == null ? null : VS[r.v];
  if (k === 'q') return +U(r.v).q || null;
  if (k === 'n') return -(r._rn || 0);
  return r[k];
}
function cmpKey(s){   // comparator for one sort key; unknown values always last regardless of direction
  var k = s.replace(/^-/, ''), d = s[0] === '-' ? -1 : 1;
  return function(a, b){
    var x = sval(a, k), y = sval(b, k);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (x - y) * d;
  };
}
function topValue(r){ return VNC[r.c] > 1 && VR[r.v] && VR[r.v] <= 10; }
function vwTitle(){ return 'Best value among the ' + (CAT === 'all' ? 'listings of the same category' : CATS[CAT].u[1]) + ' shown: price ' + VW + '% + miles ' + (100 - VW) + '%'; }

/* ---------- rendering ---------- */
function badges(r){
  var h = '';
  if (r.vk === 'warn') h += '<span class="b b-warn" title="'+esc(r.ver)+'">⚠ Possibly sold – confirm</span>';
  else h += '<span class="b b-ok" title="'+esc(r.ver)+'">✓ ' + (r.vk === 'dealer' ? 'Verified on dealer site' : 'Verified') + '</span>';
  if (r.cm) h += '<span class="b b-close">Close match: ' + esc(r.why) + '</span>';
  if (r.nu) h = '<span class="b b-unused" title="Brand-new dealer stock (not used)">NEW · unused</span>' + h;
  if (CAT === 'all') h = '<span class="b b-cat">' + CATS[r.c].n + '</span>' + h;
  if (topValue(r)) h += '<span class="b b-tv" title="' + vwTitle() + '">Top value #' + VR[r.v] + '</span>';
  if (NEW[r.v]) h += '<span class="b b-new" title="New since last refresh">Just added</span>';
  if (PC[r.v]) h += '<span class="b b-pc'+(PC[r.v]>0?' up':'')+'">Price ' + (PC[r.v] < 0 ? '↓ ' : '↑ ') + money(Math.abs(PC[r.v])) + '</span>';
  return h;
}
/* ---------- recalls ---------- */
var RSTAT = {'clear':'Not open on this VIN (NHTSA VIN check)', 'open':'Open – not fixed yet', 'remedied':'Fixed (remedied)', 'may apply':'May apply', 'unknown':'Applies to this model year; check whether it’s been fixed', 'none listed':'No recalls listed'};
function qBadge(r){
  var q = queued(r); if (!q) return '';
  return q.st === 'failed' ? '<span class="b b-rq" title="Saved on this device only – open the recall box and tap Retry">⚠ Recall link not sent – retry</span>'
    : '<span class="b b-rq" title="NHTSA link received – the recall status updates once it has been read">⏳ Recall check queued – updating soon</span>';
}
function recBadge(r){
  if (r.vc) {   // confirmed per-VIN result overrides the model-year badge
    var t = 'NHTSA VIN check ' + fdate(r.vc.d) + ' · source: ' + r.vc.s;
    if (!r.vc.o) return '<button type="button" class="b b-rok" data-act="rec" title="' + esc(t) + ': 0 unrepaired recalls">✓ Recall-free (NHTSA, ' + fdate(r.vc.d) + ')</button>' + qBadge(r);
    return (r.vc.dnd ? '<span class="b b-dnd" title="Recall says do not drive / park outside">⛔ Do not drive</span>' : '') +
      '<button type="button" class="b b-rc open" data-act="rec" title="' + esc(t + (r.vc.c ? ': ' + r.vc.c : '')) + '">⚠ ' + r.vc.o + ' open recall' + (r.vc.o === 1 ? '' : 's') + '</button>' + qBadge(r);
  }
  if (!REC) return qBadge(r);
  return recBadgeMY(r) + qBadge(r);
}
function recBadgeMY(r){   // model-year recall badge (no per-VIN result)
  var h = r.dnd ? '<span class="b b-dnd" title="Recall says do not drive / park outside">⛔ Do not drive</span>' : '';
  if (!r.rc) return h + '<button type="button" class="b b-rc0" data-act="rec" title="NHTSA lists no recalls for the ' + r.y + ' model">No recalls listed</button>';
  return h + '<button type="button" class="b b-rc' + (r.rs === 'open' ? ' open' : '') + '" data-act="rec" aria-expanded="' + (!!F.rdet) + '" title="' + esc(r.rk.map(function(c){ return RC[c] ? RC[c].label : c; }).join('; ')) + '">⚠ ' +
    r.rc + ' recall' + (r.rc === 1 ? '' : 's') + (r.rs === 'open' ? ' open' : r.rs === 'remedied' ? ' (fixed)' : r.rs === 'may apply' ? ' – may apply' : '') + '</button>';
}
function fdate(d){ var p = String(d||'').split('-'); var m = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+p[1]-1]; return m ? m + ' ' + (+p[2]) + ', ' + p[0] : esc(d); }
function vcInfo(r){   // confirmed per-VIN result + queued link, shown above the model-year recall list
  var h = '', x = r.vc, q = queued(r);
  if (x) h += '<p class="rcv ' + (x.o ? 'bad' : 'ok') + '"><b>' + (x.o ? '⚠ ' + x.o + ' open recall' + (x.o === 1 ? '' : 's') + ' on this VIN' : '✓ Recall-free: 0 unrepaired recalls on this VIN') +
    '</b><br>Source: ' + esc(x.s) + ' · checked ' + fdate(x.d) + ' · <a href="' + esc(x.u) + '" target="_blank" rel="noopener noreferrer">NHTSA page</a>' +
    (x.o && x.c ? '<br>' + esc(x.c) : '') + '</p>';
  if (q) h += '<p class="rcn">' + (q.st === 'failed' ? 'A pasted NHTSA link is saved on this device but not sent yet (use Retry in the Recall check box): '
    : 'NHTSA link queued' + (q.ts ? ' ' + esc(q.ts) : '') + ' – the status updates once it has been read: ') +
    '<a href="' + esc(q.u) + '" target="_blank" rel="noopener noreferrer">' + esc(q.u.length > 70 ? q.u.slice(0, 67) + '…' : q.u) + '</a></p>';
  return h;
}
function campSt(r, c, scoped){
  if (r.vc) return !r.vc.o ? 'clear' : (r.vc.k.length ? (r.vc.k.indexOf(c) >= 0 ? 'open' : 'clear') : 'open');
  return r.rs === 'open' || r.rs === 'remedied' ? r.rs : (scoped ? 'may apply' : 'unknown');
}
function recItems(r){
  if (!REC) return vcInfo(r);
  if (!r.rc) return vcInfo(r) + '<p class="rcn">NHTSA lists no safety recalls for the ' + r.y + ' ' + esc(r.mdl) + ' (checked ' + esc(REC.fetched) + '). ' +
    '<a href="' + esc(r.rsu) + '" target="_blank" rel="noopener noreferrer">Source</a></p>';
  var pv = (REC.perVin && REC.perVin.ok) || r.vc;
  return vcInfo(r) + (r.vc ? '<p class="rcn">Recalls NHTSA lists for the ' + r.y + ' model year:</p>' : '') + (pv ? '' : '<p class="rcn">Status for this exact VIN could not be checked (NHTSA/GM VIN lookups block scripts). Ask the seller for a GM recall printout, or check this VIN at ' +
      '<a href="' + nhtsaUrl(r.v) + '" target="_blank" rel="noopener noreferrer">nhtsa.gov/recalls</a> and paste the page link in the Recall check box.</p>') +
    r.rk.map(function(c){
      var x = RC[c]; if (!x) return '';
      var st = campSt(r, c, !!x.scope);
      return '<div class="rci"><h4>' + esc(x.label) + ' <small>· NHTSA ' + esc(x.short) + ' · ' + fdate(x.date) + ' · ' + esc(String(x.component||'').toLowerCase().split(':').join(' › ')) + '</small></h4>' +
        '<p><span class="rst ' + st.replace(' ', '-') + '">' + esc(RSTAT[st] || st) + '</span>' + (x.parkIt || x.parkOutSide ? ' <span class="b b-dnd">⛔ ' + (x.parkIt ? 'Do not drive' : '') + (x.parkIt && x.parkOutSide ? ' · ' : '') + (x.parkOutSide ? 'Park outside' : '') + '</span>' : '') + '</p>' +
        '<p><span class="k">What it is:</span> ' + esc(x.what || x.summary) + ' ' + esc(x.consequence) + '</p>' +
        (x.scope ? '<p><span class="k">Which vans:</span> ' + esc(x.scope) + (st === 'may apply' ? ' We can’t tell from the VIN whether this van is one of them.' : '') + '</p>' : '') +
        '<p><span class="k">Remedy:</span> ' + esc(x.remedy) + '</p>' +
        (x.interim ? '<p><span class="k">Until fixed:</span> ' + esc(x.interim) + '</p>' : '') +
        '<p><a href="' + esc(x.page) + '" target="_blank" rel="noopener noreferrer">NHTSA recall page</a>' + (x.doc ? ' · <a href="' + esc(x.doc) + '" target="_blank" rel="noopener noreferrer">Recall report (PDF)</a>' : '') + '</p></div>';
    }).join('');
}
function recDetails(r){
  if (!REC && !r.vc) return '';
  var sum = r.vc ? (r.vc.o ? 'Recall details (' + r.vc.o + ' open on this VIN)' : 'Recalls: none open on this VIN (NHTSA)') : (r.rc ? 'Recall details (' + r.rc + ')' : 'Recalls: none listed');
  return '<details class="vn rcd' + (r.vc ? (r.vc.o ? '' : ' none') : (r.rc ? '' : ' none')) + '"' + (F.rdet ? ' open' : '') + '><summary>' + sum + '</summary>' + recItems(r) + '</details>';
}
function rbStatus(r){
  var q = queued(r), x = r.vc;
  if (q && q.st === 'failed') return '<span class="rbs bad">Link saved on this device – not sent yet</span>';
  if (q) return '<span class="rbs q">⏳ Recall check queued – updating soon</span>';
  if (x) return '<span class="rbs ' + (x.o ? 'bad' : 'ok') + '">' + (x.o ? '⚠ ' + x.o + ' open recall' + (x.o === 1 ? '' : 's') : '✓ Recall-free') +
    ' · NHTSA ' + fdate(x.d) + ' · ' + esc(x.s) + '</span>';
  return '<span class="rbs">Not checked for this VIN yet</span>';
}
function rbMsg(r){
  var q = RQ[r.v];
  if (q && q.st === 'failed') return '<div class="rbm err">' + esc(q.e || 'Not sent') + ' <button class="btn" type="button" data-act="rretry">Retry</button></div>';
  return '<div class="rbm"></div>';
}
function rbox(r){   // 'Recall check' box: paste the NHTSA recall page link for this VIN
  return '<div class="rbox"><div class="rbh"><b>Recall check</b>' + rbStatus(r) + '</div>' +
    (r.vc || queued(r) ? '' : '<div class="rbt">Tap “Open NHTSA”, copy that page’s link, paste it here and Save.</div>') +
    '<input type="url" data-rf="url" inputmode="url" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="done" placeholder="Paste NHTSA recall page link" aria-label="Paste NHTSA recall page link for VIN ' + r.v + '">' +
    '<div class="rbb"><a class="btn" data-act="nhtsa" href="' + nhtsaUrl(r.v) + '" target="_blank" rel="noopener noreferrer">Open NHTSA for this VIN</a>' +
    '<button class="btn primary" type="button" data-act="rsave">Save</button></div>' + rbMsg(r) + '</div>';
}
function statusSel(u){
  return '<select data-f="s" aria-label="My status">' + STATUSES.map(function(s){ return '<option'+((u.s||'Not called')===s?' selected':'')+'>'+s+'</option>'; }).join('') + '</select>';
}
function mine(r, u){
  return '<div class="mine">' + statusSel(u) +
    '<label class="qwrap"><span>$</span><input data-f="q" type="text" inputmode="numeric" placeholder="Quoted price" aria-label="Quoted price" value="'+(u.q?Number(u.q).toLocaleString('en-US'):'')+'"></label>' +
    '<textarea data-f="n" rows="2" placeholder="My notes (who I spoke to, fees, condition…)" aria-label="My notes">'+esc(u.n||'')+'</textarea></div>';
}
/* ---------- Toyota Tundra / step-van cards (no recall box: recalls not checked for these) ---------- */
function cap(s){ s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
function condLabel(r){ return r.cond === 'certified' ? 'Certified pre-owned' : r.cond === 'new' ? 'New' : 'Used'; }
function flagCls(f){ return /project|not run/i.test(f) ? 'b-bad' : 'b-flag'; }
function rncBadge(r){ return '<span class="b b-rnc" title="No NHTSA recall check was run for Tundra / step-van / transit-van listings' + (r.nov ? '' : ' – use the NHTSA link in Listing details') + '">Recalls: not checked</span>'; }
function xBadges(r, tbl){
  var h = CAT === 'all' ? '<span class="b b-cat">' + CATS[r.c].n + '</span>' : '';
  if (r.c === 'tu' || r.c === 'tv') h += r.w4 === 'vpic' ? '<span class="b b-ok" title="Drive type AWD/4WD confirmed by an NHTSA vPIC decode of the VIN">✓ ' + esc(r.dt || 'AWD/4WD') + ' (VIN decode)</span>'
    : '<span class="b b-warn" title="AWD/4WD as stated in the listing (the VIN decode did not give a drive type)">' + esc(r.dt || 'AWD/4WD') + ' per listing</span>';
  if (r.c === 'au') h += auBadge(r);
  if (r.c === 'et' && !r.cm) h += '<span class="b b-warn" title="Ford builds the E-Transit RWD only – this is an aftermarket conversion; verify the work, installer and warranty">Aftermarket AWD/4x4 conversion</span>';
  h += '<span class="b b-cond">' + condLabel(r) + '</span>';
  if (r.cfp) h += '<span class="b b-flag">Call for price</span>';
  (r.fl || []).forEach(function(f){ h += '<span class="b ' + flagCls(f) + '">' + esc(cap(f)) + '</span>'; });
  if (r.cm) h += '<span class="b b-close">Close match: ' + esc(r.why) + '</span>';
  if (!tbl && topValue(r)) h += '<span class="b b-tv" title="' + vwTitle() + '">Top value #' + VR[r.v] + '</span>';
  if (NEW[r.v]) h += '<span class="b b-new" title="New since last refresh">Just added</span>';
  if (PC[r.v]) h += '<span class="b b-pc'+(PC[r.v]>0?' up':'')+'">Price ' + (PC[r.v] < 0 ? '↓ ' : '↑ ') + money(Math.abs(PC[r.v])) + '</span>';
  return h + rncBadge(r);
}
function auWhen(ce){ if (!ce) return 'no sale date yet'; var d = new Date(ce.replace(' ', 'T')); return isNaN(d) ? ce + ' ET' : d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'}) + ', ' + d.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'}) + ' ET'; }
function auBadge(r){
  var soon = r.ce && (new Date(r.ce.replace(' ', 'T')) - Date.now()) < 48 * 3600e3 && r.ls !== 'closed';
  var h = '<span class="b ' + (r.ls === 'closed' ? 'b-bad' : soon ? 'b-warn' : 'b-ok') + '" title="Auction close time (Eastern). Auctions can auto-extend.">' + (r.ls === 'closed' ? 'Closed' : 'Ends ' + esc(auWhen(r.ce))) + '</span>';
  h += '<span class="b b-cond" title="Current high bid – buyer\u2019s premium and fees are extra">Current bid ' + (r.p != null ? money(r.p) : '—') + '</span>';
  if (r.bp) h += '<span class="b b-flag" title="Buyer\u2019s premium / fees">Premium: ' + esc(r.bp.slice(0, 70)) + '</span>';
  if (r.ts) h += '<span class="b ' + (/salvage|wait/i.test(r.ts) ? 'b-bad' : 'b-ok') + '">Title: ' + esc(r.ts) + '</span>';
  if (r.bat) h += '<span class="b b-cond">' + esc(r.bat) + '</span>';
  if (r.lot) h += '<span class="b b-cat">Lot ' + esc(r.lot) + '</span>';
  if (r.pv) h += '<span class="b b-warn" title="The auction shows only the first 11 VIN characters; series, drive and battery decode from those">Partial VIN ' + esc(r.pv) + '</span>';
  return h;
}
function xTitle(r){ return [r.c === 'rv' && r.mk && r.mdl && r.mdl.indexOf(r.mk + ' ') === 0 ? '' : r.mk, r.mdl, r.tr].filter(Boolean).join(' ') || CATS[r.c].n; }
function xSub(r){
  var len = [r.bb, r.bl].filter(Boolean).join(' ');
  return (r.c === 'tu' ? [r.cb, cap(r.fu), r.eng] : [cap(r.bt), len, cap(r.fu), r.gv ? 'GVWR ' + r.gv : '', r.eng]).filter(Boolean).join(' · ');
}
function distTxt(r){ return r.d != null ? Number(r.d).toLocaleString('en-US') + ' mi away' : ''; }
function sellerName(r){ return r.sel || 'Unknown'; }
function sellerTxt(r){ return esc(sellerName(r)) + (r.sty && !/private|auction/i.test(r.sel || '') ? ' (' + esc(r.sty) + ')' : ''); }
function placeTxt(r){ var p = []; if (r.city) p.push(r.city); if (r.st) p.push(r.st); return p.join(', ') || 'Unknown location'; }
function provLine(r){ return 'Discovered: ' + esc(r.dsc || 'Unknown') + ' · Listing site: ' + esc(r.lhs || 'Unknown'); }
function rowAttrs(r){
  return ' data-v="' + esc(r.v) + '" data-c="' + esc(r.c || '') + '" data-sel="' + esc(r.sel || '') + '" data-st="' + esc(r.st || '') + '" data-dsc="' + esc(r.dsc || 'Unknown') + '" data-lhs="' + esc(r.lhs || 'Unknown') + '"' + (r.nu ? ' data-nu="1"' : '') + (r.vk === 'warn' ? ' data-sold="1"' : '');
}
function xDetails(r){
  return '<details class="vn"><summary>Listing details</summary>' +
    '<p>' + provLine(r) + (r.ds ? ' · seen ' + fdate(r.ds) : '') + (r.c === 'tu' || r.c === 'tv' ? ' · ' + (r.w4 === 'vpic' ? (r.dt || 'AWD/4WD') + ' confirmed by NHTSA VIN decode' : (r.dt || 'AWD/4WD') + ' per listing') : '') + '</p>' +
    (r.nt ? '<p>' + esc(r.nt) + '</p>' : '') +
    '<p>Recalls: not checked. ' + (r.nov ? 'No VIN in the listing – ask the seller for it.' : '<a href="' + nhtsaUrl(r.v) + '" target="_blank" rel="noopener noreferrer">Check this VIN on NHTSA</a>') + '</p>' +
    '<p><a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">Original listing</a></p></details>';
}
function cardX(r){
  var u = U(r.v), acts = '';
  if (r.tel) acts += '<a class="btn primary" href="tel:'+esc(r.tel)+'">Call <small>'+esc(r.ph)+'</small></a><a class="btn" href="sms:'+esc(r.tel)+'">Text</a>';
  acts += '<a class="btn'+(r.tel?'':' primary')+'" href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">View listing</a>';
  acts += r.nov ? '' : '<button class="btn" type="button" data-act="copy">Copy VIN</button>';
  if (!r.tel) acts += '<div class="nophone">No phone in listing – use View listing to contact</div>';
  return '<article class="card s-'+slug(u.s)+(r.cm?' is-close':'')+(r.cfp?' is-cfp':'')+'"' + rowAttrs(r) + '>' +
    '<div class="ctop"><div class="price'+(r.p==null?' na':'')+'">'+(r.p==null?'Call for price':money(r.p)) +
      (u.q ? '<span class="quoted">Quoted '+money(u.q)+'</span>' : '') + '</div>' +
      '<button class="star'+(u.f?' on':'')+'" type="button" data-act="star" aria-label="Favorite" aria-pressed="'+(!!u.f)+'">'+(u.f?'★':'☆')+'</button></div>' +
    '<div class="badges">'+xBadges(r)+'</div>' +
    '<div class="specs"><b>'+(r.y||'Year ?')+'</b> · '+miles(r.mi)+' · <span class="dtv" title="Drivetrain">'+esc(r.dt || 'Drivetrain not listed')+'</span></div>' +
    '<div class="ttl">'+esc(xTitle(r))+'</div>' +
    '<div class="bat">'+esc(xSub(r))+'</div>' +
    '<div class="loc"><span class="city">'+esc(placeTxt(r))+'</span>'+(r.d!=null?' <span class="dist">· '+distTxt(r)+'</span>':'')+' · '+sellerTxt(r)+'</div>' +
    '<div class="vin">'+(r.nov ? '<span class="vinfull">VIN not listed</span>' : '<span class="vin6" title="Last 6 of VIN">…'+r.v.slice(-6)+'</span><span class="vinfull">'+r.v+'</span>')+'</div>' +
    '<div class="actions">'+acts+'</div>' + mine(r, u) + xDetails(r) + '</article>';
}
function trowX(r){
  var u = U(r.v);
  var acts = (r.tel ? '<a class="btn primary" href="tel:'+esc(r.tel)+'" title="Call '+esc(r.ph)+'">Call</a><a class="btn" href="sms:'+esc(r.tel)+'">Text</a>' : '') +
    '<a class="btn" href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">View</a>' +
    (r.nov ? '' : '<button class="btn" type="button" data-act="copy">Copy VIN</button>') +
    '<button class="btn" type="button" data-act="exp" aria-expanded="false" title="My notes and quote">Notes'+(u.n?' •':'')+'</button>';
  return '<tr class="r s-'+slug(u.s)+(r.cm?' is-close':'')+'"' + rowAttrs(r) + '>' +
    '<td><button class="star'+(u.f?' on':'')+'" type="button" data-act="star" aria-label="Favorite">'+(u.f?'★':'☆')+'</button></td>' +
    '<td><span class="tp">'+(r.p==null?'Call':money(r.p))+'</span>'+(u.q?'<span class="tq">Quoted '+money(u.q)+'</span>':'') +
      (topValue(r) ? '<br><span class="b b-tv" title="'+vwTitle()+'">Top value #'+VR[r.v]+'</span>' : VR[r.v] ? '<span class="vr" title="Value rank: '+vwTitle()+'">Value #'+VR[r.v]+'</span>' : '')+'</td>' +
    '<td>'+(r.y||'—')+'</td><td class="num">'+(r.mi==null?'—':Number(r.mi).toLocaleString('en-US'))+'</td>' +
    '<td class="bc">'+esc(xTitle(r))+'<span class="sub"><b>'+esc(r.dt || 'drive n/a')+'</b> · '+esc(xSub(r))+'</span></td>' +
    '<td class="lc">'+esc(placeTxt(r))+'<span class="sub">'+sellerTxt(r)+(r.d!=null?' · '+distTxt(r):'')+'</span></td>' +
    '<td>'+(r.nov?'<span class="sub">VIN not listed</span>':'<span class="v6">…'+r.v.slice(-6)+'</span>')+'<span class="sub">'+(r.ph?esc(r.ph):'no phone')+'</span></td>' +
    '<td class="vc">'+xBadges(r, true)+'</td><td>'+statusSel(u)+'</td><td><div class="acts">'+acts+'</div></td></tr>' +
    '<tr class="x" data-v="'+r.v+'" hidden><td colspan="10">'+mine(r,u).replace(/<select[\s\S]*?<\/select>/,'')+xDetails(r).replace('<details class="vn">', '<details class="vn" open>')+'</td></tr>';
}
function card(r){
  if (r.c !== 'bd' && r.c !== 'b4') return cardX(r);
  var u = U(r.v);
  var acts = '';
  if (r.tel) acts += '<a class="btn primary" href="tel:'+esc(r.tel)+'">Call <small>'+esc(r.ph)+'</small></a><a class="btn" href="sms:'+esc(r.tel)+'">Text</a>';
  acts += '<a class="btn'+(r.tel?'':' primary')+'" href="'+esc(link(r))+'" target="_blank" rel="noopener noreferrer">View listing</a>';
  acts += '<button class="btn" type="button" data-act="copy">Copy VIN</button>';
  if (!r.tel) acts += '<div class="nophone">No phone in listing – use View listing to contact</div>';
  return '<article class="card s-'+slug(u.s)+(r.cm?' is-close':'')+(r.nu?' is-nu':'')+'"' + rowAttrs(r) + '>' +
    '<div class="ctop"><div class="price'+(r.p==null?' na':'')+'">'+(r.p==null?'Price not listed':money(r.p)) +
      (u.q ? '<span class="quoted">Quoted '+money(u.q)+'</span>' : '') + '</div>' +
      '<button class="star'+(u.f?' on':'')+'" type="button" data-act="star" aria-label="Favorite" aria-pressed="'+(!!u.f)+'">'+(u.f?'★':'☆')+'</button></div>' +
    '<div class="badges">'+badges(r)+recBadge(r)+'</div>' +
    '<div class="specs"><b>'+r.y+'</b> · '+miles(r.mi)+' · '+esc(r.dt)+' · '+esc(r.mdl)+'</div>' +
    '<div class="bat">'+esc(r.bat)+'</div>' +
    '<div class="loc"><span class="city">'+esc(placeTxt(r))+'</span>'+(r.d!=null?' <span class="dist">· '+distTxt(r)+'</span>':'')+' · '+esc(sellerName(r))+'</div>' +
    '<div class="vin"><span class="vin6" title="Last 6 of VIN">…'+r.v.slice(-6)+'</span><span class="vinfull">'+r.v+'</span></div>' +
    '<div class="actions">'+acts+'</div>' + recDetails(r) + rbox(r) + mine(r, u) +
    '<details class="vn"><summary>Verification details</summary><p>'+esc(r.ver)+'. '+esc(r.vn)+'</p>' +
      '<p>'+provLine(r)+'</p>' +
      '<p><a href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">Aggregator listing</a>' + (r.dlr ? ' · <a href="'+esc(r.dlr)+'" target="_blank" rel="noopener noreferrer">Dealer page</a>' : '') + '</p></details>' +
  '</article>';
}
function trow(r){
  if (r.c !== 'bd' && r.c !== 'b4') return trowX(r);
  var u = U(r.v);
  var acts = (r.tel ? '<a class="btn primary" href="tel:'+esc(r.tel)+'" title="Call '+esc(r.ph)+'">Call</a><a class="btn" href="sms:'+esc(r.tel)+'">Text</a>' : '') +
    '<a class="btn" href="'+esc(link(r))+'" target="_blank" rel="noopener noreferrer">View</a>' +
    '<button class="btn" type="button" data-act="copy">Copy VIN</button>' +
    '<button class="btn" type="button" data-act="exp" aria-expanded="false" title="My notes, quote and the Recall check box">Notes'+(u.n?' •':'')+'</button>';
  return '<tr class="r s-'+slug(u.s)+(r.cm?' is-close':'')+(r.nu?' is-nu':'')+'"' + rowAttrs(r) + '>' +
    '<td><button class="star'+(u.f?' on':'')+'" type="button" data-act="star" aria-label="Favorite">'+(u.f?'★':'☆')+'</button></td>' +
    '<td><span class="tp">'+(r.p==null?'—':money(r.p))+'</span>'+(u.q?'<span class="tq">Quoted '+money(u.q)+'</span>':'') +
      (topValue(r) ? '<br><span class="b b-tv" title="'+vwTitle()+'">Top value #'+VR[r.v]+'</span>' : VR[r.v] ? '<span class="vr" title="Value rank among the vans shown: '+vwTitle()+'">Value #'+VR[r.v]+'</span>' : '')+'</td>' +
    '<td>'+r.y+'</td><td class="num">'+(r.mi==null?'—':Number(r.mi).toLocaleString('en-US'))+'</td>' +
    '<td class="bc">'+esc(r.bs)+'<span class="sub">'+esc(r.dt)+' · '+esc(r.mdl.replace(/^(Chevrolet |BrightDrop )+/,''))+'</span></td>' +
    '<td class="lc">'+esc(placeTxt(r))+'<span class="sub">'+esc(sellerName(r))+'</span></td>' +
    '<td><span class="v6">…'+r.v.slice(-6)+'</span><span class="sub">'+(r.ph?esc(r.ph):'no phone')+'</span></td>' +
    '<td class="vc">'+tvc(r)+'</td><td>'+statusSel(u)+'</td><td><div class="acts">'+acts+'</div></td></tr>' +
    '<tr class="x" data-v="'+r.v+'" hidden><td colspan="10">'+mine(r,u).replace(/<select[\s\S]*?<\/select>/,'')+rbox(r)+
      '<p class="vnp">'+provLine(r)+'<br>'+esc(r.ver)+'. '+esc(r.vn)+' · <a href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">Aggregator listing</a>'+(r.dlr?' · <a href="'+esc(r.dlr)+'" target="_blank" rel="noopener noreferrer">Dealer page</a>':'')+'</p></td></tr>' +
    (REC || r.vc ? '<tr class="rx" data-v="'+r.v+'"'+(F.rdet && (r.rc || r.vc) ? '' : ' hidden')+'><td colspan="10">'+recItems(r)+'</td></tr>' : '');
}
function tvc(r){   // table 'Verified · recalls' cell
  var vb = r.vk === 'warn' ? '<span class="b b-warn">⚠ Possibly sold</span>' : '<span class="b b-ok">✓ '+(r.vk==='dealer'?'Dealer site':'Verified')+'</span>';
  if (r.nu) vb = '<span class="b b-unused" title="Brand-new dealer stock (not used)">NEW · unused</span> ' + vb;
  var extra = (r.cm ? '<br><span class="b b-close">'+esc(r.why)+'</span>' : '') + (NEW[r.v] ? ' <span class="b b-new" title="New since last refresh">Just added</span>' : '') +
    (PC[r.v] ? ' <span class="b b-pc'+(PC[r.v]>0?' up':'')+'">'+(PC[r.v]<0?'↓ ':'↑ ')+money(Math.abs(PC[r.v]))+'</span>' : '');
  var rb = recBadge(r);
  return vb + extra + (rb ? '<br>' + rb : '');
}
function etNote(){   // E-Transit tab: explain an empty list (Ford builds it RWD only) + point at hidden close matches
  if (CAT !== 'et') return '';
  var m = (META.cats || {}).et || {}, h = m.note ? '<b>' + esc(m.note) + '</b><br><br>' : '';
  if (!F.close && CNT.et.nc) h += CNT.et.nc + ' close match' + (CNT.et.nc === 1 ? '' : 'es') + ' hidden (turn on “Include close matches” in Filters).<br><br>';
  return h;
}
var LIMIT_STEP = 120, limit = LIMIT_STEP, climit = LIMIT_STEP, nlimit = LIMIT_STEP;
function render(keepLimit){
  if (!keepLimit) { limit = LIMIT_STEP; climit = LIMIT_STEP; nlimit = LIMIT_STEP; }
  syncControls();
  var list = filtered(), el = $('list'), cf = CFL, nl = NUL, nshown = nl.slice(0, nlimit);
  var shown = list.slice(0, limit), cshown = cf.slice(0, climit), un = CATS[CAT].u;
  var cheapest = null; list.forEach(function(r){ if (r.p != null && (cheapest == null || r.p < cheapest)) cheapest = r.p; });
  var nm = list.filter(function(r){ return !r.cm; }).length;
  $('res').innerHTML = 'Showing <b>' + list.length + '</b> ' + (list.length===1?un[0]:un[1]) +
    (F.close ? ' (' + nm + ' match' + (nm===1?'':'es') + ' + ' + (list.length-nm) + ' close)' : '') +
    (cheapest != null ? ' · from <b>' + money(cheapest) + '</b>' : '') +
    (nl.length ? ' · <b>' + nl.length + '</b> NEW (unused) below' : NUH ? ' · <button class="nulnk" type="button" data-nu="1">' + NUH + ' NEW (unused) hidden – show</button>' : '') +
    (cf.length ? ' · <b>' + cf.length + '</b> call for price below' : CFH ? ' · ' + CFH + ' call-for-price hidden' : '');
  var tbl = view === 'table' && window.innerWidth >= 900;
  el.style.display = tbl ? 'block' : '';
  if (!list.length && !cf.length && !nl.length) { el.innerHTML = '<div class="empty">' + etNote() + 'No ' + un[1] + ' match these filters.' + (NUH ? '<br>' + NUH + ' NEW (unused) dealer-stock unit' + (NUH === 1 ? '' : 's') + ' hidden (turn on “Show NEW (unused) stock”).' : '') + (CFH ? '<br>' + CFH + ' call-for-price listing' + (CFH === 1 ? '' : 's') + ' hidden (turn on “Show call-for-price”).' : '') + '<br><br><button class="btn" type="button" data-act="reset">Clear search & filters</button></div>'; return; }
  function moreBtn(n, act){ return n > 0 ? '<div style="text-align:center;margin-top:14px;grid-column:1/-1"><button class="btn" type="button" data-act="' + act + '">Show ' + Math.min(LIMIT_STEP, n) + ' more (' + n + ' left)</button></div>' : ''; }
  var more = moreBtn(list.length - shown.length, 'more'), cmore = moreBtn(cf.length - cshown.length, 'morec'), nmore = moreBtn(nl.length - nshown.length, 'moren');
  var nmin = null; nl.forEach(function(r){ if (r.p != null && (nmin == null || r.p < nmin)) nmin = r.p; });
  var nuh = nl.length ? '<h2 class="nuh" id="nuh">New (unused) · ' + nl.length + ' unit' + (nl.length === 1 ? '' : 's') + (nmin != null ? ' · from ' + money(nmin) : '') + '<small>Brand-new dealer stock, not used – kept apart from the used matches above (not counted with them, and out of Best value).</small></h2>' : '';
  var cfh = cf.length ? '<h2 class="cfph" id="cfph">Call for price · ' + cf.length + ' listing' + (cf.length === 1 ? '' : 's') + '<small>No advertised price – kept apart from the list above (and out of Best value). Call or check the listing for a price.</small></h2>' : '';
  var nutease = !nl.length && NUH ? '<button class="nuh nutease" type="button" data-act="shownu">New (unused) · ' + NUH + ' brand-new dealer-stock unit' + (NUH === 1 ? '' : 's') + ' hidden<small>Not used, so kept apart from the used matches. Tap to show them below as a separate group.</small></button>' : '';
  var empty = list.length ? '' : '<div class="empty" style="grid-column:1/-1">' + etNote() + 'No ' + (CAT === 'b4' ? 'used ' : 'priced ') + un[1] + ' match these filters.</div>';
  if (tbl) {
    var head = function(){ return '<div class="tscroll"><table class="t' + (F.close ? ' wc' : '') + '"><thead><tr><th></th>' + th('p','Price','') + th('y','Year','') + th('mi','Miles','num') + '<th>' + (CAT === 'bd' || CAT === 'b4' ? 'Battery' : 'Vehicle') + '</th><th>Location / seller</th><th>VIN · phone</th><th>' + (CAT === 'bd' || CAT === 'b4' ? 'Verified · recalls' : 'Details') + '</th><th>My status</th><th></th></tr></thead><tbody>'; };
    el.innerHTML = (list.length ? head() + shown.map(trow).join('') + '</tbody></table></div>' + more : empty) +
      (nl.length ? nuh + head() + nshown.map(trow).join('') + '</tbody></table></div>' + nmore : nutease) +
      (cf.length ? cfh + head() + cshown.map(trow).join('') + '</tbody></table></div>' + cmore : '');
  } else el.innerHTML = (list.length ? shown.map(card).join('') + more : empty) + (nl.length ? nuh + nshown.map(card).join('') + nmore : nutease) + (cf.length ? cfh + cshown.map(card).join('') + cmore : '');
}
function sbase(s){ return String(s || '').replace(/^-/, ''); }
function th(k, label, cls){   // sortable header: arrow + 1 (Sort by) / 2 (Then by)
  var ind = '', t = 'Click: sort by ' + label.toLowerCase() + ' (click again to reverse). Shift-click: then by ' + label.toLowerCase();
  if (sbase(F.sort) === k) ind = '<span class="sa">' + (F.sort[0] === '-' ? '▼' : '▲') + '1</span>';
  else if (F.sort2 !== F.sort && sbase(F.sort2) === k) ind = '<span class="sa">' + (F.sort2[0] === '-' ? '▼' : '▲') + '2</span>';
  return '<th data-sort="' + k + '" class="' + (cls || '') + (ind ? ' sorted' : '') + '" title="' + t + '" aria-sort="' +
    (sbase(F.sort) === k ? (F.sort[0] === '-' ? 'descending' : 'ascending') : 'none') + '">' + label + ind + '</th>';
}
function headerSort(k, shift){
  var first = k === 'y' ? '-y' : k;   // first click: cheapest / fewest miles / newest
  function flip(s){ return s[0] === '-' ? s.slice(1) : '-' + s; }
  if (shift) {
    if (sbase(F.sort) === k) { toast('Already the main sort'); return; }
    F.sort2 = sbase(F.sort2) === k ? flip(F.sort2) : first;
  } else if (sbase(F.sort) === k) F.sort = flip(F.sort);
  else { F.sort2 = F.sort; F.sort = first; }   // previous main sort becomes 'Then by'
  persistUI(); render();
  var lab = function(s){ for (var i = 0; i < SORTS.length; i++) if (SORTS[i][0] === s) return SORTS[i][1]; return s; };
  toast('Sort: ' + lab(F.sort) + (F.sort2 && F.sort2 !== F.sort ? ', then ' + lab(F.sort2) : ''));
}
function persistUI(){
  FS[CAT] = F; UI.fs = {};
  Object.keys(FS).forEach(function(c){ var f = {}; Object.keys(FS[c]).forEach(function(k){ if (k !== 'q') f[k] = FS[c][k]; }); UI.fs[c] = f; });
  UI.f = UI.fs.bd; UI.cat = CAT; UI.view = view; UI.sv = SORTV; save(K.ui, UI);
}
function setCat(c){
  if (!CATS[c] || c === CAT) return;
  var q = F.q; CAT = c; F = FS[c]; F.q = q;
  persistUI(); render(); window.scrollTo(0, 0);
}
$('cats').addEventListener('click', function(e){ var b = e.target.closest('[data-cat]'); if (b) setCat(b.getAttribute('data-cat')); });
/* multi-select filters (advanced panel + the always-visible state / dealer / discovery bar) */
function onMsChange(e){
  var k = e.target.getAttribute && e.target.getAttribute('data-ms'); if (!k) return;
  var a = (F[k] || []).slice(), v = e.target.value, i = a.indexOf(v);
  if (e.target.checked && i < 0) a.push(v); if (!e.target.checked && i >= 0) a.splice(i, 1);
  F[k] = a; persistUI(); render();
  var d = $('ms-' + k); if (d) { d.open = true; d.parentNode.classList.add('open'); }
}
function onMsClick(e){
  var x = e.target.closest('[data-msx]'); if (!x) return;
  var k = x.getAttribute('data-msx'); F[k] = []; persistUI(); render();
}
function onMsType(e){
  var q = e.target.getAttribute && e.target.getAttribute('data-msq'); if (!q) return;
  var needle = e.target.value.toLowerCase(), box = e.target.closest('.msb');
  if (!box) return;
  box.querySelectorAll('label').forEach(function(lb){ lb.hidden = !!(needle && lb.textContent.toLowerCase().indexOf(needle) < 0); });
}
['filters','facets'].forEach(function(id){
  $(id).addEventListener('change', onMsChange);
  $(id).addEventListener('click', onMsClick);
  $(id).addEventListener('input', onMsType);
});
document.querySelectorAll('details.msd').forEach(function(d){
  d.addEventListener('toggle', function(){
    d.parentNode.classList.toggle('open', d.open);
    if (d.open) document.querySelectorAll('details.msd[open]').forEach(function(o){ if (o !== d) o.open = false; });
  });
});
document.addEventListener('click', function(e){   // tap outside closes an open multi-select
  if (!e.target.closest('.f.ms')) document.querySelectorAll('details.msd[open]').forEach(function(o){ o.open = false; });
});
document.addEventListener('keydown', function(e){
  if (e.key !== 'Escape') return;
  document.querySelectorAll('details.msd[open]').forEach(function(o){ o.open = false; });
});

/* ---------- events ---------- */
var qT;
$('q').addEventListener('input', function(){ clearTimeout(qT); var v=this.value; qT = setTimeout(function(){ F.q = v.trim(); render(); }, 120); });
['maxp','minp','maxm','minm','ymin','ymax','maxd','status','sort','sort2'].forEach(function(id){ $(id).addEventListener('change', function(){ F[id] = this.value; persistUI(); render(); }); });
$('fav').addEventListener('change', function(){ F.fav = this.checked; persistUI(); render(); });
$('close').addEventListener('change', function(){ F.close = this.checked; persistUI(); render(); });
$('cfp').addEventListener('change', function(){ F.cfp = this.checked; persistUI(); render(); });
$('nu').addEventListener('change', function(){ F.nu = this.checked; persistUI(); render(); });
$('res').addEventListener('click', function(e){ if (e.target.closest('[data-nu]')) { F.nu = true; persistUI(); render(true); var hh = $('nuh'); if (hh) hh.scrollIntoView({block: 'start'}); } });
$('rec').addEventListener('change', function(){ F.rec = this.value; persistUI(); render(); });
$('rdet').addEventListener('change', function(){ F.rdet = this.checked; persistUI(); render(true); });
var vwT;
$('vw').addEventListener('input', function(){   // Best value weighting: live re-rank (remembered in bd600finder.ui.v1 as vw)
  VW = Math.max(0, Math.min(100, +this.value || 0)); UI.vw = VW;
  $('vwp').textContent = VW + '%'; $('vwm').textContent = (100 - VW) + '%';
  clearTimeout(vwT); vwT = setTimeout(function(){ save(K.ui, UI); render(true); }, 60);
});
$('ftoggle').addEventListener('click', function(){ var c = $('filters').classList.toggle('collapsed'); this.setAttribute('aria-expanded', String(!c)); });
function resetAll(){ F = FS[CAT] = defaults(); $('q').value=''; persistUI(); render(); }
$('chips').addEventListener('click', function(e){
  var c = e.target.closest('.chip'); if (!c) return;
  var k = c.getAttribute('data-chip'), v = c.getAttribute('data-val');
  F[k] = String(F[k]) === v ? '' : v;   // tap again to clear; price and mileage chips combine
  persistUI(); render();
});
$('reset').addEventListener('click', resetAll);
$('asum').addEventListener('click', function(e){
  var b = e.target.closest('[data-clear]'); if (!b) return;
  var k = b.getAttribute('data-clear');
  if (k === '*') { resetAll(); return; }
  if (Array.isArray(F[k])) {
    var v = b.getAttribute('data-val');
    F[k] = (F[k] || []).filter(function(x){ return x !== v; });
  } else if (k === 'fav' || k === 'close' || k === 'cfp' || k === 'nu' || k === 'rdet') F[k] = false;
  else F[k] = '';
  if (k === 'q') $('q').value = '';
  persistUI(); render();
});
document.querySelectorAll('[data-view]').forEach(function(b){ b.addEventListener('click', function(){ view = b.getAttribute('data-view'); persistUI(); render(true); }); });
var lastW = window.innerWidth, rT;
window.addEventListener('resize', function(){ clearTimeout(rT); rT = setTimeout(function(){ var w = window.innerWidth; if ((w>=900) !== (lastW>=900)) render(true); lastW = w; }, 150); });

function rowOf(el){ var x = el.closest('[data-v]'); return x ? x.getAttribute('data-v') : null; }
function byVin(v){ for (var i=0;i<ROWS.length;i++) if (ROWS[i].v===v) return ROWS[i]; }
$('list').addEventListener('click', function(e){
  var b = e.target.closest('[data-act]'); if (!b) { var th = e.target.closest('th[data-sort]'); if (th) headerSort(th.getAttribute('data-sort'), e.shiftKey); return; }
  var act = b.getAttribute('data-act'), v = rowOf(b);
  if (act === 'more') { limit += LIMIT_STEP; render(true); return; }
  if (act === 'morec') { climit += LIMIT_STEP; render(true); return; }
  if (act === 'moren') { nlimit += LIMIT_STEP; render(true); return; }
  if (act === 'shownu') { F.nu = true; persistUI(); render(true); var hh = $('nuh'); if (hh) hh.scrollIntoView({block: 'start'}); return; }
  if (act === 'reset') { resetAll(); return; }
  if (!v) return;
  if (act === 'copy') copy(v);
  else if (act === 'nhtsa') copy(v);   // link opens NHTSA in a new tab; VIN is on the clipboard in case the form needs it
  else if (act === 'rsave') saveRecallLink(v, b.closest('.rbox'));
  else if (act === 'rretry') { if (RQ[v]) sendRecall(v, RQ[v].u, b.closest('.rbox')); }
  else if (act === 'rec') {
    var rx = document.querySelector('tr.rx[data-v="'+v+'"]');
    if (rx) { rx.hidden = !rx.hidden; b.setAttribute('aria-expanded', String(!rx.hidden)); }
    else { var d = b.closest('[data-v]').querySelector('details.rcd'); if (d) { d.open = !d.open; b.setAttribute('aria-expanded', String(d.open)); if (d.open) d.scrollIntoView({block:'nearest'}); } }
  }
  else if (act === 'star') {
    var on = !U(v).f; setU(v, 'f', on ? 1 : false);
    document.querySelectorAll('[data-v="'+v+'"] .star').forEach(function(s){ s.classList.toggle('on', on); s.textContent = on ? '★' : '☆'; s.setAttribute('aria-pressed', String(on)); });
    toast(on ? 'Added to favorites' : 'Removed from favorites');
  } else if (act === 'exp') {
    var x = document.querySelector('tr.x[data-v="'+v+'"]'); x.hidden = !x.hidden; b.setAttribute('aria-expanded', String(!x.hidden));
    if (!x.hidden) { var ta = x.querySelector('textarea'); ta && ta.focus(); }
  }
});
var saveT = {};
function handleField(e){
  var el = e.target, f = el.getAttribute('data-f'); if (!f) return;
  var v = rowOf(el); if (!v) return;
  if (f === 's') {
    setU(v, 's', el.value);
    document.querySelectorAll('[data-v="'+v+'"]').forEach(function(n){ n.className = n.className.replace(/\bs-[a-z-]+/, 's-' + slug(el.value)); });
    toast('Status: ' + el.value);
  } else if (f === 'q') {
    var n = el.value.replace(/[^\d]/g, '');
    clearTimeout(saveT[v+'q']); saveT[v+'q'] = setTimeout(function(){ setU(v, 'q', n ? +n : ''); }, 300);
    if (e.type === 'change') { el.value = n ? Number(n).toLocaleString('en-US') : ''; setU(v,'q', n ? +n : ''); updateQuoted(v, n ? +n : 0); }
  } else if (f === 'n') {
    var val = el.value;
    clearTimeout(saveT[v+'n']); saveT[v+'n'] = setTimeout(function(){ setU(v, 'n', val.trim() ? val : ''); }, 300);
    if (e.type === 'change') setU(v, 'n', val.trim() ? val : '');
  }
}
function updateQuoted(v, q){
  document.querySelectorAll('[data-v="'+v+'"] .price, tr.r[data-v="'+v+'"] td:nth-child(2)').forEach(function(p){
    var s = p.querySelector('.quoted, .tq'); if (s) s.remove();
    if (q) p.insertAdjacentHTML('beforeend', p.classList.contains('price') ? '<span class="quoted">Quoted '+money(q)+'</span>' : '<span class="tq">Quoted '+money(q)+'</span>');
  });
}
$('list').addEventListener('input', handleField);
$('list').addEventListener('input', function(e){ if (e.target.getAttribute('data-rf')) { e.target.classList.remove('bad'); var m = e.target.closest('.rbox').querySelector('.rbm'); if (m && !m.querySelector('button')) { m.textContent = ''; m.className = 'rbm'; } } });
$('list').addEventListener('keydown', function(e){
  if (e.key === 'Enter' && e.target.getAttribute('data-rf')) { e.preventDefault(); var v = rowOf(e.target); if (v) saveRecallLink(v, e.target.closest('.rbox')); }
});

/* ---------- recall check: save a pasted NHTSA link (POST to the server, localStorage fallback + retry) ---------- */
function linkVins(u){   // VINs mentioned in a URL (vymm/vin params or the path)
  var out = [], t = decodeURIComponent((u.pathname || '') + ' ' + (u.search || '')).toUpperCase().split(/[^A-Z0-9]+/);
  t.forEach(function(x){ if (/^[A-HJ-NPR-Z0-9]{17}$/.test(x) && /\d/.test(x) && out.indexOf(x) < 0) out.push(x); });
  return out;
}
function checkLink(v, s){
  var u; try { u = new URL(s); } catch(e){ return 'That isn’t a web link. Copy the address of the NHTSA page and paste it here.'; }
  var h = u.hostname.toLowerCase();
  if (!/^https?:$/.test(u.protocol) || (h !== 'nhtsa.gov' && h !== 'www.nhtsa.gov') || u.username || u.password || u.port)
    return 'Please paste a link from nhtsa.gov (the NHTSA recall page for this van).';
  var vs = linkVins(u);
  if (vs.length > 1) return 'That link mentions more than one VIN.';
  if (vs.length && vs[0] !== v) return 'That link is for VIN …' + vs[0].slice(-6) + ', not this van (…' + v.slice(-6) + ').' + (VMAP[vs[0]] ? ' Paste it on that van instead.' : '');
  return '';
}
function rbSet(box, cls, html){ if (!box) return; var m = box.querySelector('.rbm'); if (m) { m.className = 'rbm ' + cls; m.innerHTML = html; } }
function saveRecallLink(v, box){
  var inp = box && box.querySelector('input[data-rf]'); if (!inp) return;
  var s = inp.value.trim();
  if (!s) { inp.classList.add('bad'); rbSet(box, 'err', 'Paste the NHTSA page link first (tap “Open NHTSA for this VIN”, then copy the address).'); inp.focus(); return; }
  if (!/^[a-z]+:\/\//i.test(s) && /^(www\.)?nhtsa\.gov\b/i.test(s)) s = 'https://' + s;
  var err = checkLink(v, s);
  if (err) { inp.classList.add('bad'); rbSet(box, 'err', esc(err)); return; }
  sendRecall(v, s, box);
}
function refreshRecall(v){   // update this van's badges, recall box status and details in place (no full re-render)
  var r = VMAP[v]; if (!r) return;
  document.querySelectorAll('article.card[data-v="'+v+'"]').forEach(function(c){
    c.querySelector('.badges').innerHTML = badges(r) + recBadge(r);
    var d = c.querySelector('details.rcd'); if (d) { var o = d.open; d.outerHTML = recDetails(r); if (o) c.querySelector('details.rcd').open = true; }
  });
  document.querySelectorAll('tr.r[data-v="'+v+'"] td.vc').forEach(function(td){ td.innerHTML = tvc(r); });
  document.querySelectorAll('tr.rx[data-v="'+v+'"] td').forEach(function(td){ td.innerHTML = recItems(r); });
  document.querySelectorAll('[data-v="'+v+'"] .rbox').forEach(function(b){
    b.querySelector('.rbs').outerHTML = rbStatus(r);
    var t = b.querySelector('.rbt'); if (t && (r.vc || queued(r))) t.remove();
  });
}
var sending = {};
function sendRecall(v, url, box, quiet){
  if (sending[v]) return;
  RQ[v] = {u: url, t: Date.now(), st: 'sending'}; save(K.rq, RQ); refreshRecall(v);
  if (!quiet) rbSet(box, 'busy', 'Saved, updating…');
  function fail(msg){
    RQ[v] = {u: url, t: Date.now(), st: 'failed', e: msg}; save(K.rq, RQ); refreshRecall(v);
    document.querySelectorAll('[data-v="'+v+'"] .rbox').forEach(function(b){ rbSet(b, 'err', esc(msg) + ' <button class="btn" type="button" data-act="rretry">Retry</button>'); });
  }
  if (!API || location.protocol === 'file:') { fail('Saved on this device only – open the online van list to send it.'); return; }
  sending[v] = 1;
  var ctl = window.AbortController ? new AbortController() : null, to = setTimeout(function(){ ctl && ctl.abort(); }, 20000);
  (RL ? relaySend({k: 'r', vin: v, url: url}).then(function(){ return {ok: true, url: url, _s: 200}; })
       : fetch(apiUrl(API.ep), {method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: ctl ? ctl.signal : undefined,
      headers: {'Content-Type': 'application/json', 'X-Api-Token': API.t}, body: JSON.stringify({vin: v, url: url})})
    .then(function(res){ return res.json().catch(function(){ return {}; }).then(function(j){ j._s = res.status; return j; }); }))
    .then(function(j){
      clearTimeout(to); delete sending[v];
      if (j.ok) {
        RQ[v] = {u: j.url || url, t: Date.now(), st: 'queued'}; save(K.rq, RQ); refreshRecall(v);
        document.querySelectorAll('[data-v="'+v+'"] .rbox').forEach(function(b){
          var i = b.querySelector('input[data-rf]'); if (i) i.value = '';
          rbSet(b, 'ok', '✓ Saved – recall check queued. The list updates on its own once the link has been read.');
        });
        if (!quiet) toast('Recall link saved'); watchUpdate();
      } else if (j._s === 400 || j._s === 404 || j._s === 422) {   // the server rejected the link itself: nothing to retry
        delete RQ[v]; save(K.rq, RQ); refreshRecall(v);
        if (box) { var i = box.querySelector('input[data-rf]'); if (i) { i.value = url; i.classList.add('bad'); } }
        rbSet(box, 'err', esc(j.error || 'The server did not accept that link.'));
      } else if (j._s === 401 || j._s === 403) fail('This page is out of date – reload it, then tap Retry.');
      else if (j._s === 429) fail('Too many saves in a short time – wait a minute, then Retry.');
      else fail('Couldn’t reach the server (' + (j.error || 'HTTP ' + j._s) + ') – saved on this device.');
    }, function(){ clearTimeout(to); delete sending[v]; fail('No connection – saved on this device.'); });
}
/* after a save: watch for the rebuilt (re-published) page and offer a reload */
var baseTag = null, watchT = null, watchN = 0;
function pageTag(){ return fetch(location.pathname, {method: 'HEAD', cache: 'no-store'}).then(function(r){ return r.ok ? (r.headers.get('ETag') || r.headers.get('Last-Modified')) : null; }, function(){ return null; }); }
function watchUpdate(){
  if (watchT || location.protocol === 'file:') return; watchN = 0;
  watchT = setInterval(function(){
    if (++watchN > 40) { clearInterval(watchT); watchT = null; return; }
    pageTag().then(function(t){ if (t && baseTag && t !== baseTag) { clearInterval(watchT); watchT = null; $('updbar').hidden = false; } });
  }, 5000);
}
$('updgo').addEventListener('click', function(){ location.reload(); });
if (API && location.protocol !== 'file:') {
  pageTag().then(function(t){ baseTag = t; });
  // retry links that were saved on this device but never reached the server
  setTimeout(function(){ Object.keys(RQ).forEach(function(v){ if (RQ[v].st !== 'queued') sendRecall(v, RQ[v].u, null, true); }); }, 1500);
}
$('list').addEventListener('change', handleField);

/* ---------- backup / restore ---------- */
$('exp').addEventListener('click', function(){
  var blob = new Blob([JSON.stringify({app:'bd600finder', saved: new Date().toISOString(), user: USER}, null, 1)], {type:'application/json'});
  var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'brightdrop-notes-' + new Date().toISOString().slice(0,10) + '.json';
  document.body.appendChild(a); a.click(); setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 1000);
});
$('imp').addEventListener('change', function(){
  var file = this.files && this.files[0]; if (!file) return;
  var rd = new FileReader();
  rd.onload = function(){
    try { var d = JSON.parse(rd.result), u = d.user || d, n = 0;
      Object.keys(u).forEach(function(v){ if (/^[A-HJ-NPR-Z0-9]{17}$/.test(v) && typeof u[v] === 'object') { var cur = USER[v]; if (!cur || (u[v].t||0) >= (cur.t||0)) { USER[v] = u[v]; n++; } } });
      save(K.user, USER); render(true); toast('Imported notes for ' + n + ' vans'); if (n) syncSoon();
    } catch(e){ toast('That file could not be read'); }
  };
  rd.readAsText(file); this.value = '';
});

if (window.innerWidth >= 900) $('filters').classList.remove('collapsed');
if (window.innerWidth < 600) $('q').placeholder = 'Search seller, city, VIN…';
render();
syncStart();
if (XERR) { $('xwarn').textContent = XERR; $('xwarn').hidden = false; }

/* ======================= Dealer Networks (Hertz Dealer Network, ...) ======================= */
(function(){
var NWK = 'bd600finder.nw.v1', NWS = load(NWK, {}), NW = {nets: null, rows: [], open: false, busy: false, lim: 30};
var MSK = [['mk','Make'],['md','Model'],['tr','Trim / series'],['dt','Drivetrain'],['ec','Exterior color'],['ic','Interior color'],['bt','Body'],['fu','Fuel'],['av','Availability'],['gb','Grade'],['vb','Damage (VDA)'],['st','State'],['c','City'],['loc','Location'],['lt','Location type'],['vc','VIN check']];
var AVB = ['Available now (today or earlier)','Within 3 days','Within 7 days','Within 14 days','Later than 14 days','No date given'];
var GBK = ['4.0 – 5.0','3.0 – 3.9','Below 3.0','AV','RG','Not graded'];
var VBK = ['None','Under $500','$500 – $1,500','$1,500 – $3,000','Over $3,000'];
var ORD = {av: AVB, gb: GBK, vb: VBK};
var GET = {mk: function(r){ return r.mk || 'Not listed'; }, md: function(r){ return r.md || 'Not listed'; }, tr: function(r){ return r.tr || 'Not listed'; },
  dt: function(r){ return r.dt || 'Unknown'; }, ec: function(r){ return r.ec || 'Not listed'; }, ic: function(r){ return r.ic || 'Not listed'; }, bt: function(r){ return r.bt || 'Not decoded'; },
  fu: function(r){ return r.fu || 'Not decoded'; }, st: function(r){ return r.st || 'Not listed'; }, c: function(r){ return r.c || 'Not listed'; }, loc: function(r){ return r.loc || 'Not listed'; },
  lt: function(r){ return r.lt || 'Not listed'; }, vc: function(r){ return vcLabel(r); }, av: function(r){ return r._av; }, gb: function(r){ return r._gb; }, vb: function(r){ return r._vb; }};
var TODAY = (function(){ var d = new Date(); return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); })();
function dayNum(iso){ var a = iso.split('-'); return Date.UTC(+a[0], +a[1] - 1, +a[2]) / 86400000; }
function nwDerive(o){
  var n = o.av ? dayNum(o.av) - dayNum(TODAY) : null;
  o._av = n == null ? AVB[5] : n <= 0 ? AVB[0] : n <= 3 ? AVB[1] : n <= 7 ? AVB[2] : n <= 14 ? AVB[3] : AVB[4];
  o._gb = o.gr != null ? (o.gr >= 4 ? GBK[0] : o.gr >= 3 ? GBK[1] : GBK[2]) : o.gx === 'AV' ? 'AV' : o.gx === 'RG' ? 'RG' : GBK[5];
  var v = o.vda || 0; o._vb = !v ? VBK[0] : v < 500 ? VBK[1] : v <= 1500 ? VBK[2] : v <= 3000 ? VBK[3] : VBK[4];
  /* "Retail ready" is NOT a column in the Hertz file. Best guess from the file: available now (no future date) + no damage amount (VDA 0) + grade 3.0+ if graded */
  o._rr = !o.rm && (n == null || n <= 0) && !v && (o.gr == null || o.gr >= 3);
}
var SORTS = [['p','Price: low to high'],['-p','Price: high to low'],['mi','Miles: low to high'],['-y','Year: newest'],['d','Distance: nearest'],['mk','Make / model A–Z'],['av','Available soonest'],['-gr','Grade: best first'],['vda','Damage: lowest first'],['new','Newly listed first'],['drop','Price drops first']];
var PRICES = [5000,8000,10000,12000,15000,20000,25000,30000,40000,50000,75000], MILES = [5000,10000,20000,30000,50000,75000,100000,150000], DISTS = [100,250,500,1000,2000];
var DT_PRESETS = {awd4: ['AWD','4WD'], awd: ['AWD'], '4wd': ['4WD'], fwd: ['FWD'], rwd: ['RWD'], '2wd': ['FWD','RWD','2WD']};
var COLORHEX = {White:'#fff',Black:'#111',Gray:'#888',Silver:'#c0c0c0',Blue:'#1d4ed8',Red:'#dc2626',Orange:'#f97316',Green:'#16a34a','Dark Green':'#14532d',Brown:'#7c4a21',Tan:'#d2b48c',Maroon:'#7f1d1d',Gold:'#d4af37',Yellow:'#facc15',Teal:'#0d9488',Beige:'#e8dcc0',Purple:'#7e22ce',Burgundy:'#6b1428',Charcoal:'#333',Copper:'#b87333',Pink:'#ec4899'};
function nwDef(){ return {net: '', q: '', ms: {}, ymin: '', ymax: '', minp: '', maxp: '', minm: '', maxm: '', maxd: '', gmin: '', gmax: '', vmin: '', vmax: '', avmin: '', avmax: '', zip: '', rr: false, hn: false, fav: false, rm: false, chg: false, mis: false, sort: 'p'}; }
var NF = nwDef();
(function(){ var s = NWS.f || {}; Object.keys(NF).forEach(function(k){ if (s[k] != null && k !== 'q') NF[k] = s[k]; }); })();
function nwSave(){ var f = {}; Object.keys(NF).forEach(function(k){ if (k !== 'q') f[k] = NF[k]; }); NWS.f = f; NWS.open = NW.open; save(NWK, NWS); }
function vcLabel(r){ return r.vs === 'fail' ? 'VIN not decoded' : r.hm ? 'Sheet and VIN disagree' : (r.fl && r.fl.length) ? 'Confirmed (minor note)' : 'Confirmed by VIN'; }
function nwNet(){ var n = null; (NW.nets || []).forEach(function(x){ if (x.id === NF.net) n = x; }); return n || (NW.nets && NW.nets[0]) || null; }

/* ---------- load (lazy; the data rides gzip+base64 inside the encrypted page) ---------- */
function nwLoad(){
  if (NW.nets) return Promise.resolve();
  if (!DATA.nw) return Promise.reject(new Error('No dealer-network data in this build.'));
  return inflate(DATA.nw).then(function(d){
    d.nets.forEach(function(n){
      n.rows = n.rows.map(function(a){ var o = {}; for (var i = 0; i < d.cols.length; i++) if (a[i] != null) o[d.cols[i]] = a[i];
        o._s = [o.y, o.mk, o.md, o.mds, o.tr, o.ec, o.ic, o.v, o.loc, o.c, o.st, o.z, o.dt, o.bt, o.fu, o.en, o.note, o.vt].join(' ').toLowerCase(); nwDerive(o); return o; });
    });
    NW.nets = d.nets;
  });
}

/* ---------- open / close ---------- */
function nwOpen(){
  NW.open = true; document.body.classList.add('nwm');
  document.querySelectorAll('#cats .cat').forEach(function(b){ b.setAttribute('aria-selected', 'false'); });
  $('nwtab').setAttribute('aria-selected', 'true');
  nwSave();
  if (!NW.nets) { $('nwlist').innerHTML = '<div class="nwbusy">Loading the dealer network inventory…</div>'; $('hsub').textContent = 'dealer network inventory'; $('stats').innerHTML = ''; }
  nwLoad().then(function(){ if (!NW.open) return; nwInit(); nwRender(); window.scrollTo(0, 0); },
    function(e){ $('nwlist').innerHTML = '<div class="nwbusy">Could not open the dealer network list: ' + esc(e.message || e) + '<br>Update Chrome / Safari and reload.</div>'; });
}
function nwClose(){
  if (!NW.open) return; NW.open = false; document.body.classList.remove('nwm'); $('nwtab').setAttribute('aria-selected', 'false'); nwSave(); stats();
}
$('nwtab').addEventListener('click', function(){ if (NW.open) { window.scrollTo(0, 0); return; } nwOpen(); });
$('cats').addEventListener('click', function(e){ if (e.target.closest('[data-cat]')) nwClose(); });

/* ---------- controls ---------- */
var inited = false;
function nwInit(){
  if (inited) return; inited = true;
  var nets = NW.nets;
  $('nwnet').innerHTML = nets.map(function(n){ return '<option value="' + esc(n.id) + '">' + esc(n.name) + ' (' + n.n.toLocaleString('en-US') + ' cars)</option>'; }).join('');
  if (!NF.net || !nets.some(function(n){ return n.id === NF.net; })) NF.net = nets[0].id;
  $('nwnet').value = NF.net;
  $('nwsort').innerHTML = SORTS.map(function(s){ return '<option value="' + s[0] + '">' + s[1] + '</option>'; }).join('');
  $('nwsort').value = NF.sort;
  var h = '';
  MSK.forEach(function(k){ h += '<div class="f ms" data-k="' + k[0] + '"><span>' + k[1] + '</span><details class="msd" id="nwms-' + k[0] + '"><summary aria-label="' + k[1] + '"><span class="msv">Any</span></summary><div class="msb"></div></details></div>'; });
  h += '<label class="f">Year from<select id="nw-ymin"></select></label><label class="f">Year to<select id="nw-ymax"></select></label>';
  h += '<label class="f">Min price<select id="nw-minp"><option value="">Any</option>' + PRICES.slice(0, 9).map(function(v){ return '<option value="' + v + '">' + money(v) + '+</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Max price<select id="nw-maxp"><option value="">Any</option>' + PRICES.map(function(v){ return '<option value="' + v + '">' + money(v) + '</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Min miles<select id="nw-minm"><option value="">Any</option>' + [1000,5000,10000,20000,50000].map(function(v){ return '<option value="' + v + '">' + v.toLocaleString('en-US') + '+ mi</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Max miles<select id="nw-maxm"><option value="">Any</option>' + MILES.map(function(v){ return '<option value="' + v + '">' + v.toLocaleString('en-US') + ' mi</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Distance from 07004<select id="nw-maxd"><option value="">Any distance</option>' + DISTS.map(function(v){ return '<option value="' + v + '">Within ' + v.toLocaleString('en-US') + ' mi</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Min grade<select id="nw-gmin"><option value="">Any</option>' + [2,2.5,3,3.5,4,4.5].map(function(v){ return '<option value="' + v + '">' + v.toFixed(1) + '+</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Max grade<select id="nw-gmax"><option value="">Any</option>' + [2.5,3,3.5,4,4.5,5].map(function(v){ return '<option value="' + v + '">' + v.toFixed(1) + '</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Min damage (VDA)<select id="nw-vmin"><option value="">Any</option>' + [1,500,1000,2000,3000,5000].map(function(v){ return '<option value="' + v + '">' + (v === 1 ? 'Any damage' : money(v) + '+') + '</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Max damage (VDA)<select id="nw-vmax"><option value="">Any</option><option value="0">None ($0)</option>' + [500,1000,2000,3000,5000].map(function(v){ return '<option value="' + v + '">' + money(v) + '</option>'; }).join('') + '</select></label>';
  h += '<label class="f">Available from<input type="date" id="nw-avmin"></label><label class="f">Available to<input type="date" id="nw-avmax"></label>';
  h += '<label class="f">ZIP starts with<input type="search" id="nw-zip" inputmode="numeric" maxlength="5" placeholder="e.g. 07 or 330" autocomplete="off"></label>';
  h += '<label class="tog" id="nwtog-rr"><input type="checkbox" id="nw-rr"> Retail ready*</label><label class="tog"><input type="checkbox" id="nw-hn"> Has a sheet note</label>';
  h += '<label class="tog" id="nwtog-fav"><input type="checkbox" id="nw-fav"> ★ Favorites</label><label class="tog"><input type="checkbox" id="nw-chg"> New / price changed only</label>' +
       '<label class="tog"><input type="checkbox" id="nw-mis"> Only sheet ≠ VIN problems</label><label class="tog"><input type="checkbox" id="nw-rm"> Show sold / removed</label>';
  $('nwfilters').innerHTML = h;
  $('nwchips').innerHTML = [['rr','1','Retail ready*'],['dt','awd4','AWD / 4WD'],['dt','2wd','2WD (FWD/RWD)'],['maxp','15000','Under $15k'],['maxp','25000','Under $25k'],['maxm','30000','Under 30k mi'],['maxm','60000','Under 60k mi'],['maxd','500','Within 500 mi'],['vc','ok','VIN-confirmed only']]
    .map(function(c){ return '<button type="button" class="chip' + (c[0] === 'rr' ? ' rr' : '') + '" data-nwchip="' + c[0] + '" data-val="' + c[1] + '" aria-pressed="false">' + c[2] + '</button>'; }).join('');
  if (window.innerWidth >= 900) $('nwfilters').classList.remove('collapsed');
}
var NWQ = {};   // per-dropdown search text
function same(a, b){ return a.length === b.length && a.every(function(x){ return b.indexOf(x) >= 0; }); }
function nwPass(r, c, out){   // returns number of failing conditions; out.k = the (last) failing key
  var f = 0; out.k = '';
  if (c.q) { for (var i = 0; i < c.q.length; i++) if (r._s.indexOf(c.q[i]) < 0) { f++; out.k = '_q'; break; } }
  for (var j = 0; j < MSK.length; j++) { var k = MSK[j][0], sel = c.ms[k]; if (sel && sel.length) { var v = k === 'vc' ? vcLabel(r) : GET[k](r); if (sel.indexOf(v) < 0) { f++; out.k = k; } } }
  if (c.ymin && (!r.y || r.y < +c.ymin)) { f++; out.k = '_y'; }
  if (c.ymax && (!r.y || r.y > +c.ymax)) { f++; out.k = '_y'; }
  if (c.minp && (r.p == null || r.p < +c.minp)) { f++; out.k = '_p'; }
  if (c.maxp && (r.p == null || r.p > +c.maxp)) { f++; out.k = '_p'; }
  if (c.minm && (r.mi == null || r.mi < +c.minm)) { f++; out.k = '_m'; }
  if (c.maxm && (r.mi == null || r.mi > +c.maxm)) { f++; out.k = '_m'; }
  if (c.maxd && (r.d == null || r.d > +c.maxd)) { f++; out.k = '_d'; }
  if (c.gmin && (r.gr == null || r.gr < +c.gmin)) { f++; out.k = '_g'; }
  if (c.gmax && (r.gr == null || r.gr > +c.gmax)) { f++; out.k = '_g'; }
  if (c.vmin !== '' && (r.vda || 0) < +c.vmin) { f++; out.k = '_v'; }
  if (c.vmax !== '' && (r.vda || 0) > +c.vmax) { f++; out.k = '_v'; }
  if (c.avmin && (!r.av || r.av < c.avmin)) { f++; out.k = '_a'; }
  if (c.avmax && (!r.av || r.av > c.avmax)) { f++; out.k = '_a'; }
  if (c.zip && (r.z || '').indexOf(c.zip) !== 0) { f++; out.k = '_z'; }
  if (c.rr && !r._rr) { f++; out.k = '_rr'; }
  if (c.hn && !r.note) { f++; out.k = '_hn'; }
  if (c.fav && !U(r.v).f) { f++; out.k = '_f'; }
  if (c.chg && !(r.nw || r.pp)) { f++; out.k = '_c'; }
  if (c.mis && !(r.hm || r.vs === 'fail')) { f++; out.k = '_x'; }
  return f;
}
function nwCond(){ var c = {ms: NF.ms, ymin: NF.ymin, ymax: NF.ymax, minp: NF.minp, maxp: NF.maxp, minm: NF.minm, maxm: NF.maxm, maxd: NF.maxd, gmin: NF.gmin, gmax: NF.gmax, vmin: NF.vmin, vmax: NF.vmax, avmin: NF.avmin, avmax: NF.avmax, zip: (NF.zip || '').trim(), rr: NF.rr, hn: NF.hn, fav: NF.fav, chg: NF.chg, mis: NF.mis};
  c.q = (NF.q || '').toLowerCase().split(/\s+/).filter(Boolean); return c; }
function cmpNum(a, b, desc){ if (a == null && b == null) return 0; if (a == null) return 1; if (b == null) return -1; return desc ? b - a : a - b; }
function nwSort(list){
  var s = NF.sort, desc = s.charAt(0) === '-', k = desc ? s.slice(1) : s;
  list.sort(function(a, b){
    var x = 0;
    if (!!a.rm !== !!b.rm) return a.rm ? 1 : -1;
    if (k === 'mk') x = (a.mk || '').localeCompare(b.mk || '') || (a.md || '').localeCompare(b.md || '') || (b.y || 0) - (a.y || 0);
    else if (k === 'new') x = (b.fs || '').localeCompare(a.fs || '') || cmpNum(a.p, b.p);
    else if (k === 'drop') x = ((b.pp && b.p ? b.pp - b.p : 0) - (a.pp && a.p ? a.pp - a.p : 0)) || cmpNum(a.p, b.p);
    else x = cmpNum(a[k], b[k], desc);
    return x || cmpNum(a.p, b.p) || (a.v < b.v ? -1 : 1);
  });
  return list;
}
function nwRows(){ var n = nwNet(); return n ? n.rows : []; }
function nwFilterAndFacets(){
  var c = nwCond(), out = {}, res = [], fac = {}, base = 0;
  MSK.forEach(function(k){ fac[k[0]] = {}; });
  var rows = nwRows();
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i]; if (r.rm && !NF.rm) continue; base++;
    var f = nwPass(r, c, out);
    if (f === 0) res.push(r);
    if (f <= 1) { var fk = out.k; MSK.forEach(function(m){ var k = m[0]; if (f === 0 || fk === k) { var v = GET[k](r); fac[k][v] = (fac[k][v] || 0) + 1; } }); }
  }
  return {list: res, fac: fac, base: base};
}
function nwFacetUI(fac){
  NW.fac = fac;
  MSK.forEach(function(m){
    var k = m[0], d = $('nwms-' + k), sel = NF.ms[k] || [], cnt = fac[k], opts = Object.keys(cnt);
    sel.forEach(function(v){ if (opts.indexOf(v) < 0) opts.push(v); });
    opts.sort(function(a, b){ if (ORD[k]) return ORD[k].indexOf(a) - ORD[k].indexOf(b); var na = /^Not |not listed$/.test(a), nb = /^Not |not listed$/.test(b); return na - nb || (k === 'mk' || k === 'md' || k === 'tr' || k === 'st' || k === 'ec' || k === 'ic' || k === 'c' || k === 'loc' ? a.localeCompare(b) : (cnt[b] || 0) - (cnt[a] || 0) || a.localeCompare(b)); });
    if (opts.length > 300 && !d.open) { d.querySelector('summary').classList.toggle('on', sel.length > 0); d.querySelector('.msv').textContent = !sel.length ? 'Any' : sel.length === 1 ? sel[0] : sel.length + ' selected'; d.querySelector('.msb').innerHTML = ''; return; }
    var q = (NWQ[k] || '').toLowerCase();
    var h = (opts.length > 12 ? '<input class="msq" type="search" data-nwq="' + k + '" placeholder="Type to find…" value="' + esc(NWQ[k] || '') + '" autocomplete="off">' : '');
    h += opts.map(function(v){ return '<label' + (cnt[v] ? '' : ' class="z"') + (q && v.toLowerCase().indexOf(q) < 0 ? ' hidden' : '') + '><input type="checkbox" data-nwms="' + k + '" value="' + esc(v) + '"' + (sel.indexOf(v) >= 0 ? ' checked' : '') + '> ' + esc(v) + '<span class="c">' + (cnt[v] || 0) + '</span></label>'; }).join('');
    if (sel.length) h += '<button type="button" class="btn ghost msx" data-nwmsx="' + k + '">Clear</button>';
    var box = d.querySelector('.msb'), hadFocus = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute('data-nwq') === k;
    box.innerHTML = h;
    if (hadFocus) { var inp = box.querySelector('.msq'); if (inp) { inp.focus(); var L = inp.value.length; try { inp.setSelectionRange(L, L); } catch(e){} } }
    d.querySelector('summary').classList.toggle('on', sel.length > 0);
    d.querySelector('.msv').textContent = !sel.length ? 'Any' : sel.length === 1 ? sel[0] : sel.length + ' selected';
  });
  var ys = {}; nwRows().forEach(function(r){ if (r.y) ys[r.y] = 1; });
  var yl = Object.keys(ys).map(Number).sort(function(a, b){ return b - a; });
  var yh = '<option value="">Any</option>' + yl.map(function(y){ return '<option value="' + y + '">' + y + '</option>'; }).join('');
  if ($('nw-ymin').getAttribute('data-n') !== String(yl.length)) { $('nw-ymin').innerHTML = yh; $('nw-ymax').innerHTML = yh; $('nw-ymin').setAttribute('data-n', yl.length); }
  ['ymin','ymax','minp','maxp','minm','maxm','maxd','gmin','gmax','vmin','vmax','avmin','avmax'].forEach(function(k){ if ($('nw-' + k).value !== String(NF[k])) $('nw-' + k).value = NF[k]; });
  if ($('nw-zip').value !== NF.zip && document.activeElement !== $('nw-zip')) $('nw-zip').value = NF.zip;
  $('nw-rr').checked = NF.rr; $('nw-hn').checked = NF.hn; $('nwrrinfo').hidden = !NF.rr;
  $('nw-fav').checked = NF.fav; $('nw-chg').checked = NF.chg; $('nw-mis').checked = NF.mis; $('nw-rm').checked = NF.rm;
  var dts = NF.ms.dt || [], pre = '';
  if (dts.length) { pre = 'custom'; Object.keys(DT_PRESETS).forEach(function(p){ if (same(DT_PRESETS[p], dts)) pre = p; }); }
  $('nwdt').value = pre;
  document.querySelectorAll('#nwchips .chip').forEach(function(b){
    var k = b.getAttribute('data-nwchip'), v = b.getAttribute('data-val'), on;
    if (k === 'rr') on = !!NF.rr;
    else if (k === 'dt') on = same(DT_PRESETS[v], dts) && dts.length > 0;
    else if (k === 'vc') on = same(['Confirmed by VIN', 'Confirmed (minor note)'], NF.ms.vc || []);
    else on = String(NF[k]) === v;
    b.setAttribute('aria-pressed', String(!!on));
  });
  var nf = 0; MSK.forEach(function(m){ if ((NF.ms[m[0]] || []).length) nf++; });
  ['ymin','ymax','minp','maxp','minm','maxm','maxd','gmin','gmax','vmin','vmax','avmin','avmax','zip'].forEach(function(k){ if (NF[k] !== '' && NF[k] != null) nf++; });
  ['rr','hn','fav','chg','mis','rm'].forEach(function(k){ if (NF[k]) nf++; });
  $('nwfcnt').hidden = !nf; $('nwfcnt').textContent = nf;
  $('nwreset').hidden = !(nf || NF.q);
}
function nwStats(n, shown, base){
  var m = n.meta || {}, dts = m.dts || {};
  var h = '<span><b>' + n.n.toLocaleString('en-US') + '</b> cars in the ' + esc(n.name) + '</span>' +
    ['AWD','4WD','FWD','RWD'].filter(function(k){ return dts[k]; }).map(function(k){ return '<span><b>' + dts[k].toLocaleString('en-US') + '</b> ' + k + '</span>'; }).join('') +
    (m['new'] ? '<span><b>' + m['new'].toLocaleString('en-US') + '</b> new today</span>' : '') + (m.chg ? '<span><b>' + m.chg.toLocaleString('en-US') + '</b> price changes</span>' : '') +
    (m.removed ? '<span><b>' + m.removed.toLocaleString('en-US') + '</b> sold / removed</span>' : '') +
    '<span class="upd">Data updated ' + esc(m.imported || m.updated || '') + '</span>';
  $('stats').innerHTML = h; $('hsub').textContent = 'dealer network inventory · pick a network, then filter';
}
function nwRender(keep){
  if (!NW.nets) return;
  if (!keep) NW.lim = 30;
  var n = nwNet(); if (!n) return;
  var o = nwFilterAndFacets(), list = nwSort(o.list);
  nwFacetUI(o.fac); nwStats(n, list.length, o.base);
  $('nwrrinfo').innerHTML = '* <b>Retail ready</b> is my best guess: the Hertz file has no retail-ready column. It means <b>no future Estimated Availability date</b> (blank or today or earlier), <b>VDA $0</b> (no damage amount) and <b>grade 3.0 or better if the car has a grade</b>. Use the Availability, Grade and Damage filters to set your own rules.';
  var cheapest = null; list.forEach(function(r){ if (!r.rm && r.p != null && (cheapest == null || r.p < cheapest)) cheapest = r.p; });
  $('nwres').innerHTML = 'Showing <b>' + list.length.toLocaleString('en-US') + '</b> of ' + o.base.toLocaleString('en-US') + ' cars' + (cheapest != null ? ' · from <b>' + money(cheapest) + '</b>' : '');
  var m = n.meta || {};
  $('nwinfo').textContent = n.name + ': ' + n.n.toLocaleString('en-US') + ' cars from ' + (m.src || 'the daily file') + ', updated ' + (m.imported || m.updated || '') + '. Drivetrain, body, engine, fuel and transmission are checked against the VIN (NHTSA vPIC decoder); '
    + (m.vmis || 0).toLocaleString('en-US') + ' cars have a sheet-vs-VIN mismatch and ' + (m.vfail || 0) + ' VINs could not be decoded. Color comes from the sheet (a VIN cannot tell color). Cars missing from a later file are kept as “sold / removed” for 30 days.';
  var el = $('nwlist');
  if (!list.length) { el.innerHTML = '<div class="empty">No cars match these filters.<br><br><button class="btn primary" type="button" data-nwact="reset">Reset filters</button></div>'; return; }
  el.innerHTML = list.slice(0, NW.lim).map(nwCard).join('') + (list.length > NW.lim ? '<div class="nwmore"><button class="btn" type="button" data-nwact="more">Show ' + Math.min(30, list.length - NW.lim) + ' more (' + (list.length - NW.lim).toLocaleString('en-US') + ' left)</button></div>' : '');
}
function kv(k, v){ return v == null || v === '' ? '' : '<div class="kv"><span>' + k + '</span><b>' + esc(v) + '</b></div>'; }
function nwCard(r){
  var u = U(r.v), fav = !!u.f, cls = 'nwc' + (r.rm ? ' gone' : r.hm || r.vs === 'fail' ? ' mis' : (r.fl && r.fl.length) ? ' mn' : '');
  var title = [r.y, r.mk, r.md, r.tr].filter(Boolean).join(' ');
  var two = r.dt === 'FWD' || r.dt === 'RWD' || r.dt === '2WD';
  var b = '<span class="b b-dt' + (two ? ' two' : '') + '" title="' + esc(r.dsrc === 'VIN' ? 'Drivetrain from the VIN (the sheet left it blank)' : 'Drivetrain') + '">' + esc(r.dt || 'Unknown') + '</span>';
  if (r.dts === 'ok' || r.dts === 'soft') b += '<span class="b b-ok">✓ drivetrain matches VIN</span>';
  else if (r.dts === 'vin') b += '<span class="b b-ok">drivetrain from VIN</span>';
  else if (r.dts === 'hard') b += '<span class="b b-bad">⚠ VIN says ' + esc(r.dtv) + '</span>';
  else if (r.dts === 'nov') b += '<span class="b b-warn">VIN has no drivetrain data</span>';
  if (r.vs === 'fail') b += '<span class="b b-bad">VIN not decoded</span>';
  else if (r.hm) b += '<span class="b b-bad">⚠ sheet ≠ VIN</span>';
  else if (r.vs !== 'fail') b += '<span class="b b-ok">✓ VIN confirmed</span>';
  if (r.rm) b += '<span class="b b-bad">Sold / removed ' + esc(fdate(r.rm)) + '</span>';
  if (r._rr) b += '<span class="b b-rr" title="No future availability date, no damage amount (VDA), grade 3.0+ if graded">Retail ready*</span>';
  else if (r.av && r._av !== AVB[0]) b += '<span class="b b-av">Available ' + esc(fdate(r.av)) + '</span>';
  if (r.vda) b += '<span class="b b-dmg">VDA ' + money(r.vda) + '</span>';
  if (r.nw && !r.rm) b += '<span class="b b-new">NEW</span>';
  if (r.pp && r.p && r.pp !== r.p && !r.rm) b += '<span class="b b-pc' + (r.p > r.pp ? ' up' : '') + '">' + (r.p < r.pp ? '▼ ' + money(r.pp - r.p) + ' lower' : '▲ ' + money(r.p - r.pp) + ' higher') + '</span>';
  var col = r.ec ? '<span class="dot" style="background:' + (COLORHEX[r.ec] || '#999') + '"></span>' + esc(r.ec) : '';
  var line1 = [col, r.mi != null ? '<b>' + Number(r.mi).toLocaleString('en-US') + '</b> mi' : 'miles n/a'].filter(Boolean).join(' · ') + (r.gr ? ' · grade ' + r.gr : r.gx ? ' · grade ' + r.gx : '');
  var spec = [r.bt, r.fu, r.en, r.tx].filter(Boolean).map(esc).join(' · ');
  var place = [r.c, r.st].filter(Boolean).join(', ') || r.loc; var dist = r.d != null ? ' · ' + Number(r.d).toLocaleString('en-US') + ' mi away' : '';
  var fl = (r.fl || []).length ? '<div class="nwfl' + (r.hm || r.vs === 'fail' ? ' bad' : '') + '"><b>' + (r.hm ? 'Sheet and VIN disagree' : 'VIN notes') + '</b><br>' + r.fl.map(esc).join('<br>') + '</div>' : '';
  var det = '<details class="nwd"><summary>VIN build details &amp; listing info</summary>' +
    '<div class="nwh">Confirmed by the VIN</div>' +
    kv('VIN', r.v) + kv('VIN model', r.vm) + kv('Model (sheet)', r.mds) + kv('VIN trim', r.vt) + kv('Drivetrain (VIN)', r.dtv) + kv('Body', r.bc) + kv('Doors', r.dr) + kv('Engine', r.en) + kv('Transmission', r.tx) + kv('Fuel', r.fu) + kv('Built in', r.pl) + kv('Weight class', r.gv) +
    '<div class="nwh">From the Hertz sheet</div>' +
    kv('Drivetrain (sheet)', r.dtc) + kv('Exterior', r.ec) + kv('Interior', r.ic) + kv('Grade', r.gr != null ? r.gr : r.gx) + kv('VDA (damage amount)', r.vda ? money(r.vda) : '') + kv('Retail ready*', r._rr ? 'Yes (best guess)' : 'No') + kv('ZIP', r.z) + kv('Location', r.loc) + kv('Type', r.lt) + kv('Available', r.av ? fdate(r.av) : '') + kv('First seen', r.fs ? fdate(r.fs) : '') + kv('Price before', r.pp ? money(r.pp) : '') + kv('Sheet note', r.note) + kv('Note updated', r.nd ? fdate(r.nd) : '') +
    '<div style="display:flex;gap:8px;margin-top:8px"><button class="btn" type="button" data-nwact="copy">Copy VIN</button></div>' +
    '<textarea data-nwf="n" placeholder="My notes on this car…" aria-label="My notes">' + esc(u.n || '') + '</textarea></details>';
  return '<article class="' + cls + '" data-v="' + esc(r.v) + '"><div class="ctop"><div><div class="price' + (r.p == null ? ' na' : '') + '">' + (r.p != null ? money(r.p) : 'Call') + '</div></div>' +
    '<button class="star' + (fav ? ' on' : '') + '" type="button" data-nwact="star" aria-pressed="' + fav + '" aria-label="Favorite">' + (fav ? '★' : '☆') + '</button></div>' +
    '<h3>' + esc(title) + '</h3><div class="badges">' + b + '</div><div class="ln">' + line1 + '</div>' +
    (spec ? '<div class="sub">' + spec + '</div>' : '') + '<div class="sub">' + esc(place) + dist + '</div>' + fl + det + '</article>';
}

/* ---------- events ---------- */
function upd(){ nwSave(); nwRender(); }
$('nwnet').addEventListener('change', function(){ NF.net = this.value; NF.ms = {}; upd(); });
$('nwsort').addEventListener('change', function(){ NF.sort = this.value; upd(); });
$('nwdt').addEventListener('change', function(){ var v = this.value; if (v === 'custom') return; if (v) NF.ms.dt = DT_PRESETS[v].slice(); else delete NF.ms.dt; upd(); });
var nwqT; $('nwq').addEventListener('input', function(){ clearTimeout(nwqT); var v = this.value.trim(); nwqT = setTimeout(function(){ NF.q = v; nwRender(); }, 150); });
$('nwft').addEventListener('click', function(){ var c = $('nwfilters').classList.toggle('collapsed'); this.setAttribute('aria-expanded', String(!c)); });
$('nwfilters').addEventListener('change', function(e){
  var t = e.target, k = t.getAttribute('data-nwms');
  if (k) { var a = (NF.ms[k] || []).slice(), i = a.indexOf(t.value); if (t.checked && i < 0) a.push(t.value); if (!t.checked && i >= 0) a.splice(i, 1); if (a.length) NF.ms[k] = a; else delete NF.ms[k]; upd(); var d = $('nwms-' + k); d.open = true; d.parentNode.classList.add('open'); return; }
  var m = /^nw-(ymin|ymax|minp|maxp|minm|maxm|maxd|gmin|gmax|vmin|vmax|avmin|avmax)$/.exec(t.id); if (m) { NF[m[1]] = t.value; upd(); return; }
  if (t.id === 'nw-fav') NF.fav = t.checked; else if (t.id === 'nw-chg') NF.chg = t.checked; else if (t.id === 'nw-mis') NF.mis = t.checked; else if (t.id === 'nw-rm') NF.rm = t.checked; else if (t.id === 'nw-rr') NF.rr = t.checked; else if (t.id === 'nw-hn') NF.hn = t.checked; else if (t.id === 'nw-zip') NF.zip = t.value.trim(); else return;
  upd();
});
var zipT; $('nwfilters').addEventListener('input', function(e){
  if (e.target.id === 'nw-zip') { clearTimeout(zipT); var zv = e.target.value.replace(/\D/g, '').slice(0, 5); zipT = setTimeout(function(){ NF.zip = zv; upd(); }, 250); return; }
  var k = e.target.getAttribute('data-nwq'); if (!k) return; NWQ[k] = e.target.value; var q = e.target.value.toLowerCase();
  e.target.parentNode.querySelectorAll('label').forEach(function(l){ l.hidden = !!q && l.textContent.toLowerCase().indexOf(q) < 0; });
});
$('nwfilters').addEventListener('click', function(e){ var x = e.target.closest('[data-nwmsx]'); if (!x) return; delete NF.ms[x.getAttribute('data-nwmsx')]; upd(); });
document.querySelectorAll('#nwfilters').forEach(function(){});
$('nwfilters').addEventListener('toggle', function(e){ var d = e.target; if (!d.classList || !d.classList.contains('msd')) return; d.parentNode.classList.toggle('open', d.open);
  if (d.open && NW.fac) nwFacetUI(NW.fac);
  if (d.open) document.querySelectorAll('#nwfilters details.msd[open]').forEach(function(o){ if (o !== d) o.open = false; }); }, true);
$('nwchips').addEventListener('click', function(e){
  var c = e.target.closest('.chip'); if (!c) return; var k = c.getAttribute('data-nwchip'), v = c.getAttribute('data-val');
  if (k === 'rr') NF.rr = !NF.rr;
  else if (k === 'dt') { if (same(DT_PRESETS[v], NF.ms.dt || [])) delete NF.ms.dt; else NF.ms.dt = DT_PRESETS[v].slice(); }
  else if (k === 'vc') { if ((NF.ms.vc || []).length) delete NF.ms.vc; else NF.ms.vc = ['Confirmed by VIN', 'Confirmed (minor note)']; }
  else NF[k] = String(NF[k]) === v ? '' : v;
  upd();
});
function nwReset(){ var net = NF.net, sort = NF.sort; NF = nwDef(); NF.net = net; NF.sort = sort; $('nwq').value = ''; NWQ = {}; upd(); }
$('nwreset').addEventListener('click', nwReset);
$('nwlist').addEventListener('click', function(e){
  var b = e.target.closest('[data-nwact]'); if (!b) return; var act = b.getAttribute('data-nwact'), art = b.closest('[data-v]'), v = art && art.getAttribute('data-v');
  if (act === 'more') { NW.lim += 30; nwRender(true); }
  else if (act === 'reset') nwReset();
  else if (act === 'copy' && v) copy(v);
  else if (act === 'star' && v) { var on = !U(v).f; setU(v, 'f', on ? 1 : false); b.classList.toggle('on', on); b.textContent = on ? '★' : '☆'; b.setAttribute('aria-pressed', String(on)); toast(on ? 'Added to favorites' : 'Removed from favorites'); }
});
$('nwlist').addEventListener('change', function(e){ var t = e.target; if (t.getAttribute('data-nwf') !== 'n') return; var art = t.closest('[data-v]'); if (art) { setU(art.getAttribute('data-v'), 'n', t.value.trim()); toast('Note saved'); } });
if (NWS.open && DATA.nw) setTimeout(nwOpen, 0);
})();
}   // start()
})();
</script>
</body>
</html>
'''

if __name__ == '__main__':
    import fcntl
    try:   # keep the sync-tunnel watcher alive (restarts it after a box reboot); no-op if it's running
        import subprocess as _sp
        _sp.Popen([os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tunnel_watch.sh'), 'ensure'],
                  stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass
    with open(BUILD_LOCK, 'a') as _lk:   # one build/publish at a time
        fcntl.flock(_lk, fcntl.LOCK_EX)
        main()
