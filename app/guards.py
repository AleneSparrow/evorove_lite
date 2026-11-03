"""CAN-SPAM and sender-reputation guards. Nothing reaches SMTP without can_send().

Ported from evorove src/persistence/email_outreach_service.py (the three send
gates, suppression, quiet hours, daily cap), src/api/routes/unsubscribe.py (one
click, confirmation page first) and src/domain/us_postal_timezones.py (the US
zone table, rekeyed from ZIP prefixes to the us_state this schema stores).
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from .board import unsubscribe_token
from .db import connect

EASTERN = 'America/New_York'
CENTRAL = 'America/Chicago'
MOUNTAIN = 'America/Denver'
ARIZONA = 'America/Phoenix'
PACIFIC = 'America/Los_Angeles'
ALASKA = 'America/Anchorage'
HAWAII = 'Pacific/Honolulu'
PUERTO_RICO = 'America/Puerto_Rico'
DEFAULT_ZONE = EASTERN

# A default, not a determination: split states (KY, TN, KS, NE, the Dakotas, ID)
# take the zone that holds most of their ZIP codes, as the old table did. A
# recipient we cannot place keeps the previous constant instead of something worse.
_ZONE_STATES = (
    (EASTERN, 'CT DE DC FL GA IN ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV'),
    (CENTRAL, 'AL AR IA IL KS KY LA MN MO MS ND NE OK SD TN TX WI'),
    (MOUNTAIN, 'CO ID MT NM UT WY'),
    (ARIZONA, 'AZ'), (PACIFIC, 'CA NV OR WA'), (ALASKA, 'AK'), (HAWAII, 'HI'),
    (PUERTO_RICO, 'PR VI'),
)
US_STATE_ZONES = {code: zone for zone, codes in _ZONE_STATES for code in codes.split()}
_STATE_NAMES = (
    'Alabama AL,Alaska AK,Arizona AZ,Arkansas AR,California CA,Colorado CO,Connecticut CT,'
    'Delaware DE,District of Columbia DC,Florida FL,Georgia GA,Hawaii HI,Idaho ID,Illinois IL,'
    'Indiana IN,Iowa IA,Kansas KS,Kentucky KY,Louisiana LA,Maine ME,Maryland MD,Massachusetts MA,'
    'Michigan MI,Minnesota MN,Mississippi MS,Missouri MO,Montana MT,Nebraska NE,Nevada NV,'
    'New Hampshire NH,New Jersey NJ,New Mexico NM,New York NY,North Carolina NC,North Dakota ND,'
    'Ohio OH,Oklahoma OK,Oregon OR,Pennsylvania PA,Puerto Rico PR,Rhode Island RI,'
    'South Carolina SC,South Dakota SD,Tennessee TN,Texas TX,Utah UT,Vermont VT,Virginia VA,'
    'Washington WA,West Virginia WV,Wisconsin WI,Wyoming WY')
_STATE_CODES = {entry.rsplit(' ', 1)[0].upper(): entry.rsplit(' ', 1)[1] for entry in _STATE_NAMES.split(',')}

# Three consecutive soft failures to one address is treated like a bounce: the
# address stops receiving until the owner looks at it.
FAILURE_LIMIT = 3


def recipient_zone(us_state):
    """IANA zone for the recipient's US state; DEFAULT_ZONE when unplaceable."""
    value = (us_state or '').strip().upper()
    return US_STATE_ZONES.get(_STATE_CODES.get(value, value), DEFAULT_ZONE)


def quiet_now(business, now_utc, zone):
    start, end = business['quiet_start'] or '20:00', business['quiet_end'] or '08:00'
    if start == end:
        return False
    local = now_utc.astimezone(ZoneInfo(zone)).strftime('%H:%M')
    return start <= local < end if start < end else local >= start or local < end


def can_send(business_id, email, now_utc, *, settings=None):
    """(True, '') when this address may be emailed right now, else (False, reason).

    The order is the order of the old service's gates: configuration first, then
    the recipient's own rights, then reputation. The recipient's quiet hours come
    from their us_state, never from the owner's clock.
    """
    from .settings import load
    settings = settings or load()
    address = (email or '').strip().lower()
    if not address:
        return False, 'no_email'
    with connect(settings.db_path) as db:
        business = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
        if not business:
            return False, 'unknown_business'
        if business['outreach_paused']:
            return False, 'outreach_paused'
        if not (business['smtp_host'] and business['smtp_user'] and business['smtp_password_enc']):
            return False, 'mailbox_not_configured'
        if len((business['postal_address'] or '').strip()) < 10:
            return False, 'postal_address_required'
        if not settings.get('APP_BASE_URL'):
            return False, 'base_url_required'
        if db.execute('SELECT 1 FROM suppressions WHERE business_id=? AND email=?', (business_id, address)).fetchone():
            return False, 'suppressed'
        person = db.execute('SELECT us_state FROM people WHERE business_id=? AND lower(email)=?',
                            (business_id, address)).fetchone()
        if quiet_now(business, now_utc, recipient_zone(person['us_state'] if person else None)):
            return False, 'quiet_hours'
        day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        sent = db.execute("SELECT count(*) FROM messages m JOIN people p ON p.id=m.person_id "
                          "WHERE p.business_id=? AND m.direction='out' AND m.created_at>=?",
                          (business_id, day)).fetchone()[0]
        if sent >= (business['daily_cap'] or 20):
            return False, 'daily_cap'
        since = (now_utc - timedelta(days=30)).isoformat()
        failures = db.execute("SELECT count(*) FROM log WHERE business_id=? AND event='send_failed' "
                              'AND detail=? AND created_at>=?', (business_id, address, since)).fetchone()[0]
    return (False, 'delivery_failures') if failures >= FAILURE_LIMIT else (True, '')


def unsubscribe_url(settings, business_id, person_id):
    return settings.base_url + '/unsubscribe/' + unsubscribe_token(settings, business_id, person_id)


def footer(business_id, person_id, *, settings=None):
    """CAN-SPAM footer: who wrote, where they are, and how to stop it in one click."""
    from .settings import load
    settings = settings or load()
    with connect(settings.db_path) as db:
        business = db.execute('SELECT name,sender_name,postal_address FROM businesses WHERE id=?',
                              (business_id,)).fetchone()
    if not business:
        raise ValueError('Unknown business')
    sender = (business['sender_name'] or business['name'] or '').strip()
    postal = (business['postal_address'] or '').strip()
    link = unsubscribe_url(settings, business_id, person_id)
    return '\n\n--\n{}\n{}\nUnsubscribe: {}\n'.format(sender, postal, link)


def unsubscribe_headers(settings, business_id, person_id):
    """RFC 8058: the same URL the footer shows, as a one-click header pair."""
    return {'List-Unsubscribe': '<' + unsubscribe_url(settings, business_id, person_id) + '>',
            'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'}


def suppress(settings, business_id, email, reason):
    """Idempotent. Every path that stops contacting an address goes through here."""
    from .db import now
    with connect(settings.db_path) as db:
        db.execute('INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?)',
                   (business_id, (email or '').strip().lower(), reason[:64], now()))
