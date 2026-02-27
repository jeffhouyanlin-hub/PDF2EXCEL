from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from core.batch import BatchProcessor
from core.converter import ExcelConverter
from core.extractor import PDFExtractor

st.set_page_config(page_title="PDF2EXCEL", page_icon="📊", layout="wide")

st.title("📊 PDF2EXCEL")
st.caption("PDF 表格 → Excel 批量转换工具")

# --- 侧边栏设置 ---
with st.sidebar:
    st.header("设置")
    sheet_mode = st.radio(
        "Sheet 策略",
        ["每个表格一个 Sheet", "合并到一个 Sheet"],
        index=0,
    )
    sheet_per_table = sheet_mode == "每个表格一个 Sheet"
    max_workers = st.slider("并行线程数", 1, 8, 4)

# --- 文件上传 ---
uploaded_files = st.file_uploader(
    "上传 PDF 文件",
    type=["pdf"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("请上传一个或多个 PDF 文件开始转换。")
    st.stop()

st.divider()

# --- 预览模式 ---
tabs = st.tabs(["📋 预览", "⚡ 批量转换"])

extractor = PDFExtractor()
converter = ExcelConverter()

with tabs[0]:
    st.subheader("表格预览")
    for uf in uploaded_files:
        with st.expander(f"📄 {uf.name}", expanded=len(uploaded_files) == 1):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(uf.getvalue())
                tmp_path = tmp.name
            try:
                result = extractor.extract(tmp_path)
                if not result.tables:
                    st.warning("未检测到表格。")
                else:
                    st.success(f"检测到 {len(result.tables)} 个表格（共 {result.page_count} 页）")
                    for i, df in enumerate(result.tables):
                        st.markdown(f"**表格 {i + 1}**")
                        st.dataframe(df, use_container_width=True)
            except Exception as e:
                st.error(f"解析失败: {e}")

with tabs[1]:
    st.subheader("批量转换")
    st.write(f"已上传 **{len(uploaded_files)}** 个文件")

    if st.button("开始转换", type="primary", use_container_width=True):
        progress_bar = st.progress(0, text="准备中...")
        status_area = st.empty()

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

            processor = BatchProcessor(max_workers=max_workers)

            def on_progress(completed, total, file_result):
                progress_bar.progress(
                    completed / total,
                    text=f"处理中... {completed}/{total}",
                )

            batch_result = processor.process(
                pdf_paths, output_dir, sheet_per_table, on_progress
            )

            progress_bar.progress(1.0, text="完成!")

            # 结果摘要
            col1, col2, col3 = st.columns(3)
            col1.metric("总计", batch_result.total)
            col2.metric("成功", batch_result.succeeded)
            col3.metric("失败", batch_result.failed)

            # 显示失败项
            for fr in batch_result.results:
                if not fr.success:
                    st.error(f"❌ {fr.source}: {fr.error}")

            # 打包下载
            excel_files = list(output_dir.glob("*.xlsx"))
            if excel_files:
                if len(excel_files) == 1:
                    with open(excel_files[0], "rb") as f:
                        st.download_button(
                            "下载 Excel",
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
                        f"下载全部（{len(excel_files)} 个文件，ZIP）",
                        buf.getvalue(),
                        file_name="pdf2excel_output.zip",
                        mime="application/zip",
                        use_container_width=True,
                    )
