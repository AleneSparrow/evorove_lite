import sys
import types

from scripts.import_from_railway import PostgresSource, apply_bundle, build_bundle
from app.db import connect, migrate


BUSINESS = {
    'id': 'client-zero', 'name': 'Evorove', 'created_at': '2026-01-01T00:00:00+00:00',
    'plan': 'pro', 'subscription_status': 'active', 'payment_customer_id': 'customer-1',
    'payment_subscription_id': 'subscription-1',
}
DNA = {
    'business': {'description': 'Evorove follows every inquiry through to a sale.'},
    'services': [{'name': 'Starter', 'description': 'Managed inquiry-to-sale follow-up.'}],
    'payment': {'payment_link': 'https://pay.example/evorove'},
}
MAILBOX = {'from_name': 'Alena', 'postal_address': '12 Main Street, Austin TX', 'daily_limit': 20}
BRIEF = {'what_we_sell': [{'text': 'Inquiry-to-sale automation'}],
         'who_may_fit': [{'text': 'US business owners'}], 'business_archetype': 'B2B SaaS'}
USERS = ({'id': 'user-1', 'email': 'Owner@Example.com', 'normalized_email': 'owner@example.com',
          'password_hash': 'pbkdf2_sha256$390000$salt$digest', 'created_at': '2026-01-02T00:00:00+00:00'},)
SUPPRESSIONS = ({'email': 'STOP@Example.com', 'reason': 'unsubscribe',
                 'suppressed_at': '2026-01-03T00:00:00+00:00'},)
PEOPLE = (
    {'person_id': 'person-1', 'tab': 'offer_sent', 'name': 'Jordan', 'email': 'JORDAN@example.com',
     'summary': 'Interested', 'observed_pain': 'Leads wait a day for replies.', 'last_kind': 'offer_sent',
     'last_touch_at': '2026-01-05T00:00:00+00:00', 'created_at': '2026-01-04T00:00:00+00:00',
     'updated_at': '2026-01-05T00:00:00+00:00'},
    {'person_id': 'phone-only', 'tab': 'cold', 'name': 'No Email', 'email': None,
     'summary': 'Cannot migrate', 'last_kind': 'assembled', 'created_at': '2026-01-04T00:00:00+00:00',
     'updated_at': '2026-01-04T00:00:00+00:00'},
)
TOUCHES = (
    {'touch_id': 'assembled-1', 'person_id': 'person-1', 'kind': 'assembled', 'summary': 'Reason',
     'payload': {'reason': 'Published slow-response problem', 'reason_source': 'https://example.com/source',
                 'segment': 'US founders'}, 'occurred_at': '2026-01-04T00:00:00+00:00'},
    {'touch_id': 'message-1', 'person_id': 'person-1', 'kind': 'message',
     'summary': 'Customer: Can this reply faster?', 'payload': {'direction': 'inbound'},
     'occurred_at': '2026-01-05T00:00:00+00:00'},
    {'touch_id': 'message-2', 'person_id': 'person-1', 'kind': 'message',
     'summary': 'Evorove: Yes, here is how.', 'payload': {'direction': 'outbound', 'move': 'answer'},
     'occurred_at': '2026-01-05T00:01:00+00:00'},
)


def bundle():
    return build_bundle(business=BUSINESS, dna=DNA, mailbox=MAILBOX, brief=BRIEF, users=USERS,
                        suppressions=SUPPRESSIONS, board_people=PEOPLE, board_touches=TOUCHES)


def test_build_bundle_maps_only_transferable_client_zero_rows():
    result = bundle()
    assert result.counts() == {'businesses': 1, 'users': 1, 'suppressions': 1, 'people': 1, 'messages': 2}
    assert result.business['offer_text'] == DNA['business']['description']
    assert result.business['payment_link'] == 'https://pay.example/evorove'
    assert result.business['postal_address'] == MAILBOX['postal_address']
    assert 'US business owners' in result.business['brief']
    assert result.users[0]['email'] == 'owner@example.com'
    assert result.users[0]['password_hash'].startswith('pbkdf2_sha256$')
    assert result.suppressions[0]['email'] == 'stop@example.com'
    assert result.people[0]['stage'] == 'offer_made'
    assert result.people[0]['evidence_quote'] == 'Leads wait a day for replies.'
    assert [row['direction'] for row in result.messages] == ['in', 'out']
    assert [row['body'] for row in result.messages] == ['Can this reply faster?', 'Yes, here is how.']


def test_apply_bundle_is_idempotent(tmp_path):
    path = tmp_path / 'target.sqlite3'
    migrate(path)
    apply_bundle(path, bundle())
    apply_bundle(path, bundle())
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM businesses').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM suppressions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM people').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
        assert db.execute('SELECT password_hash FROM users').fetchone()[0] == USERS[0]['password_hash']


def test_dry_run_counts_do_not_open_or_write_sqlite(monkeypatch, capsys):
    from scripts import import_from_railway as importer

    class Source:
        def close(self):
            pass

    monkeypatch.setattr(importer, '_required_environment', lambda: {
        'OLD_ENGINE_DATABASE_URL': 'not-printed-engine',
        'OLD_CRM_DATABASE_URL': 'not-printed-crm',
        'OLD_LEAD_DATABASE_URL': 'not-printed-lead',
        'CLIENT_ZERO_BUSINESS_ID': 'client-zero',
    })
    monkeypatch.setattr(importer, 'PostgresSource', lambda value: Source())
    monkeypatch.setattr(importer, 'read_sources', lambda *args: bundle())
    monkeypatch.setattr(importer, 'apply_bundle', lambda *args: (_ for _ in ()).throw(AssertionError('write')))
    assert importer.main([]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert output.out.splitlines() == ['subscription_status → число бизнесов', 'active → 1', 'businesses: 1', 'users: 1', 'suppressions: 1',
                                       'people: 1', 'messages: 2']
    assert 'not-printed' not in output.out


def test_postgres_source_forces_read_only_without_real_postgres(monkeypatch):
    calls = []

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return self

        def rollback(self):
            calls.append(('ROLLBACK', ()))

        def close(self):
            calls.append(('CLOSE', ()))

    def fake_connect(dsn, **kwargs):
        calls.append(('CONNECT', dsn, kwargs))
        return Connection()

    psycopg = types.ModuleType('psycopg')
    psycopg.connect = fake_connect
    rows = types.ModuleType('psycopg.rows')
    rows.dict_row = object()
    monkeypatch.setitem(sys.modules, 'psycopg', psycopg)
    monkeypatch.setitem(sys.modules, 'psycopg.rows', rows)

    source = PostgresSource('postgresql+psycopg://user:private@example/db')
    source.close()
    assert calls[0][0] == 'CONNECT'
    assert calls[0][1] == 'postgresql://user:private@example/db'
    assert calls[0][2]['options'] == '-c default_transaction_read_only=on'
    assert ('SET TRANSACTION READ ONLY', ()) in calls
    assert calls[-2:] == [('ROLLBACK', ()), ('CLOSE', ())]


def test_import_warns_before_write_and_preserves_unknown_status(tmp_path, monkeypatch, capsys):
    from dataclasses import replace
    from scripts import import_from_railway as importer

    target = tmp_path / 'target.sqlite3'
    legacy = replace(bundle(), business=bundle().business | {'subscription_status': 'legacy_paid'})
    monkeypatch.setattr(importer, '_required_environment', lambda: dict.fromkeys(
        (*importer.SOURCE_ENV, 'CLIENT_ZERO_BUSINESS_ID'), 'synthetic'))
    monkeypatch.setattr(importer, 'PostgresSource', lambda value: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(importer, 'read_sources', lambda *args: legacy)
    monkeypatch.setenv('DB_PATH', str(target))
    def write(path, value):
        output = capsys.readouterr()
        assert 'legacy_paid → 1' in output.out
        assert 'WARNING' in output.err and 'outside Lemon Squeezy' in output.err
        apply_bundle(path, value)
    monkeypatch.setattr(importer, 'apply_bundle', write)
    assert importer.main(['--apply']) == 0
    with connect(target) as db:
        assert db.execute('SELECT subscription_status FROM businesses').fetchone()[0] == 'legacy_paid'
