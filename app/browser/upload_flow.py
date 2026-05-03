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
# "SKU Images" (без числа) вже auto-mapped VTEX-ом по точному name-match.
# Треба додатково обрати тільки 3..10 — це 8 опцій з multi-select dropdown.
STATUS_POLL_INTERVAL = 10
STATUS_POLL_MAX_TRIES = 90  # 15 хв

# Fallback values для dictionary-mapping dropdowns (Schritt 2/3) коли source-value
# не має exact-match у списку OBI canonical-options. Підбираються по черзі —
# перший наявний у dropdown буде обрано. Останній резерв — перша опція в списку.
NEUTRAL_ATTRIBUTE_FALLBACKS = [
    "Sonstige",
    "Sonstiges",
    "Mehrfarbig",
    "Neutral",
    "Standard",
    "Andere",
    "Farblos",
    "Universal",
]


async def upload_xlsx_to_obi(
    page: Page, xlsx_bytes: bytes, jobname: str, category: str | None = None,
) -> dict:
    screenshots: list[str] = []
    # "Duschwannen" — це відомий leaf-категорія в OBI catalog (видно з
    # Historie importieren — багато успішних імпортів). settings.obi_default
    # ("Sonstiges") може не існувати у дереві, тому беремо leaf-fallback.
    category = category or "Duschwannen"

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

    # ── 3. Click "Neuer Import" + verify new-import frame ───────────────────
    # Раніше клік був "stateless" — _try_click_in_any_frame повертав truthy
    # навіть коли React-handler не зреєстрував подію і модалка не відкривалась.
    # Тепер після кожного кліку перевіряємо появу new-import iframe з retry.
    new_import_frame = None
    target_frame = app_frame
    for attempt in range(6):
        # Click via _try_click_in_any_frame (працює для всіх frames)
        tf, clicked = await _try_click_in_any_frame(page, "neuer import")
        if clicked:
            target_frame = tf
            log.info("Clicked 'Neuer Import' attempt %d in frame: %s",
                     attempt + 1, clicked.get("frame_url"))
        else:
            log.warning("Could not find 'Neuer Import' button (attempt %d)", attempt + 1)
            await asyncio.sleep(2)
            continue
        # Чекаємо до 15s на new-import frame
        try:
            new_import_frame = await _wait_for_app_frame(
                page, timeout_s=15, url_must_contain="new-import"
            )
            log.info("Found new-import sub-frame: %s", new_import_frame.url)
            break
        except RuntimeError:
            log.warning(
                "new-import frame did not appear after click %d — retrying", attempt + 1
            )
            screenshots.append(
                await _shot(page, f"WARN_no_new_import_frame_attempt{attempt + 1}")
            )
            await asyncio.sleep(3)

    if new_import_frame:
        app_frame = new_import_frame
    elif target_frame:
        app_frame = target_frame
        log.warning("Modal stays in same frame, using target_frame: %s", app_frame.url)
    else:
        screenshots.append(await _shot(page, "ERR_neuimport_no_frame"))
        all_frames = [(f.name, f.url) for f in page.frames]
        raise RuntimeError(
            f"Could not click 'Neuer Import' in any frame. Frames: {all_frames}"
        )

    # Чекаємо до 15s поки форма зрендериться (jobname input з'явиться).
    # Без цієї паузи буває race: frame знайдено, але React ще не змонтував UI.
    try:
        await app_frame.locator('input[name="jobName"]').first.wait_for(
            state="visible", timeout=15000
        )
        log.info("Form rendered (jobName input visible)")
    except Exception:
        log.warning("jobName input did not appear in 15s — продовжуємо спекулятивно")

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

        # VTEX категорії — це 3-рівневе tree-select. Top-level узли
        # ("[4908] Bäder, Küchen ...") мають subcategories і Confirm
        # лишається disabled поки не обрано ЛИСТ (leaf node).
        # Прагматично: використовуємо search input з category-назвою (default
        # — settings.obi_default_category, fallback "Duschwannen" що завжди
        # існує в OBI). Шукаємо input[placeholder*="Suche"|"Search"] і вводимо.
        search_query = (category or "Duschwannen").strip()
        log.info("Searching category in modal: %r", search_query)
        picked = {}
        # 1. Заповнюємо search input
        try:
            search_filled = await app_frame.evaluate(
                """
                (q) => {
                    const dialog = document.querySelector('[role="dialog"], [class*="modal" i]') || document;
                    const search = dialog.querySelector(
                        'input[placeholder*="Suche" i], input[placeholder*="Search" i], input[type="search"]'
                    );
                    if (!search) return {error: 'no_search_input'};
                    search.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(search, q);
                    search.dispatchEvent(new Event('input', {bubbles: true}));
                    search.dispatchEvent(new Event('change', {bubbles: true}));
                    return {filled: q, placeholder: search.getAttribute('placeholder')};
                }
                """,
                search_query,
            )
            log.info("Search filled: %s", search_filled)
        except Exception as e:
            log.warning("Search fill failed: %s", e)
        await page.wait_for_timeout(1500)

        # 2. Клікаємо ПЕРШИЙ видимий результат (через Playwright text-locator
        # для надійного user-event)
        try:
            # Знаходимо результат що містить наш query (case-insensitive)
            result = app_frame.get_by_text(re.compile(re.escape(search_query), re.I)).first
            if await result.count():
                await result.click(force=True, timeout=5000)
                txt = await result.text_content()
                picked = {"clicked_via": "search_result", "text": (txt or "").strip()[:80]}
                log.info("Category result clicked: %s", picked)
        except Exception as e:
            log.warning("Click search result failed: %s", e)

        # Fallback: якщо search не дав результатів — клік першого видимого radio
        if not picked:
            log.warning("Search yielded no clickable result; trying first radio leaf")
            try:
                radio = app_frame.locator('input[type="radio"]').first
                await radio.click(force=True, timeout=5000)
                picked = {"clicked_via": "fallback_radio"}
            except Exception as e:
                log.exception("All category-pick strategies failed: %s", e)
                picked = {"error": "all_strategies_failed"}

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

    # ── 7. Click "Weiter" → mapping page ───────────────────────────────────
    next_clicked = await _wait_and_click(app_frame, page, "weiter", timeout_s=20)
    if not next_clicked:
        screenshots.append(await _shot(page, "ERR_no_next_after_upload"))
        raise RuntimeError("Could not click Weiter after file upload (button stayed disabled)")
    log.info("Clicked Weiter Step 1 → 2: %s", next_clicked)

    # VTEX показує "Wir bearbeiten Ihre Datei" placeholder поки парсить xlsx.
    # Треба дочекатися поки фактичні mapping fields з'являться. Найбільш
    # специфічна ознака — текст "SKU Images" (label поля з нашими image
    # mappings) АБО "Product Content" (header секції що його містить).
    app_frame = await _wait_for_app_frame(page, timeout_s=10)
    log.info("Waiting for mapping page render (looking for 'Product Content' or 'SKU Images')...")
    try:
        await app_frame.get_by_text(
            re.compile(r"^Product Content$|^SKU Images$|^SKU Main Image", re.I)
        ).first.wait_for(state="visible", timeout=120000)
        log.info("Mapping fields rendered")
    except Exception as e:
        log.warning("Mapping fields wait timeout: %s", e)
    await page.wait_for_timeout(2000)
    screenshots.append(await _shot(page, "07_mapping_page"))

    # ── 8. Mapping: add SKU Images 3..10 у multi-select combobox ────────────
    log.info("Mapping: expand Product Content + add %d image columns", len(SKU_IMAGE_COLUMNS_TO_MAP))
    await _frame_click_by_text(app_frame, "product content")
    await page.wait_for_timeout(1500)
    screenshots.append(await _shot(page, "08_product_content_open"))

    await _frame_add_sku_images_multiselect(app_frame, page, SKU_IMAGE_COLUMNS_TO_MAP)
    screenshots.append(await _shot(page, "09_mapping_done"))

    # ── 8b. Resolve dictionary-mapping dropdowns (Schritt 2 attribute values) ─
    # VTEX може показати тут "X*" поля з Skip + dropdown — це attribute values
    # які не змаплені автоматично (напр. Color "Magenta" → Magenta/Grün-Magenta).
    resolved_step2 = await _frame_resolve_attribute_mappings(app_frame, page)
    if resolved_step2:
        log.info("Schritt 2 resolved %d dictionary mappings", len(resolved_step2))
        screenshots.append(await _shot(page, "09b_dict_resolved_step2"))

    # ── 9. Weiter → Step 3 ──────────────────────────────────────────────────
    next2 = await _wait_and_click(app_frame, page, "weiter", timeout_s=20)
    if next2:
        log.info("Clicked Weiter Step 2 → 3: %s", next2)
    else:
        log.warning("Weiter Step 2 → 3 not found")
        screenshots.append(await _shot(page, "WARN_no_next_step2"))

    # Step 3 також може показати "Wir bearbeiten" placeholder. Чекаємо
    # поки processing завершиться і Weiter стане enabled.
    await page.wait_for_timeout(2500)
    screenshots.append(await _shot(page, "10_step3_review"))
    log.info("Waiting for Schritt 3 dictionary rows to render...")

    # ── 9a. Wait for Skip rows to appear OR for Weiter to be enabled ────────
    # VTEX парсить xlsx 30-60с і ЛИШЕ ПОТІМ рендерить mandatory-rows з Skip
    # кнопкою. Без цього ожидания resolve викликається до появи рядків.
    schritt3_state = await _wait_for_skip_or_weiter(app_frame, page, timeout_s=180)
    log.info("Schritt 3 wait result: %s", schritt3_state)
    screenshots.append(await _shot(page, f"10a_schritt3_state_{schritt3_state}"))

    # ── 9b. Expand all collapsed sections (щоб Mandatory rows стали visible) ─
    expanded = await _expand_all_sections(app_frame, page)
    log.info("Expanded %d collapsed sections", expanded)
    if expanded:
        await page.wait_for_timeout(2000)
        screenshots.append(await _shot(page, "10a2_after_expand"))

    # Re-check для Mandatory після expand
    if schritt3_state != "mandatory":
        post_check = await _wait_for_skip_or_weiter(app_frame, page, timeout_s=15)
        log.info("After expand schritt3_state=%s", post_check)
        if post_check == "mandatory":
            schritt3_state = "mandatory"

    # ── 9c. Resolve dictionary-mapping dropdowns (Schritt 3 attribute values) ─
    if schritt3_state == "mandatory":
        try:
            resolved_step3 = await _frame_resolve_attribute_mappings(app_frame, page)
            log.info("Schritt 3 resolve attempted %d rows", len(resolved_step3))
            if resolved_step3:
                screenshots.append(await _shot(page, "10b_dict_resolved_step3"))
        except Exception:
            log.exception("Schritt 3 dictionary resolve failed (non-fatal)")

    # ── 9d. Fallback: Skip-click any remaining Mandatory rows ───────────────
    # Якщо після resolve залишились Mandatory (наприклад dropdown не відкрився
    # для якогось рядка) — клікаємо Skip щоб Weiter міг активуватись.
    skipped_count = 0
    for _ in range(50):
        remaining = await app_frame.evaluate(
            """
            () => [...document.querySelectorAll('*')].filter(
                el => el.children.length === 0
                  && /^\\s*Mandatory\\s*$/.test(el.innerText || '')
                  && el.offsetParent
            ).length
            """
        )
        if remaining == 0:
            break
        clicked = await app_frame.evaluate(
            """
            () => {
                const mandatory = [...document.querySelectorAll('*')].find(
                    el => el.children.length === 0
                      && /^\\s*Mandatory\\s*$/.test(el.innerText || '')
                      && el.offsetParent
                );
                if (!mandatory) return false;
                let row = mandatory.parentElement;
                for (let lvl = 0; lvl < 12 && row; lvl++) {
                    const skipBtn = [...row.querySelectorAll('button')].find(
                        b => /^\\s*skip\\s*$/i.test(b.innerText || '') && b.offsetParent
                    );
                    if (skipBtn) {
                        skipBtn.scrollIntoView({block: 'center'});
                        skipBtn.click();
                        return true;
                    }
                    row = row.parentElement;
                }
                return false;
            }
            """
        )
        if not clicked:
            log.warning("No Skip button on remaining Mandatory row #%d", skipped_count + 1)
            break
        skipped_count += 1
        await page.wait_for_timeout(400)
    if skipped_count:
        log.info("Skip-clicked %d remaining Mandatory rows as fallback", skipped_count)
        screenshots.append(await _shot(page, "10c_skip_fallback"))

    # ── 9b. Weiter → Step 4 (start import) ─────────────────────────────────
    # Чекаємо до 90с щоб Schritt 3 review/processing завершився
    next3 = await _wait_and_click(app_frame, page, "weiter", timeout_s=90)
    if not next3:
        next3 = await _wait_and_click(app_frame, page, "importieren", timeout_s=15)
    if next3:
        log.info("Clicked Weiter Step 3 → 4: %s", next3)
    else:
        log.warning("Weiter Step 3 → 4 not found after 90s")
        screenshots.append(await _shot(page, "WARN_no_next_step3"))
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


async def _wait_and_click(frame: Frame, page: Page, needle: str, timeout_s: int = 20) -> dict | None:
    """Polling: чекає поки button з accessible name=needle стане enabled, тоді клікає.

    Корисно для Weiter-кнопки що з'являється DISABLED поки форма не валідна,
    і стає ENABLED коли всі поля заповнено.
    """
    elapsed = 0.0
    last_err = None
    while elapsed < timeout_s:
        result = await _frame_click_by_text(frame, needle)
        if result:
            return result
        await asyncio.sleep(1)
        elapsed += 1
    log.warning("_wait_and_click: %r not enabled after %ss", needle, timeout_s)
    return None


async def _frame_click_by_text(frame: Frame, needle: str) -> dict | None:
    """Клік на button/link з accessible name = needle.

    Пропускає disabled-кандидатів (бо force-click disabled-кнопки нічого
    не робить, тільки маскує невирішену помилку — попереднім кроком форма
    не була повністю заповнена).
    """
    pat = re.compile(re.escape(needle), re.I)
    # 1. Playwright role-based locator (real user click events)
    for role in ("button", "link"):
        try:
            loc = frame.get_by_role(role, name=pat)
            count = await loc.count()
            for idx in range(min(count, 10)):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible(timeout=300):
                        continue
                    box = await cand.bounding_box()
                    if not box or box.get("width", 0) < 5 or box.get("height", 0) < 5:
                        continue
                    # Skip disabled — інакше force-click "успішний" але noop
                    if await cand.is_disabled():
                        continue
                    if (await cand.get_attribute("aria-disabled")) == "true":
                        continue
                    await cand.scroll_into_view_if_needed(timeout=2000)
                    await cand.click(timeout=5000)  # БЕЗ force — натуральний клік
                    name = (await cand.text_content()) or await cand.get_attribute("aria-label") or ""
                    return {
                        "tag": role.upper(),
                        "text": name.strip()[:80],
                        "via": "playwright_role",
                        "idx": idx,
                    }
                except Exception:
                    continue
        except Exception:
            continue

    # 2. JS fallback з visibility-фільтром
    try:
        return await frame.evaluate(
            """
            (needle) => {
                const lc = needle.toLowerCase();
                const targets = [...document.querySelectorAll('button, a, [role="button"], [type="submit"]')];
                // Тільки ВИДИМІ елементи
                const visible = targets.filter(el => {
                    if (el.disabled) return false;
                    const r = el.getBoundingClientRect();
                    return r.width >= 5 && r.height >= 5 && r.top < window.innerHeight + 500;
                });
                const found = visible.find(el =>
                    (el.innerText || '').toLowerCase().includes(lc)
                    || (el.getAttribute('aria-label') || '').toLowerCase().includes(lc)
                );
                if (found) {
                    found.scrollIntoView({block: 'center'});
                    found.click();
                    return {tag: found.tagName, text: (found.innerText || '').trim().slice(0, 80), via: 'js_visible'};
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


async def _frame_add_sku_images_multiselect(frame: Frame, page: Page, col_names: list[str]):
    """VTEX SKU Images combobox — type-and-Enter workflow.

    Реальний UI: input[role=combobox] як searchable filter. Юзер-flow:
      1. Click input (focus + open dropdown)
      2. Type col_name (dropdown filters до 1 option)
      3. Enter (select highlighted option → стає chip)
      4. Repeat для наступного col_name

    Це stable бо не залежить від нестабільного role=option scraping.
    """
    # 1. Знаходимо SKU Images combobox через JS handle (specific row)
    handle = await frame.evaluate_handle(
        """
        () => {
            const all = [...document.querySelectorAll('label, span, div, p')];
            const label = all.find(el => (el.textContent || '').trim() === 'SKU Images');
            if (!label) return null;
            let scope = label.parentElement;
            for (let i = 0; i < 6 && scope; i++) {
                const trigger = scope.querySelector('input[role="combobox"], [role="combobox"]');
                if (trigger && trigger !== label) return trigger;
                scope = scope.parentElement;
            }
            return null;
        }
        """
    )
    el = handle.as_element()
    if not el:
        log.warning("SKU Images combobox not found")
        return

    log.info("SKU Images combobox handle obtained")

    # ElementHandle → Locator для зручного fill() API
    el_locator = frame.locator('input[role="combobox"]').filter(has_text="").first
    # Wrap raw handle as locator альтернативно — використовуємо el напряму
    for col in col_names:
        try:
            # Focus input + open dropdown
            await el.scroll_into_view_if_needed()
            await el.click()
            await page.wait_for_timeout(400)
            # Fill через native ElementHandle — clears + types new text.
            # Це впливає лише на search input, chips незмінні.
            await el.fill(col)
            await page.wait_for_timeout(700)
            # Клікаємо ВИДИМУ опцію з точним text-match через Playwright
            # get_by_text — auto-wait + правильний user click event.
            clicked = False
            try:
                opt = frame.get_by_text(col, exact=True)
                cnt = await opt.count()
                for idx in range(min(cnt, 5)):
                    cand = opt.nth(idx)
                    if await cand.is_visible(timeout=300):
                        await cand.click(timeout=2000)
                        clicked = True
                        log.info("Added SKU Images mapping (click): %s", col)
                        break
            except Exception:
                pass
            # Fallback: Enter (selects highlighted top-match)
            if not clicked:
                await page.keyboard.press("Enter")
                log.info("Added SKU Images mapping (Enter): %s", col)
            await page.wait_for_timeout(600)
        except Exception as e:
            log.exception("Failed to add %s: %s", col, e)

    # Закриваємо dropdown
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _expand_all_sections(frame: Frame, page: Page, max_attempts: int = 3) -> int:
    """Розгорнути всі згорнуті attribute-секції на Schritt 3.

    VTEX рендерить Schritt 3 з секціями типу "Brand Name 2 of 2 mapped".
    Більшість секцій згорнуті за замовчуванням; Mandatory rows усередині них
    мають offsetParent=null → JS-query їх не бачить.

    Стратегія: знайти кнопки/елементи з aria-expanded="false" АБО з текстом
    "X of Y mapped" і клікнути їх. Повторити кілька раз — після кожного клика
    можуть з'явитись нові вкладені секції.
    """
    total_expanded = 0
    for attempt in range(max_attempts):
        expanded = await frame.evaluate(
            """
            () => {
                let candidates = [...document.querySelectorAll('[aria-expanded="false"]')]
                    .filter(el => el.offsetParent);
                if (!candidates.length) {
                    // Fallback: section headers за текстом "X of Y mapped"
                    const all = [...document.querySelectorAll('button, [role="button"], div')];
                    candidates = all.filter(el => {
                        if (!el.offsetParent || el.children.length > 80) return false;
                        const t = (el.innerText || '').trim();
                        return /\\d+\\s+of\\s+\\d+\\s+mapped/i.test(t) && t.length < 250;
                    });
                    // Тільки deepest (не containing інші candidates)
                    candidates = candidates.filter(c =>
                        !candidates.some(o => o !== c && c.contains(o))
                    );
                }
                let count = 0;
                for (const el of candidates) {
                    try {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        count++;
                    } catch (e) {}
                }
                return count;
            }
            """
        )
        log.info("Expand attempt %d: clicked %d section headers", attempt + 1, expanded)
        total_expanded += expanded
        if expanded == 0:
            break
        await page.wait_for_timeout(1500)
    return total_expanded


async def _wait_for_skip_or_weiter(
    frame: Frame, page: Page, timeout_s: int = 180
) -> str:
    """Polling до появи "Mandatory" badge (=unresolved row) АБО enabled Weiter.

    Schritt 3 розбитий на секції (Brand Name, weeeRelevant, color etc.).
    Кожен рядок має постійну Skip-кнопку незалежно від статусу — discriminator
    це badge праворуч: "Mandatory" (помаранч, unresolved) vs "Mapped" (зелений).

    Без цього wait resolve_attribute_mappings викликається до того як VTEX
    закінчить обробку xlsx (Schritt 3 backend processing 30-60+ сек).

    Повертає:
      "mandatory" — є unresolved rows з Mandatory badge → треба їх обробити
      "weiter"    — Weiter активний, mappings не потрібні
      "timeout"   — нічого не з'явилось
    """
    elapsed = 0.0
    while elapsed < timeout_s:
        try:
            state = await frame.evaluate(
                """
                () => {
                    const mandatory = [...document.querySelectorAll('*')].filter(
                        el => el.children.length === 0
                          && /^\\s*Mandatory\\s*$/.test(el.innerText || '')
                          && el.offsetParent
                    );
                    const weiterBtns = [...document.querySelectorAll('button')].filter(
                        b => /weiter/i.test(b.innerText || '') && b.offsetParent
                    );
                    const weiter = weiterBtns.find(
                        b => !b.disabled && b.getAttribute('aria-disabled') !== 'true'
                    );
                    return {
                        mandatory_count: mandatory.length,
                        weiter_enabled: !!weiter,
                    };
                }
                """
            )
        except Exception:
            state = {}
        if state.get("mandatory_count", 0) > 0:
            return "mandatory"
        if state.get("weiter_enabled"):
            return "weiter"
        await page.wait_for_timeout(2000)
        elapsed += 2.0
    return "timeout"


async def _click_first_skip(frame: Frame) -> None:
    """Клікнути першу видиму кнопку 'Skip' (для пропуску застряглого рядка)."""
    await frame.evaluate(
        """
        () => {
            const sb = [...document.querySelectorAll('button')].find(
                b => /^\\s*skip\\s*$/i.test(b.innerText || '') && b.offsetParent
            );
            if (sb) sb.click();
        }
        """
    )


async def _frame_resolve_attribute_mappings(
    frame: Frame, page: Page, max_iters: int = 100
) -> list[dict]:
    """Авто-резолв dictionary-mapping dropdowns на Schritt 2/3.

    UI patterns: рядок з "<Label>*" (source value) + input + "Skip" button +
    chevron-dropdown. Опції — type-to-search combobox: options НЕ рендеряться
    у DOM поки користувач не введе текст у input з REAL keyboard events
    (React onChange).

    Стратегія per row:
      1. Знайти input через JSHandle у рядку зі "Skip" кнопкою
      2. Playwright el.click() + el.fill(label) — REAL keyboard input,
         тригерить React-state і VTEX рендерить опції
      3. Wait + frame.get_by_text(label, exact, regex case-insensitive) → click
      4. Якщо exact-match не знайдено — спробувати neutral fallbacks
      5. Якщо нічого не знайдено — Skip-клік, рухаємось далі
      6. Guard від нескінченного циклу через seen_labels set

    Повертає список resolved/skipped-rows.
    """
    resolved: list[dict] = []
    seen_labels: set[str] = set()

    # JS-helper для пошуку рядка з "Mandatory" badge
    find_mandatory_row_js = """
        (alreadySeen) => {
            const mandatoryBadges = [...document.querySelectorAll('*')].filter(
                el => el.children.length === 0
                  && /^\\s*Mandatory\\s*$/.test(el.innerText || '')
                  && el.offsetParent
            );
            if (!mandatoryBadges.length) return null;
            // Беремо ПЕРШИЙ Mandatory badge → walk up до row containing input
            const badge = mandatoryBadges[0];
            let row = badge.parentElement;
            let input = null;
            for (let lvl = 0; lvl < 12 && row; lvl++) {
                input = row.querySelector('input:not([type=hidden])');
                if (input) break;
                row = row.parentElement;
            }
            if (!input || !row) return null;
            // Label — це перший текст у рядку (e.g. "Magenta*" або "transparent*")
            const rowText = (row.innerText || '').split('\\n')[0].trim();
            const label = rowText.replace(/\\*\\s*$/, '').trim();
            return {
                label,
                prefilled: input.value || '',
                stuck: alreadySeen.includes(label),
            };
        }
    """

    for i in range(max_iters):
        # Step 1: Read label of next "Mandatory" row
        info = await frame.evaluate(find_mandatory_row_js, list(seen_labels))
        if not info:
            log.info("No more Mandatory rows (resolved %d)", len(resolved))
            break

        label = info.get("label", "")
        if info.get("stuck"):
            log.warning("Stuck row %r — Skip-кліком пропускаємо", label)
            await _click_first_skip(frame)
            await page.wait_for_timeout(500)
            resolved.append({"source": label, "skipped": "stuck"})
            continue

        # Step 2: Get the input ElementHandle of the Mandatory row
        handle = await frame.evaluate_handle(
            """
            () => {
                const mandatoryBadges = [...document.querySelectorAll('*')].filter(
                    el => el.children.length === 0
                      && /^\\s*Mandatory\\s*$/.test(el.innerText || '')
                      && el.offsetParent
                );
                if (!mandatoryBadges.length) return null;
                const badge = mandatoryBadges[0];
                let row = badge.parentElement;
                let input = null;
                for (let lvl = 0; lvl < 12 && row; lvl++) {
                    input = row.querySelector('input:not([type=hidden])');
                    if (input) break;
                    row = row.parentElement;
                }
                return input;
            }
            """
        )
        input_el = handle.as_element()
        if not input_el:
            log.warning("Couldn't get input handle for %r — Skip-кліком", label)
            await _click_first_skip(frame)
            await page.wait_for_timeout(500)
            resolved.append({"source": label, "skipped": "no_input_handle"})
            continue

        seen_labels.add(label)

        # Step 3: Real Playwright fill — triggers React onChange, opens dropdown
        try:
            await input_el.scroll_into_view_if_needed(timeout=2000)
            await input_el.click(timeout=3000)
            await page.wait_for_timeout(200)
            await input_el.fill(label, timeout=3000)
        except Exception as e:
            log.warning("Fill failed for %r: %s — Skip", label, e)
            await _click_first_skip(frame)
            await page.wait_for_timeout(500)
            resolved.append({"source": label, "skipped": f"fill_failed:{type(e).__name__}"})
            continue

        await page.wait_for_timeout(900)  # дати VTEX час на рендер опцій

        # Step 4: Pick option — БАГАТОЕТАПНА стратегія, бо VTEX-React DOM
        # неоднорідний. ЗАВЖДИ обираємо щось з dropdown (Skip = імпорт впаде).
        candidates = [label] + NEUTRAL_ATTRIBUTE_FALLBACKS
        picked = None
        pick_method = None

        # Method 1: get_by_role("option", name=...) — semantic ARIA
        for cand in candidates:
            if picked or not cand:
                continue
            try:
                pat = re.compile(r"^\s*" + re.escape(cand) + r"\s*$", re.I)
                opt = frame.get_by_role("option", name=pat)
                cnt = await opt.count()
                for idx in range(min(cnt, 5)):
                    el = opt.nth(idx)
                    try:
                        if not await el.is_visible(timeout=300):
                            continue
                        await el.click(timeout=2000)
                        picked = cand
                        pick_method = "role=option"
                        break
                    except Exception as exc:
                        log.debug("role=option click %r failed: %s", cand, exc)
                        continue
            except Exception:
                continue

        # Method 2: get_by_text exact — by visible text content
        if not picked:
            for cand in candidates:
                if picked or not cand:
                    continue
                try:
                    pat = re.compile(r"^\s*" + re.escape(cand) + r"\s*$", re.I)
                    opt = frame.get_by_text(pat)
                    cnt = await opt.count()
                    for idx in range(min(cnt, 8)):
                        el = opt.nth(idx)
                        try:
                            if not await el.is_visible(timeout=300):
                                continue
                            await el.click(timeout=2000)
                            picked = cand
                            pick_method = "text"
                            break
                        except Exception as exc:
                            log.debug("text click %r idx %d failed: %s", cand, idx, exc)
                            continue
                except Exception:
                    continue

        # Method 3: ANY visible dropdown option (last resort — pick first
        # available щоб не Skip-нути; може бути неточний колір але імпорт пройде)
        if not picked:
            res = await frame.evaluate(
                """
                () => {
                    const opts = [...document.querySelectorAll(
                        '[role="option"], [role="menuitem"], li, [class*="option" i], [class*="DropdownItem" i]'
                    )].filter(e => {
                        if (!e.offsetParent) return false;
                        const t = (e.innerText || '').trim();
                        return t.length > 0 && t.length < 80;
                    });
                    if (!opts.length) return null;
                    opts[0].scrollIntoView({block: 'center'});
                    opts[0].click();
                    return (opts[0].innerText || '').trim();
                }
                """
            )
            if res:
                picked = res
                pick_method = "first_visible"
                log.warning("Picked first available option %r for label %r (no exact match)", res, label)

        # ОСТАННЯ резерв — Skip-клік (user popередив що це зламає import,
        # але якщо ми тут то dropdown зовсім порожній — все одно зламається)
        if not picked:
            log.error("CRITICAL: No options at all for %r — Skip (import will likely fail)", label)
            await _click_first_skip(frame)
            await page.wait_for_timeout(500)
            resolved.append({"source": label, "skipped": "no_dropdown_options"})
            continue

        log.info("Resolve %r → %r via %s", label, picked, pick_method)

        await page.wait_for_timeout(500)
        log.info(
            "Resolve #%d: source=%r prefill=%r → picked=%r",
            i + 1,
            label,
            info.get("prefilled"),
            picked,
        )
        resolved.append({
            "source": label,
            "prefill": info.get("prefilled"),
            "picked": picked,
        })

    return resolved


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
    """Очікує "Job completed"/failed на Schritt 4. Без reload — preserve state.

    Шукає СПЕЦИФІЧНІ маркери Schritt 4:
      "job completed" — успіх
      "job failed" / "fehlgeschlagen" — fail
    Не матчимо просто "completed" бо у history list багато completed-rows.
    """
    for attempt in range(STATUS_POLL_MAX_TRIES):
        try:
            frame = await _wait_for_app_frame(page, timeout_s=5)
        except RuntimeError:
            pass
        try:
            text = await frame.evaluate("() => document.body.textContent")
            text_lc = text.lower()
            if "job completed" in text_lc:
                log.info("Import status: completed (attempt %d)", attempt + 1)
                return "completed", await _read_totals(frame)
            if "job failed" in text_lc or "import fehlgeschlagen" in text_lc:
                log.info("Import status: failed (attempt %d)", attempt + 1)
                return "failed", await _read_totals(frame)
            log.info("Import status pending (attempt %d/%d)", attempt + 1, STATUS_POLL_MAX_TRIES)
        except Exception:
            log.exception("Poll iteration failed")
        await asyncio.sleep(STATUS_POLL_INTERVAL)
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
