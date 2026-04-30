"""
Upload-flow VTEX seller cabinet:
  Produkte → Produktimport → +Neuimport →
  Jobname → Kategorie wählen → upload xlsx → Mapping (SKU Images 3..10) →
  Nächster → polling status → if errors → download Fehlerbericht.
"""
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.settings import settings

log = logging.getLogger("browser.upload_flow")

ADMIN_BASE_URL = "https://hajus679.myvtex.com/admin"
# Правильний URL невідомий — використовуємо sidebar navigation замість прямого goto.
SKU_IMAGE_COLUMNS_TO_MAP = [f"SKU Images {i}" for i in range(3, 11)]  # 3..10
STATUS_TERMINAL = {"completed", "failed"}
STATUS_POLL_INTERVAL = 10  # seconds
STATUS_POLL_MAX_TRIES = 90  # 15 хвилин на імпорт


async def upload_xlsx_to_obi(
    page: Page, xlsx_bytes: bytes, jobname: str, category: str | None = None,
) -> dict:
    """
    Виконує повний upload-flow і повертає JSON-звіт:
      {
        "status": "completed" | "failed" | "timeout",
        "jobname": str,
        "totals": {imported, failed, skipped, total},
        "fehlerbericht_xlsx_b64": str | None,
        "screenshots": [path1, path2, ...]
      }
    """
    screenshots = []
    category = category or settings.obi_default_category

    # Залишаємось на /admin/products де sidebar Produkte вже розгорнутий
    # після успішного login redirect — там пункт "Import von Produkten" видимий.
    products_url = f"{ADMIN_BASE_URL}/products"
    log.info("Navigating to %s (Produkte sidebar розгорнуто там)", products_url)
    await page.goto(products_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        log.warning("networkidle timeout — продовжуємо")
    await page.wait_for_timeout(2000)
    screenshots.append(await _shot(page, "00_admin_products"))

    # Sidebar має посилання "Import von Produkten" (DE) — клікаємо його
    log.info("Looking for sidebar link 'Import von Produkten'...")
    sidebar_link = None
    sidebar_candidates = [
        ('text="Import von Produkten"', lambda: page.get_by_text(re.compile(r"Import\s+von\s+Produkten", re.I))),
        ('role:link "Import von Produkten"', lambda: page.get_by_role("link", name=re.compile(r"Import\s+von\s+Produkten", re.I))),
        ('text="Produktimport"',         lambda: page.get_by_text(re.compile(r"Produktimport", re.I))),
        ('text="Product Import"',        lambda: page.get_by_text(re.compile(r"Product\s+Import", re.I))),
    ]
    elapsed = 0.0
    while elapsed < 25.0:
        for label, factory in sidebar_candidates:
            loc = factory()
            try:
                count = await loc.count()
                if count:
                    first = loc.first
                    if await first.is_visible(timeout=500):
                        log.info("Sidebar link found via %s after %.1fs", label, elapsed)
                        sidebar_link = first
                        break
            except Exception:
                continue
        if sidebar_link is not None:
            break
        await asyncio.sleep(2)
        elapsed += 2

    if sidebar_link is None:
        screenshots.append(await _shot(page, "ERR_sidebar_link_not_found"))
        raise RuntimeError(
            f"Sidebar link 'Import von Produkten' not found on admin home. URL: {page.url}"
        )

    await sidebar_link.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        log.warning("networkidle after sidebar click timeout — продовжуємо")
    await page.wait_for_timeout(2000)
    log.info("After sidebar click, URL=%s", page.url)
    screenshots.append(await _shot(page, "00_produktimport_loaded"))

    # Polling-search для "Neuer Import" / "Neuimport" / "New Import" button до 25s
    new_btn = None
    new_import_re = re.compile(r"Neuer\s+Import|Neuimport|New\s+Import", re.I)
    candidate_factories = [
        # CSS-селектори з :has-text (найнадійніше для VTEX UI)
        ('css button:has-text("Neuer Import")', lambda: page.locator('button:has-text("Neuer Import")')),
        ('css a:has-text("Neuer Import")',      lambda: page.locator('a:has-text("Neuer Import")')),
        ('css [role=button]:has-text("Neuer Import")', lambda: page.locator('[role="button"]:has-text("Neuer Import")')),
        ('css button:has-text("Neuer")',        lambda: page.locator('button:has-text("Neuer")')),
        # Fallback на role-based
        ('role:button name=re Neuer Import', lambda: page.get_by_role("button", name=new_import_re)),
        ('role:link name=re Neuer Import',   lambda: page.get_by_role("link",   name=new_import_re)),
        ('text Neuer Import',                lambda: page.get_by_text(new_import_re)),
    ]
    log.info("Polling до 25s для Neuer-Import button...")
    elapsed = 0.0
    while elapsed < 25.0:
        for label, factory in candidate_factories:
            loc = factory()
            try:
                count = await loc.count()
                if count:
                    first = loc.first
                    if await first.is_visible(timeout=500):
                        log.info("Neuimport found via %s after %.1fs (matches=%d)", label, elapsed, count)
                        new_btn = first
                        break
            except Exception:
                continue
        if new_btn is not None:
            break
        await asyncio.sleep(2)
        elapsed += 2

    if new_btn is None:
        screenshots.append(await _shot(page, "ERR_neuimport_not_found"))
        raise RuntimeError(
            f"+Neuimport button not found on Produktimport page after 25s polling "
            f"(URL: {page.url}). Перевір screenshots."
        )

    try:
        await new_btn.click(timeout=15000)
    except Exception:
        screenshots.append(await _shot(page, "ERR_neuimport_click_failed"))
        raise

    await page.wait_for_load_state("networkidle", timeout=20000)
    screenshots.append(await _shot(page, "01_neuimport_open"))

    # Jobname
    jobname_input = page.locator('input[name*="job" i], input[id*="job" i]').first
    if await jobname_input.count():
        await jobname_input.fill(jobname)

    # Kategorie wählen
    cat_btn = page.get_by_role("button", name=re.compile(r"Kategorie\s*wählen", re.I))
    if await cat_btn.count():
        await cat_btn.first.click()
        # Шукаємо категорію в dropdown / search
        await asyncio.sleep(1)
        search = page.locator('input[type="search"], input[placeholder*="Suchen" i]').first
        if await search.count():
            await search.fill(category)
            await asyncio.sleep(1)
        # Вибираємо першу пропозицію
        first_option = page.locator('[role="option"], li[data-value]').first
        if await first_option.count():
            await first_option.click()
        screenshots.append(await _shot(page, "02_category_picked"))

    # Datei hochladen
    file_input = page.locator('input[type="file"]').first
    await file_input.set_input_files(
        files=[{"name": f"{jobname}.xlsx", "mimeType":
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "buffer": xlsx_bytes}]
    )
    await page.wait_for_load_state("networkidle")
    screenshots.append(await _shot(page, "03_file_uploaded"))

    # Nächster Schritt → Mapping
    next_btn = page.get_by_role("button", name=re.compile(r"Nächster|Next|Weiter", re.I))
    await next_btn.first.click()
    await page.wait_for_load_state("networkidle")
    screenshots.append(await _shot(page, "04_mapping_open"))

    # Mapping: розкрити Product Content і додати SKU Images 3..10
    await _expand_product_content(page)
    for col in SKU_IMAGE_COLUMNS_TO_MAP:
        await _add_image_mapping(page, col)
    screenshots.append(await _shot(page, "05_mapping_done"))

    # Next → запуск імпорту
    next_btn2 = page.get_by_role("button", name=re.compile(r"Nächster|Next|Weiter", re.I))
    await next_btn2.first.click()
    await page.wait_for_load_state("networkidle")
    screenshots.append(await _shot(page, "06_import_started"))

    # Polling статусу
    final_status, totals = await _poll_status(page)
    screenshots.append(await _shot(page, "07_final_status"))

    fehler_b64 = None
    if final_status == "failed" or (totals and totals.get("failed", 0) > 0):
        fehler_b64 = await _download_fehlerbericht(page)

    return {
        "status": final_status,
        "jobname": jobname,
        "category": category,
        "totals": totals,
        "fehlerbericht_xlsx_b64": fehler_b64,
        "screenshots": screenshots,
    }


async def _shot(page: Page, label: str) -> str:
    name = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{label}.png"
    path = Path(settings.screenshot_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        log.exception("Failed to take screenshot %s", name)
    return str(path)


async def _expand_product_content(page: Page) -> None:
    """Розкриває секцію Product Content на mapping-екрані."""
    section = page.get_by_text("Product Content", exact=False).first
    try:
        await section.click()
        await asyncio.sleep(0.5)
    except Exception:
        log.warning("Could not click Product Content section")


async def _add_image_mapping(page: Page, col_name: str) -> None:
    """Знаходить SKU Images dropdown, додає вказаний column."""
    sku_images_field = page.locator('text="SKU Images"').first
    try:
        # Клік по полю dropdown щоб відкрити
        await sku_images_field.click()
    except Exception:
        log.warning("SKU Images field not found")
        return

    # Шукаємо поле пошуку у відкритому dropdown
    search = page.locator('input[type="search"], input[placeholder*="search" i]').first
    if await search.count():
        await search.fill(col_name)
        await asyncio.sleep(0.7)

    option = page.locator(f'[role="option"]:has-text("{col_name}")').first
    if await option.count():
        await option.click()
        log.info("Mapped column %s", col_name)
    else:
        log.warning("Column %s not found in mapping dropdown", col_name)
    await asyncio.sleep(0.5)


async def _poll_status(page: Page) -> tuple[str, dict | None]:
    """
    Чекає, поки статус імпорту стане completed/failed.
    Повертає (status, totals_dict).
    """
    for attempt in range(STATUS_POLL_MAX_TRIES):
        try:
            status_text = (
                await page.locator(
                    'text=/preflight|new|completed|failed/i'
                ).first.text_content(timeout=2000)
            )
        except PlaywrightTimeout:
            status_text = ""
        s = (status_text or "").strip().lower()
        log.info("Import status (attempt %d/%d): %r", attempt + 1, STATUS_POLL_MAX_TRIES, s)

        if "completed" in s:
            return "completed", await _read_totals(page)
        if "failed" in s:
            return "failed", await _read_totals(page)
        await asyncio.sleep(STATUS_POLL_INTERVAL)
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            log.exception("Reload during polling failed")
    return "timeout", None


async def _read_totals(page: Page) -> dict:
    """Витягає Gesamtanzahl/Importierte/Fehlgeschlagene/Übersprungene з UI."""
    out = {}
    for label, key in (
        ("Gesamtanzahl", "total"),
        ("Importierte", "imported"),
        ("Fehlgeschlagene", "failed"),
        ("Übersprungene", "skipped"),
    ):
        try:
            el = page.locator(f'text=/{label}/i').first
            if await el.count():
                surrounding = await el.evaluate("el => el.closest('div').innerText")
                m = re.search(r"\d+", surrounding or "")
                if m:
                    out[key] = int(m.group(0))
        except Exception:
            log.exception("Failed to read %s", label)
    return out


async def _download_fehlerbericht(page: Page) -> str | None:
    """Кліком на Fehlerbericht XLSX/CSV завантажує файл і повертає base64."""
    import base64

    btn = page.get_by_role("link", name=re.compile(r"Fehlerbericht.*XLSX|Fehlerbericht", re.I))
    if not await btn.count():
        log.warning("Fehlerbericht link not found")
        return None
    async with page.expect_download() as dl_info:
        await btn.first.click()
    download = await dl_info.value
    save_path = Path(settings.download_dir) / download.suggested_filename
    save_path.parent.mkdir(parents=True, exist_ok=True)
    await download.save_as(str(save_path))
    return base64.b64encode(save_path.read_bytes()).decode("ascii")
