"""One-time, read-only import from the retired Railway PostgreSQL databases."""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect, migrate
from app.billing import STATUSES


SOURCE_ENV = (
    "OLD_ENGINE_DATABASE_URL",
    "OLD_CRM_DATABASE_URL",
    "OLD_LEAD_DATABASE_URL",
)
STAGES = {
    "cold": "cold",
    "in_work": "in_progress",
    "offer_sent": "offer_made",
    "done": "done",
    "discarded": "lost",
}


@dataclass(frozen=True)
class ImportBundle:
    business: dict
    users: tuple[dict, ...]
    suppressions: tuple[dict, ...]
    people: tuple[dict, ...]
    messages: tuple[dict, ...]
    notes: tuple[dict, ...] = ()
    report: dict = field(default_factory=dict)

    def counts(self):
        return {
            "businesses": 1,
            "users": len(self.users),
            "suppressions": len(self.suppressions),
            "people": len(self.people),
            "messages": len(self.messages),
            "log": len(self.notes),
        }


class PostgresSource:
    """Small psycopg boundary; every session is read-only by construction."""

    def __init__(self, dsn):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install requirements-import.txt before running this script") from exc
        psycopg_dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self.connection = psycopg.connect(
            psycopg_dsn,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
        )
        self.connection.execute("SET TRANSACTION READ ONLY")

    def close(self):
        self.connection.rollback()
        self.connection.close()

    def has_table(self, name):
        row = self.one("SELECT to_regclass(%s) IS NOT NULL AS present", (f"public.{name}",))
        return bool(row and row["present"])

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return tuple(self.connection.execute(sql, params).fetchall())


def _timestamp(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or datetime.now(timezone.utc).isoformat())


def _json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _list_text(value):
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str) and text.strip():
            found.append(text.strip())
    return found


def _business_brief(dna, brief):
    parts = []
    description = str(dna.get("business", {}).get("description") or "").strip()
    if description:
        parts.append(description)
    sold = _list_text((brief or {}).get("what_we_sell"))
    audience = _list_text((brief or {}).get("who_may_fit"))
    if not sold:
        sold = [
            str(service.get("description") or service.get("name") or "").strip()
            for service in dna.get("services", [])
            if isinstance(service, dict)
        ]
        sold = [item for item in sold if item]
    if sold:
        parts.append("Offer: " + "; ".join(sold))
    if audience:
        parts.append("Audience: " + "; ".join(audience))
    archetype = str((brief or {}).get("business_archetype") or "").strip()
    if archetype:
        parts.append("Business type: " + archetype)
    return "\n".join(dict.fromkeys(parts))


def _offer_text(dna):
    description = str(dna.get("business", {}).get("description") or "").strip()
    if description:
        return description
    services = []
    for service in dna.get("services", []):
        if isinstance(service, dict):
            text = str(service.get("description") or service.get("name") or "").strip()
            if text:
                services.append(text)
    return "; ".join(services)


def build_bundle(*, business, dna, mailbox=None, brief=None, users=(), suppressions=(),
                 board_people=(), board_touches=(), conversation_messages=(), board_notes=(),
                 engine_leads=(), lead_leads=(), include_unboarded_leads=False):
    report = defaultdict(Counter)
    def record(table, reason, count=1):
        report[table][reason] += count
    for table, rows in (('engine.businesses', (business,)), ('engine.staff_users', users),
                        ('engine.email_suppressions', suppressions), ('crm.board_people', board_people),
                        ('crm.board_touches', board_touches), ('engine.conversation_messages', conversation_messages),
                        ('crm.board_notes', board_notes), ('engine.leads', engine_leads), ('lead.leads', lead_leads)):
        record(table, 'read', len(rows))
    record('engine.email_connections', 'read', int(bool(mailbox)))
    dna = _json(dna)
    brief = _json(brief)
    mailbox = mailbox or {}
    target_business = {
        "id": business["id"],
        "name": business["name"],
        "created_at": _timestamp(business.get("created_at")),
        "plan": business.get("plan"),
        "subscription_ends_at": _timestamp(business['current_period_end']) if business.get('current_period_end') else None,
        "smtp_host": mailbox.get('smtp_host'),
        "smtp_port": 587 if int(mailbox.get('smtp_port') or 587) == 465 else int(mailbox.get('smtp_port') or 587),
        "smtp_user": mailbox.get('smtp_username'),
        "imap_host": mailbox.get('imap_host'),
        "imap_port": int(mailbox.get('imap_port') or 993),
        "imap_user": mailbox.get('smtp_username'),
        "imap_last_uid": mailbox.get('imap_last_uid'),
        "subscription_status": business.get("subscription_status") or "incomplete",
        "ls_customer_id": business.get("payment_customer_id"),
        "ls_subscription_id": business.get("payment_subscription_id"),
        "offer_text": _offer_text(dna),
        "payment_link": str(dna.get("payment", {}).get("payment_link") or "").strip(),
        "postal_address": str(mailbox.get("postal_address") or "").strip(),
        "sender_name": str(mailbox.get("from_name") or "").strip(),
        "daily_cap": int(mailbox.get("daily_limit") or 20),
        "brief": _business_brief(dna, brief),
    }
    if int(mailbox.get('smtp_port') or 587) == 465:
        record('engine.email_connections', 'smtp_port_465_to_587')
    record('engine.email_connections', 'passwords_not_imported', int(bool(mailbox)))
    target_users = tuple({
        "id": row["id"],
        "email": str(row.get("normalized_email") or row["email"]).strip().casefold(),
        "password_hash": row["password_hash"],
        "business_id": business["id"],
        "role": "owner",
        "created_at": _timestamp(row.get("created_at")),
    } for row in users)
    target_suppressions = tuple({
        "business_id": business["id"],
        "email": str(row["email"]).strip().casefold(),
        "reason": str(row.get("reason") or "legacy").strip(),
        "created_at": _timestamp(row.get("suppressed_at")),
    } for row in suppressions if str(row.get("email") or "").strip())

    for table, count in (('engine.businesses', 1), ('engine.staff_users', len(target_users)),
                         ('engine.email_connections', int(bool(mailbox))),
                         ('engine.email_suppressions', len(target_suppressions))):
        record(table, 'transfer', count)
    record('engine.email_suppressions', 'skip_no_email', len(suppressions) - len(target_suppressions))
    touches_by_person = {}
    for touch in board_touches:
        touches_by_person.setdefault(touch["person_id"], []).append(touch)
    target_people = []
    target_messages = []
    seen_emails = set()
    person_aliases = {}
    for row in board_people:
        email = str(row.get("email") or "").strip().casefold()
        if not email:
            record('crm.board_people', 'skip_no_email')
            continue
        if email in seen_emails:
            person_aliases[row['person_id']] = next(p['id'] for p in target_people if p['email'] == email)
            record('crm.board_people', 'skip_duplicate_email')
            continue
        seen_emails.add(email)
        person_aliases[row['person_id']] = row['person_id']
        record('crm.board_people', 'transfer')
        touches = sorted(touches_by_person.get(row["person_id"], ()),
                         key=lambda item: (_timestamp(item.get("occurred_at")), item["touch_id"]))
        assembled = next((item for item in touches if item.get("kind") == "assembled"), None)
        payload = _json(assembled.get("payload")) if assembled else {}
        pain = str(row.get("observed_pain") or payload.get("observed_pain")
                   or payload.get("reason") or row.get("summary") or "Legacy board contact").strip()
        source = str(payload.get("reason_source") or "").strip()
        target_people.append({
            "id": row["person_id"],
            "business_id": business["id"],
            "name": row.get("name"),
            "role": payload.get("role"),
            "email": email,
            "company": payload.get("company"),
            "website": payload.get("website"),
            "us_state": payload.get("us_state"),
            "segment": payload.get("segment"),
            "pain": pain,
            "evidence_quote": pain,
            "source_url": source,
            "stage": STAGES.get(row.get("tab"), "lost"),
            "last_move": row.get("last_kind"),
            "created_at": _timestamp(row.get("created_at")),
            "updated_at": _timestamp(row.get("updated_at") or row.get("last_touch_at")),
        })
    # Both lead sources are tenant-filtered by read_sources. Board contacts always win.
    for table, rows in (('engine.leads', engine_leads), ('lead.leads', lead_leads)):
        for row in rows:
            email = str(row.get('normalized_email') or row.get('email') or '').strip().casefold()
            if not email:
                record(table, 'skip_no_email')
            elif email in seen_emails:
                record(table, 'skip_on_board_or_duplicate')
            else:
                seen_emails.add(email)
                record(table, 'unboarded')
                if not include_unboarded_leads:
                    record(table, 'skip_requires_include_unboarded_leads')
                    continue
                payload = _json(row.get('metadata') or row.get('payload'))
                target_people.append(dict(
                    id='legacy:' + table + ':' + str(row['id']), business_id=business['id'],
                    email=email, name=row.get('name'), role=payload.get('role'), company=payload.get('company'),
                    website=payload.get('website'), us_state=payload.get('us_state'), segment=payload.get('segment'),
                    pain=payload.get('observed_pain') or 'Legacy unboarded lead; review before outreach',
                    evidence_quote=payload.get('evidence_quote') or '', source_url=payload.get('source_url') or '',
                    stage='cold', last_move=None, created_at=_timestamp(row.get('created_at')),
                    updated_at=_timestamp(row.get('updated_at'))))
                record(table, 'transfer')
    by_email = {p['email']: p['id'] for p in target_people}
    full_by_touch = {}
    seen_messages = set()
    for row in conversation_messages:
        email = str(row.get('email') or '').strip().casefold()
        pid = by_email.get(email) or person_aliases.get(row.get('person_id'))
        direction = {'inbound': 'in', 'outbound': 'out', 'in': 'in', 'out': 'out'}.get(row.get('direction'))
        body = row.get('text') or ''
        if not pid or not direction or not body:
            record('engine.conversation_messages', 'skip_no_person_direction_or_body')
            continue
        metadata = _json(row.get('metadata'))
        external = row.get('external_message_id') or ''
        smtp_id = metadata.get('smtp_message_id') or (external if external.startswith('<') else None)
        identity = (pid, direction, smtp_id or body)
        if identity in seen_messages:
            record('engine.conversation_messages', 'skip_duplicate')
            continue
        seen_messages.add(identity)
        message = dict(id='legacy:engine:' + str(row['id']), person_id=pid, direction=direction,
                       subject=metadata.get('subject') or row.get('subject'), body=body,
                       smtp_message_id=smtp_id, in_reply_to=metadata.get('in_reply_to'),
                       move=metadata.get('move'), created_at=_timestamp(row.get('created_at')))
        target_messages.append(message)
        record('engine.conversation_messages', 'transfer')
        full_by_touch[f"evorove:{row.get('conversation_id')}:msg:{row.get('sequence_number')}"] = message
        if external.startswith('cold:'):
            full_by_touch[f"evorove:{external}:msg:1"] = message
    for touch in board_touches:
        payload = _json(touch.get('payload'))
        pid = person_aliases.get(touch['person_id'])
        direction = {'inbound': 'in', 'outbound': 'out'}.get(payload.get('direction'))
        body = str(touch.get('summary') or '').strip()
        for prefix in ('Customer: ', 'Evorove: '):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        if touch.get('kind') != 'message' or not pid or not direction or not body:
            record('crm.board_touches', 'skip_not_message_or_no_person_direction_body')
            continue
        # CRM truncates summaries at 500 chars; known touch ID is authoritative.
        if touch['touch_id'] in full_by_touch or any(
            m['person_id'] == pid and m['direction'] == direction and
            (payload.get('smtp_message_id') and payload['smtp_message_id'] == m['smtp_message_id']
             or m['body'].startswith(body)) for m in target_messages):
            record('crm.board_touches', 'skip_full_message_or_duplicate')
            continue
        target_messages.append(dict(id='legacy:board:' + touch['touch_id'], person_id=pid,
            direction=direction, subject=payload.get('subject'), body=body,
            smtp_message_id=payload.get('smtp_message_id'), in_reply_to=payload.get('in_reply_to'),
            move=payload.get('move') or touch['kind'], created_at=_timestamp(touch.get('occurred_at'))))
        record('crm.board_touches', 'transfer')
    notes = []
    for row in board_notes:
        pid = person_aliases.get(row.get('person_id'))
        detail = row.get('body') or row.get('text') or row.get('note') or row.get('content')
        if not pid or not detail:
            record('crm.board_notes', 'skip_no_person_or_text')
            continue
        notes.append(dict(source_id=str(row['id']), business_id=business['id'], person_id=pid,
                          detail=detail, created_at=_timestamp(row.get('created_at'))))
        record('crm.board_notes', 'transfer')
    return ImportBundle(target_business, target_users, target_suppressions,
                        tuple(target_people), tuple(target_messages), tuple(notes), dict(report))


def read_sources(engine, crm, lead, business_id, *, include_unboarded_leads=False):
    business = engine.one(
        "SELECT id,name,created_at,plan,subscription_status,payment_customer_id,"
        "payment_subscription_id,current_period_end FROM businesses WHERE id=%s", (business_id,))
    if not business:
        raise RuntimeError("Client-zero business was not found in the old engine database")
    dna_row = engine.one(
        "SELECT configuration FROM business_dna WHERE business_id=%s AND active "
        "ORDER BY version DESC LIMIT 1", (business_id,))
    if not dna_row:
        raise RuntimeError("Active client-zero Business DNA was not found")
    memberships = engine.has_table("business_memberships")
    membership_sql = (
        " OR EXISTS (SELECT 1 FROM business_memberships m WHERE m.staff_user_id=u.id "
        "AND m.business_id=%s)" if memberships else ""
    )
    params = (business_id, business_id) if memberships else (business_id,)
    users = engine.all(
        "SELECT u.id,u.email,u.normalized_email,u.password_hash,u.created_at FROM staff_users u "
        "WHERE u.business_id=%s" + membership_sql + " ORDER BY u.created_at,u.id", params)
    mailbox = engine.one(
        "SELECT from_name,postal_address,daily_limit,smtp_host,smtp_port,smtp_username,imap_host,imap_port,imap_last_uid FROM email_connections WHERE business_id=%s",
        (business_id,)) if engine.has_table("email_connections") else None
    suppressions = engine.all(
        "SELECT email,reason,suppressed_at FROM email_suppressions WHERE business_id=%s "
        "ORDER BY email", (business_id,)) if engine.has_table("email_suppressions") else ()
    people = crm.all(
        "SELECT person_id,tab,name,email,summary,observed_pain,last_kind,last_touch_at,"
        "created_at,updated_at FROM board_people WHERE business_id=%s ORDER BY person_id",
        (business_id,)) if crm.has_table("board_people") else ()
    touches = crm.all(
        "SELECT touch_id,person_id,kind,summary,payload,occurred_at FROM board_touches "
        "WHERE business_id=%s ORDER BY occurred_at,touch_id", (business_id,)
    ) if crm.has_table("board_touches") else ()
    brief_row = lead.one(
        "SELECT what_we_sell,who_may_fit,business_archetype FROM briefs "
        "WHERE business_id=%s ORDER BY created_at DESC,id DESC LIMIT 1", (business_id,)
    ) if lead.has_table("briefs") else None
    return build_bundle(
        business=business,
        dna=dna_row["configuration"],
        mailbox=mailbox,
        brief=brief_row,
        users=users,
        suppressions=suppressions,
        board_people=people,
        board_touches=touches,
        conversation_messages=engine.all(
            "SELECT m.id,m.conversation_id,m.sequence_number,m.direction,m.text,m.external_message_id,"
            "m.metadata,m.created_at,COALESCE(l.email,c.external_session_id) AS email "
            "FROM conversation_messages m JOIN conversations c ON c.id=m.conversation_id AND c.business_id=m.business_id "
            "LEFT JOIN leads l ON l.id=c.lead_id AND l.business_id=c.business_id "
            "WHERE m.business_id=%s AND c.channel='email' ORDER BY m.created_at,m.id", (business_id,)
        ) if engine.has_table('conversation_messages') else (),
        board_notes=read_optional(crm, 'board_notes', business_id,
                                  ('id', 'person_id', 'body', 'text', 'note', 'content', 'created_at')),
        engine_leads=read_optional(engine, 'leads', business_id,
                                   ('id', 'name', 'email', 'normalized_email', 'metadata', 'created_at', 'updated_at')),
        lead_leads=read_optional(lead, 'leads', business_id,
                                 ('id', 'name', 'email', 'normalized_email', 'payload', 'created_at', 'updated_at')),
        include_unboarded_leads=include_unboarded_leads,
    )


def read_optional(source, table, business_id, allowed):
    if not source.has_table(table):
        return ()
    columns = {r['column_name'] for r in source.all(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))}
    selected = [name for name in allowed if name in columns]
    if not {'id', 'business_id'} <= columns:
        raise RuntimeError('Unsupported legacy schema')
    return source.all('SELECT ' + ','.join('"' + name + '"' for name in selected) +
                      ' FROM "' + table + '" WHERE business_id=%s ORDER BY id', (business_id,))


def apply_bundle(db_path, bundle):
    migrate(db_path)
    b = bundle.business
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO businesses(id,name,created_at,plan,subscription_status,ls_customer_id,"
            "ls_subscription_id,offer_text,payment_link,postal_address,sender_name,daily_cap,brief) "
            "VALUES (:id,:name,:created_at,:plan,:subscription_status,:ls_customer_id,"
            ":ls_subscription_id,:offer_text,:payment_link,:postal_address,:sender_name,:daily_cap,:brief) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,plan=excluded.plan,"
            "subscription_status=excluded.subscription_status,ls_customer_id=excluded.ls_customer_id,"
            "ls_subscription_id=excluded.ls_subscription_id,offer_text=excluded.offer_text,"
            "payment_link=excluded.payment_link,postal_address=excluded.postal_address,"
            "sender_name=excluded.sender_name,daily_cap=excluded.daily_cap,brief=excluded.brief", b)
        fields = ('subscription_ends_at', 'smtp_host', 'smtp_port', 'smtp_user',
                  'imap_host', 'imap_port', 'imap_user', 'imap_last_uid')
        db.execute('UPDATE businesses SET ' + ','.join(name + '=:' + name for name in fields) + ' WHERE id=:id', b)
        db.execute('INSERT OR IGNORE INTO legacy_business_map VALUES (?,?)', (b['id'], b['id']))
        for row in bundle.notes:
            db.execute("INSERT INTO log(business_id,person_id,event,detail,created_at,legacy_note_id) "
                       "SELECT :business_id,:person_id,'note',:detail,:created_at,:source_id "
                       "WHERE NOT EXISTS (SELECT 1 FROM log WHERE legacy_note_id=:source_id)", row)
        for row in bundle.users:
            db.execute(
                "INSERT INTO users(id,email,password_hash,business_id,role,created_at) "
                "VALUES (:id,:email,:password_hash,:business_id,:role,:created_at) "
                "ON CONFLICT(id) DO UPDATE SET email=excluded.email,password_hash=excluded.password_hash,"
                "business_id=excluded.business_id,role=excluded.role", row)
        for row in bundle.suppressions:
            db.execute(
                "INSERT INTO suppressions VALUES (:business_id,:email,:reason,:created_at) "
                "ON CONFLICT(business_id,email) DO UPDATE SET reason=excluded.reason", row)
        for row in bundle.people:
            db.execute(
                "INSERT INTO people(id,business_id,name,role,email,company,website,us_state,segment,pain,"
                "evidence_quote,source_url,stage,last_move,created_at,updated_at) VALUES "
                "(:id,:business_id,:name,:role,:email,:company,:website,:us_state,:segment,:pain,"
                ":evidence_quote,:source_url,:stage,:last_move,:created_at,:updated_at) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,email=excluded.email,"
                "company=excluded.company,website=excluded.website,us_state=excluded.us_state,"
                "segment=excluded.segment,pain=excluded.pain,evidence_quote=excluded.evidence_quote,"
                "source_url=excluded.source_url,stage=excluded.stage,last_move=excluded.last_move,"
                "updated_at=excluded.updated_at", row)
        for row in bundle.messages:
            db.execute(
                "INSERT INTO messages(id,person_id,direction,subject,body,smtp_message_id,in_reply_to,"
                "move,created_at) VALUES (:id,:person_id,:direction,:subject,:body,:smtp_message_id,"
                ":in_reply_to,:move,:created_at) ON CONFLICT(id) DO NOTHING", row)


def _required_environment():
    names = (*SOURCE_ENV, "CLIENT_ZERO_BUSINESS_ID")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in names}


def _print_counts(bundle):
    for table, counts in sorted(bundle.report.items()):
        # Only fixed table/reason labels and counters, never source data.
        print(table + ': ' + ', '.join(f'{reason}={count}' for reason, count in sorted(counts.items())))
    for table, count in bundle.counts().items():
        print(f'target.{table}: transfer={count}')
    if bundle.business['subscription_status'] not in STATUSES:
        print('WARNING subscription_status_outside_supported_set=1', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print counts only (default)")
    mode.add_argument("--apply", action="store_true", help="write to DB_PATH")
    parser.add_argument("--include-unboarded-leads", action="store_true")
    args = parser.parse_args(argv)
    sources = []
    try:
        env = _required_environment()
        for name in SOURCE_ENV:
            sources.append(PostgresSource(env[name]))
        bundle = read_sources(*sources, env["CLIENT_ZERO_BUSINESS_ID"],
                              include_unboarded_leads=args.include_unboarded_leads)
        _print_counts(bundle)
        if args.apply:
            db_path = os.environ.get("DB_PATH")
            if not db_path:
                raise RuntimeError("DB_PATH is required with --apply")
            apply_bundle(db_path, bundle)
    except Exception as exc:
        print(f"Import failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        for source in sources:
            source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
