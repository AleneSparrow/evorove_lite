import pytest

from app import outreach, guards
from app.db import connect
from scripts import send_test_email as probe
from test_guards import settings, make_business, make_person, DAYTIME
from test_outreach import FakeSmtp, fake_imap, inbound, stub_llm


def snapshot(settings):
    with connect(settings.db_path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: [tuple(r) for r in db.execute(f'SELECT * FROM "{table}"')] for table in tables}


@pytest.mark.parametrize('cap', [10, 100])
@pytest.mark.parametrize('blocked', [None, 'pause', 'suppression', 'reply_cap'])
def test_reply_after_first_letter_allowance_exhausted(settings, blocked, cap):
    make_business(settings, daily_cap=cap)
    smtp = FakeSmtp()
    for index in range(10):
        make_person(settings, email=f'p{index}@example.test')
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 10
    make_person(settings, email='extra@example.test')
    assert outreach.send_first_letters('biz-1', settings=settings, send=smtp, now_utc=DAYTIME) == 0
    with connect(settings.db_path) as db:
        first = db.execute('SELECT mailbox_first_sent_at FROM businesses').fetchone()[0]
        pid = db.execute("SELECT id FROM people WHERE email='p0@example.test'").fetchone()[0]
        if blocked == 'pause':
            db.execute('UPDATE businesses SET outreach_paused=1')
        elif blocked == 'suppression':
            db.execute("INSERT INTO suppressions VALUES ('biz-1','p0@example.test','unsubscribe',?)", (DAYTIME.isoformat(),))
        elif blocked == 'reply_cap':
            for index in range(guards.REPLY_CAP):
                db.execute("INSERT INTO messages(id,person_id,direction,body,move,created_at) VALUES (?,?,'out','Reply','answer',?)",
                           (f'cap{index}', pid, DAYTIME.isoformat()))
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Tell me more', sender='p0@example.test')]),
                          llm=stub_llm(), now_utc=DAYTIME)
    assert len(smtp.sent) == (11 if blocked is None else 10)
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM mailbox_sends').fetchone()[0] == 10
        assert db.execute('SELECT mailbox_first_sent_at FROM businesses').fetchone()[0] == first


def test_smtp_probe_uses_credentials_while_paused_and_writes_nothing(settings, monkeypatch, capsys):
    make_business(settings, outreach_paused=1)
    before = snapshot(settings)
    sent = []
    monkeypatch.setattr(probe, 'smtp_send', lambda target, message: sent.append((target, message)))
    monkeypatch.setenv('DB_PATH', settings.db_path)
    monkeypatch.setenv('SECRET_KEY', settings.secret)
    assert probe.main(['--business-id', 'biz-1', '--to', 'test@example.test']) == 0
    assert capsys.readouterr().out == 'OK\n'
    target, message = sent[0]
    assert target['password'] == 'smtp-secret'
    assert message['Subject'] == 'Evorove test'
    assert message['To'] == 'test@example.test'
    assert snapshot(settings) == before


def test_imap_probe_only_logs_in_and_out(settings, monkeypatch, capsys):
    make_business(settings, outreach_paused=1)
    before = snapshot(settings)
    calls = []
    class IMAP:
        def __init__(self, *args, **kwargs):
            calls.append('connect')
        def login(self, user, password):
            assert password == 'imap-secret'
            calls.append('login')
            return 'OK', []
        def logout(self):
            calls.append('logout')
    monkeypatch.setattr(probe.imaplib, 'IMAP4_SSL', IMAP)
    monkeypatch.setattr(probe, 'smtp_send', lambda *args: pytest.fail('SMTP must not be used'))
    monkeypatch.setenv('DB_PATH', settings.db_path)
    monkeypatch.setenv('SECRET_KEY', settings.secret)
    assert probe.main(['--business-id', 'biz-1', '--check-imap']) == 0
    assert capsys.readouterr().out == 'OK\n'
    assert calls == ['connect', 'login', 'logout']
    assert snapshot(settings) == before


def test_probe_error_does_not_echo_exception(settings, monkeypatch, capsys):
    make_business(settings)
    before = snapshot(settings)
    def fail(*args):
        raise RuntimeError('private-host password recipient')
    monkeypatch.setattr(probe, 'smtp_send', fail)
    monkeypatch.setenv('DB_PATH', settings.db_path)
    monkeypatch.setenv('SECRET_KEY', settings.secret)
    assert probe.main(['--business-id', 'biz-1', '--to', 'test@example.test']) == 1
    assert capsys.readouterr().out == 'RuntimeError\n'
    assert snapshot(settings) == before


def test_reply_does_not_start_first_letter_warmup(settings):
    make_business(settings)
    person = make_person(settings, stage='in_progress')
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO messages(id,person_id,direction,body,move,created_at) VALUES ('prior',?,'out','Prior conversation','greet',?)",
                   (person['id'], DAYTIME.isoformat()))
    smtp = FakeSmtp()
    outreach.poll_replies('biz-1', settings=settings, send=smtp,
                          fetch=fake_imap([inbound('Tell me more')]), llm=stub_llm(), now_utc=DAYTIME)
    assert len(smtp.sent) == 1
    with connect(settings.db_path) as db:
        assert db.execute('SELECT mailbox_first_sent_at FROM businesses').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM mailbox_sends').fetchone()[0] == 0


@pytest.mark.parametrize('args', [[], ['--business-id', 'biz-1'], ['--unknown', 'private-value']])
def test_probe_argument_errors_are_private(args, capsys):
    assert probe.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == 'ValueError\n' and captured.err == ''
