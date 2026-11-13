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
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from uuid import uuid4

from . import billing, guards, notify
from .db import ROOT, connect, now
from .llm import ask
from .settings import decrypt_password, load

SMTP_TIMEOUT = 15
IMAP_TIMEOUT = 20
MAX_PER_POLL = 50
TEXT_LIMIT = 4000
PAIN_MAX = 500
SUBJECT_MAX = 60
BODY_MAX = 2000
THREAD_CHAR_LIMIT = 12000
DUPLICATE_WINDOW = timedelta(minutes=10)

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
# A domain with no scheme (acme-clinic.com, bit.ly/abc123) is still a link, so
# it is matched directly: a letter/digit run, a dot, a 2+ letter TLD, with no
# space around the dot -- the one thing that is never true of a sentence's own
# closing full stop (always followed by whitespace or the end of the text) or
# an abbreviation like "e.g." (never 2+ letters after the dot).
URL_OR_CONTACT = re.compile(
    r'https?://|www\.|<[a-z][^>]*>|'
    r'\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z]{2,24}(?:/\S*)?\b|'
    r'\S+@\S+\.\S+|\+?\d[\d ()\-]{8,}\d', re.IGNORECASE)

# Words that mark a "name" as a company, department or role label rather than
# a person -- the search agent sometimes hands over a business name or a team
# label when it cannot find an actual decision maker.
COMPANY_WORDS = frozenset((
    'llc inc incorporated corp corporation co company group holdings solutions '
    'services enterprises industries partners associates consulting team staff '
    'department dept support sales marketing admin administration office ltd plc '
    'clinic practice studio agency firm front desk reception receptionist manager '
    'owner founder representative rep contact billing accounts operations ops '
    'customer client help hr'
).split())
# Title case only: a real first/last name, not an all-caps label or a stray
# token. The optional apostrophe/hyphen segment covers "O'Brien", "D'Angelo",
# "Smith-Jones"; the leading part may be a single letter ("O") so it does too.
NAME_WORD = re.compile(r"^[A-Z][a-z]*(?:['\-][A-Z]?[a-z]+)?$")

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
OFFER, OFFER_AGAIN, NURTURE, END_CONTACT, HANDOFF = 'offer', 'offer_again', 'nurture', 'end_contact', 'handoff'
ANSWER_QUESTION = 'answer_question'

# A handoff's and a closing's exact wording are server-controlled, as in the old
# validator: these two moves are never phrased by the model.
VERBATIM_MOVES = frozenset({HANDOFF, END_CONTACT})

# Caps so a policy that never got what it needed stops asking instead of
# looping on the person forever.
MAX_ASK_DISCOVERY = 2
MAX_ANSWER_OBJECTION = 2
MAX_OFFER_AGAIN = 2

# One honest sentence per move. Used when the model is unavailable or writes
# something the validator rejects -- never an improvised answer. A move that
# can legitimately be sent more than once in a thread (discovery, diagnosing
# or answering an objection, resending the link) gets more than one variant,
# so a second fallback in a row is never a verbatim repeat of the first.
SAFE_FALLBACK = {
    ASK_DISCOVERY: (
        'Thanks for writing back. What is the part of this that costs you the most time right now?',
        "One more angle on that -- what have you already tried, and what didn't quite work?",
    ),
    CONFIRM_NEED: 'Thanks — so I have it right: that is the problem you want solved first?',
    PRESENT_VALUE: 'Thanks for the detail. I think this is close to what we do; may I send you the specifics?',
    DIAGNOSE_OBJECTION: (
        'That is fair. So I answer the right thing: is it the cost, the timing, or the fit?',
        'Understood. Just so I answer the real concern -- what would need to be true for this to make sense?',
    ),
    ANSWER_OBJECTION: (
        'That is a fair point, and I would rather answer it properly than quickly. May I come back to you on it?',
        'I hear you, and I do not want to just repeat myself -- what would actually change your mind here?',
    ),
    OFFER: 'Here is what I can put in front of you.',
    OFFER_AGAIN: (
        'Here it is again, in case it did not come through.',
        'Sending this once more for you.',
    ),
    ANSWER_QUESTION: 'I would rather not guess at that than get it wrong — what matters most for your decision here?',
    NURTURE: 'Understood, and no pressure from me. If this moves up your list later, write back and I will pick it up.',
    END_CONTACT: 'Understood — I will leave it there. Thank you for the reply.',
    HANDOFF: 'Thanks for writing. This deserves a proper answer from a person rather than a quick one from me, '
             'so I am passing it on and someone will come back to you.',
}

ANALYSIS_FIELDS = {'problem': '', 'desired_outcome': '', 'objection': '', 'objection_cause': '', 'question': '',
                   'ready': False, 'hostile': False, 'requires_human': False}
ANALYSIS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': sorted(ANALYSIS_FIELDS), 'properties': {
    name: {'type': 'boolean' if isinstance(default, bool) else 'string'}
    for name, default in ANALYSIS_FIELDS.items()}}
REPLY_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['body'],
                'properties': {'body': {'type': 'string'}}}

_SETTINGS = None




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


def usable_person_name(value):
    """A name the letter may greet by first name: 1-3 title-case words, no line
    break, none of them a company or department word. Anything else -- a
    business name, a role label, a parsing accident -- greets "Hi," instead."""
    text = (value or '').strip()
    if not text or '\n' in text or '\r' in text:
        return None
    words = text.split()
    if not 1 <= len(words) <= 3:
        return None
    if any(not NAME_WORD.match(word) or word.casefold() in COMPANY_WORDS for word in words):
        return None
    return words[0]


def first_letter(business, person):
    """Pain first, then touch. Returns (subject, body) without the footer."""
    first_name = usable_person_name(person['name'])
    greeting = 'Hi ' + first_name + ',' if first_name else 'Hi,'
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
    """One governed move. Precedence: requires_human, an objection, a ready
    buyer's offer (or resend), a direct question, then discovery/confirm/
    value/nurture. `sent_moves` is every move already sent to this person --
    a set or a list both work, since only membership and counts are used.

    `problem` and `desired_outcome` describe what is known from the whole
    THREAD, not only the newest message (sales_analysis.md asks the model to
    keep stating them once learned); CONFIRM_NEED already having been sent
    is an equally reliable, cheaper signal that they were known at some
    point, so a later message that does not repeat them does not re-open
    discovery."""
    sent_moves = list(sent_moves)
    if analysis['requires_human']:
        return HANDOFF
    if analysis['objection']:
        if not analysis['objection_cause']:
            return DIAGNOSE_OBJECTION
        if sent_moves.count(ANSWER_OBJECTION) >= MAX_ANSWER_OBJECTION:
            return NURTURE  # the same objection, answered twice already
        return ANSWER_OBJECTION
    offer_available = bool(business['offer_text'] and business['payment_link'])
    if offer_available and analysis['ready']:
        if OFFER not in sent_moves:
            return OFFER  # ready gets the offer, whether or not discovery or confirm_need happened
        if sent_moves.count(OFFER_AGAIN) < MAX_OFFER_AGAIN:
            return OFFER_AGAIN  # still ready, asking again: resend, do not re-discover
    if analysis['question']:
        return ANSWER_QUESTION
    state_known = bool(analysis['problem'] and analysis['desired_outcome']) or CONFIRM_NEED in sent_moves
    if not state_known:
        if sent_moves.count(ASK_DISCOVERY) >= MAX_ASK_DISCOVERY:
            return NURTURE  # asked twice, still nothing to confirm
        return ASK_DISCOVERY
    if CONFIRM_NEED not in sent_moves:
        return CONFIRM_NEED
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


def fallback_for(move, sent_moves):
    """SAFE_FALLBACK's text for this move. A move with more than one variant
    picks a later one once it has already been sent before, so a capped
    move -- asked, answered, or offered again more than once -- never
    repeats itself verbatim in the same thread."""
    variants = SAFE_FALLBACK[move]
    if isinstance(variants, str):
        return variants
    index = min(list(sent_moves).count(move), len(variants) - 1)
    return variants[index]


def compose_reply(business, person, inbound, analysis, move, thread, sent_moves, *, llm=None):
    """Phrase the decided move, validate it, fall back when it does not pass."""
    if move in VERBATIM_MOVES:
        return fallback_for(move, sent_moves) + '\n\n' + (business['sender_name'] or business['name'] or '').strip()
    facts = business['offer_text'] or '' if move in {PRESENT_VALUE, ANSWER_OBJECTION, OFFER, OFFER_AGAIN,
                                                      ANSWER_QUESTION} else ''
    text = ''
    try:
        answer = (llm or ask)(
            prompt('sales_reply.md'),
            json.dumps({'APPROVED_MOVE': move, 'ALLOWED_FACTS': facts,
                        'OBSERVED_PAIN': person['pain'], 'PROSPECT_NAME': usable_person_name(person['name']) or '',
                        'SELLER': business['sender_name'] or business['name'],
                        'ANALYSIS': analysis, 'CUSTOMER_CONTENT': inbound[:TEXT_LIMIT], 'THREAD': thread},
                       ensure_ascii=False),
            schema=REPLY_SCHEMA, purpose='sales_reply')
        if isinstance(answer, dict):
            text = str(answer.get('body') or '')
    except Exception:
        text = ''
    ok, _ = validate(text, move, facts)
    body = text.strip() if ok else fallback_for(move, sent_moves)
    if move in {OFFER, OFFER_AGAIN}:
        body = body + '\n\n' + business['offer_text'].strip() + '\n\n' + billing.payment_link_for(business, person['id'])
    return body + '\n\n' + (business['sender_name'] or business['name'] or '').strip()


def next_reply(business, person, message_row, *, llm=None):
    """The whole sales-policy decision for one inbound message: analyse it in
    context, decide the move, phrase it. Pure -- no DB, no SMTP, no footer,
    no guards -- so any channel can ask "what would we say" without sending
    anything. Returns (move, body).

    `person` must already carry 'sent_moves' (every move already sent to
    them, for decide()) and `message_row` must carry 'thread' (the
    conversation so far, oldest first, as build_thread returns) -- both
    prepared by the caller, since this function does no I/O of its own.
    `message_row` may also carry a 'channel' column from a migration not
    merged into this branch yet; it is read with .get and defaults to
    'email', so this keeps working whether or not that column exists.

    A hostile message is reported back as END_CONTACT with its fixed,
    courteous closing text -- decide() itself never returns that move, so a
    caller can tell the two apart and still do its own stopping (suppression,
    stage change) on this one signal.
    """
    text = message_row['body']
    thread = message_row.get('thread') or []
    sent_moves = list(person.get('sent_moves') or [])
    channel = message_row.get('channel') or 'email'
    analysis = dict(ANALYSIS_FIELDS)
    try:
        analysis = analysis_from((llm or ask)(
            prompt('sales_analysis.md'),
            json.dumps({'CUSTOMER_CONTENT': text, 'OBSERVED_PAIN': person['pain'],
                        'THREAD_SUBJECT': message_row.get('subject') or '', 'THREAD': thread,
                        'CHANNEL': channel}, ensure_ascii=False),
            schema=ANALYSIS_SCHEMA, purpose='sales_analysis'))
    except Exception:
        pass  # an unreadable analysis keeps discovery going; it never invents readiness
    move = END_CONTACT if analysis['hostile'] else decide(analysis, sent_moves, business)
    body = compose_reply(business, person, text, analysis, move, thread, sent_moves, llm=llm)
    return move, body


# --- mail transports (injected in tests; no network there) ------------------

def smtp_send(target, message):
    with smtplib.SMTP(target['host'], target['port'], timeout=SMTP_TIMEOUT) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(target['user'], target['password'])
        client.send_message(message)


def imap_fetch(target, last_uid, last_uidvalidity=None):
    """New INBOX messages after last_uid, read-only. The first read only records
    where the inbox is, so old mail is never mistaken for a reply.

    Returns (messages, newest_uid, uidvalidity). UIDVALIDITY identifies this
    incarnation of the mailbox: a provider may reuse UIDs after a rebuild, so a
    changed value means the stored last_uid no longer means anything. In that
    case we fall back to reading the last 7 days by date; handle_reply's
    Message-ID check keeps that re-read from duplicating an answered message.
    """
    client = imaplib.IMAP4_SSL(target['host'], target['port'], ssl_context=ssl.create_default_context(),
                               timeout=IMAP_TIMEOUT)
    try:
        client.login(target['user'], target['password'])
        client.select('INBOX', readonly=True)
        # getattr/.get, not an attribute/index access: a minimal IMAP4_SSL test
        # double (real imaplib always has this) still gets a valid, if inert,
        # uidvalidity of 0 instead of an AttributeError.
        untagged = getattr(client, 'untagged_responses', {}) or {}
        uidvalidity = int((untagged.get('UIDVALIDITY') or [b'0'])[0])
        reset = last_uid is not None and last_uidvalidity is not None and uidvalidity != last_uidvalidity
        if last_uid is None:
            _, data = client.uid('SEARCH', None, 'ALL')
            uids = sorted(int(uid) for uid in (data[0] or b'').split())
            return [], (uids[-1] if uids else 0), uidvalidity
        if reset:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%d-%b-%Y')
            _, data = client.uid('SEARCH', None, 'SINCE', since)
            candidates = sorted(int(uid) for uid in (data[0] or b'').split())
        else:
            _, data = client.uid('SEARCH', None, 'UID %d:*' % (last_uid + 1))
            candidates = [uid for uid in sorted(int(uid) for uid in (data[0] or b'').split()) if uid > last_uid]
        messages = []
        for uid in candidates[:MAX_PER_POLL]:
            _, parts = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
            raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
            if raw is not None:
                messages.append(parse_email(uid, raw))
        return messages, (messages[-1]['uid'] if messages else last_uid), uidvalidity
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


def mailbox_daily_limit(business, moment):
    first = business['mailbox_first_sent_at']
    days = max(0, (moment - datetime.fromisoformat(first)).days) if first else 0
    return min(business['daily_cap'] or 20, 10 + 5 * days)


def reserve_send(settings, business, moment):
    """Count in-flight SMTP too: two concurrent jobs cannot overshoot the cap."""
    with connect(settings.db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM businesses WHERE id=?', (business['id'],)).fetchone()
        if current['mailbox_generation'] != business['mailbox_generation']:
            return None
        day = guards._owner_day_start(settings, moment)
        used = db.execute('SELECT count(*) FROM mailbox_sends WHERE business_id=? AND generation=? AND created_at>=?',
                          (business['id'], current['mailbox_generation'], day)).fetchone()[0]
        if used >= mailbox_daily_limit(current, moment):
            db.execute("INSERT INTO log(business_id,event,detail,created_at) VALUES (?,'send_blocked','mailbox_daily_cap',?)",
                       (business['id'], now()))
            return None
        reservation = str(uuid4())
        db.execute('INSERT INTO mailbox_sends VALUES (?,?,?,?)',
                   (reservation, business['id'], current['mailbox_generation'], moment.isoformat()))
        return reservation


def deliver(settings, business, person, subject, body, move, *, send, headers=None, now_utc=None):
    """Send one letter and record it, or record why it did not go. Returns the
    stored message id, or None when SMTP refused."""
    person_id, address = person['id'], person['email']
    full = body + guards.footer(business['id'], person_id, settings=settings)
    message = build_message(business, person, subject, full,
                            {**guards.unsubscribe_headers(settings, business['id'], person_id), **(headers or {})})
    moment = now_utc or datetime.now(timezone.utc)
    reservation = None
    if move == GREET:
        reservation = reserve_send(settings, business, moment)
        if reservation is None:
            return None
    else:
        # Replies have their own REPLY_CAP and all ordinary guards, but never
        # consume the first-letter allowance or start the warmup clock.
        allowed, _ = guards.can_send(business['id'], address, moment, settings=settings, kind='reply')
        if not allowed:
            return None
        with connect(settings.db_path) as db:
            current = db.execute('SELECT mailbox_generation FROM businesses WHERE id=?', (business['id'],)).fetchone()
        if not current or current['mailbox_generation'] != business['mailbox_generation']:
            return None
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
            db.execute('DELETE FROM mailbox_sends WHERE id=?', (reservation,))
            db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                       (business['id'], person_id, event, address.lower(), now()))
        if event == 'bounce':
            guards.suppress(settings, business['id'], address, 'bounce')
        return None
    message_id = str(uuid4())
    stage = {GREET: 'in_progress', OFFER: 'offer_made'}.get(move)
    with connect(settings.db_path) as db:
        if move == GREET:
            db.execute('UPDATE businesses SET mailbox_first_sent_at=COALESCE(mailbox_first_sent_at,?) WHERE id=? AND mailbox_generation=?',
                       (moment.isoformat(), business['id'], business['mailbox_generation']))
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
        if deliver(settings, business, person, subject, body, GREET, send=send, now_utc=moment):
            sent += 1
    return sent


# --- replies ----------------------------------------------------------------

def read_cursor(settings, business_id):
    """The stored IMAP read position, for callers that only need the UID."""
    with connect(settings.db_path) as db:
        row = db.execute('SELECT imap_last_uid FROM businesses WHERE id=?', (business_id,)).fetchone()
    return row['imap_last_uid'] if row else None


def write_cursor(settings, business_id, uid, uidvalidity, generation=None):
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET imap_last_uid=?,imap_uidvalidity=? WHERE id=?'
                   + (' AND mailbox_generation=?' if generation is not None else ''),
                   (uid, uidvalidity, business_id, generation) if generation is not None else (uid, uidvalidity, business_id))


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


def poll_replies(business_id: str, *, settings=None, send=None, fetch=None, llm=None, now_utc=None) -> int:
    """Read the mailbox, store replies, answer each one once. Returns inbound count.

    Every poll also retries older inbound messages that never got an answer --
    blocked earlier by quiet hours, the reply cap, or any other guard -- so a
    reply waits for its own allowed window instead of being lost once the IMAP
    cursor moves past it.
    """
    settings = settings_or_default(settings)
    send, fetch = send or smtp_send, fetch or imap_fetch
    moment = now_utc or datetime.now(timezone.utc)
    with connect(settings.db_path) as db:
        row = db.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
    if not row:
        raise ValueError('Unknown business')
    business = dict(row)
    if not (business['imap_host'] and business['imap_user'] and business['imap_password_enc']):
        return 0
    last_uid, last_uidvalidity = business['imap_last_uid'], business['imap_uidvalidity']
    messages, newest, uidvalidity = fetch(mailbox(settings, business, 'imap'), last_uid, last_uidvalidity)
    cursor = last_uid if messages else newest
    own = (business['smtp_user'] or '').strip().lower()
    received = 0
    for message in messages:
        try:
            if handle_reply(settings, business, message, own, send=send, llm=llm, now_utc=moment):
                received += 1
        except Exception:
            break  # retried on the next sweep; the cursor is not advanced past it
        cursor = message['uid']
    if cursor is not None and (cursor != last_uid or uidvalidity != last_uidvalidity):
        write_cursor(settings, business_id, cursor, uidvalidity, business['mailbox_generation'])
    answer_pending_replies(settings, business, send=send, llm=llm, now_utc=moment)
    return received


def normalized_text(text):
    """Whitespace- and case-insensitive: for spotting a resend of the same
    message, not for anything shown to a person or a model."""
    return ' '.join((text or '').split()).casefold()


def is_recent_text_duplicate(db, person_id, text, received_at):
    """A real duplicate: the same person, the same text (already stripped of
    quoted history by the caller) sent again within DUPLICATE_WINDOW. Message-
    ID duplicates are caught earlier, before anything is stored."""
    window_start = (received_at - DUPLICATE_WINDOW).isoformat()
    target = normalized_text(text)
    return any(normalized_text(row['body']) == target for row in db.execute(
        "SELECT body FROM messages WHERE person_id=? AND direction='in' AND created_at>=?",
        (person_id, window_start)))


def thread_item(row):
    body = row['body'] or ''
    if row['direction'] == 'out':
        body = body.split('\n\n--\n', 1)[0]  # drop the footer/unsubscribe boilerplate
    return {'from': 'us' if row['direction'] == 'out' else 'them', 'at': row['created_at'],
            'subject': row['subject'] or '', 'body': body}


def build_thread(db, person_id, upto):
    """The whole conversation with this person, oldest first, both
    directions, ending with `upto` (the message being answered). Capped at
    THREAD_CHAR_LIMIT: the first letter and `upto` are always kept, the most
    recent messages fill the rest, and one marker replaces whatever is
    dropped in between."""
    rows = [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE person_id=? AND direction IN ('in','out') AND (created_at<? OR id=?) "
        'ORDER BY created_at,id', (person_id, upto['created_at'], upto['id']))]
    items = [thread_item(row) for row in rows]
    if not items:
        return items
    anchors = {len(items) - 1}
    greet_at = next((i for i, row in enumerate(rows) if row['move'] == GREET), None)
    if greet_at is not None:
        anchors.add(greet_at)
    size = lambda item: len(item['subject']) + len(item['body'])
    budget = THREAD_CHAR_LIMIT - sum(size(items[i]) for i in anchors)
    kept = set(anchors)
    for i in range(len(items) - 1, -1, -1):
        if i in kept:
            continue
        cost = size(items[i])
        if cost > budget:
            break
        kept.add(i)
        budget -= cost
    if len(kept) == len(items):
        return items
    dropped, marked, result = len(items) - len(kept), False, []
    for i, item in enumerate(items):
        if i in kept:
            result.append(item)
        elif not marked:
            result.append({'from': 'system', 'at': items[i]['at'], 'subject': '',
                          'body': f'[{dropped} earlier messages omitted]'})
            marked = True
    return result


def handle_reply(settings, business, message, own_address, *, send, llm=None, now_utc=None):
    """Store one inbound email once and answer it right away, so a second
    message in the same poll is answered with the first reply already in
    the thread. Opt-out and hostility run on every inbound message and close
    the thread before any reply is attempted. A real duplicate (same
    Message-ID, or the same text within DUPLICATE_WINDOW) is stored, marked
    reply_processed=1, and never gets its own reply."""
    business_id = business['id']
    sender = message['from']
    if not sender or sender == own_address or message['automatic']:
        return False
    text = reply_text(message['text'])
    if not text:
        return False
    received_at = datetime.now(timezone.utc)
    with connect(settings.db_path) as db:
        person = match_person(db, business_id, message)
        if not person:
            return False  # not someone cycle 2 wrote to: the inbox is left alone
        if db.execute('SELECT 1 FROM messages WHERE person_id=? AND smtp_message_id=?',
                      (person['id'], message['message_id'])).fetchone():
            return False
        duplicate = is_recent_text_duplicate(db, person['id'], text, received_at)
        message_row_id = str(uuid4())
        db.execute('INSERT INTO messages(id,person_id,direction,subject,body,smtp_message_id,created_at,reply_processed)'
                   " VALUES (?,?,'in',?,?,?,?,?)",
                   (message_row_id, person['id'], message['subject'], text, message['message_id'], now(), int(duplicate)))
        db.execute('UPDATE people SET updated_at=? WHERE id=?', (now(), person['id']))
        stored = dict(db.execute('SELECT * FROM messages WHERE id=?', (message_row_id,)).fetchone())
    # Not a DB column (no schema change): kept only in memory, for this one
    # synchronous reply, so a reply answered right away can carry the real
    # References chain. A reply retried later by answer_pending_replies has
    # no live email to read it from and falls back to the single Message-ID.
    stored['references'] = ' '.join(part for part in (message['references'], message['message_id']) if part)
    if is_opt_out(text) or HOSTILE.search(text[:500]):
        return stop_contact(settings, business_id, person, 'reply_opt_out')
    if duplicate or person['stage'] in {'done', 'lost'}:
        return True  # a resend, or a thread already closed: nothing to answer
    attempt_reply(settings, business, person, stored, send=send, llm=llm, now_utc=now_utc)
    return True


def attempt_reply(settings, business, person, message_row, *, send, llm=None, now_utc=None):
    """Decide and try to send one reply to one already-stored inbound
    message, with the whole thread so far as context. Returns True when it
    went out (or when there was nothing to send -- a hostile message closes
    the thread instead), False when a guard blocked it (left exactly as it
    was for the next poll's answer_pending_replies to retry). The actual
    policy decision and phrasing is next_reply's, a pure function; this is
    everything channel-specific around it: DB-sourced context, guards,
    headers, delivery."""
    business_id, text = business['id'], message_row['body']
    with connect(settings.db_path) as db:
        sent_moves = [row[0] for row in db.execute(
            "SELECT move FROM messages WHERE person_id=? AND direction='out' AND move IS NOT NULL "
            'ORDER BY created_at,id', (person['id'],))]
        thread = build_thread(db, person['id'], message_row)
    move, body = next_reply(business, dict(person, sent_moves=sent_moves), dict(message_row, thread=thread), llm=llm)
    if move == END_CONTACT:  # next_reply's own signal for a hostile message
        return stop_contact(settings, business_id, person, 'hostile_reply')
    if move == HANDOFF:
        notify.handoff_alert(settings, business_id, business['name'])
    moment = now_utc or datetime.now(timezone.utc)
    allowed, reason = guards.can_send(business_id, person['email'], moment, settings=settings, kind='reply')
    if not allowed:
        day = guards._owner_day_start(settings, moment)
        with connect(settings.db_path) as db:
            if not db.execute(
                "SELECT 1 FROM log WHERE business_id=? AND person_id=? AND event='reply_blocked' "
                'AND detail=? AND created_at>=?', (business_id, person['id'], reason, day)).fetchone():
                db.execute('INSERT INTO log(business_id,person_id,event,detail,created_at) VALUES (?,?,?,?,?)',
                           (business_id, person['id'], 'reply_blocked', reason, now()))
        return False
    reply_subject = (message_row['subject'] or '').strip() or 'Your reply'
    if not reply_subject.casefold().startswith('re:'):
        reply_subject = 'Re: ' + reply_subject
    # The full chain when this message is being answered right away (handle_reply
    # stashed it in memory); a message retried later has no live email left to
    # read it from, so it falls back to its own single Message-ID -- same as
    # In-Reply-To, same as before task 09.
    references = (message_row.get('references') or message_row['smtp_message_id'] or '')[-900:]
    deliver(settings, business, person, reply_subject[:255], body, move, send=send,
            headers={'In-Reply-To': message_row['smtp_message_id'], 'References': references},
            now_utc=moment)
    return True


def pending_replies(db, business_id):
    """Inbound messages with no matching outbound reply yet, for people still
    worth answering. An 'in' row counts as answered once some 'out' row's
    in_reply_to names its smtp_message_id -- exactly how deliver() threads a
    reply, so a message answered here is never picked up again."""
    return [dict(row) for row in db.execute(
        "SELECT m.* FROM messages m JOIN people p ON p.id=m.person_id "
        "WHERE p.business_id=? AND m.direction='in' AND p.stage NOT IN ('done','lost') "
        "AND m.reply_processed=0 AND m.imported_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM businesses b WHERE b.id=p.business_id "
        "AND julianday(m.created_at)<julianday(b.history_imported_at)) "
        'AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.person_id=m.person_id '
        "AND o.direction='out' AND o.in_reply_to=m.smtp_message_id) "
        'ORDER BY m.created_at,m.id', (business_id,))]


def answer_pending_replies(settings, business, *, send, llm=None, now_utc=None):
    """Retry every inbound message that never got an answer, each on its own,
    in arrival order. Runs on every poll (see poll_replies), so a reply
    blocked once by quiet hours or the reply cap goes out as soon as its own
    window allows -- it is never simply dropped because the IMAP cursor
    already moved past it."""
    business_id = business['id']
    with connect(settings.db_path) as db:
        pending = pending_replies(db, business_id)
    answered = 0
    for row in pending:
        with connect(settings.db_path) as db:
            person = db.execute('SELECT * FROM people WHERE id=?', (row['person_id'],)).fetchone()
        if not person:
            continue
        person = dict(person)
        if person['stage'] in {'done', 'lost'}:
            continue  # changed since the query ran, e.g. stopped earlier this same poll
        if attempt_reply(settings, business, person, row, send=send, llm=llm, now_utc=now_utc):
            answered += 1
    return answered


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
