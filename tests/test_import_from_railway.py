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
    assert result.counts() == {'businesses': 1, 'users': 1, 'suppressions': 1, 'people': 1, 'messages': 2, 'log': 0}
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
    monkeypatch.setattr(importer, 'read_sources', lambda *args, **kwargs: bundle())
    monkeypatch.setattr(importer, 'apply_bundle', lambda *args: (_ for _ in ()).throw(AssertionError('write')))
    assert importer.main([]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert 'target.messages: transfer=2' in output.out
    assert 'skip_no_email=1' in output.out
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
    monkeypatch.setattr(importer, 'read_sources', lambda *args, **kwargs: legacy)
    monkeypatch.setenv('DB_PATH', str(target))
    def write(path, value):
        output = capsys.readouterr()
        assert 'legacy_paid' not in output.out
        assert 'WARNING subscription_status_outside_supported_set=1' in output.err
        apply_bundle(path, value)
    monkeypatch.setattr(importer, 'apply_bundle', write)
    assert importer.main(['--apply']) == 0
    with connect(target) as db:
        assert db.execute('SELECT subscription_status FROM businesses').fetchone()[0] == 'legacy_paid'


def test_import_gaps_and_idempotency(tmp_path):
    mailbox = MAILBOX | {'smtp_host': 'smtp.example.test', 'smtp_port': 465,
        'smtp_username': 'sender@example.test', 'imap_host': 'imap.example.test',
        'imap_port': 993, 'imap_last_uid': 77, 'password_encrypted': 'never-copy-this'}
    full = ({'id': 'full-1', 'conversation_id': 'conversation-1', 'sequence_number': 1,
        'email': PEOPLE[0]['email'], 'direction': 'inbound', 'text': 'Full text ' * 200,
        'external_message_id': '<original@example.test>',
        'metadata': {'subject': 'Original subject', 'in_reply_to': '<previous@example.test>'}},)
    touches = TOUCHES + ({'touch_id': 'evorove:conversation-1:msg:1', 'person_id': 'person-1',
        'kind': 'message', 'summary': 'Customer: truncated', 'payload': {'direction': 'inbound'}},)
    notes = ({'id': 'note-1', 'person_id': 'person-1', 'body': 'Keep this private note'},
             {'id': 'note-2', 'person_id': 'missing-person', 'body': 'Orphan'})
    result = build_bundle(business=BUSINESS | {'current_period_end': '2027-01-01'}, dna=DNA,
        mailbox=mailbox, board_people=PEOPLE, board_touches=touches,
        conversation_messages=full + full, board_notes=notes, keep_imap_cursor=True)
    assert result.business['smtp_port'] == 587
    assert result.business['imap_user'] == 'sender@example.test'
    assert result.business['imap_last_uid'] == 77
    assert result.business['subscription_ends_at'] == '2027-01-01'
    assert result.messages[0]['body'] == full[0]['text']
    assert result.messages[0]['subject'] == 'Original subject'
    assert result.messages[0]['smtp_message_id'] == '<original@example.test>'
    assert len(result.messages) == 3  # full + two genuinely different board-only messages
    assert result.report['engine.conversation_messages']['skip_duplicate'] == 1
    assert result.report['crm.board_touches']['skip_full_message_or_duplicate'] == 1
    assert result.report['engine.email_connections']['smtp_port_465_to_587'] == 1
    path = tmp_path / 'target.db'
    apply_bundle(path, result)
    apply_bundle(path, result)
    with connect(path) as db:
        row = db.execute('SELECT * FROM businesses').fetchone()
        assert row['smtp_password_enc'] is None and row['imap_password_enc'] is None
        assert row['imap_last_uid'] == 77 and row['smtp_port'] == 587
        assert db.execute("SELECT detail FROM log WHERE event='note'").fetchall()[0][0] == notes[0]['body']
        assert db.execute('SELECT count(*) FROM log').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 3
        assert tuple(db.execute('SELECT * FROM legacy_business_map').fetchone()) == ('client-zero', 'client-zero')


def test_unboarded_requires_flag_and_deduplicates(capsys):
    from scripts.import_from_railway import _print_counts
    engine = ({'id': 'on-board', 'email': PEOPLE[0]['email']},
              {'id': 'new', 'email': 'new@example.test'}, {'id': 'no-email'})
    lead = ({'id': 'duplicate', 'email': 'NEW@example.test'},
            {'id': 'unique', 'email': 'unique@example.test'})
    options = dict(business=BUSINESS, dna=DNA, board_people=PEOPLE,
                   engine_leads=engine, lead_leads=lead)
    dry = build_bundle(**options)
    assert len(dry.people) == 1
    assert dry.report['engine.leads']['unboarded'] == 1
    assert dry.report['lead.leads']['unboarded'] == 1
    enabled = build_bundle(**options, include_unboarded_leads=True)
    assert len(enabled.people) == 3
    assert len({p['email'] for p in enabled.people}) == 3
    _print_counts(dry)
    output = capsys.readouterr().out
    assert 'skip_requires_include_unboarded_leads=1' in output
    assert '@' not in output and BUSINESS['name'] not in output


def test_cli_passes_unboarded_flag_without_typeerror(monkeypatch, capsys):
    from scripts import import_from_railway as importer
    calls = []
    monkeypatch.setattr(importer, '_required_environment', lambda: dict.fromkeys(
        (*importer.SOURCE_ENV, 'CLIENT_ZERO_BUSINESS_ID'), 'synthetic'))
    monkeypatch.setattr(importer, 'PostgresSource', lambda _: types.SimpleNamespace(close=lambda: None))

    # Explicit production signature, rather than a stale *args-only mock.
    def source(engine, crm, lead, business_id, *, include_unboarded_leads=False, keep_imap_cursor=False):
        calls.append(include_unboarded_leads)
        return bundle()

    monkeypatch.setattr(importer, 'read_sources', source)
    assert importer.main(['--dry-run']) == 0
    assert importer.main(['--dry-run', '--include-unboarded-leads']) == 0
    assert calls == [False, True]
    assert 'Import failed' not in capsys.readouterr().err


def test_read_sources_sqlite_stubs():
    import sqlite3
    from scripts.import_from_railway import read_sources

    class Source:
        def __init__(self):
            self.db = sqlite3.connect(':memory:')
            self.db.row_factory = sqlite3.Row

        def has_table(self, table):
            return bool(self.one('SELECT name FROM sqlite_master WHERE name=%s', (table,)))

        def all(self, sql, params=()):
            if 'information_schema.columns' in sql:
                return tuple({'column_name': r[1]} for r in self.db.execute(f'PRAGMA table_info("{params[0]}")'))
            return tuple(dict(r) for r in self.db.execute(sql.replace('%s', '?'), params))

        def one(self, sql, params=()):
            return next(iter(self.all(sql, params)), None)

    engine, crm, lead = Source(), Source(), Source()
    try:
        engine.db.executescript('''
            CREATE TABLE staff_users(id,email,normalized_email,password_hash,created_at,business_id);
            CREATE TABLE businesses(id,name,created_at,plan,subscription_status,payment_customer_id,payment_subscription_id,current_period_end);
            INSERT INTO businesses VALUES ('zero','Synthetic','2026-01-01','pro','active',NULL,NULL,'2027-01-01');
            CREATE TABLE business_dna(business_id,configuration,active,version);
            INSERT INTO business_dna VALUES ('zero','{}',1,1);
            CREATE TABLE leads(id,business_id,name,email,normalized_email,metadata,created_at,updated_at);
            INSERT INTO leads VALUES ('lead','zero','Test','test@example.test',NULL,'{}',NULL,NULL);
            INSERT INTO leads VALUES ('foreign','other','Other','other@example.test',NULL,'{}',NULL,NULL);
            CREATE TABLE conversations(id,business_id,lead_id,external_session_id,channel);
            INSERT INTO conversations VALUES ('c','zero','lead',NULL,'email');
            CREATE TABLE conversation_messages(id,business_id,conversation_id,sequence_number,direction,text,external_message_id,metadata,created_at);
            INSERT INTO conversation_messages VALUES ('m','zero','c',1,'inbound','Full body','<id@example.test>','{"subject":"Subject"}',NULL);
        ''')
        result = read_sources(engine, crm, lead, 'zero', include_unboarded_leads=True)
        assert len(result.people) == 1 and result.people[0]['email'] == 'test@example.test'
        assert result.messages[0]['subject'] == 'Subject'
        assert result.messages[0]['body'] == 'Full body'
        assert result.report['crm.board_notes']['source_absent'] == 1
        assert result.report['lead.leads']['source_absent'] == 1
        assert result.report['engine.leads']['read'] == 1
    finally:
        for source in (engine, crm, lead):
            source.db.close()
