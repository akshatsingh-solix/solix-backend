import os
import uuid
import logging
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, EmailStr

from database import db, now_iso

logger = logging.getLogger("solix.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
ACCESS_TTL = timedelta(hours=12)
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email, "type": "access", "exp": datetime.now(timezone.utc) + ACCESS_TTL}
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm=JWT_ALGORITHM)


async def seed_admin() -> None:
    email = os.environ["ADMIN_EMAIL"].lower()
    password = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": email})
    if existing is None:
        await db.users.insert_one({"id": str(uuid.uuid4()), "email": email, "password_hash": hash_password(password), "name": "Solix Admin", "role": "admin", "created_at": now_iso()})
        logger.info("admin user seeded")
    elif not verify_password(password, existing["password_hash"]):
        await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_password(password)}})
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
    user = await db.users.find_one({"id": payload["sub"], "role": "admin"}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminUser(BaseModel):
    id: str
    email: str
    name: str
    role: str


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

    user = await db.users.find_one({"email": email, "role": "admin"})
    if not user or not verify_password(body.password, user["password_hash"]):
        await db.login_attempts.update_one({"identifier": identifier}, {"$inc": {"count": 1}, "$set": {"last_attempt": now.isoformat()}}, upsert=True)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.login_attempts.delete_one({"identifier": identifier})
    return LoginResponse(access_token=create_access_token(user["id"], user["email"]), user=AdminUser(id=user["id"], email=user["email"], name=user["name"], role=user["role"]))


@router.get("/me", response_model=AdminUser)
async def me(user: dict = Depends(get_current_admin)):
    return AdminUser(**user)
