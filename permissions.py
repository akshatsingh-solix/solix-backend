"""MarkHub access control: who can see and change each part of the admin.

Every admin API belongs to one *feature* (Leads, Radar, Outbound, Content...). Each user has a level per
feature, from their role's template plus any per-user override:

    none  the feature is hidden and its API refuses them
    view  read only
    edit  the role's normal work in that feature (a rep works leads and accounts)
    full  everything in that feature, including what is otherwise admin-only (Radar settings, territories)

Admins always have full access everywhere, so nobody can lock the admins out. An admin can:
* edit the role templates (what every Sales, Editor, Leadership or Compliance user gets);
* give one user more or less than their role (a sales manager with full Radar, a contractor with no Leads);
* switch a whole feature off for everyone but admins;
* set each user's seniority and which dashboard they land on.

Enforcement is on the server: `auth.get_current_admin` refuses any /api/admin request below "view", and
`auth.require_roles` (the existing role checks) also requires the right level, so a role's default can be
taken away and a user can be granted more than their role. Defaults reproduce the access each role had
before, so nothing changes until an admin changes it.
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import cache

LEVELS = ["none", "view", "edit", "full"]
RANK = {lv: i for i, lv in enumerate(LEVELS)}

FEATURES: Dict[str, dict] = {
    "dashboard": {"label": "Dashboard and reports", "paths": ("reports", "stats", "notifications", "home", "intelligence")},
    "leads": {"label": "Leads", "paths": ("leads", "leads-export", "views", "scoring")},
    "radar": {"label": "Radar (accounts, signals, deep dives)", "paths": ("radar",)},
    "outbound": {"label": "Outbound (contacts, sequences, sending)", "paths": ("outbound",)},
    "compliance": {"label": "Compliance (suppression, erasure, LIAs)", "paths": ("compliance",)},
    "sol": {"label": "Sol chats and Sol control", "paths": ("sol", "chats")},
    "seo": {"label": "SEO", "paths": ("seo",)},
    "content": {"label": "Content publishing", "paths": ("content", "content-imported", "files")},
    "migrate": {"label": "Website migration", "paths": ("migrations",)},
    "website": {"label": "Website settings", "paths": ("site",)},
    "events": {"label": "Events", "paths": ("events",)},
    "inbox": {"label": "Form inbox and asset emails", "paths": ("submissions", "deliveries", "delivery")},
    "settings": {"label": "Settings, team and users", "paths": ("settings", "team", "users", "ops", "access", "internal")},
}
PATH_FEATURE = {p: f for f, spec in FEATURES.items() for p in spec["paths"]}

ROLE_LABELS = {"admin": "Admin", "sales": "Sales", "editor": "Content editor", "viewer": "Leadership (read-only)", "compliance": "Compliance"}
SENIORITY = {"executive": "Executive / leadership", "manager": "Manager", "individual": "Individual contributor"}
DASHBOARDS = {"executive": "Executive: pipeline, revenue drivers, Radar coverage", "manager": "Manager: team, territories, coverage gaps",
              "rep": "Rep: my accounts, my next actions, my leads", "marketing": "Marketing: inbound, content, SEO",
              "compliance": "Compliance: suppression, consent, sending health"}


def _template(edit: tuple, default: str = "view") -> Dict[str, str]:
    return {f: ("edit" if f in edit else default) for f in FEATURES}


# What each role could do before access control existed: everyone could read every admin area; writes went to the
# roles in each endpoint's role check.
DEFAULT_ROLES: Dict[str, Dict[str, str]] = {
    "admin": {f: "full" for f in FEATURES},
    "sales": _template(("leads", "radar", "outbound", "compliance", "events", "inbox")),
    "editor": _template(("content", "seo", "website", "migrate", "events", "sol")),
    "viewer": _template(()),
    "compliance": _template(("compliance",)),
}


def feature_for_path(path: str) -> Optional[str]:
    m = re.match(r"^/api/admin/([^/?]+)", path or "")
    return PATH_FEATURE.get(m.group(1)) if m else None


def _db():
    import database
    return database.db


async def config() -> dict:
    async def load():
        doc = await _db().settings.find_one({"key": "access"}, {"_id": 0}) or {}
        roles = {r: {**DEFAULT_ROLES[r], **{f: lv for f, lv in (doc.get("roles") or {}).get(r, {}).items() if f in FEATURES and lv in RANK}} for r in DEFAULT_ROLES}
        roles["admin"] = dict(DEFAULT_ROLES["admin"])
        return {"roles": roles, "disabled": [f for f in doc.get("disabled") or [] if f in FEATURES],
                "role_dashboards": {**{"admin": "executive", "viewer": "executive", "sales": "rep", "editor": "marketing", "compliance": "compliance"},
                                    **{r: d for r, d in (doc.get("role_dashboards") or {}).items() if d in DASHBOARDS}}}
    return await cache.memo("access", "cfg", 30, load)


def effective_from(user: dict, cfg: dict) -> Dict[str, str]:
    """The user's level per feature: role template, then their own overrides; switched-off features are none."""
    role = user.get("role")
    if role == "admin":
        return {f: "full" for f in FEATURES}
    levels = dict(cfg["roles"].get(role) or {f: "none" for f in FEATURES})
    for f, lv in (user.get("access") or {}).items():
        if f in FEATURES and lv in RANK:
            levels[f] = lv
    for f in cfg["disabled"]:
        levels[f] = "none"
    return levels


async def effective(user: dict) -> Dict[str, str]:
    return effective_from(user, await config())


def dashboard_for(user: dict, cfg: dict) -> str:
    if user.get("dashboard") in DASHBOARDS:
        return user["dashboard"]
    if user.get("role") == "sales" and user.get("seniority") in ("manager", "executive"):
        return "manager" if user["seniority"] == "manager" else "executive"
    return cfg["role_dashboards"].get(user.get("role"), "rep")


async def profile(user: dict) -> dict:
    """What the admin needs to shape itself for this user: levels, switched-off features, dashboard."""
    cfg = await config()
    return {"access": effective_from(user, cfg), "dashboard": dashboard_for(user, cfg), "seniority": user.get("seniority") or "individual",
            "disabled_features": cfg["disabled"]}


async def check_request(user: dict, request: Request) -> None:
    """Called for every signed-in /api/admin request: below "view" on the feature means no access at all."""
    feature = feature_for_path(request.url.path)
    if not feature or user.get("role") == "admin":
        return
    if RANK[(await effective(user)).get(feature, "none")] < RANK["view"]:
        raise HTTPException(status_code=403, detail=f"You don't have access to {FEATURES[feature]['label']}")


async def allowed_by_roles(user: dict, request: Request, roles: tuple) -> bool:
    """The existing role checks, with access levels on top: a role's write also needs "edit" on the feature (an admin
    can take it away), and "full" grants what the role check alone would refuse (an admin can give it)."""
    if user.get("role") == "admin":
        return True
    feature = feature_for_path(request.url.path)
    if not feature:
        return user.get("role") in roles
    level = RANK[(await effective(user)).get(feature, "none")]
    if level >= RANK["full"]:
        return True
    if user.get("role") not in roles:
        return False
    return level >= (RANK["view"] if request.method in ("GET", "HEAD") else RANK["edit"])


async def can(user: dict, feature: str, level: str) -> bool:
    return user.get("role") == "admin" or RANK[(await effective(user)).get(feature, "none")] >= RANK[level]


# --- Admin screens: Settings > Access ----------------------------------------------------------------

router = APIRouter(prefix="/api/admin/access", tags=["access"])


def _admin_dep():
    from auth import require_roles
    return Depends(require_roles("admin"))


class RolesIn(BaseModel):
    roles: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    disabled: Optional[list] = None
    role_dashboards: Optional[Dict[str, str]] = None


class UserAccessIn(BaseModel):
    access: Optional[Dict[str, Optional[str]]] = None  # feature -> level, or null to follow the role
    seniority: Optional[str] = None
    dashboard: Optional[str] = None  # "" follows the role


@router.get("", dependencies=[_admin_dep()])
async def get_access():
    cfg = await config()
    users = await _db().users.find({}, {"_id": 0, "password_hash": 0}).sort("created_at", 1).to_list(500)
    return {"features": [{"key": k, "label": v["label"]} for k, v in FEATURES.items()], "levels": LEVELS, "roles": cfg["roles"], "role_labels": ROLE_LABELS,
            "disabled": cfg["disabled"], "role_dashboards": cfg["role_dashboards"], "dashboards": DASHBOARDS, "seniority": SENIORITY,
            "users": [{**{k: u.get(k) for k in ("id", "email", "name", "role", "disabled", "seniority", "dashboard")}, "overrides": u.get("access") or {},
                       "effective": effective_from(u, cfg), "lands_on": dashboard_for(u, cfg)} for u in users]}


@router.put("", dependencies=[_admin_dep()])
async def put_access(body: RolesIn):
    from database import now_iso
    sets: dict = {"updated_at": now_iso()}
    if body.roles:
        bad = [f"{r}.{f}" for r, fs in body.roles.items() for f, lv in fs.items() if r not in DEFAULT_ROLES or f not in FEATURES or lv not in RANK]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown role, feature or level: {', '.join(bad[:5])}")
        sets["roles"] = {r: fs for r, fs in body.roles.items() if r != "admin"}
    if body.disabled is not None:
        if "settings" in body.disabled:
            raise HTTPException(status_code=422, detail="Settings can't be switched off: it's where access is managed")
        sets["disabled"] = [f for f in body.disabled if f in FEATURES]
    if body.role_dashboards is not None:
        sets["role_dashboards"] = {r: d for r, d in body.role_dashboards.items() if r in DEFAULT_ROLES and d in DASHBOARDS}
    await _db().settings.update_one({"key": "access"}, {"$set": sets}, upsert=True)
    cache.bump("access")
    return await get_access()


@router.put("/users/{user_id}", dependencies=[_admin_dep()])
async def put_user_access(user_id: str, body: UserAccessIn):
    target = await _db().users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    sets, unsets = {}, {}
    if body.access is not None:
        access = dict(target.get("access") or {})
        for f, lv in body.access.items():
            if f not in FEATURES or (lv is not None and lv not in RANK):
                raise HTTPException(status_code=422, detail=f"Unknown feature or level: {f}={lv}")
            if lv is None:
                access.pop(f, None)
            else:
                access[f] = lv
        sets["access"] = access
    if body.seniority is not None:
        if body.seniority not in SENIORITY:
            raise HTTPException(status_code=422, detail="Unknown seniority")
        sets["seniority"] = body.seniority
    if body.dashboard is not None:
        if body.dashboard == "":
            unsets["dashboard"] = ""
        elif body.dashboard in DASHBOARDS:
            sets["dashboard"] = body.dashboard
        else:
            raise HTTPException(status_code=422, detail="Unknown dashboard")
    update = {**({"$set": sets} if sets else {}), **({"$unset": unsets} if unsets else {})}
    if update:
        await _db().users.update_one({"id": user_id}, update)
        cache.bump("users")
        cache.bump("access")
    return await get_access()
