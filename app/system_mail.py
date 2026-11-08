"""System mail sent through SYSTEM_SMTP_*. One layout, plain text and HTML."""
from email.message import EmailMessage
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import ROOT

SUPPORT_EMAIL = "support@evorove.com"
SITE_URL = "https://evorove.com"
BANNER = "FIND · CLOSE · NEXT"
PRODUCT_LINE = "Evorove writes the first email and keeps the thread on one board."

_ENV = Environment(
    loader=FileSystemLoader(ROOT / "app" / "templates" / "email"),
    autoescape=select_autoescape(enabled_extensions=("html",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_email(template, **context):
    data = {
        "support_email": SUPPORT_EMAIL,
        "site_url": SITE_URL,
        "banner": BANNER,
        "product_line": PRODUCT_LINE,
        **context,
    }
    plain = _ENV.get_template(f"{template}.txt").render(**data).strip() + "\n"
    html = _ENV.get_template(f"{template}.html").render(**data)
    return plain, html


def build_message(settings, recipient, subject, plain, html):
    message = EmailMessage()
    message["From"] = settings.get("SYSTEM_SMTP_FROM", "dev@localhost")
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return message


def reset_message(settings, recipient, token, *, expires_minutes):
    action_url = f"{settings.base_url}/reset?token={quote(token, safe='')}"
    plain, html = render_email(
        "reset",
        action_url=action_url,
        expires_minutes=expires_minutes,
        operator_name=settings.get("OPERATOR_NAME").strip(),
    )
    return build_message(settings, recipient, "Reset your Evorove password", plain, html)
