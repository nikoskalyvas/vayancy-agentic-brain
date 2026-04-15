"""
Auth utilities — backend/app/core/auth_utils.py

JWT creation/validation and password hashing for the owner auth layer.

Endpoints that use these:
  POST /auth/signup
  POST /auth/login
  GET  /auth/me
  POST /auth/onboarding/complete
  All /owner/* routes

Token structure:
  {
    "sub":             "<user uuid>",
    "email":           "owner@villa.gr",
    "name":            "Nikos",
    "tenant_id":       "<tenant uuid> | null",
    "tenant_api_key":  "<api key> | null",
    "exp":             <unix timestamp>
  }

tenant_id and tenant_api_key are null until onboarding is complete.
After complete they're populated and the owner can access /owner/* endpoints.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_bearer = HTTPBearer(auto_error=False)
TOKEN_EXPIRE_DAYS = 30


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ── Verification tokens ───────────────────────────────────────────────────────

def generate_verify_token() -> str:
    return secrets.token_urlsafe(32)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_jwt(
    user_id:        str,
    email:          str,
    name:           str,
    tenant_id:      str | None = None,
    tenant_api_key: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "sub":            user_id,
        "email":          email,
        "name":           name,
        "tenant_id":      tenant_id,
        "tenant_api_key": tenant_api_key,
        "exp":            datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_jwt(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=["HS256"])


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """Require valid JWT. Returns decoded payload."""
    if not creds:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return decode_jwt(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_owner(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require valid JWT AND completed onboarding (tenant_id present)."""
    if not user.get("tenant_id"):
        raise HTTPException(
            status_code=403,
            detail="Onboarding not complete. Please complete setup first.",
        )
    return user
