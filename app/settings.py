"""Глобальні налаштування з ENV (Coolify Application Secrets)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Auth
    worker_bearer_token: str

    # VTEX
    vtex_login_url: str
    vtex_login_user: str
    vtex_login_password: str

    # OTP server
    otp_server_url: str = "http://88.198.203.52:9510"
    otp_server_token: str
    otp_email: str = "info@hajus-ag.com"
    otp_provider: str = "VTEX"

    # Google Sheets
    gsheets_oauth_token_json: str = ""  # JSON з полями access_token/refresh_token або просто {"token":"…"}
    gsheets_spreadsheet_id: str = "1d8s7eDyB3fbyNn2yvDf9TWbplM_tafnSFj4HGuMp52g"
    gsheets_sheet_gid: int = 590448179

    # OBI defaults
    obi_default_category: str = "Sonstiges"

    # Filesystem
    storage_state_path: str = "/data/storage_state.json"
    screenshot_dir: str = "/tmp/screenshots"
    download_dir: str = "/tmp/downloads"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
