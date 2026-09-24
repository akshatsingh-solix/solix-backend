import os
import re
import uuid
import ipaddress
import logging
import httpx
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from database import db, now_iso

logger = logging.getLogger("solix.email")

EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Solix Technologies")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO")
ALERT_TYPES = {"demo", "contact", "partner", "career", "download", "trial", "event"}

_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_CRED_ASK = ("reply with your password", "reply with the code", "send your password", "cvv",
             "send us your password", "enter your password below", "confirm your card number",
             "your full card number", "seed phrase", "recovery phrase", "verify your card",
             "social security number", "confirm your bank details")
_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def _host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _SHORTENERS)


def _same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)


class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []


def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan()
    scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks the recipient for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links/assets must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Shortened, numeric-host or credential-bearing URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _HOSTISH.finditer(text):
            if not _same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} != real link host {real!r} (G3)")


async def send_email(*, to: str, subject: str, html: str, reply_to: str | None = None) -> str | None:
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    if reply_to or EMAIL_REPLY_TO:
        payload["contact_email"] = reply_to or EMAIL_REPLY_TO
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{EMAIL_BASE_URL}/api/v1/email/send", headers={"X-Email-Key": EMAIL_KEY}, json=payload)
    resp.raise_for_status()
    return resp.json().get("id")


TYPE_LABELS = {
    "demo": "Demo request",
    "trial": "Solix ECS trial sign-up",
    "contact": "Sales inquiry",
    "partner": "Partner application",
    "career": "Job application",
    "download": "Resource download",
    "event": "Event registration",
}


def _row(label: str, value) -> str:
    if not value:
        return ""
    return (f'<tr><td style="padding:6px 12px 6px 0;color:#64748b;font-size:13px;white-space:nowrap;vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:6px 0;color:#0f172a;font-size:14px">{escape(str(value))}</td></tr>')


def lead_alert_html(sub: dict) -> str:
    label = TYPE_LABELS.get(sub["type"], sub["type"].title())
    source = "AI concierge chat" if sub.get("source") == "chat" else (sub.get("source_page") or "website")
    rows = "".join([
        _row("Name", sub.get("name")),
        _row("Email", sub.get("email")),
        _row("Company", sub.get("company")),
        _row("Title", sub.get("job_title")),
        _row("Phone", sub.get("phone")),
        _row("Interest", sub.get("interest")),
        _row("Role applied", sub.get("role")),
        _row("Resource", sub.get("resource")),
        _row("Message", sub.get("message")),
        _row("Source", source),
        _row("Received", sub.get("created_at")),
    ])
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0">'
        '<tr><td align="center"><table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="background:#ffffff;border-radius:12px;font-family:Arial,Helvetica,sans-serif;max-width:600px;width:100%">'
        '<tr><td style="background:#020817;padding:20px 28px;border-radius:12px 12px 0 0">'
        f'<span style="color:#ED2423;font-size:11px;letter-spacing:2px;text-transform:uppercase">{escape(EMAIL_FROM_NAME)} · New lead</span>'
        f'<h1 style="margin:8px 0 0;color:#ffffff;font-size:22px;font-weight:600">{escape(label)}</h1></td></tr>'
        f'<tr><td style="padding:24px 28px"><table role="presentation" cellpadding="0" cellspacing="0">{rows}</table>'
        '<p style="margin:24px 0 0;color:#64748b;font-size:12px;line-height:1.5">Open the Solix admin dashboard to view, search and export all leads. '
        f'Sent automatically by {escape(EMAIL_FROM_NAME)}. This is an internal notification; we never ask for passwords or payment details by email.</p>'
        '</td></tr></table></td></tr></table>'
    )


async def get_alert_recipient() -> str | None:
    settings = await db.settings.find_one({"key": "alerts"}, {"_id": 0})
    if settings and settings.get("alert_email"):
        return settings["alert_email"]
    return os.environ.get("SALES_ALERT_EMAIL")


async def notify_lead(sub: dict) -> None:
    if sub["type"] not in ALERT_TYPES:
        return
    record = {
        "id": str(uuid.uuid4()),
        "submission_id": sub["id"],
        "type": sub["type"],
        "lead_email": sub.get("email"),
        "created_at": now_iso(),
    }
    recipient = await get_alert_recipient()
    if not EMAIL_KEY or not recipient:
        record.update(status="skipped", detail="Email key or recipient not configured")
        await db.notifications.insert_one(dict(record))
        logger.warning("lead alert skipped: %s", record["detail"])
        return
    label = TYPE_LABELS.get(sub["type"], sub["type"].title())
    subject = f"[Solix] New {label.lower()}: {sub.get('company') or sub.get('name') or sub.get('email')}"
    try:
        email_id = await send_email(to=recipient, subject=subject, html=lead_alert_html(sub), reply_to=sub.get("email"))
        record.update(status="sent", recipient=recipient, email_id=email_id)
    except Exception as exc:
        logger.exception("lead alert failed")
        record.update(status="failed", recipient=recipient, detail=str(exc)[:300])
    await db.notifications.insert_one(dict(record))
