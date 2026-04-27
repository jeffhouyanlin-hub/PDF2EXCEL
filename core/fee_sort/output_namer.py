"""Generate output filename for Fee Sort Excel."""

from __future__ import annotations

import re


class OutputNamer:
    """Generate standardised Fee Sort output filename."""

    @staticmethod
    def generate(
        bank_name: str,
        account_number: str,
        merged_filename: str,
    ) -> str:
        """Return filename like ``{bank}_{last4}_FeeSort_{start}_{end}.xlsx``.

        Falls back to ``FeeSort_{merged_filename}.xlsx`` when metadata is sparse.
        """
        bank = re.sub(r"\s+", "", bank_name) if bank_name else "Bank"
        last4 = account_number[-4:] if len(account_number) >= 4 else (account_number or "0000")

        # Try to extract date range from merged filename (e.g. "Barclays_1234_20230101-20231231")
        m = re.search(r"(\d{8})-(\d{8})", merged_filename)
        if m:
            start, end = m.group(1), m.group(2)
        else:
            start, end = "start", "end"

        return f"{bank}_{last4}_FeeSort_{start}_{end}.xlsx"
