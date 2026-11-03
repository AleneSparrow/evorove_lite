"""Tenant-scoped board queries and owner configuration."""
import re
from urllib.parse import urlparse

from fastapi import HTTPException
from itsdangerous import URLSafeSerializer, BadSignature

from .db import connect, now
from .settings import encrypt_password

STAGES = (('cold', 'Cold'), ('in_progress', 'In progress'), ('offer_made', 'Offer made'), ('done', 'Done'))


def business(settings, business_id):
    with connect(settings.db_path) as db:
        row = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Business not found')
    # Passwords are never returned to the presentation layer.
    return {key: value for key, value in dict(row).items() if not key.endswith('_password_enc')}


def columns(settings, business_id):
    with connect(settings.db_path) as db:
        rows = db.execute('SELECT * FROM people WHERE business_id=? ORDER BY updated_at DESC, id', (business_id,)).fetchall()
    return [{'stage': stage, 'title': title, 'people': [dict(row) for row in rows if row['stage'] == stage]} for stage, title in STAGES]


def person(settings, business_id, person_id):
    with connect(settings.db_path) as db:
        row = db.execute('SELECT * FROM people WHERE business_id=? AND id=?', (business_id, person_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Person not found')
        messages = [dict(item) for item in db.execute('SELECT * FROM messages WHERE person_id=? ORDER BY created_at,id', (person_id,))]
    result = dict(row)
    result['messages'] = messages
    for key in ('source_url', 'website'):
        if urlparse(result.get(key) or '').scheme not in {'http', 'https'}:
            result[key] = None
    return result


def last_search(settings, business_id):
    with connect(settings.db_path) as db:
        row = db.execute("SELECT * FROM runs WHERE business_id=? AND kind='search' ORDER BY started_at DESC,id DESC LIMIT 1", (business_id,)).fetchone()
    return dict(row) if row else None


def save_settings(settings, business_id, fields):
    values = {key: fields.get(key, '').strip() for key in (
        'offer_text', 'payment_link', 'postal_address', 'sender_name', 'smtp_host', 'smtp_user',
        'imap_host', 'imap_user', 'quiet_start', 'quiet_end', 'brief')}
    if not 10 <= len(values['postal_address']) <= 1000:
        raise HTTPException(400, 'Postal address must contain 10–1000 characters')
    if values['payment_link'] and (urlparse(values['payment_link']).scheme != 'https' or not urlparse(values['payment_link']).hostname):
        raise HTTPException(400, 'Payment link must be an HTTPS URL')
    for key in ('quiet_start', 'quiet_end'):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', values[key]):
            raise HTTPException(400, 'Quiet hours must use HH:MM')
    try:
        values['daily_cap'] = int(fields.get('daily_cap', '20'))
        values['smtp_port'] = int(fields.get('smtp_port') or '587')
        values['imap_port'] = int(fields.get('imap_port') or '993')
        if not 1 <= values['daily_cap'] <= 1000 or not all(1 <= values[key] <= 65535 for key in ('smtp_port', 'imap_port')):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Invalid daily cap or mail port') from None
    values['outreach_paused'] = int(fields.get('outreach_paused') == 'on')
    for protocol in ('smtp', 'imap'):
        password = fields.get(protocol + '_password', '')
        if password:
            values[protocol + '_password_enc'] = encrypt_password(settings, password)
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(), business_id))


def unsubscribe_token(settings, business_id, person_id):
    return URLSafeSerializer(settings.secret, salt='unsubscribe').dumps([business_id, person_id])


def unsubscribe(settings, token, *, apply=False):
    try:
        business_id, person_id = URLSafeSerializer(settings.secret, salt='unsubscribe').loads(token)
    except (BadSignature, ValueError, TypeError):
        raise HTTPException(404, 'Invalid unsubscribe link') from None
    with connect(settings.db_path) as db:
        row = db.execute('SELECT email FROM people WHERE business_id=? AND id=?', (business_id, person_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Invalid unsubscribe link')
        if apply:
            db.execute('INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?)', (business_id, row['email'].lower(), 'unsubscribe', now()))
    return row['email']
