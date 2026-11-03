import hashlib
import hmac
import json
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from .db import connect, now

EVENTS = {"subscription_created", "subscription_updated", "subscription_cancelled",
          "subscription_resumed", "subscription_expired", "subscription_paused", "subscription_unpaused"}
INVOICES = {"subscription_payment_success", "subscription_payment_recovered", "subscription_payment_failed"}
STATUSES = {"on_trial", "active", "paused", "past_due", "unpaid", "cancelled", "expired"}


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
        if name not in EVENTS | INVOICES:
            return
        attributes = data["attributes"]
        if name in EVENTS:
            if data["type"] != "subscriptions" or attributes["status"] not in STATUSES:
                raise ValueError()
        elif data["type"] != "subscription-invoices":
            raise ValueError()
        stamp = datetime.fromisoformat(attributes["updated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        stamp = stamp.astimezone(timezone.utc).isoformat()
        business_id = (meta.get("custom_data") or {}).get("business_id")
        event_id = str(meta.get("event_id") or hashlib.sha256(payload).hexdigest())
        if not all(isinstance(container.get("test_mode", False), bool) for container in (meta, attributes)):
            raise ValueError()
        test = meta.get("test_mode", False) or attributes.get("test_mode", False)
        subscription = str(data["id"] if name in EVENTS else attributes["subscription_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Malformed billing event") from None
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM billing_events WHERE event_id=?", (event_id,)).fetchone():
            return
        business = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone() if business_id else None
        if not business:
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
        if name in INVOICES:
            # Invoice events record money; subscription snapshots alone control access.
            if business["ls_subscription_id"] != subscription:
                return
            if name != "subscription_payment_failed":
                amount = attributes.get("total")
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise HTTPException(400, "Invalid payment amount")
                db.execute("INSERT OR IGNORE INTO payments(id,business_id,source,event_name,test_mode,amount_cents,raw_event_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (str(uuid4()), business_id, "lemonsqueezy", name, int(test), amount, "invoice:" + str(data["id"]), now()))
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
        db.execute("UPDATE businesses SET plan=?,subscription_status=?,ls_customer_id=?,ls_subscription_id=? WHERE id=?",
                   (plan, attributes["status"], str(attributes.get("customer_id", "")), subscription, business_id))
        db.execute("INSERT INTO log(business_id,event,detail,created_at) VALUES (?,'billing_applied',?,?)", (business_id, stamp, now()))

