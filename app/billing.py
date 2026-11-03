import hashlib
import hmac
import json
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import HTTPException

from .db import connect, now
from . import outreach

EVENTS = {"subscription_created", "subscription_updated", "subscription_cancelled",
          "subscription_resumed", "subscription_expired", "subscription_paused", "subscription_unpaused"}
INVOICES = {"subscription_payment_success", "subscription_payment_recovered", "subscription_payment_failed"}
ORDERS = {"order_created"}
STATUSES = {"on_trial", "active", "paused", "past_due", "unpaid", "cancelled", "expired"}


def utc_date(value):
    """Accept only timezone-aware timestamps; invalid provider values fail closed."""
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def entitled(business, current=None):
    if business['subscription_status'] == 'active':
        return True
    ends = utc_date(business['subscription_ends_at'])
    return (business['subscription_status'] == 'cancelled' and ends is not None
            and (current or datetime.now(timezone.utc)) < ends)


def payment_link_for(business, person_id: str) -> str:
    """Attach tenant-scoped checkout custom data to one person's offer URL."""
    raw = str(business.get("payment_link") or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or not person_id:
        return raw
    custom = {"checkout[custom][business_id]", "checkout[custom][person_id]"}
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in custom]
    query.extend((("checkout[custom][business_id]", str(business["id"])),
                  ("checkout[custom][person_id]", str(person_id))))
    return urlunparse(parsed._replace(query=urlencode(query)))


def checkout(settings, user, plan):
    if plan not in {"starter", "pro"}:
        raise HTTPException(400, "Unknown plan")
    variant = settings.get("LEMONSQUEEZY_VARIANT_" + plan.upper())
    if not all((variant, settings.get("LEMONSQUEEZY_API_KEY"), settings.get("LEMONSQUEEZY_STORE_ID"))):
        raise HTTPException(503, "Billing is not configured")
    with connect(settings.db_path) as db:
        business = db.execute("SELECT subscription_status FROM businesses WHERE id=?", (user["business_id"],)).fetchone()
    if business[0] in {"active", "on_trial", "cancelled"}:
        raise HTTPException(409, "A subscription already exists")
    body = {"data": {"type": "checkouts", "attributes": {
        "checkout_options": {"skip_trial": True},
        "checkout_data": {"email": user["email"], "custom": {"business_id": user["business_id"], "plan": plan}},
        "product_options": {"redirect_url": settings.base_url + "/app", "enabled_variants": [int(variant)]}},
        "relationships": {"store": {"data": {"type": "stores", "id": settings.get("LEMONSQUEEZY_STORE_ID")}},
                          "variant": {"data": {"type": "variants", "id": variant}}}}}
    try:
        response = httpx.post("https://api.lemonsqueezy.com/v1/checkouts", json=body, timeout=15,
                              headers={"Authorization": "Bearer " + settings.get("LEMONSQUEEZY_API_KEY"),
                                       "Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json"})
        response.raise_for_status()
        url = response.json()["data"]["attributes"]["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".lemonsqueezy.com"):
            raise ValueError()
        return url
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, "Unable to create checkout") from None


def webhook(settings, payload, signature):
    secret = settings.get("LEMONSQUEEZY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "Billing is not configured")
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid signature")
    try:
        event = json.loads(payload)
        meta, data = event["meta"], event["data"]
        name = meta["event_name"]
        if name not in EVENTS | INVOICES | ORDERS:
            return
        attributes = data["attributes"]
        if name in EVENTS:
            if data["type"] != "subscriptions" or attributes["status"] not in STATUSES:
                raise ValueError()
        elif name in INVOICES and data["type"] != "subscription-invoices":
            raise ValueError()
        elif name in ORDERS and data["type"] != "orders":
            raise ValueError()
        stamp_value = attributes.get("updated_at") or attributes.get("created_at")
        stamp = datetime.fromisoformat(stamp_value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        stamp = stamp.astimezone(timezone.utc).isoformat()
        custom_data = meta.get("custom_data") or {}
        business_id = custom_data.get("business_id")
        person_id = custom_data.get("person_id")
        if "person_id" in custom_data and (not isinstance(person_id, str) or not person_id.strip()):
            raise ValueError()
        event_id = str(meta.get("event_id") or hashlib.sha256(payload).hexdigest())
        if not all(isinstance(container.get("test_mode", False), bool) for container in (meta, attributes)):
            raise ValueError()
        test = meta.get("test_mode", False) or attributes.get("test_mode", False)
        subscription = str(data["id"] if name in EVENTS else attributes["subscription_id"]) if name not in ORDERS else None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Malformed billing event") from None
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM billing_events WHERE event_id=?", (event_id,)).fetchone():
            return
        business = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone() if business_id else None
        if not business and subscription and not person_id:
            business = db.execute("SELECT * FROM businesses WHERE ls_subscription_id=?", (subscription,)).fetchone()
        if not business:
            return
        business_id = business["id"]
        owner = db.execute("SELECT email FROM users WHERE business_id=? AND role='owner'", (business_id,)).fetchone()
        allowed = {email.strip().lower() for email in settings.get("BILLING_TEST_MODE_ALLOWED_EMAILS").split(",") if email.strip()}
        db.execute("INSERT INTO billing_events VALUES (?,?,?,?)", (event_id, business_id, stamp, now()))
        if test and (not owner or owner[0].lower() not in allowed):
            db.execute("INSERT INTO log(business_id,event,created_at) VALUES (?,'billing_test_ignored',?)", (business_id, now()))
            return
        if person_id:
            # Customer offer payments never change the business's Evorove subscription.
            if name not in {'order_created', 'subscription_payment_success'}:
                return
            if not db.execute('SELECT 1 FROM people WHERE business_id=? AND id=?', (business_id, person_id)).fetchone():
                return
            amount = attributes.get('total')
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise HTTPException(400, 'Invalid payment amount')
            prefix = 'order:' if name == 'order_created' else 'invoice:'
            inserted = db.execute("INSERT OR IGNORE INTO payments(id,business_id,person_id,source,event_name,test_mode,amount_cents,raw_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (str(uuid4()), business_id, person_id, 'lemonsqueezy', name, int(test), amount, prefix + str(data['id']), now()))
            if inserted.rowcount:
                outreach.record_payment(business_id, person_id, 'lemonsqueezy', db=db)
            return
        if name in INVOICES:
            # Invoice events record money; subscription snapshots alone control access.
            if business["ls_subscription_id"] != subscription:
                return
            if name != "subscription_payment_failed":
                amount = attributes.get("total")
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise HTTPException(400, "Invalid payment amount")
                inserted = db.execute("INSERT OR IGNORE INTO payments(id,business_id,person_id,source,event_name,test_mode,amount_cents,raw_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                      (str(uuid4()), business_id, person_id, "lemonsqueezy", name, int(test), amount, "invoice:" + str(data["id"]), now()))
            return
        if name in ORDERS:
            amount = attributes.get("total")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise HTTPException(400, "Invalid payment amount")
            inserted = db.execute("INSERT OR IGNORE INTO payments(id,business_id,person_id,source,event_name,test_mode,amount_cents,raw_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (str(uuid4()), business_id, person_id, "lemonsqueezy", name, int(test), amount, "order:" + str(data["id"]), now()))
            return
        variants = {settings.get("LEMONSQUEEZY_VARIANT_STARTER"): "starter", settings.get("LEMONSQUEEZY_VARIANT_PRO"): "pro"}
        plan = variants.get(str(attributes.get("variant_id")))
        if not plan:
            return
        latest = db.execute("SELECT detail FROM log WHERE business_id=? AND event='billing_applied' ORDER BY id DESC LIMIT 1", (business_id,)).fetchone()
        if latest and stamp <= latest[0]:
            return
        if business["ls_subscription_id"] and business["ls_subscription_id"] != subscription:
            if name != "subscription_created" or business["subscription_status"] != "expired":
                return
        ends = utc_date(attributes.get("ends_at")) if attributes["status"] == "cancelled" else None
        db.execute("UPDATE businesses SET plan=?,subscription_status=?,ls_customer_id=?,ls_subscription_id=?,subscription_ends_at=? WHERE id=?",
                   (plan, attributes["status"], str(attributes.get("customer_id", "")), subscription,
                    ends.isoformat() if ends else None, business_id))
        db.execute("INSERT INTO log(business_id,event,detail,created_at) VALUES (?,'billing_applied',?,?)", (business_id, stamp, now()))


def can_search(business, current=None):
    return business['plan'] == 'pro' and entitled(business, current)


def require_search(business):
    if not can_search(business):
        raise HTTPException(403, 'Lead search requires an active Pro subscription')


def portal(settings, user):
    with connect(settings.db_path) as db:
        row = db.execute('SELECT ls_subscription_id FROM businesses WHERE id=?', (user['business_id'],)).fetchone()
    if not row or not row[0] or not settings.get('LEMONSQUEEZY_API_KEY'):
        raise HTTPException(503, 'Subscription management is not configured')
    try:
        from urllib.parse import quote
        response = httpx.get('https://api.lemonsqueezy.com/v1/subscriptions/' + quote(row[0], safe=''),
                             headers={'Authorization': 'Bearer ' + settings.get('LEMONSQUEEZY_API_KEY'),
                                      'Accept': 'application/vnd.api+json'}, timeout=15)
        response.raise_for_status()
        url = response.json()['data']['attributes']['urls']['customer_portal']
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.lemonsqueezy.com'):
            raise ValueError()
        return url
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Unable to open subscription management') from None
