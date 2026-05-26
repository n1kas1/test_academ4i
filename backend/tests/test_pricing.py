"""Unit-тесты credit-модели (без реальной БД — get_session мокается)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings, CREDIT_PACKAGES, PACKAGES_BY_PAYLOAD
from app.ratelimit import CreditStatus, consume_credits, get_credit_status


# ─────────────────────── каталог пакетов / конфиг ───────────────────────

def test_package_catalog():
    assert len(CREDIT_PACKAGES) == 5
    by_key = {p.key: (p.credits, p.stars) for p in CREDIT_PACKAGES}
    assert by_key == {
        "sok": (10, 79),
        "mini": (25, 149),
        "standard": (75, 399),
        "large": (200, 899),
        "mega": (500, 1990),
    }
    # payload-мапа полная и уникальная
    assert len(PACKAGES_BY_PAYLOAD) == 5
    assert all(p.payload in PACKAGES_BY_PAYLOAD for p in CREDIT_PACKAGES)


def test_costs_and_trial():
    assert settings.standard_cost == 1
    assert settings.premium_cost == 10
    assert settings.trial_credits == 5


# ─────────────────────── CreditStatus ───────────────────────

def test_can_afford():
    s = CreditStatus(credits=5)
    assert s.can_afford(1)
    assert s.can_afford(5)
    assert not s.can_afford(10)


def test_admin_can_afford_anything():
    assert CreditStatus(is_admin=True).can_afford(10_000)


# ─────────────────────── consume_credits ───────────────────────

def _mock_get_session(execute_result):
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _cm():
        yield session

    return _cm, session


async def test_consume_admin_bypass():
    # Админ (settings.admin_usernames = "manag31") → True, БД не трогается.
    assert await consume_credits(123, 10, username="manag31") is True


async def test_consume_success(monkeypatch):
    result = MagicMock()
    result.first.return_value = (4,)  # credits после списания
    cm, session = _mock_get_session(result)
    monkeypatch.setattr("app.ratelimit.get_session", cm)

    ok = await consume_credits(123, 1, username="student")
    assert ok is True
    # SQL получил верные параметры
    params = session.execute.call_args.args[1]
    assert params == {"tg": 123, "cost": 1}
    session.commit.assert_awaited_once()


async def test_consume_insufficient(monkeypatch):
    result = MagicMock()
    result.first.return_value = None  # WHERE credits >= cost не сматчилось
    cm, _ = _mock_get_session(result)
    monkeypatch.setattr("app.ratelimit.get_session", cm)

    assert await consume_credits(123, 10, username="student") is False


# ─────────────────────── get_credit_status ───────────────────────

async def test_get_status_admin():
    st = await get_credit_status(123, username="manag31")
    assert st.is_admin and st.credits == 0


async def test_get_status_user(monkeypatch):
    user = MagicMock()
    user.credits = 7
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    cm, _ = _mock_get_session(result)
    monkeypatch.setattr("app.ratelimit.get_session", cm)

    st = await get_credit_status(123, username="student")
    assert st.credits == 7 and not st.is_admin


# ─────────────────────── handlers helpers ───────────────────────

def test_handler_mode_cost_and_caption():
    from app.bot import handlers as H
    assert H._mode_cost("standard") == 1
    assert H._mode_cost("premium") == 10
    assert H._mode_label("premium") == "💎 Премиум"

    class _S:
        is_admin = False
        credits = 20
    cap = H._caption(_S(), "premium", 10)
    assert "Премиум" in cap and "10" in cap


# ─────────────────────── pipeline: роутинг режима ───────────────────────

def _patch_pipeline(monkeypatch, condition="Найдите производную $y=\\ln x$.", task_ids=None):
    """Мокаем все внешние зависимости pipeline, возвращаем (ds, cv, find)."""
    import app.ai.pipeline as P
    monkeypatch.setattr(P, "prepare_image", lambda b: ("b64", "image/jpeg"))
    monkeypatch.setattr(P, "extract_condition_text",
                        AsyncMock(return_value=(condition, task_ids or [])))
    monkeypatch.setattr(P, "embed_text", AsyncMock(return_value=[0.0] * 1536))
    find = AsyncMock(return_value=[])
    monkeypatch.setattr(P, "find_similar_solutions", find)
    monkeypatch.setattr(P, "save_solution", AsyncMock())
    monkeypatch.setattr(P, "render_solution",
                        AsyncMock(return_value={"preview_png": b"x", "pdf": b"y"}))
    ds = AsyncMock(return_value="\\hd{Решение} deepseek")
    cv = AsyncMock(return_value="\\hd{Решение} sonnet")
    monkeypatch.setattr(P, "solve_with_deepseek", ds)
    monkeypatch.setattr(P, "solve_with_claude_vision", cv)
    return P, ds, cv, find


async def test_pipeline_standard_routes_to_deepseek(monkeypatch):
    P, ds, cv, find = _patch_pipeline(monkeypatch)
    res = await P.solve_task_from_photo(b"img", user_id=1, mode="standard")
    assert "latex" in res
    ds.assert_awaited_once()
    cv.assert_not_awaited()
    # standard использует кэш → find_similar_solutions вызван дважды (RAG + cache)
    assert find.await_count == 2


async def test_pipeline_premium_routes_to_sonnet_with_thinking(monkeypatch):
    P, ds, cv, find = _patch_pipeline(monkeypatch)
    res = await P.solve_task_from_photo(b"img", user_id=1, mode="premium")
    assert "latex" in res
    cv.assert_awaited_once()
    ds.assert_not_awaited()
    # premium всегда с extended thinking
    assert cv.await_args.kwargs.get("use_thinking") is True
    # premium мимо кэша → find_similar_solutions только для RAG (1 раз)
    assert find.await_count == 1


async def test_pipeline_standard_ocr_failed(monkeypatch):
    P, ds, cv, _ = _patch_pipeline(monkeypatch, condition="")  # OCR пуст
    res = await P.solve_task_from_photo(b"img", user_id=1, mode="standard")
    assert res == {"ocr_failed": True}
    ds.assert_not_awaited()
    cv.assert_not_awaited()


async def test_pipeline_multi_task_needs_choice(monkeypatch):
    P, ds, cv, _ = _patch_pipeline(monkeypatch, task_ids=["2851", "2852"])
    res = await P.solve_task_from_photo(b"img", user_id=1, user_hint="", mode="standard")
    assert res.get("needs_choice") is True
    assert res["task_ids"] == ["2851", "2852"]
    ds.assert_not_awaited()
    cv.assert_not_awaited()
