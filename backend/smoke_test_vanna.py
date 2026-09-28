"""冒烟测试：验证 Vanna 风格记忆系统 + prompt 构造器（不连真实 DB）"""
import os, sys, tempfile
os.environ["EMBEDDING_DISABLE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.memory import AgentMemory
from agent.prompt_builder import build_sql_prompt

db = os.path.join(tempfile.gettempdir(), "vanna_test_mem.db")
if os.path.exists(db):
    os.remove(db)

m = AgentMemory(db)
print("=== 1. 文档记忆 ===")
m.add_documentation("良率 = 合格数量(good_qty) / 投入数量(input_qty) * 100%，数据来自 mes_process_output 表，按工序分组")
m.add_documentation("库存预警：当 available_qty < safety_stock_qty 时触发，来自 inv_inventory_snapshot 表")
docs = m.search_documentation("各工序的良率怎么算")
print("检索文档:", docs)
assert any("良率" in d for d in docs), "文档检索失败"

print("\n=== 2. SQL 示例记忆 ===")
m.add_sql("分析各工序良率", 'SELECT p.process_name AS "工序", SUM(o.good_qty) AS 合格, ROUND(SUM(o.good_qty)::numeric/NULLIF(SUM(o.input_qty),0)*100,2) AS "良率" FROM mes_process_output o JOIN dim_process p USING(process_id) GROUP BY p.process_name ORDER BY 良率 DESC', "mes_process_output")
m.add_sql("不良类型排行", "SELECT defect_type AS 不良类型, COUNT(*) AS 数量 FROM qms_defect_detail GROUP BY defect_type ORDER BY COUNT(*) DESC LIMIT 10", "qms_defect_detail")
sim = m.search_sql("各工序良率排行")
print("检索SQL示例:", [(s["question"], s["sql"][:60]) for s in sim])
assert sim and "良率" in sim[0]["question"], "SQL 示例检索失败"

print("\n=== 3. 重复问题更新（不膨胀） ===")
m.add_sql("分析各工序良率", "SELECT 1 -- 更新版", "mes_process_output", "feedback")
sim2 = m.search_sql("分析各工序良率")
print("检索:", [(s["question"], s["sql"][:20]) for s in sim2])
assert sim2[0]["sql"].startswith("SELECT 1"), "同问题应更新而非新增"
assert m.get_stats()["sql_examples"] == 2, f"示例数量应为2，实际 {m.get_stats()['sql_examples']}"

print("\n=== 4. 统计与列表 ===")
print("stats:", m.get_stats())

print("\n=== 5. Prompt 构造（Vanna 三段式） ===")
ddl = 'CREATE TABLE "mes_process_output" (\n  "output_id" bigint,\n  "good_qty" integer,\n  "input_qty" integer\n)'
prompt = build_sql_prompt(
    query="分析各工序良率",
    schema_context="# 可用表\n## mes_process_output",
    ddl_list=[ddl],
    docs=["良率 = good_qty/input_qty*100%"],
    sql_examples=sim,
    history_text="",
    agg_hint="",
    max_tokens=6000,
)
print(prompt[:1500])
assert "Question-SQL Pairs" in prompt and "CREATE TABLE" in prompt and "业务口径" in prompt, "Prompt 结构不完整"

print("\n=== 6. 删除 ===")
mem = m.list_memories()
first_id = mem["sql_examples"][0]["id"]
assert m.delete_memory(first_id), "删除失败"
assert m.get_stats()["sql_examples"] == 1, "删除后应为1条"

print("\n✅ 全部冒烟测试通过")
