from collections.abc import Sequence


def compute_supertrend(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    atr_period: int = 10,
    factor: float = 3.0,
) -> tuple[list[float | None], list[int | None]]:
    length = len(close)
    if len(high) != length or len(low) != length:
        raise ValueError("high, low, and close series must have the same length")
    if atr_period < 1:
        raise ValueError("atr_period must be at least 1")
    if factor <= 0:
        raise ValueError("factor must be greater than 0")
    if length == 0:
        return [], []

    true_range = _calculate_true_range(high, low, close)
    atr = _calculate_rma(true_range, atr_period)

    supertrend: list[float | None] = [None] * length
    direction: list[int | None] = [None] * length
    final_upper: list[float | None] = [None] * length
    final_lower: list[float | None] = [None] * length

    for idx in range(length):
        current_atr = atr[idx]
        if current_atr is None:
            continue

        hl2 = (high[idx] + low[idx]) / 2
        upper_basic = hl2 + (factor * current_atr)
        lower_basic = hl2 - (factor * current_atr)

        if idx == 0 or final_upper[idx - 1] is None or final_lower[idx - 1] is None:
            final_upper[idx] = upper_basic
            final_lower[idx] = lower_basic
            direction[idx] = 1
            supertrend[idx] = final_upper[idx]
            continue

        prev_upper = final_upper[idx - 1]
        prev_lower = final_lower[idx - 1]
        prev_close = close[idx - 1]
        prev_direction = direction[idx - 1]

        assert prev_upper is not None
        assert prev_lower is not None
        assert prev_direction is not None

        final_upper[idx] = (
            upper_basic
            if upper_basic < prev_upper or prev_close > prev_upper
            else prev_upper
        )
        final_lower[idx] = (
            lower_basic
            if lower_basic > prev_lower or prev_close < prev_lower
            else prev_lower
        )
        current_upper = final_upper[idx]
        current_lower = final_lower[idx]
        assert current_upper is not None
        assert current_lower is not None

        if prev_direction > 0:
            if close[idx] > current_upper:
                direction[idx] = -1
                supertrend[idx] = current_lower
            else:
                direction[idx] = 1
                supertrend[idx] = current_upper
        else:
            if close[idx] < current_lower:
                direction[idx] = 1
                supertrend[idx] = current_upper
            else:
                direction[idx] = -1
                supertrend[idx] = current_lower

    return supertrend, direction


def _calculate_true_range(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> list[float]:
    true_range: list[float] = []
    for idx, (current_high, current_low) in enumerate(zip(high, low, strict=True)):
        if idx == 0:
            true_range.append(current_high - current_low)
            continue

        prev_close = close[idx - 1]
        true_range.append(
            max(
                current_high - current_low,
                abs(current_high - prev_close),
                abs(current_low - prev_close),
            )
        )

    return true_range


def _calculate_rma(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed_index = period - 1
    seed = sum(values[:period]) / period
    result[seed_index] = seed

    for idx in range(seed_index + 1, len(values)):
        prev = result[idx - 1]
        assert prev is not None
        result[idx] = ((prev * (period - 1)) + values[idx]) / period

    return result
