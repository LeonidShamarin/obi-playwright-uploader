"""Bearer-token middleware для FastAPI."""
from fastapi import Header, HTTPException, status

from app.settings import settings


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401 якщо Bearer-token не збігається з settings.worker_bearer_token."""
    expected = settings.worker_bearer_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WORKER_BEARER_TOKEN not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Bearer token",
        )
