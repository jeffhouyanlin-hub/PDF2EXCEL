from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber


@dataclass
class RowPosition:
    """PDF 中一行数据的坐标信息。"""
    page_index: int        # 0-based 页码
    y_top: float           # 行顶部 y 坐标（PDF points）
    y_bottom: float        # 行底部 y 坐标
    page_height: float     # 该页总高度（用于算百分比）


@dataclass
class StatementInfo:
    bank_name: str = ""
    account_number: str = ""
    period_from: str = ""
    period_to: str = ""
    opening_balance: str = ""
    closing_balance: str = ""
    total_deposits: str = ""
    total_withdrawals: str = ""
    transactions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())


_BANK_PATTERNS: list[tuple[str, str]] = [
    (r"Royal\s*Bank|RBC\b", "RBC Royal Bank"),
    (r"TD\s*Canada\s*Trust|TD\s*Bank", "TD Canada Trust"),
    (r"Bank\s*of\s*Montreal|BMO\b", "BMO Bank of Montreal"),
    (r"Scotiabank|Scotia\s*Bank|BNS\b", "Scotiabank"),
    (r"CIBC\b|Canadian\s*Imperial", "CIBC"),
    (r"HSBC\b", "HSBC"),
    (r"National\s*Bank|Banque\s*Nationale", "National Bank"),
    (r"Desjardins", "Desjardins"),
    (r"Tangerine", "Tangerine"),
    (r"Simplii", "Simplii Financial"),
    (r"Chase\b|JPMorgan", "Chase"),
    (r"Bank\s*of\s*America|BofA\b", "Bank of America"),
    (r"Wells\s*Fargo", "Wells Fargo"),
    (r"Citibank|Citi\b", "Citibank"),
    (r"Capital\s*One", "Capital One"),
]


_Y_TOLERANCE = 4.0  # words within this y-distance are on the same line

# Header keywords → column names (match with or without "($)" suffix)
_HEADER_KEYWORDS = {
    "Date": "date",
    "Description": "description",
    "Withdrawals": "withdrawals",
    "Deposits": "deposits",
    "Balance": "balance",
}


class BankStatementParser:
    """基于坐标的银行账单解析器（适用于 RBC 等无框线 PDF）。"""

    def parse(self, pdf_path: str | Path) -> StatementInfo:
        pdf_path = Path(pdf_path)
        info = StatementInfo()
        all_rows: list[dict] = []

        with pdfplumber.open(pdf_path) as pdf:
            # Extract summary (default x_tolerance for concatenated keyword matching)
            full_text = "\n".join(p.extract_text(layout=True) or "" for p in pdf.pages)
            self._parse_summary(full_text, info)

            # Extract transactions from each page using word coordinates
            for page in pdf.pages:
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                rows = self._extract_transaction_rows(words)
                all_rows.extend(rows)

        info.transactions = self._build_dataframe(all_rows)
        return info

    def get_row_positions(self, pdf_path: str | Path) -> tuple[list[str], list[RowPosition]]:
        """返回 (原始表头列表, 合并后的行坐标列表)。

        表头列表: 从 PDF header 行直接读取的原始文字
        行坐标列表: 与 _build_dataframe() 合并结果对齐，len(positions) == len(DataFrame)
        """
        pdf_path = Path(pdf_path)
        headers: list[str] = []
        raw_entries: list[tuple[RowPosition, dict]] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)

                col_bounds, header_y = self._detect_columns(words)
                if not col_bounds:
                    continue

                # Dynamic left boundary from header detection
                left_boundary = min(x0 for x0, _ in col_bounds.values()) - 5
                words = [w for w in words if w["x0"] >= left_boundary]

                # 提取原始表头文字（仅从第一个有效页获取）
                if not headers:
                    header_words = [
                        w for w in words
                        if abs(w["top"] - header_y) <= _Y_TOLERANCE
                    ]
                    header_words.sort(key=lambda w: w["x0"])
                    headers = [w["text"] for w in header_words]

                # 筛选 header 以下的 words
                tx_words = [w for w in words if w["top"] > header_y + 5]
                if not tx_words:
                    continue

                col_centers = self._compute_col_centers(col_bounds)
                lines = self._group_by_y(tx_words)
                for line_words in lines:
                    row = self._classify_words(line_words, col_bounds, col_centers)
                    if not row:
                        continue
                    y_top = min(w["top"] for w in line_words)
                    y_bottom = max(w["bottom"] for w in line_words)
                    pos = RowPosition(page_idx, y_top, y_bottom, page.height)
                    raw_entries.append((pos, row))

        # --- 同 _build_dataframe 一致的过滤/合并流水线 ---

        # 1. 截断: Closing Balance 之后丢弃
        truncated: list[tuple[RowPosition, dict]] = []
        for pos, row in raw_entries:
            truncated.append((pos, row))
            if re.search(r"Closing\s*Balance", row.get("Description", ""), re.I):
                break

        # 2. 噪声过滤
        noise_re = re.compile(
            r"(\d+\s*of\s*\d+|RBPDA|\d{7}-\d{3}_|0050750|^\d{4}$)", re.I,
        )
        cleaned: list[tuple[RowPosition, dict]] = []
        for pos, row in truncated:
            all_text = " ".join(v for v in row.values() if v).strip()
            if noise_re.search(all_text):
                continue
            desc = row.get("Description", "").strip()
            has_amounts = (
                row["Withdrawals"].strip()
                or row["Deposits"].strip()
                or row["Balance"].strip()
            )
            if not desc and not has_amounts:
                continue
            cleaned.append((pos, row))

        # 3. 合并多行记录（与 _build_dataframe 完全一致的逻辑）
        merged = self._merge_positions_by_content(cleaned)

        return headers, merged

    @staticmethod
    def _merge_positions_by_content(
        entries: list[tuple[RowPosition, dict]],
    ) -> list[RowPosition]:
        """用与 _build_dataframe 相同的逻辑合并续行位置。"""
        merged: list[tuple[RowPosition, dict]] = []
        for pos, row in entries:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            prev_has_amounts = bool(
                merged
                and (
                    merged[-1][1]["Withdrawals"].strip()
                    or merged[-1][1]["Deposits"].strip()
                )
            )
            if not has_date and has_desc and merged and not prev_has_amounts:
                # 续行: 同步更新 row dict（与 _build_dataframe 完全一致）
                prev_pos, prev_row = merged[-1]
                prev_row["Description"] += " " + row["Description"].strip()
                for col in ("Withdrawals", "Deposits", "Balance"):
                    if row[col].strip() and not prev_row[col].strip():
                        prev_row[col] = row[col]
                # 扩展 y_bottom
                if pos.page_index == prev_pos.page_index:
                    merged[-1] = (
                        RowPosition(
                            prev_pos.page_index,
                            prev_pos.y_top,
                            max(prev_pos.y_bottom, pos.y_bottom),
                            prev_pos.page_height,
                        ),
                        prev_row,
                    )
                # 跨页续行极少见，保持前一条不变
            else:
                merged.append((pos, dict(row)))  # 复制 dict 防止突变
        return [p for p, _ in merged]

    def get_summary_positions(
        self,
        pdf_source: str | Path | bytes,
        summary_items: list[dict],
    ) -> list[RowPosition | None]:
        """根据 Summary Excel 行的 Value 在 PDF 中搜索匹配位置。

        pdf_source: 文件路径或 PDF 字节数据
        summary_items: [{"item": "Opening Balance", "value": "1,234.56"}, ...]
        返回与输入等长的列表，匹配到则为 RowPosition，未匹配为 None。
        主索引: value 值匹配；辅助验证: item 文字。
        """
        import io as _io

        if isinstance(pdf_source, bytes):
            pdf_input = _io.BytesIO(pdf_source)
        else:
            pdf_input = Path(pdf_source)
        lines_index: list[dict] = []
        tx_header_page = -1
        tx_header_y = 0.0

        with pdfplumber.open(pdf_input) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                words = page.extract_words(keep_blank_chars=True, x_tolerance=1)
                if not words:
                    continue

                # Detect transaction header to identify summary area boundary
                if tx_header_page < 0:
                    _, hy = self._detect_columns(words)
                    if hy > 0:
                        tx_header_page = page_idx
                        tx_header_y = hy

                grouped = self._group_by_y(words)
                for line_words in grouped:
                    text = " ".join(w["text"] for w in line_words)
                    y_top = min(w["top"] for w in line_words)
                    y_bottom = max(w["bottom"] for w in line_words)
                    # Summary area = above transaction header on its page
                    is_summary = (
                        tx_header_page < 0 and page_idx == 0  # 找不到表头→第0页为summary
                    ) or (
                        tx_header_page >= 0 and (
                            page_idx < tx_header_page
                            or (page_idx == tx_header_page and y_top < tx_header_y)
                        )
                    )
                    lines_index.append({
                        "text": text,
                        "y_top": y_top,
                        "y_bottom": y_bottom,
                        "page_idx": page_idx,
                        "page_height": page.height,
                        "is_summary": is_summary,
                    })

        results: list[RowPosition | None] = []
        for item in summary_items:
            value = str(item.get("value", "")).strip()
            if not value:
                results.append(None)
                continue
            pos = self._search_value_in_lines(
                value, str(item.get("item", "")), lines_index,
            )
            results.append(pos)
        return results

    @staticmethod
    def _search_value_in_lines(
        value: str, item_name: str, lines_index: list[dict],
    ) -> RowPosition | None:
        """在 PDF 行中搜索 value，优先 summary 区域。"""
        clean_value = value.replace(",", "").replace("$", "").strip()

        for prefer_summary in (True, False):
            candidates = []
            for line in lines_index:
                if prefer_summary and not line["is_summary"]:
                    continue
                line_text = line["text"]
                clean_line = line_text.replace(",", "").replace("$", "")
                if value in line_text or clean_value in clean_line:
                    candidates.append(line)

            if not candidates:
                continue

            if len(candidates) == 1:
                c = candidates[0]
                return RowPosition(c["page_idx"], c["y_top"],
                                   c["y_bottom"], c["page_height"])

            # Multiple candidates — disambiguate with item_name
            item_parts = [p.lower() for p in item_name.split() if len(p) > 2]
            for c in candidates:
                line_lower = c["text"].lower()
                if any(part in line_lower for part in item_parts):
                    return RowPosition(c["page_idx"], c["y_top"],
                                       c["y_bottom"], c["page_height"])

            # Fallback: first candidate
            c = candidates[0]
            return RowPosition(c["page_idx"], c["y_top"],
                               c["y_bottom"], c["page_height"])
        return None

    @staticmethod
    def _parse_summary(text: str, info: StatementInfo) -> None:
        for pattern, name in _BANK_PATTERNS:
            if re.search(pattern, text, re.I):
                info.bank_name = name
                break
        if m := re.search(r"accountnumber:\s*([\d-]+)", text, re.I):
            info.account_number = m.group(1)
        if m := re.search(r"From\s*(.+?)\s*to\s*(.+?)$", text, re.M | re.I):
            info.period_from = _add_spaces(m.group(1).replace("\n", " ").strip())
            info.period_to = _add_spaces(m.group(2).strip())
        if m := re.search(r"openingbalance\s+\$?([\d,.\-]+)", text, re.I):
            info.opening_balance = m.group(1)
        if m := re.search(r"closingbalance.*?=\s*\$?([\d,.]+)", text, re.I):
            info.closing_balance = m.group(1)
        if m := re.search(r"depositsintoyouraccount\s+\+?\s*([\d,.]+)", text, re.I):
            info.total_deposits = m.group(1)
        if m := re.search(r"withdrawalsfromyouraccount\s+-?\s*([\d,.]+)", text, re.I):
            info.total_withdrawals = m.group(1)

    def _extract_transaction_rows(self, words: list[dict]) -> list[dict]:
        """从 word 坐标中提取交易行。"""
        # Two-pass column detection: first unfiltered to find header,
        # then use header-derived left boundary to filter noise.
        col_bounds, header_y = self._detect_columns(words)
        if not col_bounds:
            return []

        # Dynamic left boundary: use the leftmost column's x0 with margin
        left_boundary = min(x0 for x0, _ in col_bounds.values()) - 5
        words = [w for w in words if w["x0"] >= left_boundary]

        # Only words below the header
        tx_words = [w for w in words if w["top"] > header_y + 5]
        if not tx_words:
            return []

        lines = self._group_by_y(tx_words)

        # Compute column center points for nearest-center matching
        col_centers = self._compute_col_centers(col_bounds)

        rows: list[dict] = []
        for line_words in lines:
            row = self._classify_words(line_words, col_bounds, col_centers)
            if row:
                rows.append(row)

        return rows

    @staticmethod
    def _detect_columns(words: list[dict]) -> tuple[dict[str, tuple[float, float]], float] | tuple[None, float]:
        """从页面 header 行动态检测列的 x 坐标范围。返回 (bounds, header_y)。"""
        # Find the header row: the y-line where "Withdrawals" appears
        withdrawal_words = [w for w in words if w["text"] == "Withdrawals"]
        if not withdrawal_words:
            return None, 0.0

        # Use the rightmost "Withdrawals" (the column header, not body text)
        withdrawal_word = max(withdrawal_words, key=lambda w: w["x0"])
        header_y = withdrawal_word["top"]

        # Collect all header keywords on the same y-line
        header_words = {}
        for w in words:
            if w["text"] in _HEADER_KEYWORDS and abs(w["top"] - header_y) <= _Y_TOLERANCE:
                col_name = _HEADER_KEYWORDS[w["text"]]
                if col_name not in header_words or abs(w["top"] - header_y) < abs(header_words[col_name][2] - header_y):
                    header_words[col_name] = (w["x0"], w["x1"], w["top"])

        if "withdrawals" not in header_words:
            return None, 0.0

        # Build column bounds: each column extends from its x0 to the
        # midpoint between this column's x1 and the next column's x0
        ordered = sorted(
            [(name, (x0, x1)) for name, (x0, x1, _) in header_words.items()],
            key=lambda kv: kv[1][0],
        )
        bounds = {}
        for i, (name, (x0, x1)) in enumerate(ordered):
            if i + 1 < len(ordered):
                next_x0 = ordered[i + 1][1][0]
                # Use midpoint of gap between columns as boundary
                mid = (x1 + next_x0) / 2
                bounds[name] = (x0, mid)
            else:
                bounds[name] = (x0, x1 + 50)  # last column extends right

        # Extend date column left to catch dates
        if "date" in bounds:
            bounds["date"] = (bounds["date"][0] - 5, bounds["date"][1])

        return bounds, header_y

    @staticmethod
    def _compute_col_centers(col_bounds: dict[str, tuple[float, float]]) -> dict[str, float]:
        """Compute x-center for each column header (for nearest-center matching)."""
        return {name: (x0 + x1) / 2 for name, (x0, x1) in col_bounds.items()}

    @staticmethod
    def _group_by_y(words: list[dict]) -> list[list[dict]]:
        """按 y 坐标分组，相近的归为同一行。"""
        if not words:
            return []
        sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
        lines: list[list[dict]] = []
        current_line: list[dict] = [sorted_words[0]]
        current_y = sorted_words[0]["top"]

        for w in sorted_words[1:]:
            if abs(w["top"] - current_y) <= _Y_TOLERANCE:
                current_line.append(w)
            else:
                lines.append(current_line)
                current_line = [w]
                current_y = w["top"]
        lines.append(current_line)
        return lines

    @staticmethod
    def _classify_words(
        line_words: list[dict],
        col_bounds: dict,
        col_centers: dict[str, float] | None = None,
    ) -> dict | None:
        """根据动态列边界将 word 分到对应列。

        Uses two-phase matching:
        1. If the word's x_center falls within exactly one column's bounds → assign.
        2. If ambiguous (boundary overlap), use nearest column center.
        """
        if col_centers is None:
            col_centers = {name: (x0 + x1) / 2 for name, (x0, x1) in col_bounds.items()}

        cols = {k: [] for k in col_bounds}
        for w in line_words:
            x_center = (w["x0"] + w["x1"]) / 2

            # Phase 1: find all columns whose bounds contain this word
            candidates = []
            for col_name, (x_min, x_max) in col_bounds.items():
                if x_min <= x_center <= x_max:
                    candidates.append(col_name)

            if len(candidates) == 1:
                cols[candidates[0]].append(w["text"])
            elif len(candidates) > 1:
                # Phase 2: ambiguous — pick nearest column center
                best = min(candidates, key=lambda c: abs(x_center - col_centers[c]))
                cols[best].append(w["text"])
            else:
                # Outside all bounds — assign to nearest column center
                best = min(col_centers, key=lambda c: abs(x_center - col_centers[c]))
                cols[best].append(w["text"])

        has_content = any(cols[c] for c in cols)
        if not has_content:
            return None

        return {
            "Date": " ".join(cols.get("date", [])),
            "Description": " ".join(cols.get("description", [])),
            "Withdrawals": " ".join(cols.get("withdrawals", [])),
            "Deposits": " ".join(cols.get("deposits", [])),
            "Balance": " ".join(cols.get("balance", [])),
        }

    @staticmethod
    def _build_dataframe(rows: list[dict]) -> pd.DataFrame:
        """构建 DataFrame，合并多行描述，前推日期。"""
        if not rows:
            return pd.DataFrame(columns=["Date", "Description", "Withdrawals", "Deposits", "Balance"])

        # Truncate at "Closing Balance" — everything after is footer
        truncated: list[dict] = []
        for row in rows:
            truncated.append(row)
            desc = row.get("Description", "").strip()
            if re.search(r"Closing\s*Balance", desc, re.I):
                break

        # Filter noise rows (page numbers, barcodes)
        noise_patterns = re.compile(
            r"(\d+\s*of\s*\d+|RBPDA|\d{7}-\d{3}_|0050750|^\d{4}$)",
            re.I,
        )
        cleaned: list[dict] = []
        for row in truncated:
            all_text = " ".join(v for v in row.values() if v).strip()
            if noise_patterns.search(all_text):
                continue
            desc = row.get("Description", "").strip()
            has_amounts = row["Withdrawals"].strip() or row["Deposits"].strip() or row["Balance"].strip()
            if not desc and not has_amounts:
                continue
            cleaned.append(row)

        # Merge continuation lines into previous row
        # Rule: a no-date line is a continuation ONLY if the previous row has no amounts yet
        merged: list[dict] = []
        for row in cleaned:
            has_date = bool(row["Date"].strip())
            has_desc = bool(row["Description"].strip())
            prev_has_amounts = bool(
                merged
                and (merged[-1]["Withdrawals"].strip() or merged[-1]["Deposits"].strip())
            )

            if not has_date and has_desc and merged and not prev_has_amounts:
                # Continuation: previous row had no amounts, append desc + fill amounts
                merged[-1]["Description"] += " " + row["Description"].strip()
                for col in ("Withdrawals", "Deposits", "Balance"):
                    if row[col].strip() and not merged[-1][col].strip():
                        merged[-1][col] = row[col]
            else:
                merged.append(row)

        # Forward-fill dates
        last_date = ""
        for row in merged:
            if row["Date"].strip():
                last_date = row["Date"].strip()
            else:
                row["Date"] = last_date

        # Clean up descriptions (spaces already correct with x_tolerance=1)
        for row in merged:
            # Normalize multiple spaces
            row["Description"] = re.sub(r"  +", " ", row["Description"])
            row["Date"] = re.sub(r"  +", " ", row["Date"])

        df = pd.DataFrame(merged)
        for col in df.columns:
            df[col] = df[col].str.strip()
        return df


def _add_spaces(text: str) -> str:
    """在驼峰式粘连的文字中插入空格。例如 'OnlineBanking' → 'Online Banking'"""
    # lowercase→Uppercase: "OnlineBanking" → "Online Banking"
    result = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # digit→Uppercase word: "0732ROGERS" → "0732 ROGERS" (only before 2+ uppercase)
    result = re.sub(r"(\d)([A-Z]{2})", r"\1 \2", result)
    # Long uppercase word→digit: "ROGERS11" → "ROGERS 11" (only after 3+ uppercase)
    result = re.sub(r"([A-Z]{3,})(\d)", r"\1 \2", result)
    # Collapse multiple spaces
    result = re.sub(r"  +", " ", result)
    return result
