"""Guards: nothing reaches SMTP without can_send(), and every letter carries a footer."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import board, guards
from app.db import connect, migrate, now
from app.settings import Settings, encrypt_password

# 13:00 in New York, 10:00 in Los Angeles: inside every US working window.
DAYTIME = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
# 21:00 in New York (quiet), 18:00 in Los Angeles (still working).
EVENING = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    # Pinned to UTC so DAYTIME's own UTC midnight is the cap boundary and
    # every fixture that stamps a message with the real wall clock lands
    # after it, exactly as before guards.py started reading TIMEZONE_OWNER.
    # Dedicated tests below cover the owner-timezone boundary itself with
    # their own Settings and fully explicit timestamps.
    settings = Settings({'SECRET_KEY': 'test-only-' * 6, 'DB_PATH': str(tmp_path / 'test.sqlite3'),
                         'APP_BASE_URL': 'https://testserver', 'TIMEZONE_OWNER': 'UTC'})
    migrate(settings.db_path)
    return settings


def make_business(settings, **over):
    fields = {'id': 'biz-1', 'name': 'Acme', 'created_at': now(), 'subscription_status': 'active',
              'offer_text': 'Two boards and a weekly review.', 'payment_link': 'https://pay.example.com/acme',
              'postal_address': '100 Main Street, Austin, TX 78701', 'sender_name': 'Alena',
              'smtp_host': 'smtp.example.com', 'smtp_port': 587, 'smtp_user': 'sales@acme.example',
              'smtp_password_enc': encrypt_password(settings, 'smtp-secret'),
              'imap_host': 'imap.example.com', 'imap_port': 993, 'imap_user': 'sales@acme.example',
              'imap_password_enc': encrypt_password(settings, 'imap-secret'),
              'daily_cap': 20, 'quiet_start': '20:00', 'quiet_end': '08:00', 'outreach_paused': 0,
              'brief': 'US dental clinics'}
    fields.update(over)
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO businesses({}) VALUES ({})'.format(
            ','.join(fields), ','.join('?' * len(fields))), tuple(fields.values()))
    return fields


def make_person(settings, business_id='biz-1', **over):
    fields = {'id': str(uuid4()), 'business_id': business_id, 'name': 'Dana Reed', 'role': 'Owner',
              'email': 'dana@clinic.example', 'company': 'Reed Dental', 'website': 'https://clinic.example',
              'us_state': 'NY', 'segment': 'single-location dental clinics',
              'pain': 'front desk returns calls two days late', 'evidence_quote': 'we call people back on Thursdays',
              'source_url': 'https://clinic.example/about', 'stage': 'cold',
              'created_at': now(), 'updated_at': now()}
    fields.update(over)
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO people({}) VALUES ({})'.format(
            ','.join(fields), ','.join('?' * len(fields))), tuple(fields.values()))
    return fields


def add_out_messages(settings, person_id, count, stamp=None, move='greet'):
    with connect(settings.db_path) as db:
        for index in range(count):
            db.execute("INSERT INTO messages(id,person_id,direction,subject,body,move,created_at)"
                       ' VALUES (?,?,\'out\',\'s\',\'b\',?,?)',
                       (str(uuid4()), person_id, move, stamp or now()))


def test_recipient_zone_table():
    assert guards.recipient_zone('CA') == guards.PACIFIC
    assert guards.recipient_zone('california') == guards.PACIFIC
    assert guards.recipient_zone('New York') == guards.EASTERN
    assert guards.recipient_zone('AZ') == guards.ARIZONA
    assert guards.recipient_zone('HI') == guards.HAWAII
    assert guards.recipient_zone('AK') == guards.ALASKA
    # Unplaceable keeps the old constant rather than guessing something worse.
    assert guards.recipient_zone(None) == guards.DEFAULT_ZONE
    assert guards.recipient_zone('Ontario') == guards.DEFAULT_ZONE


def test_all_fifty_states_resolve():
    codes = {code for code in guards._STATE_CODES.values()}
    assert len(codes) == 52  # 50 states, DC and Puerto Rico
    assert all(code in guards.US_STATE_ZONES for code in codes)


def test_happy_path_allows_sending(settings):
    make_business(settings)
    make_person(settings)
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings) == (True, '')


@pytest.mark.parametrize('override,reason', [
    ({'outreach_paused': 1}, 'outreach_paused'),
    ({'smtp_host': None}, 'mailbox_not_configured'),
    ({'smtp_password_enc': None}, 'mailbox_not_configured'),
    ({'postal_address': 'TX'}, 'postal_address_required'),
])
def test_configuration_gates_block_with_their_reason(settings, override, reason):
    make_business(settings, **override)
    make_person(settings)
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings) == (False, reason)


def test_missing_base_url_blocks(settings, tmp_path):
    make_business(settings)
    make_person(settings)
    bare = Settings({'SECRET_KEY': 'test-only-' * 6, 'DB_PATH': settings.db_path})
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=bare)[1] == 'base_url_required'


def test_unknown_business_and_empty_address(settings):
    assert guards.can_send('nobody', 'dana@clinic.example', DAYTIME, settings=settings)[1] == 'unknown_business'
    assert guards.can_send('nobody', '', DAYTIME, settings=settings)[1] == 'no_email'


def test_suppressed_address_blocks_case_insensitively(settings):
    make_business(settings)
    make_person(settings)
    guards.suppress(settings, 'biz-1', 'Dana@Clinic.Example', 'unsubscribe')
    assert guards.can_send('biz-1', 'DANA@clinic.example', DAYTIME, settings=settings) == (False, 'suppressed')


def test_quiet_hours_follow_the_recipient_not_the_owner(settings):
    make_business(settings)
    make_person(settings, email='east@clinic.example', us_state='NY')
    make_person(settings, email='west@clinic.example', us_state='CA')
    assert guards.can_send('biz-1', 'east@clinic.example', EVENING, settings=settings) == (False, 'quiet_hours')
    assert guards.can_send('biz-1', 'west@clinic.example', EVENING, settings=settings) == (True, '')


def test_quiet_hours_window_can_be_disabled(settings):
    make_business(settings, quiet_start='00:00', quiet_end='00:00')
    make_person(settings)
    assert guards.can_send('biz-1', 'dana@clinic.example', EVENING, settings=settings) == (True, '')


def test_daily_cap_of_twenty_is_enforced_per_business(settings):
    make_business(settings)
    person = make_person(settings)
    other = make_person(settings, email='other@clinic.example')
    add_out_messages(settings, person['id'], 19)
    assert guards.can_send('biz-1', 'other@clinic.example', DAYTIME, settings=settings) == (True, '')
    add_out_messages(settings, other['id'], 1)
    assert guards.can_send('biz-1', 'other@clinic.example', DAYTIME, settings=settings) == (False, 'daily_cap')


def test_yesterdays_letters_do_not_count_against_the_cap(settings):
    make_business(settings)
    person = make_person(settings)
    add_out_messages(settings, person['id'], 25, stamp=(DAYTIME - timedelta(days=1)).isoformat())
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings) == (True, '')


def test_daily_cap_counts_only_greet_moves(settings):
    make_business(settings, daily_cap=1)
    person = make_person(settings)
    other = make_person(settings, email='other@clinic.example')
    # 25 replies to the first person never touch the 20-greet cap.
    add_out_messages(settings, person['id'], 25, move='nurture')
    assert guards.can_send('biz-1', 'other@clinic.example', DAYTIME, settings=settings) == (True, '')
    assert guards.can_send('biz-1', 'other@clinic.example', DAYTIME, settings=settings, kind='greet') == (True, '')
    add_out_messages(settings, other['id'], 1, move='greet')
    assert guards.can_send('biz-1', 'other@clinic.example', DAYTIME, settings=settings)[1] == 'daily_cap'


def test_reply_cap_ignores_greet_moves_and_blocks_at_sixty(settings):
    make_business(settings)
    person = make_person(settings)
    # 19 greets (under the 20 business cap) never count against the reply cap.
    add_out_messages(settings, person['id'], 19, move='greet')
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings, kind='reply') == (True, '')
    add_out_messages(settings, person['id'], guards.REPLY_CAP - 1, move='nurture')
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings, kind='reply') == (True, '')
    add_out_messages(settings, person['id'], 1, move='ask_discovery')
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings, kind='reply') == (False, 'reply_cap')
    # The business's own daily_cap (20) is unaffected: this is a reply-only ceiling.
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings, kind='greet') == (True, '')


def test_reply_cap_also_counts_a_move_of_null(settings):
    # offer/nurture/etc. always set move, but the column allows NULL and a
    # null move is a reply (not a greet) exactly like any other non-greet move.
    make_business(settings)
    person = make_person(settings)
    with connect(settings.db_path) as db:
        for _ in range(guards.REPLY_CAP):
            db.execute("INSERT INTO messages(id,person_id,direction,subject,body,created_at) VALUES (?,?,'out','s','b',?)",
                       (str(uuid4()), person['id'], now()))
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings, kind='reply') == (False, 'reply_cap')


def test_daily_cap_boundary_follows_owner_timezone_not_utc_midnight(tmp_path):
    # 2026-11-04T05:00Z is already 2026-11-04 in UTC, but it is still
    # 2026-11-03 evening in Los Angeles (UTC-8 in November): the owner's
    # "today" boundary is 2026-11-03T08:00Z, not a UTC midnight.
    la_settings = Settings({'SECRET_KEY': 'test-only-' * 6, 'DB_PATH': str(tmp_path / 'la.sqlite3'),
                            'APP_BASE_URL': 'https://testserver', 'TIMEZONE_OWNER': 'America/Los_Angeles'})
    migrate(la_settings.db_path)
    make_business(la_settings, daily_cap=1, quiet_start='00:00', quiet_end='00:00')
    person = make_person(la_settings)
    other = make_person(la_settings, email='other@clinic.example')
    moment = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    add_out_messages(la_settings, person['id'], 1, stamp='2026-11-03T07:00:00+00:00', move='greet')
    # Before the LA boundary (08:00Z): does not count as today's greet yet.
    assert guards.can_send('biz-1', 'other@clinic.example', moment, settings=la_settings) == (True, '')
    add_out_messages(la_settings, person['id'], 1, stamp='2026-11-03T09:00:00+00:00', move='greet')
    # After the LA boundary: this one does, and the cap of 1 is now reached.
    assert guards.can_send('biz-1', 'other@clinic.example', moment, settings=la_settings)[1] == 'daily_cap'


def test_repeated_delivery_failures_stop_one_address(settings):
    make_business(settings)
    make_person(settings)
    with connect(settings.db_path) as db:
        for _ in range(guards.FAILURE_LIMIT):
            db.execute("INSERT INTO log(business_id,event,detail,created_at) VALUES (?,'send_failed',?,?)",
                       ('biz-1', 'dana@clinic.example', DAYTIME.isoformat()))
    assert guards.can_send('biz-1', 'dana@clinic.example', DAYTIME, settings=settings) == (False, 'delivery_failures')


def test_footer_carries_sender_postal_address_and_a_working_link(settings):
    make_business(settings)
    person = make_person(settings)
    text = guards.footer('biz-1', person['id'], settings=settings)
    assert 'Alena' in text and '100 Main Street, Austin, TX 78701' in text
    link = text.rsplit('Unsubscribe: ', 1)[1].strip()
    assert link.startswith('https://testserver/unsubscribe/')
    # The link is the real one-click token, and it suppresses only on POST.
    token = link.rsplit('/', 1)[1]
    assert board.unsubscribe(settings, token) == 'dana@clinic.example'
    assert guards.can_send('biz-1', person['email'], DAYTIME, settings=settings) == (True, '')
    board.unsubscribe(settings, token, apply=True)
    assert guards.can_send('biz-1', person['email'], DAYTIME, settings=settings) == (False, 'suppressed')


def test_footer_falls_back_to_the_business_name(settings):
    make_business(settings, sender_name=None)
    person = make_person(settings)
    assert 'Acme' in guards.footer('biz-1', person['id'], settings=settings)


def test_unsubscribe_headers_are_rfc_8058_one_click(settings):
    make_business(settings)
    person = make_person(settings)
    headers = guards.unsubscribe_headers(settings, 'biz-1', person['id'])
    assert headers['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    assert headers['List-Unsubscribe'].startswith('<https://testserver/unsubscribe/')


def test_suppress_is_idempotent(settings):
    make_business(settings)
    guards.suppress(settings, 'biz-1', 'dana@clinic.example', 'unsubscribe')
    guards.suppress(settings, 'biz-1', 'dana@clinic.example', 'bounce')
    with connect(settings.db_path) as db:
        rows = db.execute('SELECT reason FROM suppressions WHERE business_id=?', ('biz-1',)).fetchall()
    assert [row[0] for row in rows] == ['unsubscribe']
