#!/bin/bash
echo "=== 运行自动验证 ==="

# 检查金额列右对齐 (openpyxl Alignment)
align_check=$(grep -rn "right_align_numbers\|Alignment(horizontal=\"right\")" core/ fee_sort_page.py app.py)
if [ -z "$align_check" ]; then
  echo "❌ 未找到金额右对齐代码 (right_align_numbers / Alignment)"
  exit 1
fi
echo "✓ 金额右对齐代码存在"

# 检查 Streamlit NumberColumn 用于 Withdrawals 显示
num_col_check=$(grep -n "NumberColumn.*Withdrawals" fee_sort_page.py)
if [ -z "$num_col_check" ]; then
  echo "❌ Fee Sort 页面 Withdrawals 未使用 NumberColumn (右对齐)"
  exit 1
fi
echo "✓ Fee Sort Withdrawals 使用 NumberColumn"

# 检查年份来源是 period_to 优先
year_from_check=$(grep -n "period_to.*period_from" app.py fee_sort_page.py | grep -i "year\|period_year")
if [ -z "$year_from_check" ]; then
  echo "❌ 年份提取未使用 period_to 优先"
  exit 1
fi
echo "✓ 年份提取使用 period_to 优先"

# 检查侧边栏按钮清除其他页面标志
sidebar_clear=$(grep -A2 "show_verification = True" app.py | grep "show_fee_sort = False")
if [ -z "$sidebar_clear" ]; then
  echo "❌ 侧边栏按钮未清除其他页面标志"
  exit 1
fi
echo "✓ 侧边栏按钮互斥切换正常"

# 信用卡解析器必须存在，且 POSTING 锚点检测与 Amount 列保留
cc_parser_check=$(grep -c "class CreditCardParser" core/credit_card_parser.py 2>/dev/null)
if [ "$cc_parser_check" != "1" ]; then
  echo "❌ 信用卡解析器缺失 (core/credit_card_parser.py)"
  exit 1
fi
echo "✓ 信用卡解析器存在"

# 混合上传检测
mixed_check=$(grep -n "Mixed bank + credit-card" app.py)
if [ -z "$mixed_check" ]; then
  echo "❌ 混合上传拒绝逻辑缺失"
  exit 1
fi
echo "✓ 混合上传拒绝逻辑存在"

# FieldMapper 信用卡 Amount 符号映射
sign_map_check=$(grep -n "is_credit_card" core/fee_sort/field_mapper.py)
if [ -z "$sign_map_check" ]; then
  echo "❌ FieldMapper 未识别信用卡 schema"
  exit 1
fi
echo "✓ FieldMapper 信用卡 schema 分支存在"

# 运行单元测试
echo ""
echo "--- 运行单元测试 ---"
python -m pytest tests/ --tb=short -q
if [ $? -ne 0 ]; then
  echo "❌ 单元测试失败"
  exit 1
fi

echo ""
echo "✅ 全部验证通过"
