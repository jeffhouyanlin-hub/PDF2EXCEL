from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import requests

import pandas as pd
import streamlit as st
from openpyxl.utils import get_column_letter

from core.batch import BatchProcessor
from core.converter import ExcelConverter
from core.extractor import PDFExtractor
from core.statement_parser import BankStatementParser

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
    st.caption("© Dr. Jeff Hou · v0.2.0")
    if st.button("📮 报错反馈 / Error Feedback", use_container_width=True):
        st.session_state.show_feedback = True

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

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            pdf_paths = []
            for uf in uploaded_files:
                p = input_dir / uf.name
                p.write_bytes(uf.getvalue())
                pdf_paths.append(p)

            total = len(pdf_paths)
            succeeded = 0
            failed = 0
            errors: list[str] = []

            for i, pdf_path in enumerate(pdf_paths):
                try:
                    tables, stmt_info = _try_extract(str(pdf_path), parse_mode)
                    out_name = pdf_path.stem + ".xlsx"
                    out_path = output_dir / out_name

                    if stmt_info and not stmt_info.transactions.empty:
                        _statement_to_excel(stmt_info, out_path)
                    elif tables:
                        from core.extractor import ExtractionResult
                        result = ExtractionResult(tables=tables, page_count=0, source=str(pdf_path))
                        converter.convert(result, out_path, sheet_per_table)
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

            col1, col2, col3 = st.columns(3)
            col1.metric("总计 / Total", total)
            col2.metric("成功 / Success", succeeded)
            col3.metric("失败 / Failed", failed)

            for err in errors:
                st.error(f"❌ {err}")

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
