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
    # Модель лёгкого OCR-прохода (распознать условие + номера задач).
    # Haiku: распознавание — простая работа, в разы дешевле Sonnet; решение остаётся на claude_model.
    ocr_model: str = "claude-haiku-4-5-20251001"

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
    # Бесплатный лимит: N задач на скользящее окно free_window_days дней.
    # Потолок жёсткий — накопления нет (антифарм): окно стартует при первой
    # бесплатной задаче и сбрасывается только спустя free_window_days.
    free_tasks_per_week: int = 2
    free_window_days: int = 7

    # Premium-подписка: безлимит на срок. Fair-use лимит в день — защита от
    # перерасхода (себестоимость ~7₽/задача): студенту 10/день фактически безлимит.
    premium_price_stars: int = 199
    premium_duration_days: int = 30
    premium_week_price_stars: int = 99
    premium_week_days: int = 7
    premium_daily_cap: int = 10        # макс. задач в сутки на Premium (UTC-сутки)

    # Пакеты — разовая покупка N задач (без срока, расходуются как credits).
    pack_price_stars: int = 79
    pack_tasks: int = 5
    pack_large_price_stars: int = 139
    pack_large_tasks: int = 10

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
