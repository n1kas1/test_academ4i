"""Герметичная среда для unit-тестов: dummy-env + заглушка render.

Реальная БД/сеть не используются — всё мокается в тестах. Здесь только готовим
импортируемость app.* без .env и без mkdir('/app/render_cache') в latex_to_png.
"""
import os
import sys
import types

# Dummy-env (env-vars приоритетнее .env у pydantic-settings) — Settings() сконструируется.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://t:t@localhost:5432/test")

# Заглушка render: app.ai.pipeline импортит app.render.latex_to_png на верхнем уровне,
# а тот при импорте делает mkdir('/app/render_cache'). В тестах render не нужен.
_stub = types.ModuleType("app.render.latex_to_png")


async def _noop_render(*_a, **_k):
    return {"preview_png": None, "pdf": None}


_stub.render_solution = _noop_render
sys.modules.setdefault("app.render.latex_to_png", _stub)
