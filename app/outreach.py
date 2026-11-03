"""Cycle 2: write first, read the reply, lead the sale, hand over the offer.

Logic ported from evorove, architecture left behind:
- `draft_first_email` (outreach_service.py) and the "pain first, then touch"
  commits of 26 Oct 2026: the letter opens with the observed pain, not the offer.
- `SalesPolicyEngine` (engine/sales_policy.py): one governed move per inbound
  message, chosen by code. The model never picks the move, only phrases it.
- `SalesPolicyValidator` (engine/sales_response_validator.py): the one boundary
  every candidate passes before a customer sees it. A violation falls back to
  the move's safe wording instead of sending what the model wrote.
- `EmailInboxService` (email_inbox_service.py): quoted-text stripping, opt-out
  detection, automatic-reply detection, threading by Message-ID.

No SMS, ever: a person reachable only by phone is never contacted (TCPA).
"""
import imaplib
import json
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from uuid import uuid4

from . import guards
from .db import ROOT, connect, now
from .settings import decrypt_password, load

SMTP_TIMEOUT = 15
IMAP_TIMEOUT = 20
MAX_PER_POLL = 50
TEXT_LIMIT = 4000
PAIN_MAX = 500
SUBJECT_MAX = 60
BODY_MAX = 2000

# Ported verbatim from sales_response_validator.py: the two regexes are the
# whole reason a generated letter can be trusted.
UNAUTHORIZED_EXECUTION = re.compile(
    r"\b(?:your (?:booking|appointment) is confirmed|you(?:'ve| have) been booked|"
    r'payment (?:was|has been) processed|refund (?:was|has been) issued|'
    r'discount (?:was|has been) applied)\b', re.IGNORECASE)
SENSITIVE_CLAIM = re.compile(
    r'(?:[$€£]\s?\d[\d,.]*|\b\d{1,3}\s*%|\b(?:discount|guarantee|guaranteed|'
    r'free (?:trial|consultation|service|visit|estimate|upgrade)|for free|no cost|'
    r'limited time|only \d+ (?:spots?|slots?) left)\b)', re.IGNORECASE)
URL_OR_CONTACT = re.compile(r'https?://|www\.|\S+@\S+\.\S+|\+?\d[\d ()\-]{8,}\d')

QUOTE_START = re.compile(r'^(>|On .+wrote:\s*$|-{2,}\s*Original Message|From:\s|Sent from my )', re.IGNORECASE)
AUTO_SUBJECT = re.compile(r'^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|auto:)', re.IGNORECASE)
OPT_OUT_LINE = frozenset({'no', 'no thanks', 'no thank you', 'nope', 'stop', 'unsubscribe', 'remove',
                          'remove me', 'please remove me', 'not interested', 'no interest'})
OPT_OUT_ANYWHERE = re.compile(
    r'\b(unsubscribe|remove me|take me off|stop (emailing|contacting|writing|sending)|'
    r"do not (email|contact)|don'?t (email|contact)|not interested)\b", re.IGNORECASE)
HOSTILE = re.compile(r'\b(spam|scam|report (you|this)|fcc|ftc|attorney|lawyer|sue you|harass\w*)\b', re.IGNORECASE)

GREET, ASK_DISCOVERY, CONFIRM_NEED, PRESENT_VALUE = 'greet', 'ask_discovery', 'confirm_need', 'present_value'
DIAGNOSE_OBJECTION, ANSWER_OBJECTION = 'diagnose_objection', 'answer_objection'
OFFER, NURTURE, END_CONTACT, HANDOFF = 'offer', 'nurture', 'end_contact', 'handoff'

# A handoff's and a closing's exact wording are server-controlled, as in the old
# validator: these two moves are never phrased by the model.
VERBATIM_MOVES = frozenset({HANDOFF, END_CONTACT})

# One honest sentence per move. Used when the model is unavailable or writes
# something the validator rejects -- never an improvised answer.
SAFE_FALLBACK = {
    ASK_DISCOVERY: 'Thanks for writing back. What is the part of this that costs you the most time right now?',
    CONFIRM_NEED: 'Thanks — so I have it right: that is the problem you want solved first?',
    PRESENT_VALUE: 'Thanks for the detail. I think this is close to what we do; may I send you the specifics?',
    DIAGNOSE_OBJECTION: 'That is fair. So I answer the right thing: is it the cost, the timing, or the fit?',
    ANSWER_OBJECTION: 'That is a fair point, and I would rather answer it properly than quickly. May I come back to you on it?',
    OFFER: 'Here is what I can put in front of you.',
    NURTURE: 'Understood, and no pressure from me. If this moves up your list later, write back and I will pick it up.',
    END_CONTACT: 'Understood — I will leave it there. Thank you for the reply.',
    HANDOFF: 'Thanks for writing. This deserves a proper answer from a person rather than a quick one from me, '
             'so I am passing it on and someone will come back to you.',
}

ANALYSIS_FIELDS = {'problem': '', 'desired_outcome': '', 'objection': '', 'objection_cause': '',
                   'ready': False, 'hostile': False, 'requires_human': False}
ANALYSIS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': sorted(ANALYSIS_FIELDS), 'properties': {
    name: {'type': 'boolean' if isinstance(default, bool) else 'string'}
    for name, default in ANALYSIS_FIELDS.items()}}
REPLY_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['body'],
                'properties': {'body': {'type': 'string'}}}

_SETTINGS = None


def ask(system: str, user: str, *, schema: dict | None = None, web_search: bool = False,
        max_uses: int = 5, purpose: str) -> dict:
    """Stand-in for app.llm.ask while agent 2's module is unmerged.

    Same signature as the contract's llm.ask, so the merge replaces this whole
    function with: from .llm import ask
    """
    try:
        from .llm import ask as delegate
    except ImportError:
        raise RuntimeError('LLM is not installed') from None
    return delegate(system, user, schema=schema, web_search=web_search, max_uses=max_uses, purpose=purpose)


def settings_or_default(settings):
    global _SETTINGS
    if settings:
        return settings
    if _SETTINGS is None:
        _SETTINGS = load()
    return _SETTINGS


def prompt(name):
    return (ROOT / 'app/prompts' / name).read_text()


# --- the first letter -------------------------------------------------------

def usable_pain(value):
    """A pain the letter may open with: one concrete clause, not a label."""
    pain = ' '.join((value or '').split()).strip()
    if not pain or len(pain) > PAIN_MAX or len(pain.split()) < 3:
        return None
    # The opening sentence ends with exactly one full stop, so anything the
    # search agent packed after the first sentence is dropped rather than quoted.
    return pain.split('.')[0].strip().rstrip('.,;:') or None


def first_letter(business, person):
    """Pain first, then touch. Returns (subject, body) without the footer."""
    name = (person['name'] or '').strip()
    first_name = name.split(' ')[0] if name and '@' not in name else ''
    greeting = 'Hi ' + first_name + ',' if first_name else 'Hi there,'
    company = (business['name'] or 'our team').strip()
    pain = usable_pain(person['pain'])
    opening = ('Something I noticed: ' + pain + '.') if pain else (
        'I wrote because of what your team is working on right now.')
    segment = (person['segment'] or '').strip().rstrip('.')
    about = (company + ' is for ' + segment + '.') if segment else ('I am with ' + company + '.')
    body = '\n\n'.join([greeting, opening, about,
                        'Is that worth a quick look on your side?',
                        (business['sender_name'] or company).strip()])
    subject = ('A note for ' + first_name) if first_name else ('A note from ' + company)
    ok, _ = validate(opening + '\n' + about, GREET, '')
    if not ok:
        return None  # a pain or segment carrying a claim or a link is never quoted
    return subject[:SUBJECT_MAX], body


# --- policy and validation --------------------------------------------------

def analysis_from(payload):
    """AI output is untrusted: coerce every field, keep nothing extra."""
    if not isinstance(payload, dict):
        raise ValueError('Analysis is not an object')
    result = {}
    for field, default in ANALYSIS_FIELDS.items():
        value = payload.get(field, default)
        if isinstance(default, bool):
            result[field] = value is True
        else:
            result[field] = ' '.join(str(value or '').split())[:200]
    return result


def decide(analysis, sent_moves, business):
    """One governed move. Precedence is the old SalesPolicyEngine's, trimmed to
    email: no booking, no callback, no owner-fact round trip."""
    if analysis['requires_human']:
        return HANDOFF
    if analysis['objection']:
        return ANSWER_OBJECTION if analysis['objection_cause'] else DIAGNOSE_OBJECTION
    if not analysis['problem'] or not analysis['desired_outcome']:
        return ASK_DISCOVERY
    if CONFIRM_NEED not in sent_moves:
        return CONFIRM_NEED
    if analysis['ready'] and business['offer_text'] and business['payment_link']:
        return OFFER
    if PRESENT_VALUE not in sent_moves:
        return PRESENT_VALUE
    return NURTURE


def validate(text, move, allowed_facts):
    """The one authorization boundary. Returns (ok, problems)."""
    problems = []
    text = (text or '').strip()
    if not text or len(text) > BODY_MAX:
        problems.append('length')
    if UNAUTHORIZED_EXECUTION.search(text):
        problems.append('unauthorized_execution')
    if URL_OR_CONTACT.search(text):
        problems.append('link_or_contact_detail')
    for claim in SENSITIVE_CLAIM.findall(text):
        if claim.casefold() not in allowed_facts.casefold():
            problems.append('ungrounded_claim')
            break
    return not problems, problems


def compose_reply(business, person, inbound, analysis, move, *, llm=None):
    """Phrase the decided move, validate it, fall back when it does not pass."""
    if move in VERBATIM_MOVES:
        return SAFE_FALLBACK[move] + '\n\n' + (business['sender_name'] or business['name'] or '').strip()
    facts = business['offer_text'] or '' if move in {PRESENT_VALUE, ANSWER_OBJECTION, OFFER} else ''
    text = ''
    try:
        answer = (llm or ask)(
            prompt('sales_reply.md'),
            json.dumps({'APPROVED_MOVE': move, 'ALLOWED_FACTS': facts,
                        'OBSERVED_PAIN': person['pain'], 'PROSPECT_NAME': person['name'] or '',
                        'SELLER': business['sender_name'] or business['name'],
                        'ANALYSIS': analysis, 'CUSTOMER_CONTENT': inbound[:TEXT_LIMIT]},
                       ensure_ascii=False),
            schema=REPLY_SCHEMA, purpose='sales_reply')
        if isinstance(answer, dict):
            text = str(answer.get('body') or '')
    except Exception:
        text = ''
    ok, _ = validate(text, move, facts)
    body = text.strip() if ok else SAFE_FALLBACK[move]
    if move == OFFER:
        body = body + '\n\n' + business['offer_text'].strip() + '\n\n' + business['payment_link'].strip()
    return body + '\n\n' + (business['sender_name'] or business['name'] or '').strip()


# --- mail transports (injected in tests; no network there) ------------------

def smtp_send(target, message):
    with smtplib.SMTP(target['host'], target['port'], timeout=SMTP_TIMEOUT) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(target['user'], target['password'])
        client.send_message(message)


def imap_fetch(target, last_uid):
    """New INBOX messages after last_uid, read-only. The first read only records
    where the inbox is, so old mail is never mistaken for a reply."""
    client = imaplib.IMAP4_SSL(target['host'], target['port'], ssl_context=ssl.create_default_context(),
                               timeout=IMAP_TIMEOUT)
    try:
        client.login(target['user'], target['password'])
        client.select('INBOX', readonly=True)
        _, data = client.uid('SEARCH', None, 'UNSEEN' if last_uid is None else 'UID %d:*' % (last_uid + 1))
        uids = sorted(int(uid) for uid in (data[0] or b'').split())
        if last_uid is None:
            return [], (uids[-1] if uids else 0)
        messages = []
        for uid in [uid for uid in uids if uid > last_uid][:MAX_PER_POLL]:
            _, parts = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
            raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
            if raw is not None:
                messages.append(parse_email(uid, raw))
        return messages, (messages[-1]['uid'] if messages else last_uid)
    finally:
        try:
            client.logout()
        except Exception:
            pass


def parse_email(uid, raw):
    message = message_from_bytes(raw, policy=policy.default)
    part = message.get_body(preferencelist=('plain',))
    subject = str(message.get('Subject') or '')
    auto = str(message.get('Auto-Submitted') or 'no').strip().casefold()
    return {'uid': uid,
            'message_id': str(message.get('Message-ID') or '<uid-%d-%s@unknown>' % (uid, uuid4())).strip(),
            'from': parseaddr(str(message.get('From') or ''))[1].strip().lower(),
            'subject': subject, 'text': part.get_content() if part is not None else '',
            'in_reply_to': (str(message.get('In-Reply-To')).strip() if message.get('In-Reply-To') else ''),
            'references': (str(message.get('References')).strip() if message.get('References') else ''),
            'automatic': (auto != 'no' or str(message.get('Precedence') or '').strip().casefold()
                          in {'bulk', 'junk', 'auto_reply', 'list'}
                          or message.get('X-Autoreply') is not None
                          or bool(AUTO_SUBJECT.match(subject.strip())))}


def reply_text(text):
    """The new part of a reply, without the quoted thread under it."""
    kept = []
    for line in (text or '').replace('\r\n', '\n').split('\n'):
        if QUOTE_START.match(line.strip()):
            break
        kept.append(line)
    return '\n'.join(kept).strip()[:TEXT_LIMIT]


def is_opt_out(text):
    first_line = next((line for line in text.split('\n') if line.strip()), '')
    normalized = re.sub(r"[^a-z' ]", '', first_line.casefold()).strip()
    return normalized in OPT_OUT_LINE or bool(OPT_OUT_ANYWHERE.search(text[:500]))


# --- sending ----------------------------------------------------------------

def mailbox(settings, business, protocol):
    password = decrypt_password(settings, business[protocol + '_password_enc'])
    return {'host': business[protocol + '_host'], 'port': business[protocol + '_port'] or (587 if protocol == 'smtp' else 993),
            'user': business[protocol + '_user'], 'password': password}


def build_message(business, person, subject, body, headers):
    message = EmailMessage()
    message['From'] = formataddr(((business['sender_name'] or business['name'] or '').strip(), business['smtp_user']))
    message['To'] = person['email']
    message['Subject'] = subject
    message['Message-ID'] = make_msgid(domain=business['smtp_user'].rsplit('@', 1)[-1])
    for name, value in headers.items():
        message[name] = value
    message.set_content(body)
    return message


def deliver(settings, business, person, subject, body, move, *, send, headers=None):
    """Send one letter and record it, or record why it did not go. Returns the
    stored message id, or None when SMTP refused."""
    person_id, address = person['id'], person['email']
    full = body + guards.footer(business['id'], person_id, settings=settings)
    message = build_message(business, person, subject, full,
                            {**guards.unsubscribe_headers(settings, business['id'], person_id), **(headers or {})})
    try:
        send(mailbox(settings, business, 'smtp'), message)
    except Exception as exc:
        # Only the server rejecting THIS address counts against the address. An
        # auth, TLS or connection problem is the owner's to fix and must not
        # quietly suppress everyone it happened to interrupt.
        mailbox_fault = (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError,
                         smtplib.SMTPHeloError, smtplib.SMTPServerDisconnected)
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            event = 'bounce'
        elif isinstance(exc, mailbox_fault):
            event = 'smtp_error'
        elif isinstance(exc, smtplib.SMTPResponseException) and 500 <= (exc.smtp_code or 0) < 600:
            event = 'send_failed'
        else:
            event = 'smtp_error'
        with connect(settings.db_path) as db:
            db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                       (business['id'], person_id, event, address.lower(), now()))
        if event == 'bounce':
            guards.suppress(settings, business['id'], address, 'bounce')
        return None
    message_id = str(uuid4())
    stage = {GREET: 'in_progress', OFFER: 'offer_made'}.get(move)
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO messages(id,person_id,direction,subject,body,smtp_message_id,in_reply_to,move,created_at)'
                   " VALUES (?,?,'out',?,?,?,?,?,?)",
                   (message_id, person_id, subject, full, message['Message-ID'],
                    (headers or {}).get('In-Reply-To'), move, now()))
        if stage:
            db.execute('UPDATE people SET stage=?,last_move=?,updated_at=? WHERE id=?',
                       (stage, move, now(), person_id))
        else:
            db.execute('UPDATE people SET last_move=?,updated_at=? WHERE id=?', (move, now(), person_id))
    return message_id


def send_first_letters(business_id: str, *, settings=None, send=None, now_utc=None) -> int:
    """One pain-first letter to every cold person we have not written to yet."""
    settings = settings_or_default(settings)
    send = send or smtp_send
    moment = now_utc or datetime.now(timezone.utc)
    with connect(settings.db_path) as db:
        business = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
        if not business:
            raise ValueError('Unknown business')
        business = dict(business)
        people = [dict(row) for row in db.execute(
            "SELECT * FROM people WHERE business_id=? AND stage='cold' AND id NOT IN "
            "(SELECT person_id FROM messages WHERE direction='out') ORDER BY created_at,id", (business_id,))]
    sent = 0
    for person in people:
        if not (person['email'] or '').strip():
            continue  # a person with a phone and no email is never contacted
        allowed, reason = guards.can_send(business_id, person['email'], moment, settings=settings)
        if not allowed:
            with connect(settings.db_path) as db:
                db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                           (business_id, person['id'], 'send_blocked', reason, now()))
            if reason in {'outreach_paused', 'mailbox_not_configured', 'postal_address_required',
                          'base_url_required', 'daily_cap', 'unknown_business'}:
                break  # a business-wide block stops the run; quiet hours differ per recipient
            continue
        letter = first_letter(business, person)
        if not letter:
            with connect(settings.db_path) as db:
                db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                           (business_id, person['id'], 'send_blocked', 'unsafe_pain', now()))
            continue
        subject, body = letter
        if deliver(settings, business, person, subject, body, GREET, send=send):
            sent += 1
    return sent


# --- replies ----------------------------------------------------------------

def read_cursor(settings, business_id):
    with connect(settings.db_path) as db:
        row = db.execute("SELECT detail FROM log WHERE business_id=? AND event='imap_cursor' "
                         'ORDER BY id DESC LIMIT 1', (business_id,)).fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else None


def write_cursor(settings, business_id, uid):
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO log(business_id,event,detail,created_at) VALUES (?,?,?,?)',
                   (business_id, 'imap_cursor', str(uid), now()))


def match_person(db, business_id, message):
    """Threading first, sender address second. Never creates a person: cycle 1 owns that."""
    thread = [token for token in (message['in_reply_to'] + ' ' + message['references']).split() if token]
    for token in reversed(thread):
        row = db.execute('SELECT p.* FROM people p JOIN messages m ON m.person_id=p.id '
                         'WHERE p.business_id=? AND m.smtp_message_id=?', (business_id, token)).fetchone()
        if row:
            return dict(row)
    row = db.execute('SELECT * FROM people WHERE business_id=? AND lower(email)=?',
                     (business_id, message['from'])).fetchone()
    return dict(row) if row else None


def poll_replies(business_id: str, *, settings=None, send=None, fetch=None, llm=None) -> int:
    """Read the mailbox, store replies, answer each one once. Returns inbound count."""
    settings = settings_or_default(settings)
    send, fetch = send or smtp_send, fetch or imap_fetch
    with connect(settings.db_path) as db:
        row = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
    if not row:
        raise ValueError('Unknown business')
    business = dict(row)
    if not (business['imap_host'] and business['imap_user'] and business['imap_password_enc']):
        return 0
    last_uid = read_cursor(settings, business_id)
    messages, newest = fetch(mailbox(settings, business, 'imap'), last_uid)
    cursor = last_uid if messages else newest
    own = (business['smtp_user'] or '').strip().lower()
    received = 0
    for message in messages:
        try:
            if handle_reply(settings, business, message, own, send=send, llm=llm):
                received += 1
        except Exception:
            break  # retried on the next sweep; the cursor is not advanced past it
        cursor = message['uid']
    if cursor is not None and cursor != last_uid:
        write_cursor(settings, business_id, cursor)
    return received


def handle_reply(settings, business, message, own_address, *, send, llm=None):
    business_id = business['id']
    sender = message['from']
    if not sender or sender == own_address or message['automatic']:
        return False
    text = reply_text(message['text'])
    if not text:
        return False
    with connect(settings.db_path) as db:
        person = match_person(db, business_id, message)
        if not person:
            return False  # not someone cycle 2 wrote to: the inbox is left alone
        if db.execute('SELECT 1 FROM messages WHERE person_id=? AND smtp_message_id=?',
                      (person['id'], message['message_id'])).fetchone():
            return False
        db.execute('INSERT INTO messages(id,person_id,direction,subject,body,smtp_message_id,created_at)'
                   " VALUES (?,?,'in',?,?,?,?)",
                   (str(uuid4()), person['id'], message['subject'], text, message['message_id'], now()))
        db.execute('UPDATE people SET updated_at=? WHERE id=?', (now(), person['id']))
        sent_moves = {row[0] for row in db.execute(
            "SELECT move FROM messages WHERE person_id=? AND direction='out' AND move IS NOT NULL", (person['id'],))}
    if is_opt_out(text) or HOSTILE.search(text[:500]):
        return stop_contact(settings, business_id, person, 'reply_opt_out')
    if person['stage'] in {'done', 'lost'}:
        return True  # shown on the card, not answered
    analysis = dict(ANALYSIS_FIELDS)
    try:
        analysis = analysis_from((llm or ask)(
            prompt('sales_analysis.md'),
            json.dumps({'CUSTOMER_CONTENT': text, 'OBSERVED_PAIN': person['pain'],
                        'THREAD_SUBJECT': message['subject']}, ensure_ascii=False),
            schema=ANALYSIS_SCHEMA, purpose='sales_analysis'))
    except Exception:
        pass  # an unreadable analysis keeps discovery going; it never invents readiness
    if analysis['hostile']:
        return stop_contact(settings, business_id, person, 'hostile_reply')
    move = decide(analysis, sent_moves, business)
    allowed, reason = guards.can_send(business_id, person['email'], datetime.now(timezone.utc), settings=settings)
    if not allowed:
        with connect(settings.db_path) as db:
            db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                       (business_id, person['id'], 'reply_blocked', reason, now()))
        return True
    subject = message['subject'].strip() or 'Your reply'
    if not subject.casefold().startswith('re:'):
        subject = 'Re: ' + subject
    references = ' '.join(part for part in (message['references'], message['message_id']) if part)
    deliver(settings, business, person, subject[:255],
            compose_reply(business, person, text, analysis, move, llm=llm), move, send=send,
            headers={'In-Reply-To': message['message_id'], 'References': references[-900:]})
    return True


def stop_contact(settings, business_id, person, reason):
    guards.suppress(settings, business_id, person['email'], reason)
    with connect(settings.db_path) as db:
        db.execute("UPDATE people SET stage='lost',last_move=?,updated_at=? WHERE id=?",
                   (END_CONTACT, now(), person['id']))
        db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                   (business_id, person['id'], 'outreach_stopped', reason, now()))
    return True


def record_payment(business_id: str, person_id: str, source: str, *, settings=None, db=None) -> None:
    """The only path to Done. A click on the payment link is not a payment.

    `db` lets billing.py call this inside the transaction that already holds the
    webhook's BEGIN IMMEDIATE; a second connection would only wait on it.
    """
    def apply(db):
        if not db.execute('SELECT 1 FROM people WHERE business_id=? AND id=?', (business_id, person_id)).fetchone():
            raise ValueError('Unknown person')
        db.execute("UPDATE people SET stage='done',updated_at=? WHERE business_id=? AND id=?",
                   (now(), business_id, person_id))
        db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                   (business_id, person_id, 'payment_recorded', str(source)[:64], now()))

    if db is not None:
        return apply(db)
    with connect(settings_or_default(settings).db_path) as connection:
        return apply(connection)
