import os
import uuid
import asyncio
import logging
from typing import List, Optional, Literal

from fastapi import FastAPI, APIRouter, Query
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr

from database import db, client, now_iso
from emailer import notify_lead
from auth import router as auth_router, seed_admin
from admin import router as admin_router
from chat import router as chat_router
from press import router as press_router
from accounts import router as accounts_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("solix")

app = FastAPI(title="Solix Technologies API")
api_router = APIRouter(prefix="/api")

SubmissionType = Literal["demo", "contact", "newsletter", "career", "partner", "download", "trial"]


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
    source_page: Optional[str] = None


class Submission(SubmissionCreate):
    model_config = ConfigDict(extra="ignore")
    id: str
    created_at: str
    source: Optional[str] = "web"


@api_router.get("/")
async def root():
    return {"service": "solix-api", "status": "ok"}


@api_router.post("/submissions", response_model=Submission, status_code=201)
async def create_submission(payload: SubmissionCreate):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_iso()
    doc["source"] = "web"
    await db.submissions.insert_one(dict(doc))
    asyncio.create_task(notify_lead(doc))
    logger.info("submission %s from %s", doc["type"], doc["email"])
    return Submission(**doc)


@api_router.get("/submissions", response_model=List[Submission])
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

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await db.submissions.create_index("created_at")
    await db.submissions.create_index("type")
    await db.chat_messages.create_index([("session_id", 1), ("created_at", 1)])
    await db.users.create_index("email", unique=True)
    await db.accounts.create_index("email", unique=True)
    await db.accounts.create_index("id", unique=True)
    await db.login_attempts.create_index("identifier")
    await db.notifications.create_index("created_at")
    await seed_admin()


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
