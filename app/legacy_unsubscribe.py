"""Compatibility with Engine email_outreach_service.read_unsubscribe_token.

Legacy links use base64 JSON + truncated HMAC-SHA256, not Fernet encryption.
"""
import base64
import hashlib
import hmac
import json

from app.db import connect, now


def unsubscribe(settings, token, *, apply=False):
    key = settings.get('LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY')
    body, _, signature = token.partition('.')
    if not key or not body or len(token) > 8192:
        return None
    expected = hmac.new(f'unsubscribe:{key}'.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    try:
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if not isinstance(payload, list) or len(payload) != 2 or not all(isinstance(v, str) for v in payload):
            return None
        old_business_id, email = payload
        email = email.strip().casefold()
        if not email or '@' not in email or '\r' in email or '\n' in email:
            return None
    except (ValueError, TypeError, UnicodeError):
        return None
    with connect(settings.db_path) as db:
        mapping = db.execute('SELECT business_id FROM legacy_business_map WHERE old_business_id=?',
                             (old_business_id,)).fetchone()
        if not mapping:
            return None
        if apply:
            db.execute('INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?)',
                       (mapping['business_id'], email, 'unsubscribe', now()))
    return email
