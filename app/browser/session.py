"""
Менеджер Playwright-сесії: тримає persistent storage_state.json,
fallback на повний логін з TOTP, якщо сесія протухла.

VTEX Admin login може мати кілька форм:
  1) одна сторінка email + password
  2) email -> Continue -> новий екран з password
  3) magic-link (email-only) — тоді password взагалі нема
  4) iframe (VTEX ID embed)
"""
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
    await page.wait_for_timeout(2000)  # дати React зрендериться

    if ADMIN_HOME_PATTERN.search(page.url):
        log.info("Session is alive (URL: %s)", page.url)
        return

    log.info("Login required (URL: %s)", page.url)
    await _shot(page, "01_login_page")
    await _do_login(page, context)


async def _find_email_input(page: Page):
    """Спробувати кілька селекторів. Повертає Locator або None."""
    candidates = [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[placeholder*="email" i]',
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible(timeout=2000):
                log.info("Email input found via: %s", sel)
                return loc
        except Exception:
            continue
    return None


async def _find_password_input(page: Page, timeout_ms: int = 30000):
    """Чекає до timeout_ms на видимий password input. Повертає Locator або None."""
    candidates = [
        'input[type="password"]',
        'input[name*="password" i]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
    ]
    log.info("Waiting up to %dms for password input via %d selectors", timeout_ms, len(candidates))
    deadline = timeout_ms / 1000.0
    import asyncio
    elapsed = 0.0
    while elapsed < deadline:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=500):
                    log.info("Password input found via: %s (after %.1fs)", sel, elapsed)
                    return loc
            except Exception:
                continue
        await asyncio.sleep(1)
        elapsed += 1
    return None


async def _click_continue(page: Page) -> bool:
    """Клікає 'Continue' / 'Weiter' / 'Next'. Повертає True якщо клікнули."""
    candidates_role = [r"weiter", r"next", r"continue", r"weitermachen", r"fortfahren"]
    pat = re.compile("|".join(candidates_role), re.IGNORECASE)
    btn = page.get_by_role("button", name=pat)
    if await btn.count():
        try:
            await btn.first.click()
            log.info("Clicked continue/next button")
            return True
        except Exception:
            log.exception("Continue click failed")
    # Інший варіант — submit-кнопка біля email
    submit = page.locator('button[type="submit"]').first
    if await submit.count():
        try:
            await submit.click()
            log.info("Clicked submit button")
            return True
        except Exception:
            log.exception("Submit click failed")
    return False


async def _do_login(page: Page, context: BrowserContext) -> None:
    user = settings.vtex_login_user
    pwd = settings.vtex_login_password

    # 1) Email step
    email_input = await _find_email_input(page)
    if not email_input:
        await _shot(page, "02_email_input_NOT_FOUND")
        raise RuntimeError(
            f"Email input not found on login page (URL: {page.url}). "
            "Check screenshots/login_*_email_input_NOT_FOUND.png"
        )
    await email_input.fill(user)
    await _shot(page, "02_email_filled")

    # 2) Continue / Next (якщо є)
    clicked = await _click_continue(page)
    if clicked:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)
    await _shot(page, "03_after_email_continue")

    # 3) Password step (з ширшими селекторами і довшим таймаутом)
    pwd_input = await _find_password_input(page, timeout_ms=30000)
    if not pwd_input:
        await _shot(page, "04_password_NOT_FOUND")
        raise RuntimeError(
            f"Password input not found after email step (URL: {page.url}). "
            "Можливо це email-magic-link flow. Перевір скріншоти "
            "screenshots/login_*_password_NOT_FOUND.png"
        )
    await pwd_input.fill(pwd)
    await _shot(page, "05_password_filled")

    # 4) Submit
    submit = page.get_by_role(
        "button", name=re.compile(r"log\s*in|anmelden|sign\s*in|einloggen", re.I),
    )
    if await submit.count():
        await submit.first.click()
    else:
        await pwd_input.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=30000)
    await _shot(page, "06_after_password_submit")

    # 5) OTP — якщо запитується
    if await _looks_like_otp_prompt(page):
        log.info("OTP prompt detected — fetching code from OTP server")
        code = get_vtex_otp()
        await _fill_otp(page, code)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await _shot(page, "07_after_otp")

    # 6) Перевірка успіху
    if LOGIN_PATTERN.search(page.url):
        await _shot(page, "08_login_failed_final")
        raise RuntimeError(f"Login failed — still on {page.url}")

    log.info("Login successful (URL: %s)", page.url)
    await _shot(page, "08_login_success")


async def _looks_like_otp_prompt(page: Page) -> bool:
    try:
        otp_field = page.locator(
            'input[name*="otp" i], input[name*="code" i], input[autocomplete="one-time-code"]'
        ).first
        return await otp_field.count() > 0
    except Exception:
        return False


async def _fill_otp(page: Page, code: str) -> None:
    field = page.locator(
        'input[name*="otp" i], input[name*="code" i], input[autocomplete="one-time-code"]'
    ).first
    await field.fill(code)
    submit = page.get_by_role("button", name=re.compile(r"verify|bestätigen|submit|continue", re.I))
    if await submit.count():
        await submit.first.click()
    else:
        await field.press("Enter")
