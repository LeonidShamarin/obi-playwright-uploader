"""
Читає рядки з Google Sheet за списком Product Ref ID,
повертає dict[ref_id → row_dict] (header → value).

Обнуляє Stock-колонку перед поверненням, бо OBI імпорт стартує зі Stock=0
(реальний сток підтягне n8n Force Stock Sync через VTEX API).
"""
import json
import logging
from typing import Any

import gspread
from google.oauth2.credentials import Credentials

from app.settings import settings

log = logging.getLogger("sheet_reader")

STOCK_COLUMN = "Stock - Hauptlager\n(total)"
KEY_COLUMN = "Product Ref ID"


def _get_client(access_token: str | None = None) -> gspread.Client:
    """
    Якщо `access_token` передано — використовується напряму (Windmill сам auto-refresh-ить
    через свій gsheets OAuth resource і шле свіжий токен у кожному запиті).
    Інакше fallback на ENV GSHEETS_OAUTH_TOKEN_JSON (повний OAuth payload із refresh_token).
    """
    if access_token:
        creds = Credentials(token=access_token,
                            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return gspread.authorize(creds)

    if not settings.gsheets_oauth_token_json:
        raise RuntimeError(
            "Neither gsheets_access_token (request body) nor "
            "GSHEETS_OAUTH_TOKEN_JSON (env) configured"
        )
    raw = json.loads(settings.gsheets_oauth_token_json)
    creds = Credentials(
        token=raw.get("token") or raw.get("access_token"),
        refresh_token=raw.get("refresh_token"),
        token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=raw.get("client_id"),
        client_secret=raw.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def fetch_rows_by_ref_ids(
    ref_ids: list[str], access_token: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Повертає {ref_id: {col_name: value, ...}} для запитаних Ref ID."""
    if not ref_ids:
        return {}
    client = _get_client(access_token=access_token)
    sh = client.open_by_key(settings.gsheets_spreadsheet_id)

    # Знаходимо worksheet за gid
    target_ws = next(
        (ws for ws in sh.worksheets() if ws.id == settings.gsheets_sheet_gid),
        None,
    )
    if not target_ws:
        raise RuntimeError(f"Sheet with gid={settings.gsheets_sheet_gid} not found")

    all_values = target_ws.get_all_values()
    if not all_values:
        return {}
    header = all_values[0]
    if KEY_COLUMN not in header:
        raise RuntimeError(f"Header has no '{KEY_COLUMN}' column")
    key_idx = header.index(KEY_COLUMN)

    wanted = {str(r).strip() for r in ref_ids}
    result: dict[str, dict[str, Any]] = {}
    for row in all_values[1:]:
        if len(row) <= key_idx:
            continue
        rid = str(row[key_idx]).strip()
        if rid not in wanted:
            continue
        row_dict = {}
        for i, col in enumerate(header):
            row_dict[col] = row[i] if i < len(row) else ""
        # Гарантуємо Stock=0 у фінальному xlsx, як вирішив Леонід
        if STOCK_COLUMN in row_dict:
            row_dict[STOCK_COLUMN] = "0"
        result[rid] = row_dict

    log.info(
        "Fetched %d/%d rows from Sheet (gid=%s)",
        len(result), len(wanted), settings.gsheets_sheet_gid,
    )
    return result
