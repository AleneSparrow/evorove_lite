"""Owner notifications; errors never include provider URLs or exception text."""
from datetime import datetime, timedelta, timezone

import httpx

from .db import connect, now


def telegram(settings, text):
    token, chat = settings.get('TELEGRAM_BOT_TOKEN'), settings.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return False
    try:
        response = httpx.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              json={'chat_id': chat, 'text': text[:4000]}, timeout=15)
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError()
        return True
    except Exception:
        with connect(settings.db_path) as db:
            db.execute("INSERT INTO log(event,detail,created_at) VALUES ('telegram_failed','Notification delivery failed',?)", (now(),))
        return False


def failure_alert(settings, business_id, kind, error_type):
    return telegram(settings, f'Evorove job failed: {kind}\nBusiness: {business_id or "system"}\nError: {error_type}')


def daily_summary(settings, current=None):
    current = current or datetime.now(timezone.utc)
    since = (current - timedelta(hours=24)).isoformat()
    with connect(settings.db_path) as db:
        businesses = db.execute('SELECT id,name FROM businesses ORDER BY created_at').fetchall()
        system_errors = db.execute("SELECT count(*) FROM log WHERE business_id IS NULL AND created_at>=? AND (event LIKE '%failed' OR event='error')", (since,)).fetchone()[0]
        parts = ['Evorove — last 24 hours', f'System errors: {system_errors}']
        for business in businesses:
            bid = business['id']
            last = db.execute("SELECT status,found FROM runs WHERE business_id=? AND kind='search' ORDER BY started_at DESC,id DESC LIMIT 1", (bid,)).fetchone()
            counts = {}
            for direction, label in (('out', 'Letters'), ('in', 'Replies')):
                counts[label] = db.execute('SELECT count(*) FROM messages m JOIN people p ON p.id=m.person_id WHERE p.business_id=? AND m.direction=? AND m.created_at>=?', (bid, direction, since)).fetchone()[0]
            counts['Offers'] = db.execute("SELECT count(*) FROM messages m JOIN people p ON p.id=m.person_id WHERE p.business_id=? AND m.direction='out' AND lower(m.move)='offer' AND m.created_at>=?", (bid, since)).fetchone()[0]
            counts['Payments'] = db.execute('SELECT count(*) FROM payments WHERE business_id=? AND test_mode=0 AND created_at>=?', (bid, since)).fetchone()[0]
            counts['Errors'] = db.execute("SELECT count(*) FROM runs WHERE business_id=? AND status='failed' AND started_at>=?", (bid, since)).fetchone()[0]
            counts['Errors'] += db.execute("SELECT count(*) FROM log WHERE business_id=? AND created_at>=? AND (event LIKE '%failed' OR event='error')", (bid, since)).fetchone()[0]
            parts.append(f'{business["name"]}: Search {last["status"] + ", found " + str(last["found"]) if last else "not run"}; ' + '; '.join(f'{key}: {value}' for key, value in counts.items()))
    # Deliver all businesses without silently truncating the tail of a large account.
    delivered = True
    chunk = ''
    for part in parts:
        if len(chunk) + len(part) > 3900:
            delivered = telegram(settings, chunk) and delivered
            chunk = ''
        chunk += part + '\n'
    return telegram(settings, chunk) and delivered
