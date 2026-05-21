"""Настройки приложения. Грузим из .env через pydantic-settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Telegram ===
    telegram_bot_token: str
    telegram_webhook_secret: str = "change-me"
    webhook_domain: str = "academ4i.duckdns.org"

    # === Anthropic (Claude) — через ProxyAPI ===
    anthropic_api_key: str
    anthropic_base_url: str = "https://api.proxyapi.ru/anthropic"
    claude_model: str = "claude-sonnet-4-6"
    claude_use_extended_thinking: bool = True

    # === OpenAI — через ProxyAPI (для эмбеддингов и парсинга PDF) ===
    openai_api_key: str
    openai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    embedding_model: str = "text-embedding-3-small"
    parser_model: str = "gpt-4o"

    # === Postgres ===
    database_url: str
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_project_ref: str = ""

    # === Redis ===
    redis_url: str = "redis://redis:6379/0"

    # === Тарифы ===
    free_lifetime_tasks: int = 3       # 3 задачи бесплатно на всю жизнь аккаунта
    premium_price_stars: int = 200     # 399₽ ≈ 200 Stars (Telegram сам конвертирует валюту)
    premium_price_rub: int = 399       # справочно для UX-текстов
    premium_duration_days: int = 30

    # === Админы (безлимит) — usernames через запятую без @ ===
    admin_usernames: str = "manag31"

    @property
    def admin_usernames_set(self) -> set[str]:
        return {u.strip().lower().lstrip("@") for u in self.admin_usernames.split(",") if u.strip()}

    # === Прочее ===
    env: str = "production"
    log_level: str = "INFO"
    sentry_dsn: str = ""

    @property
    def webhook_url(self) -> str:
        return f"https://{self.webhook_domain}/webhook"


settings = Settings()
