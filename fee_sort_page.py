"""Fee Sort — Streamlit page for transaction classification."""

from __future__ import annotations

import re
from typing import Sequence

import pandas as pd
import streamlit as st

from core.fee_sort.field_mapper import FieldMapper, StandardRow
from core.fee_sort.merchant_overrides import MerchantOverrides, merchant_signature
from core.fee_sort.output_builder import OUTPUT_COLUMNS, OutputBuilder
from core.fee_sort.output_namer import OutputNamer
from core.fee_sort.rule_engine import Category, RuleEngine
from core.fee_sort.rule_engine_cc import CCCategory, CreditCardRuleEngine


# ---------------------------------------------------------------------------
# Year helpers
# ---------------------------------------------------------------------------

def _extract_year(merged_data: dict) -> str:
    """Best-effort year extraction from merged filename."""
    fname = merged_data.get("merged_filename", "")
    m = re.search(r"(20\d{2})", fname)
    return m.group(1) if m else ""


def _build_bank_acc(merged_data: dict) -> str:
    """Build Bank_Acc string like 'Barclays_1234'."""
    bank = merged_data.get("bank_name", "")
    acct = merged_data.get("account_number", "")
    bank_short = re.sub(r"\s+", "", bank) if bank else "Bank"
    last4 = acct[-4:] if len(acct) >= 4 else (acct or "0000")
    return f"{bank_short}_{last4}"


def _period_year(period_str: str) -> int | None:
    if not period_str:
        return None
    m = re.search(r"(20\d{2})", period_str)
    return int(m.group(1)) if m else None


def _period_month(period_str: str) -> int | None:
    if not period_str:
        return None
    dt = pd.to_datetime(period_str, errors="coerce")
    return dt.month if pd.notna(dt) else None


_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _tx_month(date_str: str) -> int | None:
    """Parse month from transaction date like 'Jan 03' or '03/01/2023'."""
    if not date_str or not str(date_str).strip():
        return None
    m = re.match(r"([A-Za-z]{3})", str(date_str).strip())
    if m:
        return _MONTH_MAP.get(m.group(1).lower())
    # Try numeric date (DD/MM/YYYY or YYYY-MM-DD)
    dt = pd.to_datetime(date_str, errors="coerce", dayfirst=True)
    return dt.month if pd.notna(dt) else None


def _determine_majority_year(files_data: dict) -> int | None:
    """Determine the majority (main) year from per-file summary data.

    Logic:
    - Collect (year, month) for each PDF from summary_pdf_vals.
    - If a January statement exists, the main year is determined by non-January
      statements' year.  If Jan year == that year → it's the main-year January.
      If Jan year == that year + 1 → it's the next-year January.
    - Otherwise, the most frequent year across all PDFs is the main year.
    """
    year_month_pairs: list[tuple[int, int | None]] = []
    for stem, fdata in files_data.items():
        spv = fdata.get("summary_pdf_vals", {})
        y = _period_year(spv.get("Period To", "")) or _period_year(spv.get("Period From", ""))
        mo = _period_month(spv.get("Period To", "")) or _period_month(spv.get("Period From", ""))
        if y:
            year_month_pairs.append((y, mo))

    if not year_month_pairs:
        return None

    # Count years from non-January PDFs
    non_jan_years: dict[int, int] = {}
    for y, mo in year_month_pairs:
        if mo != 1:
            non_jan_years[y] = non_jan_years.get(y, 0) + 1

    if non_jan_years:
        return max(non_jan_years, key=non_jan_years.get)

    # All PDFs are January (rare) — use the smallest year
    return min(y for y, _ in year_month_pairs)


def _build_stem_period_map(files_data: dict) -> dict[str, tuple[int | None, int | None]]:
    """Return {stem: (year, month)} from per-file summary data."""
    result: dict[str, tuple[int | None, int | None]] = {}
    for stem, fdata in files_data.items():
        spv = fdata.get("summary_pdf_vals", {})
        y = _period_year(spv.get("Period To", "")) or _period_year(spv.get("Period From", ""))
        mo = _period_month(spv.get("Period To", "")) or _period_month(spv.get("Period From", ""))
        result[stem] = (y, mo)
    return result


def _filter_january_rows(
    rows: list[StandardRow],
    row_mapping: list[tuple[str, int]],
    stem_period: dict[str, tuple[int | None, int | None]],
    majority_year: int,
) -> list[tuple[StandardRow, tuple[str, int]]]:
    """Filter out cross-year records from January statements.

    - Jan PDF of majority year: remove rows whose tx month is December
    - Jan PDF of majority_year+1: remove rows whose tx month is January
    """
    filtered: list[tuple[StandardRow, tuple[str, int]]] = []
    for row, mapping in zip(rows, row_mapping):
        stem = mapping[0]
        if stem == "__separator__" or row.row_type in ("separator", "opening_balance"):
            filtered.append((row, mapping))
            continue

        period_info = stem_period.get(stem)
        if not period_info:
            filtered.append((row, mapping))
            continue

        p_year, p_month = period_info
        if p_month != 1 or p_year is None:
            # Not a January PDF — keep as-is
            filtered.append((row, mapping))
            continue

        # This is a January PDF — filter by tx month
        txm = _tx_month(row.date)
        if p_year == majority_year:
            # Jan of main year: exclude Dec tx (belongs to prior year)
            if txm == 12:
                continue
        elif p_year == majority_year + 1:
            # Jan of next year: exclude Jan tx (belongs to next year), keep Dec
            if txm != 12:
                continue

        filtered.append((row, mapping))
    return filtered


# ---------------------------------------------------------------------------
# Classification pipeline
# ---------------------------------------------------------------------------

def _run_classification(merged_data: dict, files_data: dict) -> pd.DataFrame:
    """Run full FieldMapper → year filter → RuleEngine → OutputBuilder pipeline."""
    excel_bytes = merged_data["merged_excel_bytes"]
    row_mapping = merged_data["row_mapping"]

    std_rows = FieldMapper.map(excel_bytes, row_mapping)

    # --- Year filtering for January statements ---
    stem_period = _build_stem_period_map(files_data)
    majority_year = _determine_majority_year(files_data)

    if majority_year is not None:
        pairs = _filter_january_rows(std_rows, row_mapping, stem_period, majority_year)
        std_rows = [p[0] for p in pairs]

    # --- Classification — pick engine by schema ---
    acct = merged_data.get("account_number", "")
    is_credit_card = merged_data.get("is_credit_card", False)
    if is_credit_card:
        engine = CreditCardRuleEngine(
            tesla_charging_threshold=float(st.session_state.get("_tesla_threshold", 50)),
            restaurant_personal_threshold=float(st.session_state.get("_restaurant_threshold", 50)),
            overrides=MerchantOverrides(),
        )
    else:
        engine = RuleEngine(account_numbers=[acct] if acct else [])
    classifications = [engine.classify(r) for r in std_rows]

    bank_acc = _build_bank_acc(merged_data)
    year = str(majority_year) if majority_year else _extract_year(merged_data)

    return OutputBuilder.build(std_rows, classifications, bank_acc, year)


def fee_sort_page() -> None:
    """Render the Fee Sort page."""
    vdata = st.session_state.get("verification_data", {})
    merged_data = vdata.get("merged")

    if not merged_data or not merged_data.get("is_merged"):
        st.warning(
            "暂无合并数据，请先上传 PDF 并完成批量转换。\n\n"
            "No merged data available. Please upload PDFs and run batch conversion first."
        )
        if st.button("返回 / Back", key="fee_sort_back_empty"):
            st.session_state.show_fee_sort = False
            st.rerun()
        return

    # --- Run classification (cached in session_state, busted on config change) ---
    files_data = vdata.get("files", {})
    _cfg_key = (
        st.session_state.get("_tesla_threshold", 50),
        st.session_state.get("_restaurant_threshold", 50),
        len(MerchantOverrides()),  # overrides count — changes invalidate cache
    )
    if st.session_state.get("_fee_sort_cfg_key") != _cfg_key:
        st.session_state.pop("fee_sort_df", None)
        st.session_state._fee_sort_cfg_key = _cfg_key
    if "fee_sort_df" not in st.session_state:
        with st.spinner("正在分类... / Classifying..."):
            st.session_state.fee_sort_df = _run_classification(merged_data, files_data)

    df: pd.DataFrame = st.session_state.fee_sort_df

    # --- Header (T777 button available for both bank and CC schemas) ---
    hdr_cols = st.columns([4, 2, 2, 1.5])
    with hdr_cols[0]:
        st.subheader("📂 费用分类 / Fee Sort")
    with hdr_cols[1]:
        out_name = OutputNamer.generate(
            merged_data.get("bank_name", ""),
            merged_data.get("account_number", ""),
            merged_data.get("merged_filename", ""),
        )
        xlsx_bytes = OutputBuilder.to_excel_bytes(df)
        st.download_button(
            "⬇ 下载 / Download",
            xlsx_bytes,
            file_name=out_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="fee_sort_download_top",
        )
    with hdr_cols[2]:
        from core.t777 import build_t777_excel
        t777_bytes = build_t777_excel(
            merged_data["merged_excel_bytes"],
            merged_data["row_mapping"],
        )
        stem = merged_data.get("merged_filename", "statement")
        st.download_button(
            "⬇ T777 初筛 / T777 Screening",
            t777_bytes,
            file_name=f"{stem}_T777初筛.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="fee_sort_download_t777",
            help="按加拿大 T777（Statement of Employment Expenses）做疑似归类初筛，保守策略",
        )
    with hdr_cols[-1]:
        if st.button("↩ 返回 / Back", key="fee_sort_back", use_container_width=True):
            st.session_state.show_fee_sort = False
            st.rerun()

    # --- Summary cards ---
    total = len(df)
    excluded = int((df["Exclude"] == "Y").sum())
    categorized = int((df["Category"] != "").sum()) - int(
        ((df["Category"] != "") & (df["Need_Review"] == "Y")).sum()
    )
    need_review = int((df["Need_Review"] == "Y").sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("总计 / Total", total)
    m2.metric("排除 / Excluded", excluded)
    m3.metric("已分类 / Categorized", categorized)
    m4.metric("待审核 / Need Review", need_review)

    # --- View toggle ---
    view_mode = st.radio(
        "查看模式 / View Mode",
        ["全部 / All", "仅待审核 / Need Review Only"],
        horizontal=True,
        key="fee_sort_view_mode",
    )

    display_df = df.copy()
    if "仅待审核" in view_mode:
        display_df = display_df[display_df["Need_Review"] == "Y"].copy()

    if display_df.empty:
        st.info("无待审核记录。/ No records need review.")
        return

    # Reset DataFrame index so the left-margin row labels start from 1 in the UI.
    # Capture the original (filtered) df indices first so edits can be mapped
    # back to the underlying df when the user saves.
    _orig_idx_list = list(display_df.index)
    display_df = display_df.reset_index(drop=True)
    display_df.index = display_df.index + 1

    # Convert Withdrawals to numeric for right-aligned display
    display_df["Withdrawals"] = pd.to_numeric(
        display_df["Withdrawals"].str.replace(",", "", regex=False),
        errors="coerce",
    )

    # --- Category options for editor (schema-specific) ---
    is_cc = merged_data.get("is_credit_card", False)
    category_options = [c.value for c in (CCCategory if is_cc else Category)]

    # --- Editable table ---
    edited = st.data_editor(
        display_df,
        use_container_width=True,
        num_rows="fixed",
        key="fee_sort_editor",
        column_config={
            "序号": st.column_config.NumberColumn("序号", disabled=True, width="small", format="%d"),
            "Bank_Acc": st.column_config.TextColumn("Bank_Acc", disabled=True),
            "Year": st.column_config.TextColumn("Year", disabled=True),
            "Date": st.column_config.TextColumn("Date", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Withdrawals": st.column_config.NumberColumn("Withdrawals", disabled=True, format="%.2f"),
            "Exclude": st.column_config.SelectboxColumn(
                "Exclude", options=["", "Y"], width="small",
            ),
            "Category": st.column_config.SelectboxColumn(
                "Category", options=category_options, width="medium",
            ),
            "Detail": st.column_config.TextColumn("Detail", width="medium"),
            "Rule_Hit": st.column_config.TextColumn("Rule_Hit", disabled=True, width="small"),
            "Need_Review": st.column_config.SelectboxColumn(
                "Need_Review", options=["", "Y"], width="small",
            ),
        },
    )

    # --- Apply edits back to main df ---
    # edited.index is 1..N (display-only); map positions → original df indices
    # via _orig_idx_list captured before the reset.
    if edited is not None:
        editable = [c for c in ("Exclude", "Category", "Detail", "Need_Review")
                    if c in edited.columns]
        for pos in range(len(edited)):
            orig_idx = _orig_idx_list[pos]
            for col in editable:
                df.at[orig_idx, col] = edited.iloc[pos][col]
        st.session_state.fee_sort_df = df

    # --- Download with edits ---
    st.divider()
    final_bytes = OutputBuilder.to_excel_bytes(df)
    st.download_button(
        "⬇ 下载最终 Excel / Download Final Excel",
        final_bytes,
        file_name=out_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key="fee_sort_download_bottom",
    )

    # --- Merchant overrides (CC mode only — persistent learning) ---
    if merged_data.get("is_credit_card", False):
        _render_overrides_panel(df, merged_data)


def _render_overrides_panel(df: pd.DataFrame, merged_data: dict) -> None:
    """Show persisted merchant overrides + button to learn from current edits."""
    st.divider()
    overrides = MerchantOverrides()
    t_thresh = float(st.session_state.get("_tesla_threshold", 50))
    r_thresh = float(st.session_state.get("_restaurant_threshold", 50))

    st.subheader("💡 商户持久化学习 / Merchant Override Learning")

    col_a, col_b = st.columns([2, 3])
    with col_a:
        if st.button(
            "💾 把当前修订保存为永久规则 / Save current edits as rules",
            use_container_width=True,
            help="扫描本次分类与当前表格的差异，把用户改动过的商户→类别映射写入 "
                 "merchant_overrides.json；下次遇到同商户自动按此分类。",
        ):
            added = _capture_overrides_from_df(df, overrides, t_thresh, r_thresh)
            if added > 0:
                st.session_state.pop("fee_sort_df", None)  # force re-classify
                st.success(f"✅ 已学习 {added} 条新规则 / {added} new rules learned")
                st.rerun()
            else:
                st.info("当前表格与自动分类一致，无新规则可学 / No edits differ from auto-classification.")
    with col_b:
        st.caption(
            f"已学习 **{len(overrides)}** 条商户规则。下次分类时自动应用。"
            f"如需手动编辑请改 `core/fee_sort/merchant_overrides.json`。"
        )

    if len(overrides) == 0:
        return

    with st.expander(f"📋 查看/管理已学习的规则 ({len(overrides)})", expanded=False):
        rows_ui = []
        for sig, o in overrides.all():
            rows_ui.append({
                "商户签名 / Signature": sig,
                "类别 / Category": o.category,
                "原始描述 / Last desc": o.last_desc,
                "次数 / Count": o.count,
                "更新时间 / Updated": o.updated,
            })
        st.dataframe(pd.DataFrame(rows_ui), use_container_width=True, hide_index=True)

        col_del, col_clear = st.columns([3, 1])
        with col_del:
            _sig_to_remove = st.text_input(
                "输入商户签名删除单条规则 / Signature to remove",
                key="override_del_input",
                placeholder="例: MCDONALD'S WEST VANCOUVEBC",
            )
            if st.button("🗑 删除 / Remove", key="override_del_btn"):
                if overrides.remove(_sig_to_remove.strip()):
                    st.session_state.pop("fee_sort_df", None)
                    st.success("已删除 / Removed")
                    st.rerun()
                else:
                    st.warning("未找到该签名 / Signature not found")
        with col_clear:
            st.write("")
            if st.button("⚠️ 全部清除 / Clear all", key="override_clear_all"):
                overrides.clear()
                st.session_state.pop("fee_sort_df", None)
                st.success("已清除全部学习规则 / All overrides cleared")
                st.rerun()


def _capture_overrides_from_df(
    df: pd.DataFrame,
    overrides: MerchantOverrides,
    tesla_thresh: float,
    restaurant_thresh: float,
) -> int:
    """Diff the current fee-sort df vs a pristine rule-engine prediction and
    save user-corrected merchant → category pairs as overrides."""
    pristine = CreditCardRuleEngine(
        tesla_charging_threshold=tesla_thresh,
        restaurant_personal_threshold=restaurant_thresh,
        overrides=None,  # pristine: no existing overrides considered
    )
    added = 0
    for _, r in df.iterrows():
        desc = str(r.get("Description", "")).strip()
        cat = str(r.get("Category", "")).strip()
        if not desc or not cat:
            continue
        amt_str = str(r.get("Withdrawals", "")).replace(",", "").replace("$", "").strip()
        try:
            amt = float(amt_str) if amt_str else 0.0
        except ValueError:
            amt = 0.0
        std = StandardRow(
            idx=0, row_type="transaction", statement="",
            date=str(r.get("Date", "")), description=desc,
            withdrawals="", deposits="", balance="",
            withdrawals_float=amt, schema="credit_card",
        )
        predicted = pristine.classify(std).category
        if predicted != cat:
            if overrides.add(desc, cat):
                added += 1
    return added
