"""
Upload-flow VTEX seller cabinet — iframe-aware версія.

VTEX seller-product-importer рендериться у IO iframe (`<iframe src=".../admin/app">`).
Всі content actions (forms, buttons, dropdowns) — всередині цього frame.
Тому використовуємо `page.frames` + JS-evaluate для більшості кроків.

Послідовність:
  1. goto /admin/products → click sidebar "Import von Produkten" (main frame)
  2. Знайти app frame (url містить /admin/app) → JS-click "Neuer Import"
  3. У формі new-import: fill Jobname, pick Kategorie, upload xlsx
  4. Mapping: додати SKU Images 3..10
  5. Polling status, download Fehlerbericht якщо помилки.
"""
import asyncio
import base64
import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, Frame, TimeoutError as PlaywrightTimeout

from app.settings import settings

log = logging.getLogger("browser.upload_flow")

ADMIN_BASE_URL = "https://hajus679.myvtex.com/admin"
APP_FRAME_MARKER = "/admin/app"  # iframe src
SKU_IMAGE_COLUMNS_TO_MAP = [f"SKU Images {i}" for i in range(3, 11)]
STATUS_POLL_INTERVAL = 10
STATUS_POLL_MAX_TRIES = 90  # 15 хв


async def upload_xlsx_to_obi(
    page: Page, xlsx_bytes: bytes, jobname: str, category: str | None = None,
) -> dict:
    screenshots: list[str] = []
    category = category or settings.obi_default_category

    # ── 1. Navigate to products + sidebar click ─────────────────────────────
    await page.goto(f"{ADMIN_BASE_URL}/products", wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        log.warning("networkidle timeout on /admin/products")
    await page.wait_for_timeout(2000)
    screenshots.append(await _shot(page, "00_admin_products"))

    sidebar_link = await _find_in_main(
        page,
        candidates=[
            lambda: page.get_by_text(re.compile(r"Import\s+von\s+Produkten", re.I)),
            lambda: page.get_by_role("link", name=re.compile(r"Import\s+von\s+Produkten", re.I)),
        ],
        label="sidebar 'Import von Produkten'",
        timeout_s=15,
    )
    if not sidebar_link:
        screenshots.append(await _shot(page, "ERR_sidebar_not_found"))
        raise RuntimeError("Sidebar link 'Import von Produkten' not found")

    await sidebar_link.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        log.warning("networkidle timeout after sidebar click")
    await page.wait_for_timeout(2000)
    log.info("After sidebar click URL=%s", page.url)
    screenshots.append(await _shot(page, "01_product_imports_list"))

    # ── 2. Find app frame з seller-product-importer URL ──────────────────────
    app_frame = await _wait_for_app_frame(
        page, timeout_s=25, url_must_contain="seller-product-importer"
    )
    log.info("App frame ready: %s", app_frame.url)

    # ── 3. Click "Neuer Import" — пробуємо в усіх frames ─────────────────────
    target_frame, clicked = None, None
    for attempt in range(15):
        target_frame, clicked = await _try_click_in_any_frame(page, "neuer import")
        if clicked:
            break
        await asyncio.sleep(2)
    if not clicked:
        screenshots.append(await _shot(page, "ERR_neuimport_no_frame"))
        # Diagnostic
        all_frames = [(f.name, f.url) for f in page.frames]
        raise RuntimeError(
            f"Could not click 'Neuer Import' in any frame. Frames: {all_frames}"
        )
    log.info("Clicked 'Neuer Import' in frame %s: %s", clicked.get("frame_url"), clicked)
    # Дамо більше часу — VTEX іноді повільно піднімає new-import iframe.
    await page.wait_for_timeout(5000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    # Чекаємо до 25s на frame з new-import
    try:
        app_frame = await _wait_for_app_frame(page, timeout_s=25, url_must_contain="new-import")
        log.info("Found new-import sub-frame: %s", app_frame.url)
    except RuntimeError:
        app_frame = target_frame
        log.info("Modal stays in same frame, using target_frame: %s", app_frame.url)
    screenshots.append(await _shot(page, "02_neuimport_form"))

    # ── 4. Fill Jobname ─────────────────────────────────────────────────────
    await _frame_set_input_value(app_frame, "jobname", jobname,
                                 placeholder_pat=r"Jobname|Importauftrag|job")
    screenshots.append(await _shot(page, "03_jobname_filled"))

    # ── 5. Kategorie wählen → modal з radio-списком + Confirm ───────────────
    cat_clicked = await _frame_click_by_text(app_frame, "kategorie wählen")
    if not cat_clicked:
        cat_clicked = await _frame_click_by_text(app_frame, "kategorie")
    if cat_clicked:
        log.info("Clicked Kategorie wählen: %s", cat_clicked)
        await page.wait_for_timeout(2500)
        screenshots.append(await _shot(page, "04_category_dropdown_open"))

        # Click LABEL/row containing the first radio — VTEX renders radios as
        # custom UI, тому input.click() не реєструється у React. Натомість
        # клікаємо контейнер (label/listrow), який слухає user input event.
        try:
            # Спочатку пробуємо через Playwright locator з force=True (обхід
            # перевірки видимості, бо input може бути hidden під custom UI).
            radio = app_frame.locator('input[type="radio"]').first
            await radio.click(force=True, timeout=5000)
            log.info("Radio clicked via Playwright locator (force=True)")
        except Exception as e:
            log.warning("Radio click via locator failed: %s — fallback to JS label-click", e)

        # Fallback/duplicate: клік по батьківському label (емулює user click)
        picked = await app_frame.evaluate(
            """
            () => {
                const dialog = document.querySelector('[role="dialog"], .vtex-modal, [class*="modal" i]');
                const scope = dialog || document;
                const radio = scope.querySelector('input[type="radio"]');
                if (!radio) return {error: 'no_radio'};
                // VTEX wrapу радіо у label або div-row. Клік по label
                // правильно тригерить React-handlers.
                let target = radio.closest('label');
                if (!target) {
                    // fallback — найближчий клікабельний контейнер
                    target = radio.closest('[class*="row" i], [class*="item" i], div');
                }
                if (target) {
                    target.click();
                    return {clicked_via: target.tagName, class: target.className?.toString().slice(0,80)};
                }
                radio.click();
                return {clicked_via: 'INPUT'};
            }
            """
        )
        log.info("Category label/radio clicked: %s", picked)

        # Чекаємо поки Confirm стане enabled (до 6 сек)
        for tick in range(12):
            await page.wait_for_timeout(500)
            confirm_state = await app_frame.evaluate(
                """
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    const found = btns.find(b => /^\\s*confirm\\s*$|bestätigen|^\\s*ok\\s*$/i.test(b.innerText || ''));
                    if (!found) return {found: false};
                    return {found: true, disabled: found.disabled || found.getAttribute('aria-disabled') === 'true'};
                }
                """
            )
            if confirm_state.get("found") and not confirm_state.get("disabled"):
                break
            log.info("Waiting for Confirm to enable (tick %d): %s", tick, confirm_state)

        confirmed = await app_frame.evaluate(
            """
            () => {
                const btns = [...document.querySelectorAll('button')];
                const found = btns.find(b => /^\\s*confirm\\s*$|bestätigen|^\\s*ok\\s*$/i.test(b.innerText || ''));
                if (!found) return {error: 'no_confirm', buttons: btns.map(b => (b.innerText || '').trim()).filter(Boolean).slice(0,20)};
                if (found.disabled) return {error: 'disabled', text: found.innerText.trim()};
                found.click();
                return {clicked: found.innerText.trim()};
            }
            """
        )
        log.info("Confirm clicked: %s", confirmed)
        if confirmed.get("error"):
            screenshots.append(await _shot(page, "ERR_confirm_disabled"))
            raise RuntimeError(f"Could not confirm category: {confirmed}")
        await page.wait_for_timeout(2000)
        screenshots.append(await _shot(page, "05_category_picked"))
    else:
        log.warning("Kategorie wählen button not found")
        screenshots.append(await _shot(page, "WARN_no_kategorie_btn"))

    # ── 6. Upload xlsx ──────────────────────────────────────────────────────
    file_input = await _frame_find_file_input(app_frame, timeout_s=15)
    if not file_input:
        screenshots.append(await _shot(page, "ERR_no_file_input"))
        raise RuntimeError("File input not found in form")
    await file_input.set_input_files(
        files=[{
            "name": f"{jobname}.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": xlsx_bytes,
        }]
    )
    log.info("File uploaded (%d bytes)", len(xlsx_bytes))
    await page.wait_for_timeout(3000)
    screenshots.append(await _shot(page, "06_file_uploaded"))

    # ── 7. Click "Nächster Schritt" → mapping page ─────────────────────────
    next_clicked = await _frame_click_by_text(app_frame, "weiter")
    if not next_clicked:
        next_clicked = await _frame_click_by_text(app_frame, "nächster")
    if not next_clicked:
        next_clicked = await _frame_click_by_text(app_frame, "next")
    if not next_clicked:
        screenshots.append(await _shot(page, "ERR_no_next_after_upload"))
        raise RuntimeError("Could not click Nächster after file upload")
    log.info("Clicked Nächster: %s", next_clicked)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)
    app_frame = await _wait_for_app_frame(page, timeout_s=10)
    screenshots.append(await _shot(page, "07_mapping_page"))

    # ── 8. Mapping: add SKU Images 3..10 ────────────────────────────────────
    log.info("Mapping: expand Product Content + add %d image columns", len(SKU_IMAGE_COLUMNS_TO_MAP))
    # Розгортаємо Product Content
    await _frame_click_by_text(app_frame, "product content")
    await page.wait_for_timeout(1000)
    screenshots.append(await _shot(page, "08_product_content_open"))

    for col in SKU_IMAGE_COLUMNS_TO_MAP:
        await _frame_add_sku_image_mapping(app_frame, col)
    screenshots.append(await _shot(page, "09_mapping_done"))

    # ── 9. Click Next → start import ───────────────────────────────────────
    next2 = await _frame_click_by_text(app_frame, "nächster")
    if not next2:
        next2 = await _frame_click_by_text(app_frame, "next")
    if next2:
        log.info("Clicked Nächster (start import): %s", next2)
    else:
        log.warning("Next button after mapping not found — продовжуємо до polling")
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)
    app_frame = await _wait_for_app_frame(page, timeout_s=10)
    screenshots.append(await _shot(page, "10_import_started"))

    # ── 10. Polling status ──────────────────────────────────────────────────
    final_status, totals = await _poll_status(page, app_frame)
    screenshots.append(await _shot(page, "11_final_status"))

    # ── 11. Fehlerbericht ──────────────────────────────────────────────────
    fehler_b64 = None
    if final_status in ("failed",) or (totals and totals.get("failed", 0) > 0):
        fehler_b64 = await _download_fehlerbericht(page, app_frame)
        screenshots.append(await _shot(page, "12_fehlerbericht_downloaded"))

    return {
        "status": final_status,
        "jobname": jobname,
        "category": category,
        "totals": totals,
        "fehlerbericht_xlsx_b64": fehler_b64,
        "screenshots": screenshots,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _shot(page: Page, label: str) -> str:
    name = f"flow_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{label}.png"
    path = Path(settings.screenshot_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        log.exception("Screenshot %s failed", name)
    return str(path)


async def _find_in_main(page: Page, candidates: list, label: str, timeout_s: int = 15):
    elapsed = 0
    while elapsed < timeout_s:
        for factory in candidates:
            loc = factory()
            try:
                if await loc.count() and await loc.first.is_visible(timeout=500):
                    return loc.first
            except Exception:
                continue
        await asyncio.sleep(1)
        elapsed += 1
    return None


async def _wait_for_app_frame(page: Page, timeout_s: int = 20, url_must_contain: str | None = None) -> Frame:
    """Знаходить app iframe. Якщо url_must_contain заданий — чекає поки frame з цим в URL з'явиться."""
    elapsed = 0
    last_seen: list[str] = []
    while elapsed < timeout_s:
        last_seen = []
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            url = frame.url or ""
            last_seen.append(url)
            if APP_FRAME_MARKER not in url:
                continue
            if url_must_contain and url_must_contain not in url:
                continue
            return frame
        await asyncio.sleep(1)
        elapsed += 1
    raise RuntimeError(
        f"App iframe (containing {APP_FRAME_MARKER}, must_contain={url_must_contain}) not found. "
        f"Seen frames: {last_seen}"
    )


async def _try_click_in_any_frame(page: Page, needle: str) -> tuple[Frame | None, dict | None]:
    """Перебирає всі non-main frames і пробує JS-click за needle. Повертає (frame, click_info) або (None, None)."""
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            result = await frame.evaluate(
                """
                (n) => {
                    const lc = n.toLowerCase();
                    const targets = [...document.querySelectorAll('button, a, [role="button"], [type="submit"]')];
                    const found = targets.find(el =>
                        (el.innerText || '').toLowerCase().includes(lc)
                        || (el.getAttribute('aria-label') || '').toLowerCase().includes(lc)
                    );
                    if (found) {
                        found.scrollIntoView({block: 'center'});
                        found.click();
                        return {tag: found.tagName, text: (found.innerText || '').trim().slice(0, 80), frame_url: location.href};
                    }
                    return null;
                }
                """,
                needle,
            )
            if result:
                return frame, result
        except Exception:
            continue
    return None, None


async def _frame_click_by_text(frame: Frame, needle: str) -> dict | None:
    """JS click на button/a/[role=button] чий innerText/aria-label містить needle (case-insensitive)."""
    try:
        return await frame.evaluate(
            """
            (needle) => {
                const lc = needle.toLowerCase();
                const targets = [...document.querySelectorAll('button, a, [role="button"], [type="submit"]')];
                const found = targets.find(el =>
                    (el.innerText || '').toLowerCase().includes(lc)
                    || (el.getAttribute('aria-label') || '').toLowerCase().includes(lc)
                );
                if (found) {
                    found.scrollIntoView({block: 'center'});
                    found.click();
                    return {tag: found.tagName, text: (el => (el.innerText || '').trim().slice(0, 80))(found)};
                }
                return null;
            }
            """,
            needle,
        )
    except Exception:
        log.exception("Frame JS click failed for %r", needle)
        return None


async def _frame_set_input_value(frame: Frame, label: str, value: str, placeholder_pat: str = ""):
    """Шукає input/textarea для введення (за placeholder/name/label) і виставляє value."""
    try:
        result = await frame.evaluate(
            """
            ([labelLc, value, placeholderPat]) => {
                const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=file]), textarea')];
                const re = placeholderPat ? new RegExp(placeholderPat, 'i') : null;
                const found = inputs.find(el => {
                    const ph = el.getAttribute('placeholder') || '';
                    const name = el.getAttribute('name') || '';
                    const id = el.id || '';
                    if (re && (re.test(ph) || re.test(name) || re.test(id))) return true;
                    return false;
                }) || inputs[0];  // fallback: перший input
                if (found) {
                    found.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(found, value);
                    found.dispatchEvent(new Event('input', {bubbles: true}));
                    found.dispatchEvent(new Event('change', {bubbles: true}));
                    return {placeholder: found.getAttribute('placeholder'), name: found.getAttribute('name')};
                }
                return null;
            }
            """,
            [label.lower(), value, placeholder_pat],
        )
        log.info("Input set %r: %s", label, result)
    except Exception:
        log.exception("Failed to set input %r", label)


async def _frame_find_file_input(frame: Frame, timeout_s: int = 15):
    """Знаходить ElementHandle на input[type=file]. Чекає до timeout_s."""
    elapsed = 0
    while elapsed < timeout_s:
        try:
            handle = await frame.query_selector('input[type="file"]')
            if handle:
                return handle
        except Exception:
            pass
        await asyncio.sleep(1)
        elapsed += 1
    return None


async def _frame_pick_dropdown_option(frame: Frame, value: str):
    """Після відкриття dropdown — шукає поле пошуку, набирає value, клікає першу опцію."""
    try:
        await frame.evaluate(
            """
            (value) => {
                const search = document.querySelector('input[type="search"], input[placeholder*="uchen" i], input[placeholder*="earch" i]');
                if (search) {
                    search.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(search, value);
                    search.dispatchEvent(new Event('input', {bubbles: true}));
                }
            }
            """,
            value,
        )
        await asyncio.sleep(1.5)
        # Click first option
        await frame.evaluate(
            """
            () => {
                const opts = [...document.querySelectorAll('[role="option"], li[data-value], button[data-value]')];
                if (opts.length) {
                    opts[0].click();
                }
            }
            """
        )
    except Exception:
        log.exception("dropdown pick failed for %r", value)


async def _frame_add_sku_image_mapping(frame: Frame, col_name: str):
    """Розкриває SKU Images dropdown і додає col_name."""
    try:
        # Спершу клік на поле "SKU Images" — лояльний пошук label-а
        opened = await frame.evaluate(
            """
            () => {
                const ALLOWED = ['DIV','SPAN','LABEL','BUTTON','H2','H3','H4','LI','P'];
                const candidates = [...document.querySelectorAll('div, span, label, button, h2, h3, h4, li, p')]
                    .filter(el => {
                        const t = (el.innerText || '').trim();
                        return /^sku images?$/i.test(t) || t === 'SKU Images';
                    });
                for (const el of candidates) {
                    // Шукаємо ближній dropdown trigger (combobox/button/input у тому ж блоці)
                    const block = el.closest('div,section,fieldset,tr,li');
                    let target = block?.querySelector('[role="combobox"], [role="button"], button, input[type="text"]');
                    if (!target) target = el;
                    target.scrollIntoView({block: 'center'});
                    target.click();
                    return {clicked: el.tagName, text: (el.innerText || '').trim().slice(0,40)};
                }
                return null;
            }
            """
        )
        if not opened:
            log.warning("SKU Images dropdown not opened")
            return
        await asyncio.sleep(0.7)
        # Search col_name + click matching option
        await frame.evaluate(
            """
            (col) => {
                const search = document.querySelector('input[type="search"], input[role="combobox"]');
                if (search) {
                    search.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(search, col);
                    search.dispatchEvent(new Event('input', {bubbles: true}));
                }
            }
            """,
            col_name,
        )
        await asyncio.sleep(0.7)
        await frame.evaluate(
            """
            (col) => {
                const candidates = [...document.querySelectorAll('[role="option"], li, button')];
                const found = candidates.find(el => (el.innerText || '').trim() === col);
                if (found) found.click();
                return found ? {clicked: col} : null;
            }
            """,
            col_name,
        )
        await asyncio.sleep(0.5)
        log.info("Added SKU Images mapping: %s", col_name)
    except Exception:
        log.exception("SKU mapping failed for %s", col_name)


async def _poll_status(page: Page, frame: Frame) -> tuple[str, dict | None]:
    """Очікує completed/failed. Повертає (status, totals)."""
    for attempt in range(STATUS_POLL_MAX_TRIES):
        try:
            text = await frame.evaluate("() => document.body.innerText.toLowerCase()")
            if "completed" in text:
                log.info("Import status: completed (attempt %d)", attempt + 1)
                return "completed", await _read_totals(frame)
            if "failed" in text or "fehlgeschlagen" in text:
                log.info("Import status: failed (attempt %d)", attempt + 1)
                return "failed", await _read_totals(frame)
            log.info("Import status pending (attempt %d/%d)", attempt + 1, STATUS_POLL_MAX_TRIES)
        except Exception:
            log.exception("Poll iteration failed")
        await asyncio.sleep(STATUS_POLL_INTERVAL)
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            frame = await _wait_for_app_frame(page, timeout_s=10)
        except Exception:
            log.exception("Reload during polling failed")
    return "timeout", None


async def _read_totals(frame: Frame) -> dict:
    try:
        return await frame.evaluate(
            """
            () => {
                const text = document.body.innerText;
                const out = {};
                for (const [label, key] of [
                    ['Gesamtanzahl', 'total'],
                    ['Importierte', 'imported'],
                    ['Fehlgeschlagene', 'failed'],
                    ['Übersprungene', 'skipped'],
                ]) {
                    const m = text.match(new RegExp(label + '[^0-9]*(\\\\d+)'));
                    if (m) out[key] = parseInt(m[1], 10);
                }
                return out;
            }
            """
        )
    except Exception:
        return {}


async def _download_fehlerbericht(page: Page, frame: Frame) -> str | None:
    try:
        # Знаходимо link Fehlerbericht (XLSX) і кліком стартуємо download
        async with page.expect_download(timeout=30000) as dl_info:
            clicked = await frame.evaluate(
                """
                () => {
                    const links = [...document.querySelectorAll('a, button')];
                    const found = links.find(el => /fehlerbericht.*xlsx|fehlerbericht/i.test(el.innerText || ''));
                    if (found) { found.click(); return true; }
                    return false;
                }
                """
            )
            if not clicked:
                log.warning("Fehlerbericht link not found")
                return None
        download = await dl_info.value
        save_path = Path(settings.download_dir) / download.suggested_filename
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(save_path))
        return base64.b64encode(save_path.read_bytes()).decode("ascii")
    except Exception:
        log.exception("Fehlerbericht download failed")
        return None
