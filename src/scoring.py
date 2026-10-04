"""Скоринг-матрица. Код, без LLM.

Вход считает `compute_mechanical_scores`: покупатели, кривая, публичная
карточка /coins. Grok в эту сумму не входит.

`compute_scores` остаётся для разбора лога и для опционального вето:
четыре компонента (аудит, нарратив, тайминг, метрики) с весами из конфига.
Отсутствующий агент даёт ноль — сбой не должен поднимать итог.

Веса из конфига нормализуются: если пользователь напишет 0.5/0.5/0.5/0.5,
итог всё равно останется в диапазоне 0..1, а пропорции сохранятся.
"""

from __future__ import annotations

from .models import Analysis, Config, Scores, ScoringWeights

# Компоненты, которых нет (агент не отработал), считаются нулём — не
# средним и не «пропустить компонент». Отсутствие сигнала не аргумент за.
MISSING_COMPONENT = 0.0


def normalized_weights(weights: ScoringWeights) -> dict[str, float]:
    raw = {
        "audit": max(0.0, weights.audit),
        "narrative": max(0.0, weights.narrative),
        "timing": max(0.0, weights.timing),
        "metrics": max(0.0, weights.metrics),
    }
    total = sum(raw.values())
    if total <= 0:
        # Вырожденный конфиг: равные веса лучше деления на ноль.
        return dict.fromkeys(raw, 0.25)
    return {key: value / total for key, value in raw.items()}


# Механический вход: метрики, живые покупатели, кривая. Без Grok.
MECHANICAL_METRICS = 0.45
MECHANICAL_TRACTION = 0.30
MECHANICAL_CURVE = 0.25
# Карточка last_trade без посчитанных кошельков слабее ленты.
INFERRED_TRACTION_HAIRCUT = 0.50


def compute_mechanical_scores(analysis: Analysis, config: Config) -> Scores:
    """Скоринг входа: WS/лента, кривая, публичная карточка. Grok не зовём."""
    token = analysis.token
    metrics = analysis.metrics
    min_buyers = max(1, config.filter.min_unique_buyers)
    inferred = token.buyers_inferred and token.ws_buyers <= 0
    if inferred:
        buyers = token.unique_buyers
        traction = _clamp(buyers / (min_buyers * 2)) * INFERRED_TRACTION_HAIRCUT
    else:
        buyers = token.ws_buyers or metrics.unique_wallets or token.unique_buyers
        traction = _clamp(buyers / (min_buyers * 2))

    min_liq = max(config.market.min_curve_liquidity_sol, 1e-9)
    liq_score = _clamp(metrics.curve_liquidity_sol / (min_liq * 2))
    if metrics.curve_health > 0:
        curve = _clamp(0.5 * liq_score + 0.5 * metrics.curve_health)
    else:
        curve = liq_score

    quality = metrics.quality
    total = (
        MECHANICAL_METRICS * quality
        + MECHANICAL_TRACTION * traction
        + MECHANICAL_CURVE * curve
    )
    return Scores(
        audit=round(traction, 4),
        narrative=round(metrics.social_signals, 4),
        timing=round(curve, 4),
        metrics=round(_clamp(quality), 4),
        total=round(_clamp(total), 4),
    )


def compute_scores(analysis: Analysis, config: Config) -> Scores:
    """Разложенный скоринг по компонентам плюс итог."""
    weights = normalized_weights(config.scoring.weights)

    components = {
        "audit": analysis.audit.score if analysis.audit else MISSING_COMPONENT,
        "narrative": analysis.narrative.score if analysis.narrative else MISSING_COMPONENT,
        "timing": analysis.timing.score if analysis.timing else MISSING_COMPONENT,
        "metrics": analysis.metrics.quality,
    }
    components = {key: _clamp(value) for key, value in components.items()}

    total = sum(components[key] * weights[key] for key in components)

    return Scores(
        audit=round(components["audit"], 4),
        narrative=round(components["narrative"], 4),
        timing=round(components["timing"], 4),
        metrics=round(components["metrics"], 4),
        total=round(_clamp(total), 4),
    )


def passes_threshold(scores: Scores, config: Config) -> tuple[bool, str]:
    """Дотянул ли токен до похода к адверсариальному чекеру."""
    threshold = config.filter.min_total_score
    if scores.total < threshold:
        return False, f"score_below_threshold ({scores.total:.3f} < {threshold:.3f})"
    return True, "ok"


def weakest_component(scores: Scores) -> tuple[str, float]:
    """Самый слабый компонент — уходит в лог как деталь причины пропуска."""
    named = {
        "audit": scores.audit,
        "narrative": scores.narrative,
        "timing": scores.timing,
        "metrics": scores.metrics,
    }
    name = min(named, key=lambda key: named[key])
    return name, named[name]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
