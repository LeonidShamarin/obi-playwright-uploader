"""
FastAPI-сервер для автоматизованого xlsx-імпорту в OBI VTEX seller cabinet.

Endpoints:
  GET  /health                  — liveness probe
  POST /upload-xlsx (Bearer)    — повний flow: Sheet → xlsx → VTEX UI → status
"""
import logging
import traceback
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_bearer
from app.browser.session import vtex_browser
from app.browser.upload_flow import upload_xlsx_to_obi
from app.sheet_reader import fetch_rows_by_ref_ids
from app.settings import settings
from app.xlsx_builder import build_xlsx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("playwright_server")

app = FastAPI(title="OBI Playwright Uploader", version="0.1.0")


class UploadRequest(BaseModel):
    ref_ids: list[str] = Field(..., description="Список Product Ref ID для імпорту")
    category: str | None = Field(None, description="Кат. для OBI (default з settings)")
    jobname: str | None = Field(None, description="Кастомний jobname (default — дата)")
    gsheets_access_token: str | None = Field(
        None,
        description=(
            "Опційний свіжий Google Sheets access_token (Windmill auto-refresh-ить через "
            "свій gsheets OAuth resource). Якщо не передано — fallback на ENV."
        ),
    )


class UploadResponse(BaseModel):
    status: str
    jobname: str
    category: str | None
    rows_fetched: int
    totals: dict[str, Any] | None = None
    fehlerbericht_xlsx_b64: str | None = None
    screenshots: list[str] = []
    error: str | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "obi-playwright-uploader", "version": "0.1.0"}


@app.get("/screenshots", dependencies=[Depends(require_bearer)])
def list_screenshots() -> dict:
    """Список доступних скріншотів."""
    from pathlib import Path
    p = Path(settings.screenshot_dir)
    if not p.exists():
        return {"files": []}
    files = sorted(
        [f.name for f in p.iterdir() if f.is_file()],
        reverse=True,
    )
    return {"files": files, "dir": str(p)}


@app.get("/screenshot/{filename}", dependencies=[Depends(require_bearer)])
def get_screenshot(filename: str):
    """Віддає screenshot як image/png. Bearer-protected."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    # Захист від path traversal
    if "/" in filename or ".." in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(settings.screenshot_dir) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="image/png", filename=filename)


@app.post("/upload-xlsx", response_model=UploadResponse, dependencies=[Depends(require_bearer)])
async def upload_xlsx(req: UploadRequest) -> UploadResponse:
    """Прокидує товари за списком Ref ID у OBI через UI-імпорт."""
    jobname = req.jobname or f"OBI Auto - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    log.info("upload-xlsx: ref_ids=%d category=%r jobname=%r",
             len(req.ref_ids), req.category, jobname)

    try:
        rows_dict = fetch_rows_by_ref_ids(req.ref_ids, access_token=req.gsheets_access_token)
        if not rows_dict:
            return UploadResponse(
                status="no_rows",
                jobname=jobname, category=req.category,
                rows_fetched=0,
                error=f"None of the {len(req.ref_ids)} ref_ids found in Sheet",
            )

        # Зберігаємо порядок як у запиті, де є дані
        ordered_rows = [rows_dict[r] for r in req.ref_ids if r in rows_dict]
        xlsx_bytes = build_xlsx(ordered_rows)

        async with vtex_browser() as (page, _ctx):
            report = await upload_xlsx_to_obi(
                page, xlsx_bytes,
                jobname=jobname,
                category=req.category,
            )

        return UploadResponse(
            status=report["status"],
            jobname=report["jobname"],
            category=report.get("category"),
            rows_fetched=len(ordered_rows),
            totals=report.get("totals"),
            fehlerbericht_xlsx_b64=report.get("fehlerbericht_xlsx_b64"),
            screenshots=report.get("screenshots") or [],
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upload-xlsx failed")
        return UploadResponse(
            status="error",
            jobname=jobname,
            category=req.category,
            rows_fetched=0,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}",
        )
