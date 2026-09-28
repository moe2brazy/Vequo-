# yans 数据库大题库测试报告

- 测试时间: 2026-09-02 00:44:03
- 测试库: yans（大赛数据 10 表）
- 题库规模: 494 题（手题库 114 + 生成题库 434 合并去重）
- 耗时: 0.9s

## 总览

| 指标 | 数值 |
|---|---|
| 编译命中率 | 100.0% (483/483) |
| SQL 执行成功率 | 100.0% (483/483) |
| 综合通过率（含 LLM 兜底不判错） | 98.6% (487/494) |
| 完全正确 | 487 |
| 编译未命中（走 LLM 兜底） | 7 |
| 执行失败 | 0 |
| 行数不符 | 0 |
| 时间窗口无数据（本月/上月，数据范围不含当前月，合理） | 10 |

## 编译未命中清单（走 LLM 兜底，需人工核验 LLM 回答质量）

- ❌ `产品类别分布`（期望 llm）
- ❌ `各关键工序的数量`（期望 llm）
- ❌ `各车间的停机原因排行`（期望 llm）
- ❌ `产量同比`（期望 llm）
- ❌ `良率环比`（期望 llm）
- ❌ `各状态各产品的工单数`（期望 llm）
- ❌ `各工序各班次的产量`（期望 llm）

## 执行失败清单（必须修复）

- 无 🎉

## 行数不符清单（语义可能偏差，需人工核验）

- 无

## 时间窗口无数据清单（合理：数据时间范围不含当前月份）

- 🕐 `本月各工序的产量`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产线的产量`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产线的良率`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各设备的停机时长`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产线的停机时长`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产线的停机次数`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产品的库存量`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产品的工单数`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月各产品的抽检数`（0 行，SQL 含 CURRENT_DATE 月度锚点）
- 🕐 `本月累计停机时长`（0 行，SQL 含 CURRENT_DATE 月度锚点）

## 编译命中抽样（每类取前 3 条）

| 问法 | 期望 | 命中指标 | 行数 | SQL |
|---|---|---|---|---|
| 各工序的产量是多少 | group | 产量 | 8 | `SELECT d.process_name AS "工序", SUM(good_qty)::numeric AS "产量" FROM mes` |
| 各工序的产量排行 | rank | 产量 | 8 | `SELECT d.process_name AS "工序", SUM(good_qty)::numeric AS "产量" FROM mes` |
| 产量最高的工序是哪个 | top | 产量 | 1 | `SELECT d.process_name AS "工序", SUM(good_qty)::numeric AS "产量" FROM mes` |
| 总产量是多少 | total | 产量 | 1 | `SELECT SUM(good_qty)::numeric AS "产量" FROM mes_process_output f LIMIT ` |
| 每日的产量趋势 | trend | 产量 | 20 | `SELECT to_char(f.stat_date, 'YYYY-MM-DD') AS "日期", SUM(good_qty)::nume` |
| 已完成状态的工单数 | llm | 已完成工单数 | 4 | `SELECT f.order_status AS "状态", SUM(CASE WHEN order_status = 'completed` |
