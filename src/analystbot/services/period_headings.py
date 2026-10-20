FREQUENCY_LABELS = {
    "d": "Daily",
    "w": "Weekly",
    "m": "Monthly",
    "q": "Quarterly",
}


def frequency_label(frequency: str) -> str:
    return FREQUENCY_LABELS.get(frequency, frequency)


def format_observation_value(value: float, suffix: str) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}{suffix}"
    return f"{value:.2f}{suffix}"


def format_period_heading(title: str, frequency: str, date_str: str) -> str:
    return f"{title}, {frequency_label(frequency)}, {date_str}"


def format_combined_heading(title: str, frequencies: list[str], date_str: str) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for frequency in frequencies:
        label = frequency_label(frequency)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    if not labels:
        return f"{title}, {date_str}"
    return f"{title}, {' / '.join(labels)}, {date_str}"
