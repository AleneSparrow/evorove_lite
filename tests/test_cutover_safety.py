"""Cutover protections using only disposable databases and fake mail transports."""
from datetime import timedelta

import pytest

from app import board, outreach
from app.db import connect, now
from app.settings import encrypt_password
from scripts import import_from_railway as importer
from test_guards import settings, make_business, make_person, DAYTIME
from test_outreach import FakeSmtp, fake_imap, stub_llm
from test_import_from_railway import bundle, BUSINESS, DNA, MAILBOX


def test_imported_history_is_processed_and_poll_does_not_answer(settings):
    imported = bundle()
    importer.apply_bundle(settings.db_path, imported)
    bid = imported.business['id']
    with connect(settings.db_path) as db:
        history = db.execute('SELECT * FROM businesses WHERE id=?', (bid,)).fetchone()['history_imported_at']
        assert history
        assert all(row['reply_processed'] and row['imported_at'] for row in db.execute('SELECT * FROM messages'))
        db.execute("UPDATE businesses SET imap_host='imap.example',imap_user='test@example.test',imap_password_enc=? WHERE id=?",
                   (encrypt_password(settings, 'synthetic'), bid))
        # Independent timestamp barrier: even an unmarked old row must be ignored.
        db.execute("INSERT INTO messages(id,person_id,direction,body,created_at) VALUES ('old-unmarked','person-1','in','Old question',?)",
                   ((DAYTIME - timedelta(days=100)).isoformat(),))
        assert outreach.pending_replies(db, bid) == []
    smtp, llm = FakeSmtp(), stub_llm()
    assert outreach.poll_replies(bid, settings=settings, send=smtp, fetch=fake_imap([]), llm=llm, now_utc=DAYTIME) == 0
    outreach.answer_pending_replies(settings, {'id': bid}, send=smtp, llm=llm, now_utc=DAYTIME)
    assert not smtp.sent and not llm.calls


@pytest.mark.parametrize('keep', [False, True])
def test_import_cursor_opt_in_and_cap(keep, capsys):
    mailbox = MAILBOX | {'imap_last_uid': 77, 'imap_uidvalidity': 5, 'daily_limit': 500}
    result = importer.build_bundle(business=BUSINESS, dna=DNA, mailbox=mailbox, keep_imap_cursor=keep)
    assert result.business['imap_last_uid'] == (77 if keep else None)
    assert result.business['imap_uidvalidity'] == (5 if keep else None)
    assert result.business['daily_cap'] == 20
    importer._print_counts(result)
    assert ('imap_cursor_kept=1' if keep else 'imap_cursor_reset=1') in capsys.readouterr().out


@pytest.mark.parametrize('changed', ['imap_host', 'imap_user', 'smtp_host', 'smtp_user', None])
def test_save_settings_resets_only_changed_mailbox(settings, changed):
    original = make_business(settings, imap_last_uid=100, imap_uidvalidity=42,
                             mailbox_first_sent_at=DAYTIME.isoformat())
    fields = {key: str(value) if value is not None else '' for key, value in original.items()}
    if changed:
        fields[changed] = 'other.example' if changed.endswith('host') else 'other@example.test'
    board.save_settings(settings, 'biz-1', fields)
    with connect(settings.db_path) as db:
        row = db.execute('SELECT * FROM businesses').fetchone()
        imap_changed = changed in ('imap_host', 'imap_user')
        assert row['imap_last_uid'] == (None if imap_changed else 100)
        assert row['imap_uidvalidity'] == (None if imap_changed else 42)
        assert row['mailbox_first_sent_at'] == (None if changed else DAYTIME.isoformat())
        assert row['mailbox_generation'] == int(changed is not None)


@pytest.mark.parametrize('live_kind', ['runs', 'messages'])
def test_reimport_requires_flag_and_preserves_live_rows(settings, live_kind):
    initial = bundle()
    importer.apply_bundle(settings.db_path, initial)
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET subscription_status='cancelled',smtp_host='new.example',imap_last_uid=999,imap_uidvalidity=19,daily_cap=3")
        db.execute("UPDATE users SET password_hash='new-password-hash'")
        db.execute("UPDATE people SET stage='done'")
        if live_kind == 'runs':
            db.execute("INSERT INTO runs(id,kind,started_at) VALUES ('live','poll',?)", (now(),))
        else:
            db.execute("INSERT INTO messages(id,person_id,direction,body,created_at) VALUES ('live','person-1','out','New server',?)", (now(),))
        before = tuple(db.execute('SELECT * FROM businesses').fetchone())
    with pytest.raises(importer.ReimportRefused):
        importer.apply_bundle(settings.db_path, initial)
    importer.apply_bundle(settings.db_path, initial, force_reimport=True)
    with connect(settings.db_path) as db:
        assert tuple(db.execute('SELECT * FROM businesses').fetchone()) == before
        assert db.execute('SELECT password_hash FROM users').fetchone()[0] == 'new-password-hash'
        assert db.execute('SELECT stage FROM people').fetchone()[0] == 'done'


def test_suppressions_delta_changes_nothing_else(settings):
    importer.apply_bundle(settings.db_path, bundle())
    with connect(settings.db_path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('suppressions','sqlite_sequence')")]
        before = {table: [tuple(r) for r in db.execute(f'SELECT * FROM "{table}"')] for table in tables}
    rows = ({'email': 'new-stop@example.test', 'reason': 'unsubscribe', 'created_at': now()},
            {'email': 'stop@example.com', 'reason': 'changed', 'created_at': now()})
    for _ in range(2):
        importer.apply_suppressions(settings.db_path, 'client-zero', rows)
    with connect(settings.db_path) as db:
        assert {table: [tuple(r) for r in db.execute(f'SELECT * FROM "{table}"')] for table in tables} == before
        assert db.execute('SELECT count(*) FROM suppressions').fetchone()[0] == 2
        assert db.execute("SELECT reason FROM suppressions WHERE email='stop@example.com'").fetchone()[0] == 'unsubscribe'


@pytest.mark.parametrize('days,cap,limit', [(0, 100, 10), (1, 100, 15), (2, 100, 20), (10, 12, 12)])
def test_mailbox_warmup_formula(days, cap, limit):
    assert outreach.mailbox_daily_limit({'daily_cap': cap, 'mailbox_first_sent_at': DAYTIME.isoformat()},
                                        DAYTIME + timedelta(days=days)) == limit


def test_actual_warmup_limit_and_failure_does_not_start_clock(settings):
    original = make_business(settings, daily_cap=100)
    person = make_person(settings)
    with connect(settings.db_path) as db:
        current = dict(db.execute('SELECT * FROM businesses').fetchone())
    failed = FakeSmtp(error=RuntimeError('synthetic failure'))
    assert outreach.deliver(settings, current, person, 'Subject', 'Body', 'greet', send=failed, now_utc=DAYTIME) is None
    with connect(settings.db_path) as db:
        assert db.execute('SELECT mailbox_first_sent_at FROM businesses').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM mailbox_sends').fetchone()[0] == 0
    smtp = FakeSmtp()
    for _ in range(12):
        outreach.deliver(settings, current, person, 'Subject', 'Body', 'greet', send=smtp, now_utc=DAYTIME)
    assert len(smtp.sent) == 10
    with connect(settings.db_path) as db:
        assert db.execute('SELECT mailbox_first_sent_at FROM businesses').fetchone()[0] == DAYTIME.isoformat()
        current = dict(db.execute('SELECT * FROM businesses').fetchone())
    for _ in range(16):
        outreach.deliver(settings, current, person, 'Subject', 'Body', 'greet', send=smtp, now_utc=DAYTIME + timedelta(days=1))
    assert len(smtp.sent) == 25


def test_reset_warmup_restarts_ten_send_allowance(settings):
    original = make_business(settings, daily_cap=100)
    person = make_person(settings)
    smtp = FakeSmtp()
    with connect(settings.db_path) as db:
        current = dict(db.execute('SELECT * FROM businesses').fetchone())
    for _ in range(10):
        assert outreach.deliver(settings, current, person, 'Subject', 'Body', 'greet', send=smtp, now_utc=DAYTIME)
    fields = {key: str(value) if value is not None else '' for key, value in original.items()}
    fields['smtp_user'] = 'new@example.test'
    board.save_settings(settings, 'biz-1', fields)
    with connect(settings.db_path) as db:
        current = dict(db.execute('SELECT * FROM businesses').fetchone())
    for _ in range(11):
        outreach.deliver(settings, current, person, 'Subject', 'Body', 'greet', send=smtp, now_utc=DAYTIME)
    assert len(smtp.sent) == 20


def test_suppressions_only_cli_reads_stop_lists_and_no_profiles(settings, monkeypatch, capsys):
    importer.apply_bundle(settings.db_path, bundle())
    class Source:
        def close(self):
            pass

        def has_table(self, table):
            assert table in ('email_suppressions', 'suppression_entries', 'suppressions')
            return table == 'suppression_entries'

        def all(self, sql, params=()):
            if 'information_schema' in sql:
                return [{'column_name': col} for col in ('business_id', 'email', 'reason', 'created_at')]
            assert params == ('client-zero',)
            return [{'email': 'DELTA@example.test', 'reason': 'reply_opt_out', 'created_at': now()}]

    monkeypatch.setattr(importer, '_required_environment', lambda: {
        **dict.fromkeys(importer.SOURCE_ENV, 'synthetic'), 'CLIENT_ZERO_BUSINESS_ID': 'client-zero'})
    monkeypatch.setattr(importer, 'PostgresSource', lambda _: Source())
    monkeypatch.setenv('DB_PATH', settings.db_path)
    monkeypatch.setattr(importer, 'read_sources', lambda *a, **kw: pytest.fail('full profile read'))
    for _ in range(2):
        assert importer.main(['--suppressions-only', '--apply']) == 0
    assert 'DELTA' not in capsys.readouterr().out
    with connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM suppressions WHERE email='delta@example.test'").fetchone()[0] == 1


def test_stale_poll_cannot_restore_old_mailbox_cursor(settings):
    original = make_business(settings, imap_last_uid=99, imap_uidvalidity=3)
    fields = {key: str(value) if value is not None else '' for key, value in original.items()}
    fields['imap_host'] = 'new.example.test'
    board.save_settings(settings, 'biz-1', fields)
    outreach.write_cursor(settings, 'biz-1', 100, 3, generation=0)
    assert outreach.read_cursor(settings, 'biz-1') is None


def test_upgrade_marks_legacy_without_hiding_native_pending(settings):
    # Simulate a database imported with the previous version: 006 is applied last.
    import sqlite3
    from pathlib import Path
    path = Path(settings.db_path).with_name('old-schema.db')
    with sqlite3.connect(path) as db:
        for migration in sorted((Path(__file__).parents[1] / 'migrations').glob('*.sql')):
            if migration.name.startswith('006_'):
                continue
            db.executescript(migration.read_text())
        db.execute("INSERT INTO businesses(id,name,created_at) VALUES ('old','Old',?)", (now(),))
        db.execute("INSERT INTO legacy_business_map VALUES ('old','old')")
        db.execute("INSERT INTO people(id,business_id,email,pain,evidence_quote,source_url,created_at,updated_at) VALUES ('p','old','p@example.test','','','',?,?)", (now(), now()))
        for mid in ('legacy:engine:old', 'native'):
            db.execute("INSERT INTO messages(id,person_id,direction,body,created_at) VALUES (?,'p','in','Question',?)", (mid, DAYTIME.isoformat()))
        migration = Path(__file__).parents[1] / 'migrations/006_cutover_safety.sql'
        db.executescript(migration.read_text())
    with connect(path) as db:
        assert [row['id'] for row in outreach.pending_replies(db, 'old')] == ['native']
