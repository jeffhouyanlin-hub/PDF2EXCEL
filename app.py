from __future__ import annotations

import base64
import io
import json
import tempfile
import zipfile
from pathlib import Path

import requests

import pandas as pd
import streamlit as st
import streamlit.components.v1 as stc
from openpyxl.utils import get_column_letter

from core.batch import BatchProcessor
from core.converter import ExcelConverter
from core.extractor import PDFExtractor
from core.statement_parser import BankStatementParser, RowPosition
from core.verifier import DataVerifier, _normalize

st.set_page_config(page_title="PDF2EXCEL", page_icon="📊", layout="wide")

# --- 密码验证 / Password Auth ---
def _check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True
    pwd = st.text_input("请输入访问密码 / Enter password", type="password", key="pwd_input")
    if not pwd:
        st.stop()
    if pwd == st.secrets.get("APP_PASSWORD", ""):
        st.session_state.authenticated = True
        st.rerun()
    else:
        st.error("密码错误 / Incorrect password")
        st.stop()
    return False

_check_password()

st.title("📊 PDF2EXCEL")
st.caption("PDF 表格 → Excel 批量转换工具 / PDF Table → Excel Batch Converter")

# --- 侧边栏 / Sidebar ---
_MODE_LABELS = {
    "自动检测": "自动检测 / Auto Detect",
    "标准表格": "标准表格 / Standard Table",
    "银行账单": "银行账单 / Bank Statement",
}
_SHEET_LABELS = {
    "每个表格一个 Sheet": "每个表格一个 Sheet / One Sheet per Table",
    "合并到一个 Sheet": "合并到一个 Sheet / Merge into One Sheet",
}

with st.sidebar:
    st.header("设置 / Settings")
    parse_mode = st.radio(
        "解析模式 / Parse Mode",
        list(_MODE_LABELS.keys()),
        index=0,
        format_func=lambda x: _MODE_LABELS[x],
        help="自动检测：先尝试标准表格提取，失败则用银行账单解析器 / Auto: try standard first, fallback to bank statement",
    )
    sheet_mode = st.radio(
        "Sheet 策略 / Sheet Strategy",
        list(_SHEET_LABELS.keys()),
        index=0,
        format_func=lambda x: _SHEET_LABELS[x],
    )
    sheet_per_table = sheet_mode == "每个表格一个 Sheet"
    max_workers = st.slider("并行线程数 / Parallel Threads", 1, 8, 4)
    st.divider()
    st.caption("© Dr. Jeff Hou · v0.3.0")
    if st.button("📮 报错反馈 / Error Feedback", use_container_width=True):
        st.session_state.show_feedback = True
    if st.button("🔍 数据复核 / Data Verification", use_container_width=True):
        st.session_state.show_verification = True

# --- 意见反馈页面 / Feedback Page ---
if st.session_state.get("show_feedback"):
    st.subheader("📮 意见反馈 / Error Feedback")
    st.info(
        "若您的对账单无法识别或识别错误，请提交样本和错误描述，我们会安排优化解决。\n\n"
        "If your statement cannot be recognized or is incorrectly parsed, "
        "please submit a sample and error description. We will arrange optimization."
    )

    with st.form("feedback_form", clear_on_submit=True):
        feedback_file = st.file_uploader(
            "上传 PDF 样本 / Upload PDF Sample", type=["pdf"], key="feedback_pdf"
        )
        feedback_text = st.text_area(
            "错误描述 / Error Description",
            placeholder="请描述您遇到的问题... / Please describe the issue...",
        )
        col_submit, col_back = st.columns(2)
        with col_submit:
            submitted = st.form_submit_button(
                "提交反馈 / Submit", type="primary", use_container_width=True
            )
        with col_back:
            back = st.form_submit_button("返回 / Back", use_container_width=True)

    if back:
        st.session_state.show_feedback = False
        st.rerun()

    if submitted:
        if not feedback_file:
            st.warning("请上传 PDF 文件。/ Please upload a PDF file.")
        elif not feedback_text.strip():
            st.warning("请填写错误描述。/ Please fill in the error description.")
        else:
            formspree_id = st.secrets.get("FORMSPREE_ID", "")
            if not formspree_id:
                st.error("反馈服务未配置，请联系管理员。/ Feedback service not configured.")
            else:
                import base64
                with st.spinner("正在提交... / Submitting..."):
                    pdf_b64 = base64.b64encode(feedback_file.getvalue()).decode()
                    resp = requests.post(
                        f"https://formspree.io/f/{formspree_id}",
                        json={
                            "email": "feedback@pdf2excel.app",
                            "message": feedback_text,
                            "_subject": f"PDF2EXCEL 反馈: {feedback_file.name}",
                            "filename": feedback_file.name,
                            "filesize": f"{len(feedback_file.getvalue()) / 1024:.1f} KB",
                            "pdf_data": pdf_b64,
                        },
                        headers={"Accept": "application/json"},
                    )
                if resp.ok:
                    st.success("反馈已提交，感谢您的支持！/ Feedback submitted, thank you!")
                else:
                    st.error(
                        f"提交失败（{resp.status_code}），请稍后重试。/ "
                        f"Failed ({resp.status_code}), please try again."
                    )

    st.stop()


def _build_verification_payload(
    unified_rows: list[dict],
    row_positions: list,
    row_headers: list[str],
    is_statement: bool,
    summary_positions: list | None = None,
) -> dict:
    """Build data payload for the dual-panel verification component.

    Returns a dict with segments, rows, comparisons, and PDF positions,
    ready to be serialized to JSON and embedded in the HTML component.
    """
    # --- Segments: group consecutive rows by sheet ---
    segments: list[dict] = []
    prev_sheet = None
    for i, row in enumerate(unified_rows):
        if row["sheet"] != prev_sheet:
            if segments:
                segments[-1]["endIdx"] = i
            segments.append({
                "sheet": row["sheet"],
                "columns": row["columns"],
                "startIdx": i,
            })
            prev_sheet = row["sheet"]
    if segments:
        segments[-1]["endIdx"] = len(unified_rows)

    # --- Rows: compact per-row data ---
    rows = []
    for i, ur in enumerate(unified_rows):
        seg_idx = next(
            j for j, s in enumerate(segments)
            if s["startIdx"] <= i < s["endIdx"]
        )
        cols = ur["columns"]
        rows.append({
            "seg": seg_idx,
            "ev": [ur["excel_vals"].get(c, "") for c in cols],
            "pv": [ur["pdf_vals"].get(c, "") for c in cols],
            "ok": len(ur["diff_cols"]) == 0,
            "dc": [ci for ci, c in enumerate(cols) if c in ur["diff_cols"]],
        })

    # --- PDF positions ---
    positions = []
    if is_statement and row_positions:
        for rp in row_positions:
            positions.append({
                "pg": rp.page_index,
                "yt": round(rp.y_top, 1),
                "yb": round(rp.y_bottom, 1),
                "ph": round(rp.page_height, 1),
            })

    # --- Summary PDF positions ---
    sum_positions = []
    if summary_positions:
        for sp in summary_positions:
            if sp is not None:
                sum_positions.append({
                    "pg": sp.page_index,
                    "yt": round(sp.y_top, 1),
                    "yb": round(sp.y_bottom, 1),
                    "ph": round(sp.page_height, 1),
                })
            else:
                sum_positions.append(None)

    return {
        "segments": segments,
        "rows": rows,
        "pdfPos": positions,
        "summaryPdfPos": sum_positions,
        "pdfHeaders": row_headers,
        "isStmt": is_statement,
        "total": len(unified_rows),
        "matched": sum(1 for r in rows if r["ok"]),
        "mismatched": sum(1 for r in rows if not r["ok"]),
    }


def _build_verification_html(payload_json: str, pdf_b64: str) -> str:
    """Build HTML for the dual-panel verification component."""
    tpl = (Path(__file__).parent / "templates" / "verification.html").read_text(encoding="utf-8")
    return tpl.replace('"__PAYLOAD__"', payload_json).replace("__PDF_B64__", pdf_b64)


# --- 数据复核页面 / Verification Page ---
if st.session_state.get("show_verification"):
    # 全屏模式：隐藏侧边栏
    st.markdown(
        """<style>
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stSidebarCollapsedControl"] { display: none; }
        .block-container { max-width: 100% !important; padding-left: 1rem; padding-right: 1rem; }
        </style>""",
        unsafe_allow_html=True,
    )

    _vdata = st.session_state.get("verification_data")
    if not _vdata:
        st.warning(
            "暂无可复核数据，请先上传 PDF 并完成批量转换。\n\n"
            "No data to verify. Please upload PDF and run batch conversion first."
        )
        if st.button("返回 / Back", key="verify_back_empty"):
            st.session_state.show_verification = False
            st.rerun()
        st.stop()

    st.subheader("🔍 数据复核 / Data Verification")

    # --- 数据准备 ---
    _v_file_names = list(_vdata["files"].keys())
    _ctrl = st.columns([5, 1])
    with _ctrl[0]:
        _v_sel_file = st.selectbox(
            "文件 / File", _v_file_names, key="verify_file_sel",
        )
    with _ctrl[1]:
        if st.button("返回 / Back", key="verify_back", use_container_width=True):
            st.session_state.show_verification = False
            st.rerun()

    _v_info = _vdata["files"][_v_sel_file]
    _v_result = _v_info["result"]
    _v_pdf_bytes = _v_info["pdf_bytes"]
    _v_expected_dfs = _v_info["expected_dfs"]
    _v_excel_bytes = _v_info["excel_bytes"]
    pdf_b64 = base64.b64encode(_v_pdf_bytes).decode()

    # 读取 Excel 全部 sheet
    _v_excel_dfs: dict[str, pd.DataFrame] = {}
    xls = pd.ExcelFile(io.BytesIO(_v_excel_bytes))
    for sn in xls.sheet_names:
        _v_excel_dfs[sn] = pd.read_excel(xls, sheet_name=sn)

    # --- 构建统一行列表（全部 sheet 合并） ---
    # 每行: {sheet, row, pdf_vals:{col:str}, excel_vals:{col:str}, columns:[], diff_cols:set}
    _unified_rows: list[dict] = []

    # 差异 lookup: (sheet, row, col)
    _diff_set: set[tuple[str, int, str]] = set()
    for d in _v_result.diffs:
        _diff_set.add((d.sheet, d.row, d.column))

    # Summary PDF 原始值 (从 StatementInfo 解析)
    _summary_pdf_vals_map: dict[str, str] = _v_info.get("summary_pdf_vals", {})

    if _v_info.get("is_statement"):
        # 银行账单: Summary + Transactions
        if "Summary" in _v_excel_dfs:
            sdf = _v_excel_dfs["Summary"]
            for r in range(len(sdf)):
                cols = list(sdf.columns)
                # Excel 值
                ev = {c: str(sdf.iloc[r][c]) if str(sdf.iloc[r][c]) != "nan" else "" for c in cols}
                # PDF 值: 从 Excel 的 Item 列读取 item 名称，查找 PDF 解析值
                pv = dict(ev)  # 默认与 Excel 相同
                if "Item" in cols and "Value" in cols:
                    excel_item = ev.get("Item", "")
                    pdf_val = _summary_pdf_vals_map.get(excel_item, "")
                    pv["Value"] = pdf_val
                # 比较差异
                diff_cols: set[str] = set()
                for c in cols:
                    if _normalize(pv.get(c, "")) != _normalize(ev.get(c, "")):
                        diff_cols.add(c)
                _unified_rows.append({
                    "sheet": "Summary", "row": r, "columns": cols,
                    "pdf_vals": pv, "excel_vals": ev,
                    "diff_cols": diff_cols,
                })
        if "Transactions" in _v_excel_dfs:
            edf = _v_excel_dfs["Transactions"]
            pdf_df = _v_expected_dfs[0] if _v_expected_dfs else edf
            cols = list(edf.columns)
            max_r = max(len(pdf_df), len(edf))
            for r in range(max_r):
                pv = {}
                ev = {}
                for c in cols:
                    raw_p = str(pdf_df.iloc[r][c]) if r < len(pdf_df) and c in pdf_df.columns else ""
                    pv[c] = "" if raw_p == "nan" else raw_p
                    raw_e = str(edf.iloc[r][c]) if r < len(edf) else ""
                    ev[c] = "" if raw_e == "nan" else raw_e
                _unified_rows.append({
                    "sheet": "Transactions", "row": r, "columns": cols,
                    "pdf_vals": pv, "excel_vals": ev,
                    "diff_cols": {c for c in cols if ("Transactions", r, c) in _diff_set},
                })
    else:
        # 标准表格模式
        for i, exp_df in enumerate(_v_expected_dfs):
            sn = f"Table_{i + 1}"
            edf = _v_excel_dfs.get(sn, pd.DataFrame())
            cols = list(edf.columns) if not edf.empty else list(exp_df.columns)
            max_r = max(len(exp_df), len(edf))
            for r in range(max_r):
                pv = {}
                ev = {}
                for c in cols:
                    raw_p = str(exp_df.iloc[r][c]) if r < len(exp_df) and c in exp_df.columns else ""
                    pv[c] = "" if raw_p == "nan" else raw_p
                    raw_e = str(edf.iloc[r][c]) if r < len(edf) else ""
                    ev[c] = "" if raw_e == "nan" else raw_e
                _unified_rows.append({
                    "sheet": sn, "row": r, "columns": cols,
                    "pdf_vals": pv, "excel_vals": ev,
                    "diff_cols": {c for c in cols if (sn, r, c) in _diff_set},
                })

    # --- 获取行坐标和表头 ---
    _row_positions: list[RowPosition] = _v_info.get("row_positions", [])
    _row_headers: list[str] = _v_info.get("row_headers", [])
    _is_statement = _v_info.get("is_statement", False)

    # --- 获取 Summary PDF 坐标 ---
    _summary_positions: list | None = None
    if _is_statement and "Summary" in _v_excel_dfs:
        _sdf = _v_excel_dfs["Summary"]
        _sum_items = []
        for _r in range(len(_sdf)):
            _item = str(_sdf.iloc[_r].get("Item", "")) if "Item" in _sdf.columns else ""
            _val = str(_sdf.iloc[_r].get("Value", "")) if "Value" in _sdf.columns else ""
            if _item == "nan":
                _item = ""
            if _val == "nan":
                _val = ""
            _sum_items.append({"item": _item, "value": _val})
        try:
            _summary_positions = BankStatementParser().get_summary_positions(_v_pdf_bytes, _sum_items)
        except Exception:
            _summary_positions = None

    # Build payload for dual-panel verification component
    _payload = _build_verification_payload(
        _unified_rows, _row_positions, _row_headers, _is_statement,
        summary_positions=_summary_positions,
    )
    _payload_json = json.dumps(_payload, ensure_ascii=False)

    # --- 双面板核对组件 / Dual-panel verification component ---
    _html = _build_verification_html(_payload_json, pdf_b64)
    stc.html(_html, height=720, scrolling=False)

    st.divider()

    # 验证结果汇总
    if _v_result.matched:
        st.success(f"✅ {_v_result.message}")
    else:
        st.warning(f"⚠️ {_v_result.message}")

    # 差异列表
    if _v_result.diffs:
        st.markdown("### 📋 差异列表 / Difference List")
        diff_table = pd.DataFrame([
            {
                "Sheet": d.sheet,
                "行 / Row": d.row + 1,
                "列 / Column": d.column,
                "PDF 值 / Expected": d.expected or "(空/empty)",
                "Excel 值 / Actual": d.actual or "(空/empty)",
            }
            for d in _v_result.diffs
        ])
        st.dataframe(diff_table, use_container_width=True)

    # 可编辑版本
    with st.expander("✏️ 手工修正 / Manual Edit"):
        _edit_sheet = st.selectbox("Sheet", list(_v_excel_dfs.keys()), key="verify_edit_sheet")
        _edit_df = _v_excel_dfs[_edit_sheet]
        edited_df = st.data_editor(
            _edit_df, use_container_width=True, num_rows="dynamic",
            key=f"verify_editor_{_v_sel_file}_{_edit_sheet}",
        )
        if st.button("💾 重新导出 / Re-export Excel", key="verify_reexport"):
            out_buf = io.BytesIO()
            with pd.ExcelWriter(out_buf, engine="openpyxl") as writer:
                for sn in _v_excel_dfs:
                    df_w = edited_df if sn == _edit_sheet else _v_excel_dfs[sn]
                    df_w.to_excel(writer, sheet_name=sn, index=False)
            st.download_button(
                "下载修正后的 Excel / Download Corrected Excel",
                out_buf.getvalue(),
                file_name=f"{_v_sel_file}_corrected.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="verify_download_corrected",
            )

    st.stop()

# --- 文件上传 / File Upload ---
uploaded_files = st.file_uploader(
    "上传 PDF 文件 / Upload PDF Files",
    type=["pdf"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("请上传一个或多个 PDF 文件开始转换。/ Please upload one or more PDF files to start.")
    st.stop()

st.divider()

extractor = PDFExtractor()
converter = ExcelConverter()
statement_parser = BankStatementParser()


def _save_to_tmp(uf) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(uf.getvalue())
        return tmp.name


def _try_extract(tmp_path: str, mode: str):
    """尝试提取 PDF 内容。返回 (tables, statement_info)。"""
    if mode == "银行账单":
        info = statement_parser.parse(tmp_path)
        return [], info

    result = extractor.extract(tmp_path)
    if result.tables or mode == "标准表格":
        return result.tables, None

    # 自动检测：标准提取无结果，回退到银行账单解析
    info = statement_parser.parse(tmp_path)
    if not info.transactions.empty:
        return [], info
    return [], None


def _statement_to_excel(info, output_path: Path) -> Path:
    """将银行账单信息写入 Excel。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_data = {
            "Item": ["Bank Name", "Account Number", "Period From", "Period To",
                     "Opening Balance", "Closing Balance",
                     "Total Deposits", "Total Withdrawals"],
            "Value": [info.bank_name, info.account_number, info.period_from, info.period_to,
                      info.opening_balance, info.closing_balance,
                      info.total_deposits, info.total_withdrawals],
        }
        summary_df = pd.DataFrame(summary_data)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        info.transactions.to_excel(writer, sheet_name="Transactions", index=False)

        for sheet_name in ["Summary", "Transactions"]:
            ws = writer.sheets[sheet_name]
            df = summary_df if sheet_name == "Summary" else info.transactions
            for col_idx, col_name in enumerate(df.columns, 1):
                max_len = len(str(col_name))
                for val in df.iloc[:, col_idx - 1]:
                    max_len = max(max_len, len(str(val)) if val is not None else 0)
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 55)
    return output_path


# --- 预览 & 转换 / Preview & Convert ---
tabs = st.tabs(["📋 预览 / Preview", "⚡ 批量转换 / Batch Convert"])

with tabs[0]:
    st.subheader("表格预览 / Table Preview")
    for uf in uploaded_files:
        with st.expander(f"📄 {uf.name}", expanded=len(uploaded_files) == 1):
            tmp_path = _save_to_tmp(uf)
            try:
                tables, stmt_info = _try_extract(tmp_path, parse_mode)

                if tables:
                    st.success(
                        f"检测到 {len(tables)} 个表格 / {len(tables)} table(s) detected"
                    )
                    for i, df in enumerate(tables):
                        st.markdown(f"**表格 / Table {i + 1}**")
                        st.dataframe(df, use_container_width=True)
                elif stmt_info and not stmt_info.transactions.empty:
                    st.success(
                        f"银行账单模式 — 检测到 {len(stmt_info.transactions)} 笔交易 / "
                        f"Bank statement — {len(stmt_info.transactions)} transaction(s)"
                    )
                    st.markdown("**账户摘要 / Account Summary**")
                    if stmt_info.bank_name:
                        st.metric("银行 / Bank", stmt_info.bank_name)
                    col1, col2 = st.columns(2)
                    col1.metric("账户 / Account", stmt_info.account_number)
                    col2.metric(
                        "期间 / Period",
                        f"{stmt_info.period_from} ~ {stmt_info.period_to}",
                    )
                    col3, col4 = st.columns(2)
                    col3.metric("总存入 / Deposits", f"${stmt_info.total_deposits}")
                    col4.metric("总支出 / Withdrawals", f"${stmt_info.total_withdrawals}")
                    st.markdown("**交易明细 / Transactions**")
                    st.dataframe(stmt_info.transactions, use_container_width=True)
                else:
                    st.warning("未检测到表格或交易数据。/ No tables or transactions detected.")
            except Exception as e:
                st.error(f"解析失败 / Parse failed: {e}")

with tabs[1]:
    st.subheader("批量转换 / Batch Convert")
    st.write(f"已上传 **{len(uploaded_files)}** 个文件 / file(s)")

    if st.button("开始转换 / Start", type="primary", use_container_width=True):
        progress_bar = st.progress(0, text="准备中... / Preparing...")
        _verifier = DataVerifier()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            pdf_paths = []
            uf_map: dict[str, bytes] = {}
            for uf in uploaded_files:
                p = input_dir / uf.name
                pdf_bytes = uf.getvalue()
                p.write_bytes(pdf_bytes)
                pdf_paths.append(p)
                uf_map[uf.name] = pdf_bytes

            total = len(pdf_paths)
            succeeded = 0
            failed = 0
            errors: list[str] = []
            verify_files: dict = {}

            for i, pdf_path in enumerate(pdf_paths):
                try:
                    tables, stmt_info = _try_extract(str(pdf_path), parse_mode)
                    out_name = pdf_path.stem + ".xlsx"
                    out_path = output_dir / out_name

                    if stmt_info and not stmt_info.transactions.empty:
                        _statement_to_excel(stmt_info, out_path)
                        # 自动验证
                        v_result = _verifier.verify_statement(
                            stmt_info.transactions, out_path
                        )
                        # 获取行坐标
                        row_headers, row_positions = statement_parser.get_row_positions(pdf_path)
                        # Summary 字段: PDF 解析的原始值
                        summary_pdf_vals = {
                            "Bank Name": stmt_info.bank_name,
                            "Account Number": stmt_info.account_number,
                            "Period From": stmt_info.period_from,
                            "Period To": stmt_info.period_to,
                            "Opening Balance": stmt_info.opening_balance,
                            "Closing Balance": stmt_info.closing_balance,
                            "Total Deposits": stmt_info.total_deposits,
                            "Total Withdrawals": stmt_info.total_withdrawals,
                        }
                        verify_files[pdf_path.stem] = {
                            "pdf_bytes": uf_map[pdf_path.name],
                            "expected_dfs": [stmt_info.transactions],
                            "excel_bytes": out_path.read_bytes(),
                            "result": v_result,
                            "is_statement": True,
                            "row_positions": row_positions,
                            "row_headers": row_headers,
                            "summary_pdf_vals": summary_pdf_vals,
                        }
                    elif tables:
                        from core.extractor import ExtractionResult
                        result = ExtractionResult(tables=tables, page_count=0, source=str(pdf_path))
                        converter.convert(result, out_path, sheet_per_table)
                        # 自动验证
                        v_result = _verifier.verify(tables, out_path)
                        verify_files[pdf_path.stem] = {
                            "pdf_bytes": uf_map[pdf_path.name],
                            "expected_dfs": tables,
                            "excel_bytes": out_path.read_bytes(),
                            "result": v_result,
                            "is_statement": False,
                        }
                    else:
                        errors.append(f"{pdf_path.name}: 未检测到数据 / No data detected")
                        failed += 1
                        continue
                    succeeded += 1
                except Exception as e:
                    errors.append(f"{pdf_path.name}: {e}")
                    failed += 1

                progress_bar.progress((i + 1) / total, text=f"处理中... / Processing {i + 1}/{total}")

            progress_bar.progress(1.0, text="完成！/ Done!")

            # 存储验证数据到 session_state
            if verify_files:
                st.session_state.verification_data = {"files": verify_files}

            col1, col2, col3 = st.columns(3)
            col1.metric("总计 / Total", total)
            col2.metric("成功 / Success", succeeded)
            col3.metric("失败 / Failed", failed)

            for err in errors:
                st.error(f"❌ {err}")

            # 自动验证结果汇总
            if verify_files:
                st.divider()
                all_matched = all(v["result"].matched for v in verify_files.values())
                total_diffs = sum(v["result"].mismatched_cells for v in verify_files.values())
                if all_matched:
                    st.success(
                        "✅ 数据审核通过，EXCEL文件与PDF文件内容一致 / "
                        "Verification passed, Excel matches PDF"
                    )
                else:
                    st.warning(
                        f"⚠️ 发现 {total_diffs} 处不一致，请点击侧边栏「数据复核」查看详情 / "
                        f"Found {total_diffs} mismatch(es), click 'Data Verification' in sidebar for details"
                    )

            excel_files = list(output_dir.glob("*.xlsx"))
            if excel_files:
                if len(excel_files) == 1:
                    with open(excel_files[0], "rb") as f:
                        st.download_button(
                            "下载 Excel / Download",
                            f.read(),
                            file_name=excel_files[0].name,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                        )
                else:
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for ef in excel_files:
                            zf.write(ef, ef.name)
                    st.download_button(
                        f"下载全部 / Download All ({len(excel_files)} files, ZIP)",
                        buf.getvalue(),
                        file_name="pdf2excel_output.zip",
                        mime="application/zip",
                        use_container_width=True,
                    )
