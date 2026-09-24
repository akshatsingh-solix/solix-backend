"""Customer accounts for the Solix ECS 30-day trial: sign up, sign in, profile.

Kept deliberately separate from the admin users in auth.py - accounts live
in their own collection and carry a different JWT `type`, so a customer
token can never pass the admin guard (and vice versa).
"""
import os
import re
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from auth import hash_password, verify_password, JWT_ALGORITHM, MAX_ATTEMPTS, LOCKOUT, _client_ip
from database import db, now_iso
from emailer import notify_lead
from intent import record_submission

logger = logging.getLogger("solix.accounts")
router = APIRouter(prefix="/api/accounts", tags=["accounts"])

TOKEN_TYPE = "account"
ACCOUNT_TTL = timedelta(days=7)
TRIAL_DAYS = 30
# Mirrors the rule shown on the sign-up form: 6-30 chars with a letter, a
# digit and a symbol.
PASSWORD_RX = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[^A-Za-z0-9]).{6,30}$")
LANGUAGES = {"en", "es", "fr", "de"}


class SignupRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    company: str = Field(min_length=1, max_length=160)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=40)
    password: str
    job_title: Optional[str] = Field(default=None, max_length=120)
    company_size: Optional[str] = Field(default=None, max_length=40)
    country: Optional[str] = Field(default=None, max_length=80)
    use_case: Optional[str] = Field(default=None, max_length=160)
    marketing_opt_in: bool = False
    language: str = "en"
    # Links the sign-up to tracked browsing (only sent after analytics consent).
    visitor_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("first_name", "last_name", "company")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("password")
    @classmethod
    def _strong(cls, v: str) -> str:
        if not PASSWORD_RX.match(v):
            raise ValueError("Password must be 6-30 characters and include a letter, a number and a symbol.")
        return v

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        return v if v in LANGUAGES else "en"


class SigninRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ForgotRequest(BaseModel):
    email: EmailStr


class Account(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    company: str
    phone: Optional[str] = None
    job_title: Optional[str] = None
    company_size: Optional[str] = None
    country: Optional[str] = None
    use_case: Optional[str] = None
    language: str = "en"
    plan: str = "trial"
    trial_ends_at: str
    created_at: str


class AuthResponse(BaseModel):
    access_token: str
    account: Account


def _token(account_id: str, email: str) -> str:
    payload = {"sub": account_id, "email": email, "type": TOKEN_TYPE, "exp": datetime.now(timezone.utc) + ACCOUNT_TTL}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm=JWT_ALGORITHM)


def _public(doc: dict) -> Account:
    return Account(**{k: v for k, v in doc.items() if k in Account.model_fields})


async def get_current_account(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in")
    try:
        payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")
    if payload.get("type") != TOKEN_TYPE:
        raise HTTPException(status_code=401, detail="Invalid session")
    account = await db.accounts.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not account:
        raise HTTPException(status_code=401, detail="Account not found")
    return account


@router.post("/signup", response_model=AuthResponse, status_code=201)
async def signup(body: SignupRequest):
    email = body.email.lower()
    if await db.accounts.find_one({"email": email}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in instead.")

    now = datetime.now(timezone.utc)
    doc = body.model_dump(exclude={"password", "visitor_id"})
    doc.update(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password(body.password),
        plan="trial",
        trial_ends_at=(now + timedelta(days=TRIAL_DAYS)).isoformat(),
        created_at=now.isoformat(),
    )
    await db.accounts.insert_one(dict(doc))

    # Every trial is also a sales lead: it lands in the admin dashboard and
    # triggers the same alert email as a demo request.
    details = [f"{label}: {doc[key]}" for key, label in (("company_size", "Company size"), ("country", "Country"), ("use_case", "Use case")) if doc.get(key)]
    lead = {
        "id": str(uuid.uuid4()),
        "type": "trial",
        "email": email,
        "name": f"{doc['first_name']} {doc['last_name']}",
        "company": doc["company"],
        "phone": doc.get("phone"),
        "job_title": doc.get("job_title"),
        "interest": "Solix ECS 30-day trial",
        "message": " · ".join(details) or None,
        "source": "signup",
        "source_page": "/signup",
        "created_at": now_iso(),
    }
    await db.submissions.insert_one(dict(lead))
    asyncio.create_task(notify_lead(lead))
    try:
        await record_submission(lead, visitor_id=body.visitor_id, topics=["enterprise-content-services"], extra={"country": doc.get("country"), "company_size": doc.get("company_size"), "language": doc.get("language")})
    except Exception:
        logger.exception("lead scoring failed for trial sign-up %s", doc["id"])
    logger.info("trial account created %s", doc["id"])
    return AuthResponse(access_token=_token(doc["id"], email), account=_public(doc))


@router.post("/signin", response_model=AuthResponse)
async def signin(body: SigninRequest, request: Request):
    email = body.email.lower()
    identifier = f"acct:{_client_ip(request)}:{email}"
    now = datetime.now(timezone.utc)
    attempt = await db.login_attempts.find_one({"identifier": identifier})
    if attempt and attempt.get("count", 0) >= MAX_ATTEMPTS:
        if now < datetime.fromisoformat(attempt["last_attempt"]) + LOCKOUT:
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
        await db.login_attempts.delete_one({"identifier": identifier})

    account = await db.accounts.find_one({"email": email})
    if not account or not verify_password(body.password, account["password_hash"]):
        await db.login_attempts.update_one({"identifier": identifier}, {"$inc": {"count": 1}, "$set": {"last_attempt": now.isoformat()}}, upsert=True)
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    await db.login_attempts.delete_one({"identifier": identifier})
    await db.accounts.update_one({"id": account["id"]}, {"$set": {"last_signin_at": now.isoformat()}})
    return AuthResponse(access_token=_token(account["id"], email), account=_public(account))


@router.get("/me", response_model=Account)
async def me(account: dict = Depends(get_current_account)):
    return _public(account)


@router.post("/forgot-password", status_code=202)
async def forgot_password(body: ForgotRequest):
    """Always answers the same way so the endpoint can't be used to find out
    which emails have accounts. A real request is routed to the Solix team
    through the leads dashboard and alert email."""
    email = body.email.lower()
    account = await db.accounts.find_one({"email": email}, {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "company": 1})
    if account:
        lead = {
            "id": str(uuid.uuid4()),
            "type": "contact",
            "email": email,
            "name": f"{account['first_name']} {account['last_name']}",
            "company": account.get("company"),
            "message": "Password reset requested for a Solix ECS trial account.",
            "source": "account",
            "source_page": "/signin",
            "created_at": now_iso(),
        }
        await db.submissions.insert_one(dict(lead))
        asyncio.create_task(notify_lead(lead))
    return {"ok": True}
