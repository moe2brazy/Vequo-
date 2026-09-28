import sys
sys.path.insert(0, '.')
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from agent.metric_registry import get_effective_metrics, find_metrics
names = [m["name"] for m in get_effective_metrics()]
print("yans 有效指标数:", len(names))
for bad in ["实际完成数量","工单完成率","库存周转天数","返工数量","设备数"]:
    print(f"  {bad}: {'✅在' if bad in names else '❌不在'}")
print("\n维度保护验证（列缺失维度不应命中）:")
from agent.metric_compiler import _detect_dim
for q, ft in [("各产品的缺陷数", "qms_defect_detail"), ("各处置方式的缺陷数", "qms_defect_detail"),
              ("各工序的缺陷数", "qms_defect_detail"), ("各检验的缺陷数", "qms_defect_detail"),
              ("各设备状态的设备数", "dim_equipment"), ("各工单状态的工单数", "mes_work_order")]:
    print(f"  {q} [{ft}] → {_detect_dim(q, ft)}")
