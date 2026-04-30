"""Будує XLSX-файл (in-memory) з рядків Sheet, готовий для імпорту в OBI."""
import io
import logging
from typing import Iterable

from openpyxl import Workbook

log = logging.getLogger("xlsx_builder")


def build_xlsx(rows: list[dict], sheet_name: str = "Products") -> bytes:
    """
    `rows` — список dict-ів {column_name: value}. Перший рядок утворює header
    (порядок колонок — sorted, або з першого row якщо list-like).
    Повертає bytes XLSX-файлу.
    """
    if not rows:
        raise ValueError("No rows to write")

    # Беремо порядок колонок з першого рядка (gspread повертає row у тому ж
    # порядку, що header — стабільно).
    header = list(rows[0].keys())

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    ws.append(header)
    for row in rows:
        ws.append([_serialize(row.get(col, "")) for col in header])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log.info("Built xlsx: %d rows × %d cols", len(rows), len(header))
    return buf.getvalue()


def _serialize(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    return value
