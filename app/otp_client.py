"""Клієнт до внутрішнього OTP-сервера на Coolify (Zero-Knowledge TOTP)."""
import logging
from urllib.parse import quote

import requests

from app.settings import settings

log = logging.getLogger("otp_client")


def get_vtex_otp(timeout: int = 15) -> str:
    """Повертає поточний 6-значний TOTP-код з OTP-сервера для VTEX."""
    url = (
        settings.otp_server_url.rstrip("/")
        + f"/otp/{quote(settings.otp_email, safe='@')}/{quote(settings.otp_provider)}"
    )
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {settings.otp_server_token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()
    if isinstance(data, dict):
        for key in ("code", "otp", "token", "value"):
            if data.get(key):
                return str(data[key]).strip()
        log.warning("OTP server returned unknown shape: %s", list(data.keys()))
        return str(data)
    return str(data).strip()
