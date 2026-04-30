# OBI Playwright Uploader

Окремий мікросервіс на Coolify, який автоматизує UI-імпорт xlsx у VTEX seller cabinet (`hajus679.myvtex.com/admin`).

## Архітектура

```
Windmill (cron / post-processing)
         │ POST /upload-xlsx (Bearer)
         ▼
┌────────────────────────────────────────┐
│  obi-playwright-uploader  (Coolify)    │
│  ─────────────────────────────         │
│  FastAPI + Playwright + Chromium       │
│                                        │
│  1. Читає рядки з Google Sheet за      │
│     ref_ids (через OAuth-token)        │
│  2. Обнуляє Stock у кожному рядку      │
│  3. Будує xlsx (openpyxl)              │
│  4. Логіниться у VTEX (storage_state   │
│     persistent + TOTP fallback через   │
│     OTP-сервер на 88.198.203.52:9510)  │
│  5. Produktimport → +Neuimport →       │
│     Kategorie → upload → Mapping       │
│     (SKU Images 3..10) → Next          │
│  6. Polling status, screenshots        │
│  7. Якщо помилки → завантажує          │
│     Fehlerbericht і повертає у JSON    │
└────────────────────────────────────────┘
```

## Endpoints

### `GET /health`

Liveness probe. Повертає `{"ok": true, "service": "obi-playwright-uploader", "version": "0.1.0"}`.

### `POST /upload-xlsx`

Bearer-захищений (`Authorization: Bearer <WORKER_BEARER_TOKEN>`).

**Request:**
```json
{
  "ref_ids": ["NEW-695", "NEW-709", "..."],
  "category": "Sonstiges",        // optional, default з ENV
  "jobname": "OBI Auto - 2026-04-30"  // optional
}
```

**Response:**
```json
{
  "status": "completed | failed | timeout | error | no_rows",
  "jobname": "...",
  "category": "...",
  "rows_fetched": 10,
  "totals": {"total": 10, "imported": 9, "failed": 1, "skipped": 0},
  "fehlerbericht_xlsx_b64": "base64-encoded xlsx (опційно)",
  "screenshots": ["/tmp/screenshots/01_neuimport_open.png", "..."],
  "error": "..."
}
```

## Розгортання на Coolify

### 1. Створити Application

У проєкті `Leonid Shamarin`:
- **Source**: Public/Private Git Repository (URL твого GitHub repo з цим кодом)
- **Build Pack**: Dockerfile (автоматично виявляється)
- **Server**: `localhost` (як решта команди)
- **Port (exposed)**: `8080`

### 2. Persistent Storage

Додати persistent volume:
- Mount path: `/data` — для `storage_state.json` (не пропадає між редеплоями)

### 3. Environment Variables

Скопіювати з `.env.example` і заповнити секрети у Coolify "Application Secrets":

| Variable | Значення |
|---|---|
| `WORKER_BEARER_TOKEN` | Згенеруй через `openssl rand -hex 32` |
| `VTEX_LOGIN_URL` | `https://hajus679.myvtex.com/_v/segment/admin-login/v1/login?returnUrl=%2Fadmin%2Fproducts` |
| `VTEX_LOGIN_USER` | `admin@boni-brands.com` |
| `VTEX_LOGIN_PASSWORD` | (з LastPass) |
| `OTP_SERVER_URL` | `http://88.198.203.52:9510` |
| `OTP_SERVER_TOKEN` | (той самий, що Леонід має, `kGYcCss...`) |
| `OTP_EMAIL` | `info@hajus-ag.com` |
| `OTP_PROVIDER` | `VTEX` |
| `GSHEETS_OAUTH_TOKEN_JSON` | JSON `{"token":"…","refresh_token":"…","client_id":"…","client_secret":"…"}` (з Windmill `gsheets` resource — копія) |
| `GSHEETS_SPREADSHEET_ID` | `1d8s7eDyB3fbyNn2yvDf9TWbplM_tafnSFj4HGuMp52g` |
| `GSHEETS_SHEET_GID` | `590448179` |
| `OBI_DEFAULT_CATEGORY` | `Sonstiges` (або інша дефолтна) |

### 4. Domain (опційно)

Coolify може автоматично згенерувати subdomain. Після деплою отримаєш URL типу `https://obi-playwright-uploader-xxx.boni.tools`.

### 5. Перший логін

При першому запиті `/upload-xlsx` Playwright виконає повний логін у VTEX, отримає TOTP з OTP-сервера, збереже `storage_state.json` у `/data/`. Подальші запити будуть швидші — пропускають логін.

## Локальний запуск (для розробки)

```bash
cp .env.example .env
# Заповни значення
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Інтеграція з Windmill

Windmill-скрипт `f/LeonidShamarin/posting/upload_to_obi_via_playwright`:

```python
import requests
import wmill

def main(ref_ids: list[str], category: str | None = None) -> dict:
    url = wmill.get_variable("f/LeonidShamarin/playwright_server_url").rstrip("/")
    token = wmill.get_variable("f/LeonidShamarin/playwright_server_token")

    r = requests.post(
        f"{url}/upload-xlsx",
        headers={"Authorization": f"Bearer {token}"},
        json={"ref_ids": ref_ids, "category": category},
        timeout=900,  # 15 хв на повний імпорт
    )
    r.raise_for_status()
    return r.json()
```

Викликається з кінця cron-flow після `write_to_sheet`. Результат пишеться у Sheet (колонки `Outcome`/`Error`/`Warnings`/`ProductLink`) окремим скриптом.
