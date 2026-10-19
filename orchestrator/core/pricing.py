"""Цены моделей, стоимость работы и метрики SWE-бенчмарков.

Цены берутся из provod.ai (`GET /v1/models` → поле `pricing`) в USD за 1 токен,
хранятся в БД и периодически обновляются. Стоимость работы задачи считается как
токены (из `llm_metrics` OpenHands) × сохранённые цены модели. Для показа в ₽
используется курс рубля (настраиваемый, по умолчанию `DEFAULT_RUB_PER_USD`).

Метрики SWE-benчмарков — справочные значения из отчёта «provod.ai → Flutter:
анализ цена/качество» (SWE-bench Verified, mini-SWE-agent).
"""

from __future__ import annotations

import logging
from typing import Optional

from orchestrator.core.state import StateStore

logger = logging.getLogger(__name__)

# Курс ₽ за $1 для показа цен. Переопределяется настройкой `rub_per_usd`.
DEFAULT_RUB_PER_USD = 82.8

# SWE-bench Verified (%) из отчёта цена/качество. Значения «н/д» → None.
BENCHMARKS_SEED: dict[str, tuple[Optional[float], str]] = {
    "glm-5.3-flash": (92.0, "👑 лидер цена/качество, 1M контекст, vision"),
    "gpt-5.6-luna": (93.0, "премиум-качество почти по цене flash"),
    "mimo-v2.6-pro": (71.0, "дёшево, но слабее по логике"),
    "mimo-v2.5-flash": (71.0, "дёшево, но слабее по логике"),
    "deepseek-v4.1-flash": (60.0, "только текст, дешёвый fallback"),
    "claude-haiku-5.5": (None, "1M контекст, быстрый бюджетник Anthropic"),
    "deepseek-v4-pro-0813": (96.4, "🥈 лучший полуфлагман, #2 мира"),
    "deepseek-v4-pro": (96.4, "полуфлагман"),
    "glm-5.3": (95.4, "старший брат flash, 1M контекст"),
    "glm-5.3-flashx": (90.0, "flash + скорость, дороже"),
    "glm-5.3-prime": (95.4, "вариант glm-5.3"),
    "gemini-3.7-flash": (80.8, "мультимодал (аудио/видео)"),
    "deepseek-v4-flash-0731": (88.8, "золотая середина DeepSeek"),
    "claude-opus-5": (97.0, "#1 SWE-bench, критичная логика"),
    "claude-opus-5.5": (None, "преемник opus-5, дешевле"),
    "gpt-5.6-sol": (96.2, "сильный агент, дорогой выход"),
    "gpt-5.6-terra": (95.4, "«Sol за полцены»"),
    "claude-fable-5.1": (95.0, "самый дорогой, для сложных задач"),
    "gpt-6-astra": (None, "новейший флагман OpenAI"),
    "kimi-k3": (93.4, "хорош, но дороже GLM/DeepSeek"),
    "grok-4.6": (95.6, "недооценён: дешёвый выход, топ-5 качества"),
    "claude-sonnet-4.6": (77.4, "переплата ×2.5 за уровень GLM 5.3 Flash"),
    "qwen3.7-max": (68.8, "плохое соотношение цена/качество"),
}


def rub_per_usd(state: StateStore) -> float:
    raw = state.get_setting("rub_per_usd")
    try:
        return float(raw) if raw else DEFAULT_RUB_PER_USD
    except (TypeError, ValueError):
        return DEFAULT_RUB_PER_USD


def seed_benchmarks(state: StateStore) -> int:
    """Заполнить метрики SWE-бенчмарков из отчёта (идемпотентно)."""
    n = 0
    for model, (swe, note) in BENCHMARKS_SEED.items():
        state.upsert_benchmark(model, swe, note)
        n += 1
    logger.info("[pricing] seeded %d benchmarks", n)
    return n


async def refresh_prices(state: StateStore, openhands) -> int:
    """Обновить цены моделей из provod.ai (`/v1/models` → `pricing`).

    Цены в API — USD за 1 токен. Сохраняем как есть; для ₽ конвертируем при
    показе. Возвращает число обновлённых моделей.
    """
    res = await openhands.list_models_raw()
    if res.get("status") != "ok":
        logger.warning("[pricing] refresh_prices failed: %s", res.get("error"))
        return 0
    n = 0
    for m in res.get("models", []):
        mid = m.get("id", "").split("/", 1)[-1]
        pricing = m.get("pricing") or {}
        if not mid or not pricing:
            continue
        try:
            prompt = float(pricing.get("prompt", 0) or 0)
            completion = float(pricing.get("completion", 0) or 0)
            cache_read = float(pricing.get("input_cache_read", prompt) or 0)
        except (TypeError, ValueError):
            continue
        state.upsert_price(mid, prompt, completion, cache_read)
        n += 1
    logger.info("[pricing] refreshed prices for %d models", n)
    return n


def compute_cost(state: StateStore, model: str,
                 prompt_tokens: int, completion_tokens: int,
                 cache_read_tokens: int = 0) -> tuple[float, float]:
    """Стоимость работы модели. Возвращает (рубли, доллары).

    Учитываем кеш: чтение из кеша дешевле обычного входа. Не-кешевой вход =
    max(0, prompt_tokens - cache_read_tokens).
    """
    bare = (model or "").split("/", 1)[-1]
    price = state.get_price(bare) or {}
    prompt_usd = price.get("prompt_usd", 0.0) or 0.0
    completion_usd = price.get("completion_usd", 0.0) or 0.0
    cache_read_usd = price.get("cache_read_usd", prompt_usd) or prompt_usd

    non_cache = max(0, prompt_tokens - cache_read_tokens)
    cost_usd = (
        non_cache * prompt_usd
        + cache_read_tokens * cache_read_usd
        + completion_tokens * completion_usd
    )
    cost_rub = cost_usd * rub_per_usd(state)
    return round(cost_rub, 2), round(cost_usd, 4)
