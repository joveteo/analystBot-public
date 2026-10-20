from __future__ import annotations

import unittest

from analystbot.core.validation import filter_valid_symbols, validate_symbol


class SymbolValidationTests(unittest.TestCase):
    def test_accepts_letters_digits_and_dots_up_to_ten_chars(self) -> None:
        self.assertTrue(validate_symbol("AAPL"))
        self.assertTrue(validate_symbol("brka"))
        self.assertTrue(validate_symbol("BRK.B"))
        self.assertTrue(validate_symbol("123"))

    def test_rejects_empty_too_long_or_special_characters(self) -> None:
        self.assertFalse(validate_symbol(""))
        self.assertFalse(validate_symbol("TOOLONGNAME"))
        self.assertFalse(validate_symbol("AA-PL"))
        self.assertFalse(validate_symbol("AA PL"))

    def test_filter_uppercases_and_drops_invalid(self) -> None:
        self.assertEqual(
            filter_valid_symbols(["aapl", "bad!", "", "MSFT", "toolongname"]),
            ["AAPL", "MSFT"],
        )


if __name__ == "__main__":
    unittest.main()
