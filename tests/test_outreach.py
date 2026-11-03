"""Cycle 2 end to end on a fake SMTP, a fake IMAP and a stub LLM. No network."""
from email.message import EmailMessage
from uuid import uuid4

import pytest

from app import guards, outreach
from app.db import connect
from test_guards import DAYTIME, EVENING, make_business, make_person, settings  # noqa: F401


class FakeSmtp:
    """Records what would have gone out; raises what the test asks it to raise."""

    def __init__(self, error=None):
        self.sent, self.targets, self.error = [], [], error

    def __call__(self, target, message):
        if self.error:
            raise self.error
        self.targets.append(target)
        self.sent.append(message)

    @property
    def bodies(self):
        return [message.get_content() for message in self.sent]


def stub_llm(analysis=None, body=None, error=None):
    """Stands in for llm.ask: one canned answer per purpose."""
    calls = []

    def ask(system, user, *, schema=None, web_search=False, max_uses=5, purpose):
        calls.append(purpose)
        if error:
            raise error
        if purpose == 'sales_analysis':
            return dict({'problem': '', 'desired_outcome': '', 'objection': '', 'objection_cause': '',
                         'ready': False, 'hostile': False, 'requires_human': False}, **(analysis or {}))
        return {'body': body if body is not None else 'Thanks — what is slowest for you today?'}

    ask.calls = calls
    return ask


def inbound(text, *, sender='dana@clinic.example', subject='Re: A note for Dana', uid=11,
            in_reply_to=None, references=None, headers=None):
    """A real RFC 5322 message, parsed by the same code IMAP output goes through."""
    message = EmailMessage()
    message['From'] = sender
    message['To'] = 'sales@acme.example'
    message['Subject'] = subject
    message['Message-ID'] = '<inbound-%s@clinic.example>' % uid
    if in_reply_to:
        message['In-Reply-To'] = in_reply_to
    if references:
        message['References'] = references
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text)
    return outreach.parse_email(uid, message.as_bytes())


def fake_imap(messages, newest=None, uidvalidity=1):
    """`newest` stands in for a first-ever poll, which only records the position."""
    def fetch(target, last_uid, last_uidvalidity=None):
        if newest is not None:
            return [], newest, uidvalidity
        return messages, (messages[-1]['uid'] if messages else last_uid), uidvalidity
    return fetch


def people(settings):
    with connect(settings.db_path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM people ORDER BY created_at,id')]


def messages(settings, person_id=None):
    with connect(settings.db_path) as db:
        if person_id:
            rows = db.execute('SELECT * FROM messages WHERE person_id=? ORDER BY created_at,id', (person_id,))
        else:
            rows = db.execute('SELECT * FROM messages ORDER BY created_at,id')
        return [dict(row) for row in rows]


def log_events(settings):
    with connect(settings.db_path) as db:
        return [(row[0], row[1]) for row in db.execute('SELECT event,detail FROM log ORDER BY id')]


# --- the first letter -------------------------------------------------------

def test_first_letter_opens_with_the_pain_and_promises_nothing(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 1

    body = smtp.bodies[0]
    paragraphs = [block.strip() for block in body.split('\n\n') if block.strip()]
    assert paragraphs[0] == 'Hi Dana,'
    # Pain first, then touch: the pain is the opening sentence and ends with one full stop.
    assert paragraphs[1] == 'Something I noticed: front desk returns calls two days late.'
    assert paragraphs[1].count('.') == 1
    assert not outreach.SENSITIVE_CLAIM.search(body)
    assert 'offer' not in body.lower() and 'pay.example.com' not in body
    assert len(smtp.sent[0]['Subject']) <= outreach.SUBJECT_MAX

    stored = messages(settings, person['id'])
    assert [(row['direction'], row['move']) for row in stored] == [('out', 'greet')]
    assert stored[0]['smtp_message_id'] == smtp.sent[0]['Message-ID']
    assert people(settings)[0]['stage'] == 'in_progress'


def test_first_letter_carries_the_footer_and_one_click_headers(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)

    body, message = smtp.bodies[0], smtp.sent[0]
    assert '100 Main Street, Austin, TX 78701' in body
    assert guards.unsubscribe_url(settings, 'biz-1', person['id']) in body
    assert message['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    assert message['From'] == 'Alena <sales@acme.example>'
    assert smtp.targets[0]['password'] == 'smtp-secret'


def test_a_person_with_no_email_is_never_contacted(settings):
    make_business(settings)
    make_person(settings, email='')
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 0
    assert smtp.sent == []


def test_nobody_is_written_to_twice(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 1
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 0


def test_each_guard_blocks_the_send_and_says_why(settings):
    make_business(settings, outreach_paused=1)
    make_person(settings)
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 0
    assert smtp.sent == []
    assert ('send_blocked', 'outreach_paused') in log_events(settings)


def test_quiet_hours_skip_one_recipient_without_stopping_the_run(settings):
    make_business(settings)
    make_person(settings, email='east@clinic.example', us_state='NY')
    make_person(settings, email='west@clinic.example', us_state='CA')
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=EVENING) == 1
    assert smtp.sent[0]['To'] == 'west@clinic.example'
    assert ('send_blocked', 'quiet_hours') in log_events(settings)


def test_the_daily_cap_stops_the_run(settings):
    make_business(settings, daily_cap=2)
    for index in range(4):
        make_person(settings, email='p%d@clinic.example' % index)
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 2
    assert ('send_blocked', 'daily_cap') in log_events(settings)


def test_a_pain_carrying_a_claim_is_never_quoted(settings):
    make_business(settings)
    make_person(settings, pain='clinic advertises a 50% discount on every cleaning')
    smtp = FakeSmtp()
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 0
    assert ('send_blocked', 'unsafe_pain') in log_events(settings)


def test_a_refused_recipient_is_suppressed_but_an_outage_is_not(settings):
    import smtplib
    make_business(settings)
    make_person(settings)
    refused = FakeSmtp(error=smtplib.SMTPRecipientsRefused({'dana@clinic.example': (550, b'no such user')}))
    assert outreach.send_first_letters('biz-1', settings=settings, send=refused, now_utc=DAYTIME) == 0
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings) == (False, 'suppressed')

    make_person(settings, email='two@clinic.example')
    outage = FakeSmtp(error=smtplib.SMTPAuthenticationError(535, b'bad credentials'))
    assert outreach.send_first_letters('biz-1', settings=settings, send=outage, now_utc=DAYTIME) == 0
    assert guards.can_send('biz-1', 'two@clinic.example', DAYTIME, settings=settings) == (True, '')
    assert ('smtp_error', 'two@clinic.example') in log_events(settings)


# --- the policy and the validator -------------------------------------------

def test_policy_precedence(settings):
    make_business(settings)
    business = {'offer_text': 'Two boards.', 'payment_link': 'https://pay.example.com/acme'}
    base = dict(outreach.ANALYSIS_FIELDS)
    assert outreach.decide(dict(base, requires_human=True, ready=True), set(), business) == outreach.HANDOFF
    assert outreach.decide(dict(base, objection='too expensive'), set(), business) == outreach.DIAGNOSE_OBJECTION
    assert outreach.decide(dict(base, objection='too expensive', objection_cause='affordability'),
                           set(), business) == outreach.ANSWER_OBJECTION
    assert outreach.decide(base, set(), business) == outreach.ASK_DISCOVERY
    known = dict(base, problem='calls go unanswered', desired_outcome='same-day callbacks')
    assert outreach.decide(known, set(), business) == outreach.CONFIRM_NEED
    assert outreach.decide(known, {outreach.CONFIRM_NEED}, business) == outreach.PRESENT_VALUE
    assert outreach.decide(dict(known, ready=True), {outreach.CONFIRM_NEED}, business) == outreach.OFFER
    assert outreach.decide(known, {outreach.CONFIRM_NEED, outreach.PRESENT_VALUE}, business) == outreach.NURTURE
    # Readiness without a configured offer never invents one.
    assert outreach.decide(dict(known, ready=True), {outreach.CONFIRM_NEED},
                           {'offer_text': '', 'payment_link': ''}) == outreach.PRESENT_VALUE


def test_validator_rejects_what_the_model_may_not_say():
    assert outreach.validate('What is slowest for you?', outreach.ASK_DISCOVERY, '') == (True, [])
    assert outreach.validate('', outreach.ASK_DISCOVERY, '')[1] == ['length']
    assert 'ungrounded_claim' in outreach.validate('We can do 20% off.', outreach.PRESENT_VALUE, '')[1]
    # The same claim is fine when it is the owner's own approved offer text.
    assert outreach.validate('We can do 20% off.', outreach.PRESENT_VALUE, 'Launch deal: 20% off year one.')[0]
    assert 'unauthorized_execution' in outreach.validate(
        'Your booking is confirmed.', outreach.NURTURE, '')[1]
    assert 'link_or_contact_detail' in outreach.validate(
        'Pay at https://evil.example', outreach.OFFER, '')[1]


def test_untrusted_analysis_is_coerced_not_trusted():
    coerced = outreach.analysis_from({'problem': ['nope'], 'ready': 'yes', 'hostile': 1, 'extra': 'ignored'})
    assert coerced['ready'] is False and coerced['hostile'] is False
    assert set(coerced) == set(outreach.ANALYSIS_FIELDS)
    assert len(outreach.analysis_from({'problem': 'x' * 900})['problem']) == 200
    with pytest.raises(ValueError):
        outreach.analysis_from('not an object')


# --- replies ----------------------------------------------------------------

def test_a_reply_continues_the_same_person_and_is_answered_once(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    first = smtp.sent[0]['Message-ID']
    reply = inbound('We do call people back late. What would you change?', in_reply_to=first)

    llm = stub_llm(analysis={'problem': 'late callbacks'})
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([reply]), llm=llm) == 1
    assert len(people(settings)) == 1  # no duplicate person
    stored = messages(settings, person['id'])
    assert [row['direction'] for row in stored] == ['out', 'in', 'out']
    assert stored[2]['move'] == outreach.ASK_DISCOVERY
    assert stored[2]['in_reply_to'] == reply['message_id']
    assert smtp.sent[1]['In-Reply-To'] == reply['message_id']
    assert smtp.sent[1]['Subject'].startswith('Re: ')
    assert llm.calls == ['sales_analysis', 'sales_reply']

    # The same IMAP message arriving twice is stored and answered once.
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([reply]), llm=llm) == 0
    assert len(messages(settings, person['id'])) == 3


def test_a_reply_is_matched_by_sender_when_threading_headers_are_missing(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Tell me more.')]), llm=stub_llm())
    assert len(people(settings)) == 1
    assert [row['direction'] for row in messages(settings, person['id'])] == ['out', 'in', 'out']


def test_mail_from_a_stranger_leaves_the_inbox_alone(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap(
        [inbound('Hello', sender='newsletter@elsewhere.example')]), llm=stub_llm()) == 0
    assert messages(settings) == []


def test_our_own_mail_and_auto_replies_are_ignored(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    ours = inbound('loop', sender='sales@acme.example', uid=12)
    vacation = inbound('I am away', uid=13, subject='Automatic reply: A note for Dana')
    flagged = inbound('I am away', uid=14, headers={'Auto-Submitted': 'auto-replied'})
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([ours, vacation, flagged]), llm=stub_llm()) == 0
    assert smtp.sent == []


def test_stop_suppresses_the_address_loses_the_card_and_never_replies(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    llm = stub_llm()
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([inbound('Unsubscribe me, please.')]), llm=llm) == 1
    assert len(smtp.sent) == 1  # only the first letter; no reply to an opt-out
    assert llm.calls == []  # the model is never asked about an opt-out
    assert people(settings)[0]['stage'] == 'lost'
    assert guards.can_send('biz-1', person['email'], DAYTIME, settings=settings) == (False, 'suppressed')
    assert [row['direction'] for row in messages(settings, person['id'])] == ['out', 'in']


@pytest.mark.parametrize('text', ['no', 'Not interested.', 'Take me off your list.', 'STOP'])
def test_every_opt_out_wording_is_honoured(settings, text):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound(text)]), llm=stub_llm())
    assert people(settings)[0]['stage'] == 'lost'
    assert len(smtp.sent) == 1


def test_a_hostile_reply_stops_contact_without_an_answer(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('This is spam and I will report you.')]), llm=stub_llm())
    assert people(settings)[0]['stage'] == 'lost'
    assert len(smtp.sent) == 1
    assert ('outreach_stopped', 'reply_opt_out') in log_events(settings)


def test_hostility_the_model_spots_also_stops_contact(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound('Who gave you this?')]),
                          llm=stub_llm(analysis={'hostile': True}))
    assert people(settings)[0]['stage'] == 'lost'
    assert ('outreach_stopped', 'hostile_reply') in log_events(settings)


def test_quoted_history_is_not_read_as_a_new_message(settings):
    assert outreach.reply_text('Sounds good.\n\nOn Tue someone wrote:\n> unsubscribe me') == 'Sounds good.'
    # ... so a quote of our own footer never looks like an opt-out.
    assert not outreach.is_opt_out(outreach.reply_text('Sounds good.\n> Unsubscribe: https://x/y'))


def test_the_offer_hands_over_the_owners_words_and_link(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO messages(id,person_id,direction,subject,body,move,created_at)"
                   " VALUES (?,?,'out','s','b','confirm_need','2026-11-03T12:00:00+00:00')",
                   (str(uuid4()), person['id']))
    llm = stub_llm(analysis={'problem': 'late callbacks', 'desired_outcome': 'same-day callbacks', 'ready': True},
                   body='Here is what I can put in front of you.')
    outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound('Send me the details.')]),
                          llm=llm)

    letter = smtp.bodies[-1]
    assert 'Two boards and a weekly review.' in letter
    assert 'https://pay.example.com/acme' in letter
    assert messages(settings, person['id'])[-1]['move'] == outreach.OFFER
    assert people(settings)[0]['stage'] == 'offer_made'


def test_a_model_that_invents_a_claim_is_replaced_by_the_safe_wording(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound('Tell me more.')]),
                          llm=stub_llm(body='We guarantee 40% fewer missed calls, free for a month.'))
    letter = smtp.bodies[-1]
    assert 'guarantee' not in letter and '40%' not in letter
    assert outreach.SAFE_FALLBACK[outreach.ASK_DISCOVERY] in letter


def test_an_unavailable_model_still_answers_safely(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound('Tell me more.')]),
                                 llm=stub_llm(error=RuntimeError('LLM is not installed'))) == 1
    assert outreach.SAFE_FALLBACK[outreach.ASK_DISCOVERY] in smtp.bodies[-1]


def test_a_question_for_a_human_gets_the_servers_own_wording(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    llm = stub_llm(analysis={'requires_human': True}, body='Sure, I will sign your DPA right now.')
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Send me your signed DPA.')]), llm=llm)
    assert outreach.SAFE_FALLBACK[outreach.HANDOFF] in smtp.bodies[-1]
    assert 'sign your DPA' not in smtp.bodies[-1]
    assert llm.calls == ['sales_analysis']  # a handoff is never phrased by the model


def test_a_reply_is_stored_even_when_a_guard_blocks_the_answer(settings):
    make_business(settings)
    person = make_person(settings, us_state='NY')
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET daily_cap=1 WHERE id=?', ('biz-1',))
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp, fetch=fake_imap([inbound('Tell me more.')]),
                                 llm=stub_llm()) == 1
    assert len(smtp.sent) == 1
    assert ('reply_blocked', 'daily_cap') in log_events(settings)
    assert [row['direction'] for row in messages(settings, person['id'])] == ['out', 'in']


def test_the_first_poll_only_records_where_the_inbox_is(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    old = inbound('ancient mail', uid=5)
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([old], newest=5), llm=stub_llm()) == 0
    assert outreach.read_cursor(settings, 'biz-1') == 5
    assert messages(settings) == []


def test_an_unconfigured_mailbox_polls_nothing(settings):
    make_business(settings, imap_host=None)
    make_person(settings)
    assert outreach.poll_replies('biz-1', settings=settings, send=FakeSmtp(),
                                 fetch=fake_imap([inbound('hi')]), llm=stub_llm()) == 0


# --- payment ----------------------------------------------------------------

def test_record_payment_is_the_only_way_to_done(settings):
    make_business(settings)
    person = make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    llm = stub_llm(analysis={'problem': 'p', 'desired_outcome': 'o', 'ready': True})
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO messages(id,person_id,direction,subject,body,move,created_at)"
                   " VALUES (?,?,'out','s','b','confirm_need','2026-11-03T12:00:00+00:00')",
                   (str(uuid4()), person['id']))
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Send me the link.')]), llm=llm)
    # The offer went out and the link was handed over: that alone is not a payment.
    assert people(settings)[0]['stage'] == 'offer_made'

    outreach.record_payment('biz-1', person['id'], 'lemonsqueezy', settings=settings)
    assert people(settings)[0]['stage'] == 'done'
    assert ('payment_recorded', 'lemonsqueezy') in log_events(settings)


def test_record_payment_is_tenant_scoped(settings):
    make_business(settings)
    make_business(settings, id='biz-2', name='Other')
    person = make_person(settings)
    with pytest.raises(ValueError):
        outreach.record_payment('biz-2', person['id'], 'manual', settings=settings)
    assert people(settings)[0]['stage'] == 'cold'


def test_contract_signatures_stay_callable_without_keywords(settings, monkeypatch):
    """The scheduler calls these with one positional argument and nothing else."""
    monkeypatch.setattr(outreach, '_SETTINGS', settings)
    make_business(settings, imap_host=None, outreach_paused=1)
    assert outreach.send_first_letters('biz-1') == 0
    assert outreach.poll_replies('biz-1') == 0
    with pytest.raises(ValueError):
        outreach.record_payment('biz-1', 'nobody', 'manual')


def test_the_llm_seam_matches_the_contract_signature():
    import inspect
    signature = inspect.signature(outreach.ask)
    assert list(signature.parameters) == ['system', 'user', 'schema', 'web_search', 'max_uses', 'purpose']
    with pytest.raises(RuntimeError):
        outreach.ask('s', 'u', purpose='probe')


def test_record_payment_can_join_an_open_transaction(settings):
    """billing.py calls this from inside its own webhook transaction."""
    make_business(settings)
    person = make_person(settings)
    with connect(settings.db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        outreach.record_payment('biz-1', person['id'], 'lemonsqueezy', db=db)
    assert people(settings)[0]['stage'] == 'done'


def test_the_payment_link_seam_matches_the_contract_signature():
    import inspect
    signature = inspect.signature(outreach.payment_link_for)
    assert list(signature.parameters) == ['business', 'person_id']
    # Falls back to the raw link while billing.payment_link_for is unmerged.
    assert outreach.payment_link_for({'payment_link': ' https://pay.example.com/acme '}, 'person-1') \
        == 'https://pay.example.com/acme'


# --- the IMAP cursor: stored on businesses, resilient to UIDVALIDITY changes -

def _raw_email(message_id, text='Hello', sender='dana@clinic.example'):
    message = EmailMessage()
    message['From'] = sender
    message['To'] = 'sales@acme.example'
    message['Subject'] = 'Hi'
    message['Message-ID'] = message_id
    message.set_content(text)
    return message.as_bytes()


class _FakeImapClient:
    """A minimal double for imaplib.IMAP4_SSL, enough to drive imap_fetch
    itself (the fake used elsewhere replaces imap_fetch wholesale)."""

    def __init__(self, mail, uidvalidity):
        self._mail, self._uidvalidity = mail, uidvalidity
        self.untagged_responses = {}

    def login(self, user, password):
        pass

    def select(self, name, readonly=True):
        self.untagged_responses['UIDVALIDITY'] = [str(self._uidvalidity).encode()]
        return 'OK', [b'1']

    def uid(self, command, *args):
        if command == 'SEARCH':
            criterion = args[1]
            if criterion in ('ALL', 'SINCE'):
                matched = sorted(self._mail)
            else:  # 'UID <n>:*'
                floor = int(criterion.split()[1].split(':')[0])
                matched = sorted(uid for uid in self._mail if uid >= floor)
            return 'OK', [' '.join(str(uid) for uid in matched).encode()]
        if command == 'FETCH':
            return 'OK', [(b'uid fetch', self._mail[int(args[0])])]
        raise AssertionError(command)

    def logout(self):
        pass


def test_imap_fetch_first_poll_only_records_the_newest_uid(monkeypatch):
    mail = {3: _raw_email('<a@x>'), 7: _raw_email('<b@x>')}
    monkeypatch.setattr(outreach.imaplib, 'IMAP4_SSL', lambda *a, **k: _FakeImapClient(mail, 42))
    target = {'host': 'h', 'port': 993, 'user': 'u', 'password': 'p'}
    messages, newest, uidvalidity = outreach.imap_fetch(target, None, None)
    assert messages == []
    assert (newest, uidvalidity) == (7, 42)


def test_imap_fetch_resumes_from_the_stored_uid(monkeypatch):
    mail = {3: _raw_email('<a@x>'), 7: _raw_email('<b@x>'), 9: _raw_email('<c@x>')}
    monkeypatch.setattr(outreach.imaplib, 'IMAP4_SSL', lambda *a, **k: _FakeImapClient(mail, 42))
    target = {'host': 'h', 'port': 993, 'user': 'u', 'password': 'p'}
    messages, newest, uidvalidity = outreach.imap_fetch(target, 3, 42)
    assert [message['uid'] for message in messages] == [7, 9]
    assert (newest, uidvalidity) == (9, 42)


def test_imap_fetch_rereads_seven_days_when_uidvalidity_changes(monkeypatch):
    # The provider rebuilt the mailbox: UID 2 in this namespace has nothing to
    # do with UID 2 the old namespace already answered, so the UID range is
    # abandoned in favour of a date search.
    mail = {2: _raw_email('<unrelated-to-old-uid-2@x>'), 5: _raw_email('<new@x>')}
    monkeypatch.setattr(outreach.imaplib, 'IMAP4_SSL', lambda *a, **k: _FakeImapClient(mail, 99))
    target = {'host': 'h', 'port': 993, 'user': 'u', 'password': 'p'}
    messages, newest, uidvalidity = outreach.imap_fetch(target, 50, 42)
    assert sorted(message['uid'] for message in messages) == [2, 5]
    assert (newest, uidvalidity) == (5, 99)


def test_poll_replies_persists_the_cursor_on_the_business_row(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    first = smtp.sent[0]['Message-ID']
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Hi.', in_reply_to=first, uid=50)], uidvalidity=100),
                          llm=stub_llm())
    with connect(settings.db_path) as db:
        row = db.execute('SELECT imap_last_uid,imap_uidvalidity FROM businesses WHERE id=?', ('biz-1',)).fetchone()
    assert (row['imap_last_uid'], row['imap_uidvalidity']) == (50, 100)


def test_a_mailbox_rebuild_rereads_without_duplicating_the_reply(settings):
    make_business(settings)
    make_person(settings)
    smtp = FakeSmtp()
    outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME)
    first = smtp.sent[0]['Message-ID']
    reply = inbound('Tell me more.', in_reply_to=first, uid=50)
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([reply], uidvalidity=100), llm=stub_llm()) == 1
    assert len(smtp.sent) == 2  # first letter + one reply

    # UIDVALIDITY changed: the provider hands back the SAME message under a
    # new, unrelated UID. It is recognised by Message-ID and not re-answered.
    reread = inbound('Tell me more.', in_reply_to=first, uid=9)
    reread['message_id'] = reply['message_id']
    assert outreach.poll_replies('biz-1', settings=settings, send=smtp,
                                 fetch=fake_imap([reread], uidvalidity=200), llm=stub_llm()) == 0
    assert len(smtp.sent) == 2  # no duplicate reply
    with connect(settings.db_path) as db:
        row = db.execute('SELECT imap_last_uid,imap_uidvalidity FROM businesses WHERE id=?', ('biz-1',)).fetchone()
    assert (row['imap_last_uid'], row['imap_uidvalidity']) == (9, 200)
