"""One-time, read-only import from the retired Railway PostgreSQL databases."""

import argparse
import json
import os
import sys
from dataclasses import dataclass
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

    def counts(self):
        return {
            "businesses": 1,
            "users": len(self.users),
            "suppressions": len(self.suppressions),
            "people": len(self.people),
            "messages": len(self.messages),
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
                 board_people=(), board_touches=()):
    dna = _json(dna)
    brief = _json(brief)
    mailbox = mailbox or {}
    target_business = {
        "id": business["id"],
        "name": business["name"],
        "created_at": _timestamp(business.get("created_at")),
        "plan": business.get("plan"),
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

    touches_by_person = {}
    for touch in board_touches:
        touches_by_person.setdefault(touch["person_id"], []).append(touch)
    target_people = []
    target_messages = []
    for row in board_people:
        email = str(row.get("email") or "").strip().casefold()
        if not email:
            continue
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
        for touch in touches:
            if touch.get("kind") != "message":
                continue
            message_payload = _json(touch.get("payload"))
            direction = {"inbound": "in", "outbound": "out"}.get(message_payload.get("direction"))
            if direction is None:
                continue
            summary = str(touch.get("summary") or "").strip()
            for prefix in ("Customer: ", "Evorove: "):
                if summary.startswith(prefix):
                    summary = summary[len(prefix):]
                    break
            if not summary:
                continue
            target_messages.append({
                "id": "legacy:board:" + touch["touch_id"],
                "person_id": row["person_id"],
                "direction": direction,
                "subject": message_payload.get("subject"),
                "body": summary,
                "smtp_message_id": message_payload.get("smtp_message_id"),
                "in_reply_to": message_payload.get("in_reply_to"),
                "move": message_payload.get("move") or touch.get("kind"),
                "created_at": _timestamp(touch.get("occurred_at")),
            })
    return ImportBundle(target_business, target_users, target_suppressions,
                        tuple(target_people), tuple(target_messages))


def read_sources(engine, crm, lead, business_id):
    business = engine.one(
        "SELECT id,name,created_at,plan,subscription_status,payment_customer_id,"
        "payment_subscription_id FROM businesses WHERE id=%s", (business_id,))
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
        "SELECT from_name,postal_address,daily_limit FROM email_connections WHERE business_id=%s",
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
    )


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
    status = bundle.business["subscription_status"]
    print("subscription_status → число бизнесов")
    print(f"{status} → 1")
    if status not in STATUSES:
        print(f"WARNING: subscription_status {status!r} is outside Lemon Squeezy statuses; preserved unchanged", file=sys.stderr)
    for table, count in bundle.counts().items():
        print(f"{table}: {count}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print counts only (default)")
    mode.add_argument("--apply", action="store_true", help="write to DB_PATH")
    args = parser.parse_args(argv)
    sources = []
    try:
        env = _required_environment()
        for name in SOURCE_ENV:
            sources.append(PostgresSource(env[name]))
        bundle = read_sources(*sources, env["CLIENT_ZERO_BUSINESS_ID"])
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
