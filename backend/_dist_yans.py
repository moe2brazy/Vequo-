# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, '.')
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from database import engine
from sqlalchemy import text

def q(sql):
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(sql)).fetchall()]

def dist(t, col, limit=10):
    try:
        rows = q(f"SELECT {col}, COUNT(*) FROM {t} GROUP BY {col} ORDER BY 2 DESC LIMIT {limit}")
        return rows
    except Exception as e:
        return f"ERR {e}"

def agg(t, col):
    try:
        return q(f"SELECT MIN({col}), MAX({col}), ROUND(AVG({col})::numeric,2) FROM {t}")[0]
    except Exception as e:
        return f"ERR {e}"

print("═══ 维度取值分布 ═══")
for t, col in [("dim_equipment","equipment_type"),("dim_equipment","equipment_status"),
               ("dim_production_line","workshop_name"),("dim_production_line","line_status"),
               ("dim_product","product_category"),("dim_product","is_active"),
               ("dim_process","is_key_process"),
               ("mes_work_order","order_status"),
               ("qms_inspection","inspection_result"),
               ("qms_defect_detail","defect_type"),("qms_defect_detail","severity_level"),
               ("eqp_downtime_record","downtime_reason"),("eqp_downtime_record","is_planned"),
               ("mes_process_output","shift_code"),
               ("inv_inventory_snapshot","warehouse_code")]:
    print(f"  {t}.{col}: {dist(t, col)}")

print("\n═══ 度量范围 ═══")
for t, col in [("mes_process_output","input_qty"),("mes_process_output","good_qty"),
               ("mes_process_output","defect_qty"),("mes_process_output","rework_qty"),
               ("mes_work_order","plan_qty"),
               ("qms_inspection","sample_qty"),("qms_inspection","defect_qty"),
               ("qms_defect_detail","defect_qty"),
               ("eqp_downtime_record","downtime_minutes"),
               ("inv_inventory_snapshot","available_qty"),("inv_inventory_snapshot","frozen_qty"),
               ("inv_inventory_snapshot","safety_stock_qty"),
               ("dim_process","standard_yield_rate")]:
    print(f"  {t}.{col}: min/max/avg = {agg(t, col)}")

print("\n═══ 时间跨度 ═══")
for t, col in [("mes_process_output","stat_date"),("mes_work_order","start_date"),
               ("mes_work_order","end_date"),("qms_inspection","inspection_date"),
               ("inv_inventory_snapshot","snapshot_date"),("eqp_downtime_record","start_time")]:
    print(f"  {t}.{col}: {agg(t, col)}")

print("\n═══ 关键关系样例 ═══")
print("  inv 预警(available<safety) 条数:", q("SELECT COUNT(*) FROM inv_inventory_snapshot WHERE available_qty < safety_stock_qty")[0][0])
print("  inv 冻结>0 条数:", q("SELECT COUNT(*) FROM inv_inventory_snapshot WHERE frozen_qty > 0")[0][0])
print("  eqp 计划内停机:", q("SELECT COUNT(*) FROM eqp_downtime_record WHERE is_planned")[0][0], "/ 336")
print("  wo 各状态:", q("SELECT order_status, COUNT(*) FROM mes_work_order GROUP BY order_status"))
print("  poc 返工>0:", q("SELECT COUNT(*) FROM mes_process_output WHERE rework_qty>0")[0][0], "/ 2752")
print("  qms 不良率(defect/sample) max:", q("SELECT MAX(defect_qty::float/sample_qty) FROM qms_inspection")[0][0])
print("  qdd 严重度=critical:", q("SELECT COUNT(*) FROM qms_defect_detail WHERE severity_level='critical'")[0][0])
