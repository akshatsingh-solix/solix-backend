import os
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request, Depends
from typing import Dict, List, Optional

from pydantic import BaseModel, EmailStr

import cache
from database import db, now_iso

logger = logging.getLogger("solix.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
ACCESS_TTL = timedelta(hours=12)
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

# Staff roles. admin: everything; sales: work leads; editor: publish content;
# viewer: read-only leads and reports (leadership); compliance: suppression,
# erasure and the LIA register for outbound.
ROLES = ("admin", "sales", "editor", "viewer", "compliance")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# bcrypt is deliberately slow (~0.3s of CPU per call, several seconds on a small
# instance). Run on the event loop it freezes every other request - site
# visitors, the admin, the chat - for that long, so request handlers use these.
async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


def create_access_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email, "type": "access", "exp": datetime.now(timezone.utc) + ACCESS_TTL}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm=JWT_ALGORITHM)


async def seed_admin() -> None:
    email = os.environ["ADMIN_EMAIL"].lower()
    password = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": email})
    if existing is None:
        await db.users.insert_one({"id": str(uuid.uuid4()), "email": email, "password_hash": await hash_password_async(password), "name": "Solix Admin", "role": "admin", "created_at": now_iso()})
        logger.info("admin user seeded")
    elif not await verify_password_async(password, existing["password_hash"]):
        await db.users.update_one({"email": email}, {"$set": {"password_hash": await hash_password_async(password)}})
        logger.info("admin password updated from env")


async def get_current_admin(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    # Cached briefly: every admin request needs the user, and on a remote database
    # that lookup is a full round trip. Any change to users clears the cache.
    sub = payload["sub"]
    user = await cache.memo("users", sub, 30, lambda: db.users.find_one({"id": sub, "role": {"$in": list(ROLES)}}, {"_id": 0, "password_hash": 0}))
    if not user or user.get("disabled"):
        raise HTTPException(status_code=401, detail="User not found")
    import permissions
    await permissions.check_request(user, request)  # no access below "view" on the feature this API belongs to
    return user


def require_roles(*roles: str):
    """Dependency: the signed-in staff user must hold one of `roles`, and the access level the admin set for the
    feature (permissions.py): a role's write needs "edit"; "full" also allows what the role alone would not."""
    async def check(request: Request, user: dict = Depends(get_current_admin)) -> dict:
        import permissions
        if not await permissions.allowed_by_roles(user, request, roles):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user
    return check


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminUser(BaseModel):
    id: str
    email: str
    name: str
    role: str
    # What the admin shapes itself with (permissions.profile): level per feature, dashboard, seniority.
    access: Optional[Dict[str, str]] = None
    dashboard: Optional[str] = None
    seniority: Optional[str] = None
    disabled_features: Optional[List[str]] = None


class LoginResponse(BaseModel):
    access_token: str
    user: AdminUser


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    email = body.email.lower()
    identifier = f"{_client_ip(request)}:{email}"
    attempt = await db.login_attempts.find_one({"identifier": identifier})
    now = datetime.now(timezone.utc)
    if attempt and attempt.get("count", 0) >= MAX_ATTEMPTS:
        locked_until = datetime.fromisoformat(attempt["last_attempt"]) + LOCKOUT
        if now < locked_until:
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")
        await db.login_attempts.delete_one({"identifier": identifier})

    user = await db.users.find_one({"email": email, "role": {"$in": list(ROLES)}})
    if not user or user.get("disabled") or not await verify_password_async(body.password, user["password_hash"]):
        await db.login_attempts.update_one({"identifier": identifier}, {"$inc": {"count": 1}, "$set": {"last_attempt": now.isoformat()}}, upsert=True)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.login_attempts.delete_one({"identifier": identifier})
    import permissions
    return LoginResponse(access_token=create_access_token(user["id"], user["email"]),
                         user=AdminUser(id=user["id"], email=user["email"], name=user["name"], role=user["role"], **await permissions.profile(user)))


@router.get("/me", response_model=AdminUser)
async def me(user: dict = Depends(get_current_admin)):
    import permissions
    return AdminUser(**{k: user.get(k) for k in ("id", "email", "name", "role")}, **await permissions.profile(user))


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/password", status_code=204)
async def change_password(body: PasswordChange, user: dict = Depends(get_current_admin)):
    full = await db.users.find_one({"id": user["id"]})
    if not full or not await verify_password_async(body.current_password, full["password_hash"]):
        raise HTTPException(status_code=400, detail="Your current password is incorrect")
    pw = body.new_password
    if len(pw) < 10 or not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        raise HTTPException(status_code=422, detail="Use at least 10 characters with letters and numbers")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": await hash_password_async(pw), "password_changed_at": now_iso()}})
    cache.bump("users")
