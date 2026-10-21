import os
import uuid
import asyncio
import logging
from typing import List, Optional, Literal

from fastapi import FastAPI, APIRouter, Depends, Query, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from database import db, client, now_iso
from emailer import notify_lead
from auth import router as auth_router, seed_admin, get_current_admin
from admin import router as admin_router
from chat import router as chat_router
from press import router as press_router
from accounts import router as accounts_router
from intent import router as intent_router, record_submission, rescore_all
from leads_admin import router as leads_admin_router
from content import public as content_router, admin as content_admin_router
from events import public as events_router, admin as events_admin_router
from delivery import public as delivery_router, admin as delivery_admin_router, api_base, deliver_for_submission
from migrate import router as migrate_router, resume_interrupted
from site_settings import public as site_router, admin as site_admin_router
from seo import router as seo_router
from radar import router as radar_router
from outbound import router as outbound_router, webhooks as outbound_webhooks
from compliance import router as compliance_router, public as outbound_public
import ops

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("solix")

app = FastAPI(title="Solix Technologies API")
api_router = APIRouter(prefix="/api")

SubmissionType = Literal["demo", "contact", "newsletter", "career", "partner", "download", "trial", "event", "chat"]


class SubmissionCreate(BaseModel):
    type: SubmissionType
    email: EmailStr
    name: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    job_title: Optional[str] = None
    interest: Optional[str] = None
    message: Optional[str] = None
    role: Optional[str] = None
    resource: Optional[str] = None
    # Slug of the gated asset, so the download email can carry the right file.
    resource_slug: Optional[str] = Field(default=None, max_length=120)
    source_page: Optional[str] = None
    country: Optional[str] = Field(default=None, max_length=80)
    company_size: Optional[str] = Field(default=None, max_length=40)
    # Links the form to the visitor's tracked browsing (only sent after analytics consent).
    visitor_id: Optional[str] = Field(default=None, max_length=64)
    topics: Optional[List[str]] = Field(default=None, max_length=10)


class Submission(SubmissionCreate):
    model_config = ConfigDict(extra="ignore")
    id: str
    created_at: str
    source: Optional[str] = "web"


@api_router.get("/")
async def root():
    return {"service": "solix-api", "status": "ok"}


@api_router.post("/submissions", response_model=Submission, status_code=201)
async def create_submission(payload: SubmissionCreate, request: Request):
    doc = payload.model_dump(exclude={"visitor_id", "topics"})
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_iso()
    doc["source"] = "web"
    await db.submissions.insert_one(dict(doc))
    asyncio.create_task(notify_lead(doc))
    asyncio.create_task(deliver_for_submission(doc, api_base(request)))
    try:
        await record_submission(doc, visitor_id=payload.visitor_id, topics=payload.topics)
    except Exception:  # scoring must never lose a lead
        logger.exception("lead scoring failed for submission %s", doc["id"])
    logger.info("submission %s from %s", doc["type"], doc["email"])
    return Submission(**doc)


# Staff only: these are people's names, emails and phone numbers. The admin uses
# /api/admin/submissions; this older list stays for scripts that hold a token.
@api_router.get("/submissions", response_model=List[Submission], dependencies=[Depends(get_current_admin)])
async def list_submissions(type: Optional[SubmissionType] = Query(default=None), limit: int = Query(default=100, le=500)):
    query = {"type": type} if type else {}
    docs = await db.submissions.find(query, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [Submission(**d) for d in docs]


app.include_router(api_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(press_router)
app.include_router(accounts_router)
app.include_router(intent_router)
app.include_router(leads_admin_router)
app.include_router(content_router)
app.include_router(content_admin_router)
app.include_router(events_router)
app.include_router(events_admin_router)
app.include_router(delivery_router)
app.include_router(delivery_admin_router)
app.include_router(migrate_router)
app.include_router(site_router)
app.include_router(site_admin_router)
app.include_router(seo_router)
app.include_router(radar_router)
app.include_router(outbound_router)
app.include_router(outbound_webhooks)
app.include_router(outbound_public)
app.include_router(compliance_router)
app.include_router(ops.router)
app.include_router(ops.public)

# Compress JSON/CSV responses; tiny responses aren't worth the CPU.
app.add_middleware(GZipMiddleware, minimum_size=800)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    # Index creation is idempotent; running it concurrently turns ~40 round trips
    # to the database into about one, so a cold start answers sooner.
    await asyncio.gather(
        db.submissions.create_index("created_at"),
        db.submissions.create_index("type"),
        db.chat_messages.create_index([("session_id", 1), ("created_at", 1)]),
        db.users.create_index("email", unique=True),
        db.accounts.create_index("email", unique=True),
        db.accounts.create_index("id", unique=True),
        db.login_attempts.create_index("identifier"),
        db.notifications.create_index("created_at"),
        *(db.leads.create_index(field, unique=unique) for field, unique in (("id", True), ("email", True), ("created_at", False), ("mql_at", False), ("last_activity_at", False), ("score", False), ("primary_line", False), ("stage", False))),
        db.visitors.create_index("id", unique=True),
        db.visitors.create_index("last_seen"),
        db.events.create_index([("visitor_id", 1), ("at", -1)]),
        db.events.create_index([("type", 1), ("at", -1)]),
        # Raw events expire after 180 days to keep the free database tier small.
        db.events.create_index("at_dt", expireAfterSeconds=180 * 86400),
        db.content.create_index("id", unique=True),
        db.content.create_index("slug", unique=True),
        db.content.create_index([("status", 1), ("publish_at", -1)]),
        db.content_versions.create_index([("content_id", 1), ("version", -1)]),
        db.files.create_index("id", unique=True),
        db.files.create_index("sha256"),
        db.event_configs.create_index("slug", unique=True),
        db.event_registrations.create_index("id", unique=True),
        db.event_registrations.create_index([("event", 1), ("code", 1)], unique=True),
        db.event_registrations.create_index([("event", 1), ("email", 1)]),
        db.event_registrations.create_index([("event", 1), ("created_at", -1)]),
        db.content.create_index("source_url", sparse=True),
        db.content.create_index("origin"),
        db.content.create_index([("status", 1), ("updated_at", -1)]),
        db.content.create_index([("updated_at", -1)]),
        db.deliveries.create_index("created_at"),
        db.deliveries.create_index([("email", 1), ("slug", 1), ("created_at", -1)]),
        db.migration_jobs.create_index("id", unique=True),
        db.migration_jobs.create_index("created_at"),
        db.seo_snapshots.create_index("geo", unique=True),
        db.seo_cache.create_index([("kind", 1), ("key", 1)], unique=True),
        db.seo_ai_runs.create_index("ran_at"),
        # Radar + EchoLocate.
        db.leads.create_index("company_id", sparse=True),
        db.companies.create_index("id", unique=True),
        db.companies.create_index("key"),
        db.companies.create_index("alias_keys"),
        db.companies.create_index("domains"),
        db.companies.create_index([("owner", 1), ("strength", 1)]),
        db.companies.create_index([("score", -1)]),
        db.company_signals.create_index("dedupe_key", unique=True),
        db.company_signals.create_index([("company_id", 1), ("posted_at", -1)]),
        db.company_signals.create_index([("type", 1), ("first_seen_at", -1)]),
        db.radar_research.create_index("id", unique=True),
        db.radar_research.create_index([("company_id", 1), ("created_at", -1)]),
        db.radar_research.create_index([("status", 1), ("created_at", 1)]),
        db.radar_seen.create_index([("user_id", 1), ("company_id", 1)], unique=True),
        db.contacts.create_index("id", unique=True),
        db.contacts.create_index("email", sparse=True),
        db.contacts.create_index("company_id"),
        db.sequences.create_index("id", unique=True),
        db.enrollments.create_index("id", unique=True),
        db.enrollments.create_index([("status", 1), ("next_send_at", 1)]),
        db.enrollments.create_index("contact_id"),
        db.sends.create_index([("sent_at", -1)]),
        db.sends.create_index("contact_id"),
        db.sends.create_index([("sequence_id", 1), ("variant", 1)]),
        db.reply_log.create_index([("received_at", -1)]),
        db.reply_log.create_index("contact_id"),
        db.suppression.create_index("email_hash", unique=True),
        db.suppression.create_index("domain", sparse=True),
        db.ops_runs.create_index([("job", 1), ("started_at", -1)]),
        db.ops_outbox.create_index([("status", 1), ("next_attempt_at", 1)]),
        db.ops_outbox.create_index("key", sparse=True),
        db.ops_alerts.create_index([("code", 1), ("created_at", -1)]),
        db.webhook_events.create_index("received_at"),
    )
    await seed_admin()
    await resume_interrupted()
    asyncio.create_task(_rescore_loop())
    # Outbox, outbound sender and watchdog (Radar + EchoLocate); OPS_LOOP=0 turns it off.
    ops.start_background()


async def _rescore_loop():
    """Apply score decay to active leads every 6 hours."""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await rescore_all()
        except Exception:
            logger.exception("periodic rescore failed")
        try:
            import radar_service  # decay moves Radar account scores too
            await radar_service.recompute_all()
        except Exception:
            logger.exception("periodic radar recompute failed")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
