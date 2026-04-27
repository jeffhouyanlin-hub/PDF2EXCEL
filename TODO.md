# PDF2EXCEL — TODO

## 🧪 自动化回归：Golden Corpus + LRAP 循环

**目标**：把"每次加新银行/新规则都可能漏掉边界 case"的反复 bug 流，转成自动发现 → 自动修复的闭环。

### 背景
反复出现的小 bug 都是**同语义不同字符串形式**引起（`"- 55,503.68"` vs `"-55,503.68"`、`"$40.23"` vs `"40.23"`、`Opening Balance` vs `Previous Balance` 等）。现有单测只覆盖已见过的格式，新 parser 或新 PDF 变种一进来就暴露。

### 方案

#### 1. 建立 Golden Corpus（高优先级）
- [ ] 把所有真实 PDF 固化为 fixtures（放到 `tests/fixtures/pdfs/` 或引用 Desktop 原路径）
  - RBC 信用卡 14 个 (`/Users/vox/Desktop/RBC 2025/Mastercard 6031/`)
  - CIBC 银行 14 个 (`/Users/vox/Desktop/CIBC 2025/Cheque 0885/`)
  - RBC 银行（等 user 提供路径）
- [ ] 每个 PDF 生成一次"期望输出"（Excel + T777 Summary JSON），作为 golden reference
- [ ] 加 `tests/test_golden_corpus.py`：每个 PDF 跑 parse → 与 golden 对比，差异为 0 才通过
- [ ] 新加 parser 前：先跑全量 golden，确认不回归

#### 2. Property-Based 测试（hypothesis）
- [ ] `test_normalize_invariants.py`：随机生成等价金额字符串（不同空白、逗号、货币符号组合），断言 `_normalize` / `_normalize_cross` 输出一致
- [ ] 同样覆盖日期字符串（"DEC 06, 2024" vs "Dec 6, 2024" vs "DEC06,2024"）
- [ ] 覆盖账号格式（`541590******6031` vs `5415 90** **** 6031`）

#### 3. 跨源不变量 CI Gate
- [ ] 每次 commit 自动跑 "A源 vs B源" 全量对比（针对 RBC bank 这类有双源的）
- [ ] 算术连贯性（`Balance[i] = Balance[i-1] - W + D`）对每个文件都必须通过
- [ ] 差异阈值 > 0 → CI 红

#### 4. LRAP 自动修复循环（进阶）
用 `/lrap` 生成长循环 agent prompt，设计如下：

```
loop until 差异 == 0:
  1. 对整个 golden corpus 批处理
  2. 收集：Layer 1/2/3 差异 + 算术问题 + fallthrough 热点
  3. 按 root-cause 聚类（同类 bug 一次性根治，不逐个补丁）
  4. 对每个 cluster：
     a. 先写一个 property test 固化不变量
     b. 修 root cause（不是 surface fix）
     c. 全量回归 + 跨 parser 对比
  5. 差异下降 → 继续；上升 → 回滚；停滞 → 升级 fallthrough 热点为新规则
```

**关键约束**：
- 循环退出条件是**全量真实数据 0 差异**，不是"通过现有单测"
- 每次修 bug 必须**同时加 test**（否则下轮同样的 bug 还会出）
- 禁止 `try: ... except: pass` 类的掩盖式修复

### 预期收益
| 现状 | LRAP + Golden Corpus 后 |
|---|---|
| 每次新 bug 用户汇报 → 我修 → 可能漏掉类似的 | 每次 commit 自动跑全量，类似 bug 同批发现 |
| 测试只覆盖已见过的输入 | property test 覆盖所有等价变体 |
| 加新银行 = 祈祷不回归 | 加新银行 = 先跑 golden，确认不回归 |

### 需要用户决策的点
- [ ] 同意把 Desktop 的真实 PDF 作为 golden（还是换成合成 fixture）？
- [ ] 是否希望 LRAP 跑在本地（持续占着一个 terminal）还是 cron 定时？
- [ ] 修复优先级：root-cause 干净修复 vs 快速 patch（当前是后者，LRAP 应该倾向前者）

---

## 其它挂起项

### 架构
- [ ] `RuleEngine`（UK 业务 bank engine）已废弃，但测试还在；下次清理决定是否删除
- [ ] `TextBasedExtractor` 目前只支持 RBC bank；为 CIBC / 其它银行写独立的 Source B 实现
- [ ] 当前 Amount 的 `$` 剥离在 CC parser 最后一步做；未来统一到 FieldMapper 会更稳

### UI
- [ ] 合并数据复核页面默认展示合并视图，但用户可能想看单文件层级差异 —— 加 toggle
- [ ] 费用分类的"保存修订为规则"按钮当前是同步阻塞；大 df 时可能卡，可加进度条

### 规则引擎
- [ ] 退款（Refund）目前按"原商户类别 + 需人工审核"处理；考虑自动匹配原始正值交易并冲减
- [ ] 金额阈值参数化已完成，但只对 Tesla / 餐厅两条；考虑把"金额 > $500 一律 Need_Review" 也参数化
