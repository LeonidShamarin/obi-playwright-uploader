"""
Менеджер Playwright-сесії: тримає persistent storage_state.json,
fallback на повний логін, якщо сесія протухла.

VTEX Admin login (для нашого акаунту info@hajus-ag.com) — passwordless email-code:
  1. Email → Weiter
  2. VTEX надсилає 6-значний код на email
  3. OTP-сервер (88.198.203.52:9510) читає цей код і віддає через REST
  4. Code → Weiter → admin home
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from playwright.async_api import (
    Browser, BrowserContext, Page, async_playwright,
)

from app.settings import settings
from app.otp_client import get_vtex_otp

log = logging.getLogger("browser.session")

ADMIN_HOME_PATTERN = re.compile(r"/admin/(catalog|products|Site|home)", re.IGNORECASE)
LOGIN_PATTERN = re.compile(r"/admin-login/|/_v/segment/admin-login", re.IGNORECASE)
OTP_RETRIEVE_DELAY_SEC = 6  # дамо VTEX час доставити email до OTP-сервера


@asynccontextmanager
async def vtex_browser():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
        ])
        context = await _make_context(browser)
        page = await context.new_page()

        try:
            await _ensure_logged_in(page, context)
            yield page, context
        finally:
            try:
                await context.storage_state(path=settings.storage_state_path)
            except Exception:
                log.exception("Failed to save storage_state")
            await context.close()
            await browser.close()


async def _make_context(browser: Browser) -> BrowserContext:
    state_path = Path(settings.storage_state_path)
    kwargs = {
        "viewport": {"width": 1440, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "accept_downloads": True,
    }
    if state_path.exists():
        kwargs["storage_state"] = str(state_path)
        log.info("Loaded storage_state from %s", state_path)
    return await browser.new_context(**kwargs)


async def _shot(page: Page, label: str) -> str:
    name = f"login_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{label}.png"
    path = Path(settings.screenshot_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
    except Exception:
        log.exception("Screenshot %s failed", name)
    return str(path)


async def _ensure_logged_in(page: Page, context: BrowserContext) -> None:
    target = settings.vtex_login_url
    log.info("Navigating to %s", target)
    await page.goto(target, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    if ADMIN_HOME_PATTERN.search(page.url):
        log.info("Session is alive (URL: %s)", page.url)
        return

    log.info("Login required (URL: %s)", page.url)
    await _shot(page, "01_login_page")
    await _do_login(page, context)


async def _find_input(page: Page, candidates: list[str], label: str, timeout_ms: int = 30000):
    """Polling-пошук видимого input серед заданих селекторів."""
    log.info("Waiting up to %dms for %s input", timeout_ms, label)
    deadline = timeout_ms / 1000.0
    elapsed = 0.0
    while elapsed < deadline:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=500):
                    log.info("%s input found via: %s (after %.1fs)", label, sel, elapsed)
                    return loc
            except Exception:
                continue
        await asyncio.sleep(1)
        elapsed += 1
    return None


async def _click_continue(page: Page) -> bool:
    pat = re.compile(r"weiter|next|continue|fortfahren", re.IGNORECASE)
    btn = page.get_by_role("button", name=pat)
    if await btn.count():
        try:
            await btn.first.click()
            log.info("Clicked WEITER/Next button")
            return True
        except Exception:
            log.exception("Click WEITER failed")
    submit = page.locator('button[type="submit"]').first
    if await submit.count():
        try:
            await submit.click()
            log.info("Clicked submit button (no role match)")
            return True
        except Exception:
            log.exception("Click submit failed")
    return False


async def _do_login(page: Page, context: BrowserContext) -> None:
    user = settings.vtex_login_user

    # 1) Email step
    email_input = await _find_input(
        page,
        candidates=[
            'input[type="email"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[placeholder*="email" i]',
            'input[autocomplete="username"]',
            'input[autocomplete="email"]',
        ],
        label="email",
        timeout_ms=15000,
    )
    if not email_input:
        await _shot(page, "ERR_email_input_NOT_FOUND")
        raise RuntimeError(f"Email input not found on login page (URL: {page.url})")
    await email_input.fill(user)
    await _shot(page, "02_email_filled")

    # 2) Weiter
    if not await _click_continue(page):
        await _shot(page, "ERR_no_continue_button")
        raise RuntimeError("Continue/Weiter button not found")

    await page.wait_for_load_state("domcontentloaded", timeout=15000)
    await page.wait_for_timeout(1500)
    await _shot(page, "03_after_email_continue")

    # 3) Очікуємо появу поля Code (VTEX надіслав email-magic-code)
    code_input = await _find_input(
        page,
        candidates=[
            'input[autocomplete="one-time-code"]',
            'input[name*="code" i]',
            'input[id*="code" i]',
            'input[placeholder*="code" i]',
            # fallback — VTEX може використати generic text input на цій сторінці
            'input[type="text"]:not([type="email"])',
            'input[type="tel"]',
            'input[inputmode="numeric"]',
        ],
        label="email-code",
        timeout_ms=20000,
    )
    if not code_input:
        await _shot(page, "ERR_code_input_NOT_FOUND")
        raise RuntimeError(
            f"Code input not found after email continue (URL: {page.url}). "
            "Очікуємо input для email-code, але VTEX зробив щось інше."
        )

    # 4) Дамо OTP-серверу час прийняти email і витягти код
    log.info("Waiting %ds for OTP server to receive email", OTP_RETRIEVE_DELAY_SEC)
    await asyncio.sleep(OTP_RETRIEVE_DELAY_SEC)

    # 5) Тягнемо код з OTP-сервера (з кількома retry якщо ще не дійшов)
    code = None
    for attempt in range(1, 5):
        try:
            code = get_vtex_otp()
            log.info("OTP-server returned code (length=%d) on attempt %d", len(code), attempt)
            if code and len(code) >= 4:
                break
        except Exception:
            log.exception("OTP fetch attempt %d failed", attempt)
        await asyncio.sleep(5)

    if not code:
        await _shot(page, "ERR_no_otp_code")
        raise RuntimeError("Failed to retrieve OTP code from OTP server")

    await code_input.fill(code)
    await _shot(page, "04_code_filled")

    # 6) Submit
    if not await _click_continue(page):
        # fallback на Enter в полі
        try:
            await code_input.press("Enter")
        except Exception:
            log.exception("Could not submit code")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await _shot(page, "05_after_code_submit")

    # 7) Перевірка успіху
    if LOGIN_PATTERN.search(page.url):
        await _shot(page, "ERR_still_on_login_page")
        raise RuntimeError(f"Login failed — still on {page.url}")

    log.info("Login successful (URL: %s)", page.url)
    await _shot(page, "06_login_success")
