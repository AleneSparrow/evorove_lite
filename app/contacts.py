"""Owner-supplied contacts enter the existing Cold/outreach pipeline."""
import csv
import io
from email import policy
from email.parser import BytesParser
from uuid import uuid4
from urllib.parse import urlparse

from fastapi import HTTPException

from . import outreach
from .auth import email_address
from .db import connect, now

COLUMNS = ('email', 'name', 'company', 'website', 'role', 'us_state', 'note')


def multipart_fields(content_type, body):
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    if not message.is_multipart():
        raise HTTPException(400, 'Invalid upload form')
    fields = {}
    for part in message.iter_parts():
        key = part.get_param('name', header='content-disposition')
        if not key or key in fields:
            raise HTTPException(400, 'Invalid upload fields')
        try:
            fields[key] = part.get_payload(decode=True).decode('utf-8-sig')
        except (UnicodeError, AttributeError):
            raise HTTPException(400, 'Upload a UTF-8 CSV file') from None
    return fields


def csv_rows(text):
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), strict=True)
        if not reader.fieldnames or not {'email', 'note'} <= set(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise HTTPException(400, 'CSV needs unique column headers including email and note')
        rows = []
        for row in reader:
            if len(rows) == 1000:
                raise HTTPException(400, 'Upload at most 1000 contacts at a time')
            rows.append(row)
        return rows
    except csv.Error:
        raise HTTPException(400, 'Invalid CSV format') from None


def add(settings, business_id, rows):
    if len(rows) > 1000:
        raise HTTPException(400, 'Upload at most 1000 contacts at a time')
    report = {'added': 0, 'skipped': 0, 'issues': []}
    with connect(settings.db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        for number, row in enumerate(rows, 1):
            values = {key: (row.get(key) or '').strip() for key in COLUMNS}
            reason = ''
            try:
                values['email'] = email_address(values['email'])
            except HTTPException:
                reason = 'Invalid email'
            if not reason and (None in row or any(len(value) > (500 if key == 'note' else 1000) for key, value in values.items())):
                reason = 'Extra columns or field too long (note: maximum 500 characters)'
            if not reason and not values['note']:
                reason = 'Note is required: explain the reason for the first email'
            if not reason and not outreach.validate(values['note'], outreach.GREET, '')[0]:
                reason = 'Note contains a link, price or discount — remove it'
            if not reason and values['website']:
                try:
                    parsed = urlparse(values['website'])
                    valid = parsed.scheme in {'http', 'https'} and parsed.hostname
                except ValueError:
                    valid = False
                if not valid:
                    reason = 'Website must be an HTTP or HTTPS URL'
            if not reason and db.execute('SELECT 1 FROM suppressions WHERE business_id=? AND lower(email)=?', (business_id, values['email'])).fetchone():
                reason = 'Suppressed email'
            if not reason and db.execute('SELECT 1 FROM people WHERE business_id=? AND lower(email)=?', (business_id, values['email'])).fetchone():
                reason = 'Duplicate email'
            if reason:
                report['skipped'] += 1
                report['issues'].append({'row': number, 'reason': reason})
                continue
            stamp = now()
            db.execute("INSERT INTO people(id,business_id,email,name,company,website,role,us_state,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'owner','cold',?,?)",
                       (str(uuid4()), business_id, values['email'], values['name'], values['company'], values['website'],
                        values['role'], values['us_state'], values['note'], values['note'], stamp, stamp))
            report['added'] += 1
    return report
