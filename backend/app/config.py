"""Настройки приложения. Грузим из .env через pydantic-settings."""
from dataclasses import dataclass

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
    # Haiku — дешёвая модель для лёгких задач (topic-gate, fix_latex). НЕ для OCR.
    ocr_model: str = "claude-haiku-4-5-20251001"
    # Vision-OCR условия задачи: Sonnet (≈2.6× дороже Haiku, но кратно меньше
    # галлюцинаций на формулах). ≈1₽ за OCR vs 0.4₽ у Haiku. Стоит того —
    # битый condition_text ломает всю цепочку решения, переплачивать на solve
    # в разы дороже потерянной точности.
    vision_ocr_model: str = "claude-sonnet-4-6"

    # === OpenAI — через ProxyAPI (для эмбеддингов и парсинга PDF) ===
    openai_api_key: str
    openai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    embedding_model: str = "text-embedding-3-small"
    parser_model: str = "gpt-4o"

    # === DeepSeek (standard-режим) — через OpenAI-совместимый шлюз ProxyAPI ===
    # Шлюз маршрутизирует по префиксу модели (openrouter/<provider>/<model>).
    # Отличается от openai_base_url (тот — чистый OpenAI passthrough для эмбеддингов).
    deepseek_model: str = "openrouter/deepseek/deepseek-chat-v3.1"
    openai_compat_base_url: str = "https://openai.api.proxyapi.ru/v1"

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

    # === Credit-based pricing (новая модель) ===
    # Вес режимов — сколько кредитов списывается за одно решение.
    standard_cost: int = 1      # DeepSeek v3.1 (текст после Haiku-OCR)
    premium_cost: int = 10      # Sonnet 4.6 + extended thinking (vision)
    trial_credits: int = 5      # начисляется новому юзеру при первом /start

    # === Free-mode (временная промо-модель: всё бесплатно, только DeepSeek) ===
    # Включи `free_mode=True` чтобы выключить весь paywall: фото/текст идут сразу
    # в DeepSeek, без выбора режима, без списания кредитов, без покупок в меню.
    # `free_mode=False` → credit-pricing активен (выбор режима, списания, пакеты).
    # Daily cap per user (Redis-based, UTC-сутки) — защита от абьюза без paywall.
    free_mode: bool = True
    free_daily_cap: int = 30
    # Принимаем только математику/физику. Haiku-гейт классифицирует входящий текст
    # (для фото — после OCR). Используем settings.ocr_model (Haiku).
    topic_gate_enabled: bool = True

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


# === Каталог пакетов кредитов (Telegram Stars) ===

@dataclass(frozen=True)
class CreditPackage:
    key: str        # внутренний идентификатор
    title: str      # отображаемое имя
    credits: int    # сколько кредитов начисляется
    stars: int      # цена в Telegram Stars
    payload: str    # invoice payload (идемпотентность платежа)


CREDIT_PACKAGES: list[CreditPackage] = [
    CreditPackage("sok",      "Sok",      10,  79,   "academ4i_credits_sok"),
    CreditPackage("mini",     "Mini",     25,  149,  "academ4i_credits_mini"),
    CreditPackage("standard", "Standard", 75,  399,  "academ4i_credits_standard"),
    CreditPackage("large",    "Large",    200, 899,  "academ4i_credits_large"),
    CreditPackage("mega",     "Mega",     500, 1990, "academ4i_credits_mega"),
]

PACKAGES_BY_PAYLOAD: dict[str, CreditPackage] = {p.payload: p for p in CREDIT_PACKAGES}
