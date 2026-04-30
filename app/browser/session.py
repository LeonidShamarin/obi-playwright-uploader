"""
Менеджер Playwright-сесії: тримає persistent storage_state.json,
fallback на повний логін з TOTP, якщо сесія протухла.
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import (
    Browser, BrowserContext, Page, Playwright, async_playwright,
)

from app.settings import settings
from app.otp_client import get_vtex_otp

log = logging.getLogger("browser.session")

ADMIN_HOME_PATTERN = re.compile(r"/admin/(catalog|products|Site|home)", re.IGNORECASE)
LOGIN_PATTERN = re.compile(r"/admin-login/|/_v/segment/admin-login", re.IGNORECASE)


@asynccontextmanager
async def vtex_browser():
    """Async-контекстменеджер, який повертає (page, context) із сесією VTEX-admin."""
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
    """Контекст з прикріпленим storage_state, якщо файл існує."""
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


async def _ensure_logged_in(page: Page, context: BrowserContext) -> None:
    """Заходимо на admin-сторінку. Якщо редіректить на /admin-login → робимо логін."""
    target = settings.vtex_login_url
    log.info("Navigating to %s", target)
    await page.goto(target, wait_until="domcontentloaded")

    # Якщо вже потрапили в admin — все ок
    if ADMIN_HOME_PATTERN.search(page.url):
        log.info("Session is alive (URL: %s)", page.url)
        return

    log.info("Login required (URL: %s)", page.url)
    await _do_login(page, context)


async def _do_login(page: Page, context: BrowserContext) -> None:
    """Email+password логін, з TOTP fallback якщо запитається."""
    user = settings.vtex_login_user
    pwd = settings.vtex_login_password

    # Чекаємо поле email/пароль
    email_input = page.get_by_label("Email", exact=False).first
    try:
        await email_input.wait_for(timeout=15000)
        await email_input.fill(user)
    except Exception:
        # Резервний селектор
        await page.fill('input[type="email"]', user)

    # "Next" або одразу password — пробуємо обидва шляхи
    try:
        next_btn = page.get_by_role("button", name=re.compile(r"weiter|next|continue", re.I))
        if await next_btn.count():
            await next_btn.first.click()
            await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass

    # Password
    pwd_input = page.locator('input[type="password"]').first
    await pwd_input.wait_for(timeout=15000)
    await pwd_input.fill(pwd)

    submit = page.get_by_role("button", name=re.compile(r"log\s*in|anmelden|sign\s*in", re.I))
    if await submit.count():
        await submit.first.click()
    else:
        await pwd_input.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=30000)

    # Якщо запитується OTP-код
    if await _looks_like_otp_prompt(page):
        log.info("OTP prompt detected — fetching code from OTP server")
        code = get_vtex_otp()
        await _fill_otp(page, code)
        await page.wait_for_load_state("networkidle", timeout=30000)

    # Перевірка успіху
    if LOGIN_PATTERN.search(page.url):
        screenshot = Path(settings.screenshot_dir) / "login_failed.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot), full_page=True)
        raise RuntimeError(f"Login failed — still on {page.url}")

    log.info("Login successful (URL: %s)", page.url)


async def _looks_like_otp_prompt(page: Page) -> bool:
    """Heuristic: чи зараз запит OTP."""
    try:
        otp_field = page.locator('input[name*="otp" i], input[name*="code" i], input[autocomplete="one-time-code"]').first
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
