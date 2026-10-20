import html
from datetime import UTC, date, datetime


def format_next_release_date(release_date: str, *, as_of: date | None = None) -> str:
    today = as_of or datetime.now(UTC).date()
    days = (date.fromisoformat(release_date) - today).days
    unit = "Day" if days == 1 else "Days"
    return f"{release_date}, {days} {unit}"


def format_image_caption(
    title: str,
    date_str: str,
    lines: list[tuple[str, str]] | None = None,
    *,
    description: str | None = None,
    next_releases: list[tuple[str | None, str]] | None = None,
    notes: list[str] | None = None,
    as_of: date | None = None,
) -> str:
    parts = [title]
    if description:
        parts.append(description)
    for note in notes or []:
        if note:
            parts.append(note)
    if date_str and date_str not in title:
        parts.append(f"Date: {date_str}")
    for label, release_date in next_releases or []:
        dated = format_next_release_date(release_date, as_of=as_of)
        if label:
            parts.append(f"Next release date ({label}): {dated}")
        else:
            parts.append(f"Next release date: {dated}")
    for key, value in lines or []:
        parts.append(f"{key}: {value}")
    return "\n".join(parts)


class TableFormatter:
    SYMBOL_HEADER = "Symbol"
    PRICE_HEADER = "Last"
    SMA_PERCENT_HEADER = "SMA%"
    PRICE_MAX_CHARS = 5
    INDICATOR_MAX_CHARS = 5
    SMA_PERCENT_MAX_CHARS = 7

    @staticmethod
    def get_symbol_width(data: list[dict]) -> int:
        max_symbol_length = max(
            (len(record.get("symbol", "")) for record in data),
            default=0,
        )
        return max(len(TableFormatter.SYMBOL_HEADER), max_symbol_length)

    @staticmethod
    def format_symbol(symbol: str, width: int) -> str:
        return symbol.ljust(width)

    @staticmethod
    def format_number(value: float, limit: int, max_decimal_places: int = 2) -> str:
        for decimal_places in range(max_decimal_places, -1, -1):
            fmt = f".{decimal_places}f"
            text = f"{value:{fmt}}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            if len(text) <= limit:
                return text.ljust(limit)
        return str(int(value))[:limit].ljust(limit)

    @staticmethod
    def format_price(price: float) -> str:
        return TableFormatter.format_number(price, TableFormatter.PRICE_MAX_CHARS)

    @staticmethod
    def format_indicator(value: float) -> str:
        return TableFormatter.format_number(
            value, TableFormatter.INDICATOR_MAX_CHARS, max_decimal_places=1
        )

    @staticmethod
    def format_sma_percentage(value: float | None) -> str:
        if value is None:
            return "—".ljust(TableFormatter.SMA_PERCENT_MAX_CHARS)
        number = TableFormatter.format_number(
            value, TableFormatter.SMA_PERCENT_MAX_CHARS - 1, max_decimal_places=1
        ).rstrip()
        return f"{number}%".ljust(TableFormatter.SMA_PERCENT_MAX_CHARS)

    @staticmethod
    def format_header(symbol_width: int, indicator_column: str) -> str:
        return (
            f"{TableFormatter.SYMBOL_HEADER:<{symbol_width}} "
            f"{TableFormatter.PRICE_HEADER:<{TableFormatter.PRICE_MAX_CHARS}} "
            f"{indicator_column:<{TableFormatter.INDICATOR_MAX_CHARS}} "
            f"{TableFormatter.SMA_PERCENT_HEADER:<{TableFormatter.SMA_PERCENT_MAX_CHARS}}"
        )


class MessageGenerator:
    MAIN_EMOJI = "💡"
    BUY_EMOJI = "🟢"
    SELL_EMOJI = "🔴"
    MAX_MESSAGE_LENGTH = 4000

    @staticmethod
    def _get_signal_config(signal_type: str) -> dict:
        if signal_type == "BTD":
            return {
                "emoji": MessageGenerator.BUY_EMOJI,
                "heading": "Buy Tip",
                "description": "Tickers have significantly dipped in price based on the 22-period VixFix reading.",
                "column": "B022",
                "indicator_key": "btd_22",
            }
        else:
            return {
                "emoji": MessageGenerator.SELL_EMOJI,
                "heading": "Sell Tip",
                "description": "Tickers show overbought conditions based on the 22-period VixFix reading.",
                "column": "S022",
                "indicator_key": "str_22",
            }

    @staticmethod
    def _format_table_row(record: dict, indicator_key: str, symbol_width: int) -> str:
        symbol = html.escape(
            TableFormatter.format_symbol(record["symbol"], symbol_width)
        )
        price = html.escape(
            TableFormatter.format_price(record.get("close_price", 0) or 0)
        )
        indicator = html.escape(
            TableFormatter.format_indicator(record.get(indicator_key, 0) or 0)
        )
        sma_percentage = html.escape(
            TableFormatter.format_sma_percentage(record.get("pct_from_sma_200"))
        )
        return f"{symbol} {price} {indicator} {sma_percentage}"

    @staticmethod
    def _build_table_rows(
        data: list[dict], indicator_key: str, symbol_width: int
    ) -> list[str]:
        return [
            MessageGenerator._format_table_row(record, indicator_key, symbol_width)
            for record in data
        ]

    @staticmethod
    def build_table_section(data: list[dict], signal_type: str) -> str:
        config = MessageGenerator._get_signal_config(signal_type)
        emoji = config["emoji"]
        heading = config["heading"]
        column = config["column"]
        indicator_key = config["indicator_key"]

        if not data:
            return f"{emoji} <b>{heading}</b>\n<i>No tickers found.</i>\n"

        symbol_width = TableFormatter.get_symbol_width(data)
        lines = [
            f"{emoji} <b>{heading}</b>",
            TableFormatter.format_header(symbol_width, column),
        ]

        lines.extend(
            MessageGenerator._build_table_rows(data, indicator_key, symbol_width)
        )

        return "\n".join(lines)

    @staticmethod
    def _get_header() -> str:
        current_date = datetime.now().strftime("%Y-%m-%d")
        return f"{MessageGenerator.MAIN_EMOJI} <b>William's VixFix Indicator ({current_date})</b>"

    @staticmethod
    def _chunk_message_with_table(
        header_parts: list[str],
        table_rows: list[str],
        table_header: str,
        max_length: int | None = None,
    ) -> list[str]:
        max_len = max_length or MessageGenerator.MAX_MESSAGE_LENGTH
        chunks = []

        header_text = "\n".join(header_parts)
        table_start = "<pre>\n" + table_header
        table_end = "\n</pre>"

        first_chunk_overhead = (
            len(header_text) + len("\n") + len(table_start) + len(table_end)
        )
        continuation_overhead = len(table_start) + len(table_end)

        current_rows: list[str] = []
        current_length = first_chunk_overhead
        is_first_chunk = True

        for row in table_rows:
            row_length = len(row) + 1

            if current_length + row_length > max_len and current_rows:
                if is_first_chunk:
                    chunk = (
                        header_text
                        + "\n"
                        + table_start
                        + "\n"
                        + "\n".join(current_rows)
                        + table_end
                    )
                    is_first_chunk = False
                else:
                    chunk = table_start + "\n" + "\n".join(current_rows) + table_end
                chunks.append(chunk)

                current_rows = [row]
                current_length = continuation_overhead + row_length
            else:
                current_rows.append(row)
                current_length += row_length

        if current_rows:
            if is_first_chunk:
                chunk = (
                    header_text
                    + "\n"
                    + table_start
                    + "\n"
                    + "\n".join(current_rows)
                    + table_end
                )
            else:
                chunk = table_start + "\n" + "\n".join(current_rows) + table_end
            chunks.append(chunk)
        elif is_first_chunk:
            chunks.append(header_text)

        return chunks

    @staticmethod
    def generate_buy_message(btd_data: list[dict]) -> list[str]:
        config = MessageGenerator._get_signal_config("BTD")
        header = MessageGenerator._get_header()

        header_parts = [
            header,
            "",
            f"{config['emoji']} <b>{config['heading']}</b>",
            html.escape(config["description"]),
        ]

        if not btd_data:
            message = "\n".join(header_parts[:-1])
            message += "\n<i>No tickers found.</i>"
            return [message]

        symbol_width = TableFormatter.get_symbol_width(btd_data)
        table_rows = MessageGenerator._build_table_rows(
            btd_data, config["indicator_key"], symbol_width
        )
        table_header = TableFormatter.format_header(symbol_width, config["column"])

        return MessageGenerator._chunk_message_with_table(
            header_parts, table_rows, table_header
        )

    @staticmethod
    def generate_sell_message(str_data: list[dict]) -> list[str]:
        config = MessageGenerator._get_signal_config("STR")

        header_parts = [
            f"{config['emoji']} <b>{config['heading']}</b>",
            html.escape(config["description"]),
        ]

        if not str_data:
            message = f"{config['emoji']} <b>{config['heading']}</b>\n<i>No tickers found.</i>"
            return [message]

        symbol_width = TableFormatter.get_symbol_width(str_data)
        table_rows = MessageGenerator._build_table_rows(
            str_data, config["indicator_key"], symbol_width
        )
        table_header = TableFormatter.format_header(symbol_width, config["column"])

        return MessageGenerator._chunk_message_with_table(
            header_parts, table_rows, table_header
        )
