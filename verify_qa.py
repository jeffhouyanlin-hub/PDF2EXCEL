#!/usr/bin/env python3
"""
QA Guardian - 自动验证脚本
每次代码修改后自动运行，检查常见低级错误
"""
import os
import re
import sys
from pathlib import Path

errors = []
warnings = []

def check(condition, error_msg, is_warning=False):
    if not condition:
        if is_warning:
            warnings.append(f"⚠️  {error_msg}")
        else:
            errors.append(f"❌ {error_msg}")

SELF_NAME = Path(__file__).name  # exclude self from scanning

def scan_files(extensions=(".py", ".html", ".js", ".jsx", ".css")):
    """扫描项目中所有相关文件"""
    root = Path(".")
    files = []
    exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for ext in extensions:
        for f in root.rglob(f"*{ext}"):
            if not any(part in exclude_dirs for part in f.parts):
                if f.name != SELF_NAME:  # PROJECT-SPECIFIC: 排除自身以避免误报
                    files.append(f)
    return files

# ============================================================
# 检查1：数字字段混淆（年份 vs 账户尾号）
# ============================================================
print("🔍 检查1: 数字字段混淆...")
files = scan_files((".py", ".html", ".js", ".jsx"))
for f in files:
    content = f.read_text(errors="ignore")
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # 检测年份变量被赋值为非年份范围的数字
        if re.search(r'year\s*=\s*(\d{4})', line, re.IGNORECASE):
            match = re.search(r'year\s*=\s*(\d{4})', line, re.IGNORECASE)
            val = int(match.group(1))
            if not (1990 <= val <= 2100):
                check(False, f"{f}:{i} 年份字段值异常: {val}（应在1990-2100之间）")
        # 检测账户尾号变量是否被用在年份显示位置
        if re.search(r'(年份|year|date_year)[^=]*=.*?(tail|suffix|last4|尾号)', line, re.IGNORECASE):
            check(False, f"{f}:{i} 疑似年份字段被赋值为账户尾号: {line.strip()}")

# ============================================================
# 检查2：金额对齐
# ============================================================
print("🔍 检查2: 金额对齐...")
for f in scan_files((".css", ".html", ".py")):
    content = f.read_text(errors="ignore")
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # 检测金额相关列是否有左对齐设置
        # PROJECT-SPECIFIC: 跳过注释行，避免误报
        stripped = line.strip()
        if not stripped.startswith("#") and not stripped.startswith("//"):
            if re.search(r'(amount|balance|金额|余额|存款|取款|debit|credit)', line, re.IGNORECASE):
                if re.search(r'text-align\s*:\s*left', line, re.IGNORECASE):
                    check(False, f"{f}:{i} 金额列疑似左对齐，应为右对齐: {stripped}")
        # 检测有金额class但没有right-align的情况（HTML table）
        if re.search(r'class=["\'].*?(amount|money|balance).*?["\']', line, re.IGNORECASE):
            if not re.search(r'right|text-right', line, re.IGNORECASE):
                check(False, f"{f}:{i} 金额相关class未设置右对齐: {line.strip()}", is_warning=True)

# ============================================================
# 检查3：按钮功能不全
# ============================================================
print("🔍 检查3: 按钮功能...")
for f in scan_files((".html", ".js", ".jsx")):
    content = f.read_text(errors="ignore")
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # 检测空的onclick
        # PROJECT-SPECIFIC: 排除JS中 .onclick=function(){...} 赋值（这是正常的事件绑定）
        if re.search(r'onclick\s*=\s*["\']?\s*["\'](?:\s|>)|onClick\s*=\s*\{\s*\}|onClick\s*=\s*\{\(\)\s*=>\s*\}', line, re.IGNORECASE):
            if not re.search(r'\.onclick\s*=\s*function', line, re.IGNORECASE):
                check(False, f"{f}:{i} 按钮onClick为空: {line.strip()}")
        # 检测按钮没有绑定事件
        if re.search(r'<button(?![^>]*onclick)(?![^>]*onClick)(?![^>]*type=["\']submit["\'])[^>]*>', line, re.IGNORECASE):
            if not re.search(r'disabled', line, re.IGNORECASE):
                check(False, f"{f}:{i} 按钮缺少事件绑定: {line.strip()}", is_warning=True)

# ============================================================
# 检查4：PDF提取常见问题
# ============================================================
print("🔍 检查4: PDF提取逻辑...")
for f in scan_files((".py",)):
    content = f.read_text(errors="ignore")
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        # 检测拼接文本时没有加空格
        if re.search(r'text\s*\+=\s*["\'](?!\s)', line) or re.search(r'\.join\(\[\]', line):
            check(False, f"{f}:{i} PDF文本拼接可能缺少空格分隔符: {line.strip()}", is_warning=True)
        # 检测没有处理跨页坐标重置
        if re.search(r'(page|pdf).*extract', line, re.IGNORECASE):
            if not re.search(r'page_num|page_index|reset|新页|跨页', content, re.IGNORECASE):
                check(False, f"{f} PDF提取未见跨页坐标重置处理", is_warning=True)
                break  # 每个文件只报一次

# ============================================================
# 检查5：UI明显不合理
# ============================================================
print("🔍 检查5: UI合理性...")
for f in scan_files((".html", ".css")):
    content = f.read_text(errors="ignore")
    # 检测过小的字体（财务数据不应小于12px）
    for match in re.finditer(r'font-size\s*:\s*(\d+)px', content):
        size = int(match.group(1))
        if size < 10:
            lineno = content[:match.start()].count('\n') + 1
            check(False, f"{f}:{lineno} 字体过小({size}px)，财务数据建议≥12px")
    # 检测表格没有设置overflow处理
    if re.search(r'<table', content, re.IGNORECASE):
        if not re.search(r'overflow|scroll|responsive', content, re.IGNORECASE):
            check(False, f"{f} 表格缺少overflow/scroll处理，可能导致移动端显示异常", is_warning=True)

# ============================================================
# 输出结果
# ============================================================
print("\n" + "="*50)
if not errors and not warnings:
    print("✅ 所有检查通过！无错误，无警告。")
    sys.exit(0)

if warnings:
    print(f"\n⚠️  警告 ({len(warnings)} 条):")
    for w in warnings:
        print(f"  {w}")

if errors:
    print(f"\n❌ 错误 ({len(errors)} 条，必须修复):")
    for e in errors:
        print(f"  {e}")
    print("\n请修复以上错误后再回复用户。")
    sys.exit(1)
else:
    print("\n✅ 无严重错误（有警告，请酌情处理）")
    sys.exit(0)
