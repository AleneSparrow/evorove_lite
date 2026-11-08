"""Check one configured business mailbox, without changing application state.

Supply DB_PATH and SECRET_KEY in the process environment; no .env is loaded.
--check-imap only checks IMAP login/logout and sends no email.
"""
import argparse
from contextlib import closing
from email.headerregistry import Address
from email.message import EmailMessage
import imaplib
import os
from pathlib import Path
import sqlite3
import ssl
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.outreach import mailbox, smtp_send
from app.settings import Settings


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes argument values. Keep failures private too.
        raise ValueError()


def check_imap(target):
    client = imaplib.IMAP4_SSL(target['host'], target['port'],
                              ssl_context=ssl.create_default_context(), timeout=30)
    try:
        status, _ = client.login(target['user'], target['password'])
        if status != 'OK':
            raise imaplib.IMAP4.error()
    finally:
        client.logout()


def run(settings, business_id, recipient=None, *, imap=False):
    if not settings.get('DB_PATH') or not settings.secret:
        raise ValueError()
    uri = Path(settings.db_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        business = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
    if business is None:
        raise LookupError()
    protocol = 'imap' if imap else 'smtp'
    if not all(business[protocol + suffix] for suffix in ('_host', '_user', '_password_enc')):
        raise ValueError()
    target = mailbox(settings, business, protocol)
    if imap:
        check_imap(target)
        return
    address = Address(addr_spec=recipient or '')
    if not address.username or not address.domain:
        raise ValueError()
    message = EmailMessage()
    message['From'] = business['smtp_user']
    message['To'] = address.addr_spec
    message['Subject'] = 'Evorove test'
    message.set_content('This is a test email from Evorove.')
    smtp_send(target, message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--business-id', required=True)
        parser.add_argument('--to')
        parser.add_argument('--check-imap', action='store_true')
        args = parser.parse_args(argv)
        if not args.check_imap and not args.to:
            raise ValueError()
        run(Settings(dict(os.environ)), args.business_id, args.to, imap=args.check_imap)
    except Exception as exc:
        print(type(exc).__name__)
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
