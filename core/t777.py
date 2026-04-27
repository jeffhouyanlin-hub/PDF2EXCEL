"""T777 pre-screening — map credit-card categories to Canadian T777 fields.

Produces a pre-screening annotation (not final tax conclusion) for every
transaction, intended as a conservative first pass that flags the likely
T777 field plus a review note. Uncertain cases fall through to
'需人工判断' rather than being forced into a deductible bucket.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, replace
from typing import Iterable

import pandas as pd
from openpyxl.utils import get_column_letter

from core.fee_sort.field_mapper import FieldMapper, StandardRow
from core.fee_sort.rule_engine_cc import CCCategory, CreditCardRuleEngine

# ---------------------------------------------------------------------------
# Screening result labels (Chinese, as required by the user's prompt)
# ---------------------------------------------------------------------------
SCREEN_DEDUCTIBLE = "疑似可抵扣"
SCREEN_NOT_DEDUCTIBLE = "疑似不可抵扣"
SCREEN_NEED_REVIEW = "需人工判断"

# Confidence levels
CONF_HIGH = "高"
CONF_MID = "中"
CONF_LOW = "低"

# T777 output columns (appended to the merged transaction sheet).
T777_COLUMNS = [
    "税务初筛结果",
    "T777 Field (English)",
    "Line No.",
    "可扣性判断说明",
    "识别置信度",
    "人工复核建议",
]


@dataclass(frozen=True)
class T777Annotation:
    """Pre-screening result for one transaction row."""

    screening: str          # SCREEN_DEDUCTIBLE | SCREEN_NOT_DEDUCTIBLE | SCREEN_NEED_REVIEW
    field_en: str           # T777 Field (English) — may be "" when not applicable
    line_no: int | None     # T777 Line No. — None when blank
    reason: str             # 可扣性判断说明
    confidence: str         # CONF_HIGH | CONF_MID | CONF_LOW
    review_note: str        # 人工复核建议


# ---------------------------------------------------------------------------
# CC category → T777 annotation (static data; refund handled separately)
# ---------------------------------------------------------------------------
_T777_MAP: dict[str, T777Annotation] = {
    CCCategory.PARKING.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Parking", 9281, "",
        CONF_HIGH, "需确认是否属于固定工作地点停车",
    ),
    CCCategory.TRAVEL.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Travelling expenses", 9200, "",
        CONF_MID, "需确认是否属于工作出差住宿/交通，非私人旅行",
    ),
    CCCategory.TRANSIT.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Travelling expenses", 9200, "",
        CONF_MID, "需确认是否属于工作出差而非日常通勤（通勤不可扣）",
    ),
    CCCategory.ANNUAL_LICENCE_FEES.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Other expenses", 9270, "",
        CONF_MID, "需确认执照与工作直接相关",
    ),
    CCCategory.PROFESSIONAL_TRAINING.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Other expenses", 9270, "",
        CONF_MID, "需确认为维持执业资格的培训/继续教育；一般性进修通常不可扣",
    ),
    CCCategory.CLIENT_MEALS.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Meals and entertainment", 8523, "",
        CONF_MID, "每笔需人工审核确认为业务相关；按 50% 规则抵扣",
    ),
    CCCategory.CLIENT_ENTERTAINMENT.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Meals and entertainment", 8523, "",
        CONF_MID, "通常按 50% 规则，需确认是否为业务相关",
    ),
    CCCategory.MEALS_AND_ENTERTAINMENT.value: T777Annotation(
        SCREEN_DEDUCTIBLE, "Meals and entertainment", 8523, "",
        CONF_MID, "通常按 50% 规则，需确认是否为业务相关",
    ),

    # Motor vehicle expenses — T777 form has a dedicated motor-vehicle section
    # with no single Line No.; individual fuel/repair receipts support the total.
    CCCategory.GAS.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Motor vehicle expenses", None, "",
        CONF_MID, "需确认工作使用比例（按工作/总里程）",
    ),
    CCCategory.ELECTRICITY_FOR_VEHICLE.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Motor vehicle expenses", None, "",
        CONF_MID, "需确认工作使用比例",
    ),
    CCCategory.AUTO_REPAIR.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Motor vehicle expenses", None, "",
        CONF_MID, "需确认工作使用比例，区分维修 vs. 资本性改良",
    ),
    CCCategory.AUTO_LEASE.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Motor vehicle expenses", None,
        "Motor vehicle expenses — Lease fee",
        CONF_HIGH, "按工作/总里程比例抵扣；区分自有车贷利息与经营性租赁费",
    ),
    CCCategory.AUTO_INSURANCE.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Motor vehicle expenses", None,
        "Motor vehicle expenses — Insurance",
        CONF_HIGH, "按工作/总里程比例抵扣；若金额为 $208.66 属 RAV4，其他金额需确认车辆归属",
    ),

    # Unclear from merchant alone → need review
    CCCategory.RESTAURANTS.value: T777Annotation(
        SCREEN_NEED_REVIEW, "", None, "",
        CONF_LOW, "需确认是否为客户招待或出差用餐（默认个人消费不可扣）",
    ),
    CCCategory.SUBSCRIPTIONS.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None,
        "订阅费/会员费通常为个人消费，不属 T777 雇佣费用",
        CONF_HIGH, "如为工作必需订阅，请手动改为 Supplies (8810) 并提供证据",
    ),
    CCCategory.PHONE.value: T777Annotation(
        SCREEN_NEED_REVIEW, "Other expenses", 9270, "",
        CONF_MID, "仅工作相关部分可能可扣",
    ),
    CCCategory.GIFT.value: T777Annotation(
        SCREEN_NEED_REVIEW, "", None, "",
        CONF_LOW, "需确认用途（免税店/礼品常为个人消费）",
    ),
    CCCategory.ONLINE_SHOPPING.value: T777Annotation(
        SCREEN_NEED_REVIEW, "", None, "",
        CONF_LOW, "需确认是否工作相关",
    ),

    # Clearly personal / not deductible
    CCCategory.GROCERIES.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "属个人生活开支",
        CONF_HIGH, "",
    ),
    CCCategory.ENTERTAINMENT.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "个人娱乐通常不可扣",
        CONF_HIGH, "",
    ),
    CCCategory.PERSONAL_CARE.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "个人护理不属 T777 范围",
        CONF_HIGH, "",
    ),
    CCCategory.MEDICAL.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None,
        "个人医疗不属 T777 雇佣费用（另可通过 Medical Expense Tax Credit 申报）",
        CONF_HIGH, "",
    ),
    CCCategory.UTILITIES.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None,
        "无居家办公，水费/电费/市政付款不可抵扣",
        CONF_HIGH, "",
    ),
    CCCategory.PERSONAL.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "用户标记为个人消费",
        CONF_HIGH, "",
    ),
    CCCategory.FINE.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "罚款/违约金不可扣",
        CONF_HIGH, "",
    ),
    CCCategory.INTEREST.value: T777Annotation(
        SCREEN_NOT_DEDUCTIBLE, "", None, "信用卡利息不属 T777 雇佣费用",
        CONF_HIGH, "",
    ),

    CCCategory.OTHER_EXPENSE.value: T777Annotation(
        SCREEN_NEED_REVIEW, "", None, "",
        CONF_LOW, "商户类型不明，需手动判断",
    ),
}

# Excluded rows don't represent a deductible expense and get a neutral
# annotation. Covers CC cases (PAYMENT / CASH BACK / separators / Previous
# Balance) AND bank cases (opening balance / separators / deposits-are-income).
_EXCLUDED_ANNOTATION = T777Annotation(
    "", "", None, "非支出行（还款/返现/期初余额/分隔/存款收入）",
    CONF_HIGH, "",
)


class T777Annotator:
    """Map credit-card categories to T777 annotations."""

    def __init__(self, engine: CreditCardRuleEngine | None = None) -> None:
        self._engine = engine or CreditCardRuleEngine()

    def annotate(self, row: StandardRow) -> T777Annotation:
        """Return a T777 pre-screening annotation for a standard row.

        Refunds (negative amount) reuse the same-merchant's T777 category so a
        SHOPPERS refund follows Personal Care (non-deductible) rather than
        being lumped into a generic 'Refund' bucket.
        """
        # Bank-specific: deposit-only rows are income (salary/transfers in),
        # not an expense — drop from T777 output.
        if (
            row.schema == "bank"
            and row.deposits_float > 0
            and row.withdrawals_float == 0
        ):
            return _EXCLUDED_ANNOTATION

        cls = self._engine.classify(row)

        # Excluded rows (separator/opening_balance/PAYMENT/CASH BACK) — skip.
        if cls.exclude:
            return _EXCLUDED_ANNOTATION

        # Restaurant <$50 default-personal: specific review note so the user
        # knows to flip to Client Meals if it was actually a business meal.
        if cls.rule_hit == "P15-restaurant_small_personal":
            return T777Annotation(
                screening=SCREEN_NOT_DEDUCTIBLE,
                field_en="",
                line_no=None,
                reason="<$50 餐厅默认按个人消费处理",
                confidence=CONF_MID,
                review_note="如为客户招待请手动改为 Client Meals (8523) 可抵扣",
            )

        # Refund: re-classify on the underlying merchant (positive amount) to
        # find the T777 category the refund should inherit.
        if cls.rule_hit == "P1-refund":
            merchant_row = replace(
                row, withdrawals_float=abs(row.withdrawals_float),
            )
            merchant_cls = self._engine.classify(merchant_row)
            base = _T777_MAP.get(merchant_cls.category, _T777_MAP[CCCategory.OTHER_EXPENSE.value])
            note = "退款 — 冲减原次抵扣；" + base.review_note
            reason = (
                f"退款，按原商户类别（{merchant_cls.category}）处理"
                + (f"；{base.reason}" if base.reason else "")
            )
            return T777Annotation(
                screening=base.screening,
                field_en=base.field_en,
                line_no=base.line_no,
                reason=reason,
                confidence=base.confidence,
                review_note=note.rstrip("；"),
            )

        return _T777_MAP.get(cls.category, _T777_MAP[CCCategory.OTHER_EXPENSE.value])


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def build_t777_excel(
    merged_excel_bytes: bytes,
    row_mapping: list[tuple[str, int]],
    annotator: T777Annotator | None = None,
) -> bytes:
    """Produce the T777 pre-screening Excel (data sheet + summary sheet)."""
    annotator = annotator or T777Annotator()

    # --- Load merged Excel & map rows to StandardRow for classification ---
    std_rows = FieldMapper.map(merged_excel_bytes, row_mapping)
    annotations = [annotator.annotate(r) for r in std_rows]

    xls = pd.ExcelFile(io.BytesIO(merged_excel_bytes))
    df = pd.read_excel(xls, sheet_name="Transactions")

    # Align annotation list length with DataFrame (FieldMapper already handles
    # openpyxl's trailing-blank truncation).
    annotations = annotations[:len(df)]
    while len(annotations) < len(df):
        annotations.append(_EXCLUDED_ANNOTATION)

    # Drop non-transaction rows (separators / Previous Balance / PAYMENT /
    # CASH BACK REWARD) — T777 is for real expenses only. Refund rows get a
    # dynamic annotation (not _EXCLUDED_ANNOTATION), so they're kept.
    keep_idx = [i for i, a in enumerate(annotations) if a is not _EXCLUDED_ANNOTATION]
    df = df.iloc[keep_idx].reset_index(drop=True)
    annotations = [annotations[i] for i in keep_idx]

    # Re-number 序号 from 1 after the drop so the output is contiguous.
    if "序号" in df.columns:
        df["序号"] = list(range(1, len(df) + 1))
    else:
        df.insert(0, "序号", list(range(1, len(df) + 1)))

    df["税务初筛结果"] = [a.screening for a in annotations]
    df["T777 Field (English)"] = [a.field_en for a in annotations]
    df["Line No."] = [a.line_no if a.line_no is not None else "" for a in annotations]
    df["可扣性判断说明"] = [a.reason for a in annotations]
    df["识别置信度"] = [a.confidence for a in annotations]
    df["人工复核建议"] = [a.review_note for a in annotations]

    # --- Summary ---
    summary_df, totals = _build_summary(df)

    # --- Write Excel ---
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Transactions", index=False)
        summary_df.to_excel(writer, sheet_name="T777 Summary", index=False)
        totals.to_excel(writer, sheet_name="T777 Summary", index=False,
                         startrow=len(summary_df) + 3)
        _autofit(writer.sheets["Transactions"], df, max_width=55)
        _autofit(writer.sheets["T777 Summary"], summary_df, max_width=45)
    return buf.getvalue()


def _amount_float(val: object) -> float:
    """Parse a signed amount from the Excel 'Amount' column."""
    import re as _re
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    s = _re.sub(r"[^\d.\-]", "", str(val))
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _build_summary(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Produce per-field summary + overall totals DataFrame."""
    amount_col = "Amount" if "Amount" in df.columns else (
        "Withdrawals" if "Withdrawals" in df.columns else None
    )

    def amt(row: pd.Series) -> float:
        if amount_col is None:
            return 0.0
        return _amount_float(row.get(amount_col))

    # Per-field aggregation over deductible rows only
    rows = []
    ded = df[df["税务初筛结果"] == SCREEN_DEDUCTIBLE]
    for (field_en, line_no), group in ded.groupby(
        ["T777 Field (English)", "Line No."], dropna=False,
    ):
        rows.append({
            "T777 Field (English)": field_en or "",
            "Line No.": line_no if line_no != "" else "",
            "疑似可抵扣笔数": len(group),
            "疑似可抵扣金额合计": round(sum(amt(r) for _, r in group.iterrows()), 2),
        })
    summary_df = pd.DataFrame(rows, columns=[
        "T777 Field (English)", "Line No.",
        "疑似可抵扣笔数", "疑似可抵扣金额合计",
    ])

    # Overall totals
    total_rows = {
        "总交易笔数": len(df),
        "疑似可抵扣总额": round(sum(amt(r) for _, r in ded.iterrows()), 2),
        "疑似不可抵扣总额": round(sum(
            amt(r) for _, r in df[df["税务初筛结果"] == SCREEN_NOT_DEDUCTIBLE].iterrows()
        ), 2),
        "需人工判断总额": round(sum(
            amt(r) for _, r in df[df["税务初筛结果"] == SCREEN_NEED_REVIEW].iterrows()
        ), 2),
    }
    totals = pd.DataFrame(
        [{"指标": k, "值": v} for k, v in total_rows.items()],
        columns=["指标", "值"],
    )
    return summary_df, totals


def _autofit(ws, df: pd.DataFrame, max_width: int = 55) -> None:
    """Set column widths based on content."""
    for col_idx, col_name in enumerate(df.columns, 1):
        max_len = len(str(col_name))
        for val in df.iloc[:, col_idx - 1]:
            v = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            max_len = max(max_len, len(v))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, max_width)
