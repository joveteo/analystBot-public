import re

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.]{1,10}$")


def validate_symbol(symbol: str) -> bool:
    if not symbol:
        return False
    return bool(SYMBOL_PATTERN.match(symbol.upper()))


def filter_valid_symbols(symbols: list[str]) -> list[str]:
    return [s.upper() for s in symbols if validate_symbol(s)]
