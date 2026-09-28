"""数据库表元数据 — 完整 10 张制造业务表"""

TABLES = [
    {
        "table_name": "mes_process_output",
        "table_alias": "工序产量表",
        "category": "fact",
        "description": "按工单、工序、产线、日期记录投入、合格、不良、返工数量,是产量与良率分析的核心事实表。",
        "row_count": 2752,
        "keywords": ["产量", "良率", "工序", "合格", "不良", "投入", "返工", "生产", "产出",
                     "不合格", "不合格品", "次品"],
        "related_tables": ["mes_work_order", "dim_product", "dim_process", "dim_production_line"],
        "fields": [
            {"name": "output_id", "type": "bigint", "key": "PK", "description": "产量记录ID,主键", "sample": "10234"},
            {"name": "work_order_id", "type": "varchar", "key": "FK", "description": "工单ID → mes_work_order", "sample": "WO-2026-0142"},
            {"name": "product_id", "type": "varchar", "key": "FK", "description": "产品ID → dim_product", "sample": "P005"},
            {"name": "process_id", "type": "varchar", "key": "FK", "description": "工序ID → dim_process", "sample": "PR06"},
            {"name": "line_id", "type": "varchar", "key": "FK", "description": "产线ID → dim_production_line", "sample": "L01"},
            {"name": "stat_date", "type": "date", "key": "", "description": "统计日期", "sample": "2026-07-15"},
            {"name": "input_qty", "type": "integer", "key": "", "description": "投入数量", "sample": "320"},
            {"name": "good_qty", "type": "integer", "key": "", "description": "合格数量", "sample": "312"},
            {"name": "defect_qty", "type": "integer", "key": "", "description": "不良数量", "sample": "8"},
            {"name": "shift_code", "type": "char", "key": "", "description": "班次 (D白班/N夜班)", "sample": "D"},
        ],
    },
    {
        "table_name": "mes_work_order",
        "table_alias": "生产工单表",
        "category": "fact",
        "description": "记录所有生产工单的创建、计划、执行状态,是生产任务调度和执行跟踪的基础表。",
        "row_count": 344,
        "keywords": ["工单", "生产", "排产", "计划", "生产计划", "计划产量", "计划量", "执行", "工单状态"],
        "related_tables": ["mes_process_output", "dim_product", "dim_production_line"],
        "fields": [
            {"name": "work_order_id", "type": "varchar", "key": "PK", "description": "工单ID,主键", "sample": "WO-2026-0142"},
            {"name": "product_id", "type": "varchar", "key": "FK", "description": "产品ID → dim_product", "sample": "P005"},
            {"name": "line_id", "type": "varchar", "key": "FK", "description": "产线ID → dim_production_line", "sample": "L01"},
            {"name": "plan_qty", "type": "integer", "key": "", "description": "计划产量", "sample": "500"},
            {"name": "actual_qty", "type": "integer", "key": "", "description": "实际产量（本表已含该字段，比较 plan_qty > actual_qty 直接可用，无需从其他表聚合）", "sample": "488"},
            {"name": "start_date", "type": "date", "key": "", "description": "计划开始日期", "sample": "2026-07-14"},
            {"name": "end_date", "type": "date", "key": "", "description": "计划完成日期", "sample": "2026-07-16"},
            {"name": "status", "type": "varchar", "key": "", "description": "工单状态(进行中/已完成/已取消)", "sample": "进行中"},
        ],
    },
    {
        "table_name": "qms_inspection",
        "table_alias": "质量检验表",
        "category": "fact",
        "description": "记录各工序的质量抽检结果,包含抽检数、合格数、不良数和检验结论。",
        "row_count": 1376,
        "keywords": ["质量", "检验", "抽检", "合格率", "质检", "检测", "检验结果",
                     "不合格", "不合格率"],
        "related_tables": ["mes_work_order", "dim_product", "dim_process"],
        "fields": [
            {"name": "inspection_id", "type": "bigint", "key": "PK", "description": "检验记录ID", "sample": "5001"},
            {"name": "work_order_id", "type": "varchar", "key": "FK", "description": "工单ID → mes_work_order", "sample": "WO-2026-0142"},
            {"name": "product_id", "type": "varchar", "key": "FK", "description": "产品ID → dim_product", "sample": "P005"},
            {"name": "process_id", "type": "varchar", "key": "FK", "description": "工序ID → dim_process", "sample": "PR06"},
            {"name": "sample_qty", "type": "integer", "key": "", "description": "抽检数量", "sample": "50"},
            {"name": "defect_qty", "type": "integer", "key": "", "description": "不良数量", "sample": "2"},
            {"name": "inspection_date", "type": "date", "key": "", "description": "检验日期", "sample": "2026-07-15"},
            {"name": "result", "type": "varchar", "key": "", "description": "检验结论(合格/不合格)", "sample": "合格"},
        ],
    },
    {
        "table_name": "qms_defect_detail",
        "table_alias": "不良明细表",
        "category": "fact",
        "description": "记录每一条不良品的详细信息,包括不良类型、严重等级、责任工序和处置方式。",
        "row_count": 2115,
        "keywords": ["不良", "缺陷", "不良品", "不良类型", "缺陷分析", "质量", "故障", "次品",
                     "不合格", "不合格品"],
        "related_tables": ["mes_work_order", "dim_product", "dim_process"],
        "fields": [
            {"name": "defect_id", "type": "bigint", "key": "PK", "description": "不良记录ID", "sample": "8001"},
            {"name": "work_order_id", "type": "varchar", "key": "FK", "description": "工单ID", "sample": "WO-2026-0142"},
            {"name": "product_id", "type": "varchar", "key": "FK", "description": "产品ID", "sample": "P005"},
            {"name": "process_id", "type": "varchar", "key": "FK", "description": "责任工序ID", "sample": "PR06"},
            {"name": "defect_type", "type": "varchar", "key": "", "description": "不良类型(功能失效/参数超差/焊接不良等)", "sample": "功能失效"},
            {"name": "severity", "type": "varchar", "key": "", "description": "严重等级(critical/major/minor)", "sample": "major"},
            {"name": "disposal", "type": "varchar", "key": "", "description": "处置方式(返工/报废/让步)", "sample": "返工"},
        ],
    },
    {
        "table_name": "eqp_downtime_record",
        "table_alias": "设备停机记录表",
        "category": "fact",
        "description": "记录所有设备的停机事件,包括计划/非计划停机、停机原因和持续时长。",
        "row_count": 336,
        "keywords": ["设备", "停机", "宕机", "故障", "维护", "设备状态", "运行", "停机时长",
                     "不可用", "不可用分钟", "非计划", "非计划停机", "计划停机", "停机时间",
                     "停机分钟", "可用率", "OEE"],
        "related_tables": ["dim_equipment", "dim_production_line"],
        "fields": [
            {"name": "downtime_id", "type": "bigint", "key": "PK", "description": "停机记录ID", "sample": "2001"},
            {"name": "equipment_id", "type": "varchar", "key": "FK", "description": "设备ID → dim_equipment", "sample": "EQ-CNC-03"},
            {"name": "line_id", "type": "varchar", "key": "FK", "description": "产线ID", "sample": "L03"},
            {"name": "start_time", "type": "datetime", "key": "", "description": "停机开始时间", "sample": "2026-07-15 09:12:00"},
            {"name": "end_time", "type": "datetime", "key": "", "description": "停机结束时间", "sample": "2026-07-15 10:30:00"},
            {"name": "downtime_minutes", "type": "integer", "key": "", "description": "停机持续分钟数", "sample": "78"},
            {"name": "is_planned", "type": "boolean", "key": "", "description": "是否计划停机", "sample": "false"},
            {"name": "reason", "type": "varchar", "key": "", "description": "停机原因", "sample": "刀具磨损更换"},
        ],
    },
    {
        "table_name": "inv_inventory_snapshot",
        "table_alias": "库存快照表",
        "category": "fact",
        "description": "每日库存快照数据,记录各物料/产品在各仓库的库存水平,用于库存预警分析。",
        "row_count": 1004,
        "keywords": ["库存", "仓库", "物料", "存储", "安全库存", "预警", "呆滞", "周转"],
        "related_tables": ["dim_product"],
        "fields": [
            {"name": "snapshot_id", "type": "bigint", "key": "PK", "description": "快照记录ID", "sample": "3001"},
            {"name": "product_id", "type": "varchar", "key": "FK", "description": "产品/物料ID → dim_product", "sample": "P005"},
            {"name": "warehouse_code", "type": "varchar", "key": "", "description": "仓库编码", "sample": "WH-A1"},
            {"name": "available_qty", "type": "integer", "key": "", "description": "可用库存", "sample": "120"},
            {"name": "frozen_qty", "type": "integer", "key": "", "description": "冻结库存", "sample": "15"},
            {"name": "safety_stock_qty", "type": "integer", "key": "", "description": "安全库存阈值", "sample": "200"},
            {"name": "snapshot_date", "type": "date", "key": "", "description": "快照日期", "sample": "2026-07-15"},
        ],
    },
    {
        "table_name": "dim_product",
        "table_alias": "产品主数据表",
        "category": "master",
        "description": "产品主数据,包括产品编码、名称、规格、类型、单位等基础信息。",
        "row_count": 30,
        "keywords": ["产品", "型号", "规格", "产品信息", "机械", "零部件", "成品"],
        "related_tables": ["mes_process_output", "mes_work_order", "qms_inspection", "qms_defect_detail", "inv_inventory_snapshot"],
        "fields": [
            {"name": "product_id", "type": "varchar", "key": "PK", "description": "产品ID,主键", "sample": "P005"},
            {"name": "product_code", "type": "varchar", "key": "", "description": "产品编码", "sample": "CTRL-05-N"},
            {"name": "product_name", "type": "varchar", "key": "", "description": "产品名称", "sample": "控制器05·标准版"},
            {"name": "category", "type": "varchar", "key": "", "description": "产品分类(机械/电子/电气/结构件)", "sample": "电子"},
            {"name": "spec", "type": "varchar", "key": "", "description": "规格型号", "sample": "V3.2-标准"},
            {"name": "unit", "type": "varchar", "key": "", "description": "单位", "sample": "个"},
            {"name": "is_active", "type": "boolean", "key": "", "description": "是否有效", "sample": "true"},
        ],
    },
    {
        "table_name": "dim_process",
        "table_alias": "工序主数据表",
        "category": "master",
        "description": "工序基础信息,定义各道工序的名称、顺序、标准良率等。",
        "row_count": 8,
        "keywords": ["工序", "流程", "工艺", "SMT", "焊接", "检测", "测试", "包装", "装配"],
        "related_tables": ["mes_process_output", "qms_inspection", "qms_defect_detail"],
        "fields": [
            {"name": "process_id", "type": "varchar", "key": "PK", "description": "工序ID", "sample": "PR06"},
            {"name": "process_name", "type": "varchar", "key": "", "description": "工序名称", "sample": "功能测试"},
            {"name": "process_seq", "type": "integer", "key": "", "description": "工序顺序号", "sample": "6"},
            {"name": "is_critical", "type": "boolean", "key": "", "description": "是否关键工序", "sample": "true"},
            {"name": "std_yield_rate", "type": "decimal", "key": "", "description": "标准良率(%)", "sample": "97.0"},
            {"name": "department", "type": "varchar", "key": "", "description": "负责部门", "sample": "品保部"},
        ],
    },
    {
        "table_name": "dim_production_line",
        "table_alias": "产线主数据表",
        "category": "master",
        "description": "产线基础信息,包括产线名称、所属车间、主管、当前状态。",
        "row_count": 6,
        "keywords": ["产线", "车间", "生产线", "主管", "线体"],
        "related_tables": ["mes_process_output", "mes_work_order", "eqp_downtime_record"],
        "fields": [
            {"name": "line_id", "type": "varchar", "key": "PK", "description": "产线ID", "sample": "L01"},
            {"name": "line_name", "type": "varchar", "key": "", "description": "产线名称", "sample": "一车间-1号线"},
            {"name": "workshop", "type": "varchar", "key": "", "description": "所属车间", "sample": "一车间"},
            {"name": "supervisor", "type": "varchar", "key": "", "description": "产线主管", "sample": "主管1"},
            {"name": "status", "type": "varchar", "key": "", "description": "当前状态(运行中/维护中/空闲)", "sample": "运行中"},
            {"name": "active_orders", "type": "integer", "key": "", "description": "在产工单数", "sample": "3"},
        ],
    },
    {
        "table_name": "dim_equipment",
        "table_alias": "设备主数据表",
        "category": "master",
        "description": "生产设备的基础信息,包括设备名称、类型、所属产线、购置日期等。",
        "row_count": 48,
        "keywords": ["设备", "机器", "装备", "机械", "CNC", "机床", "仪器", "设备台账"],
        "related_tables": ["eqp_downtime_record", "dim_production_line"],
        "fields": [
            {"name": "equipment_id", "type": "varchar", "key": "PK", "description": "设备ID", "sample": "EQ-CNC-03"},
            {"name": "equipment_name", "type": "varchar", "key": "", "description": "设备名称", "sample": "CNC加工中心#03"},
            {"name": "equipment_type", "type": "varchar", "key": "", "description": "设备类型(CNC/注塑机/贴片机/机械臂等)", "sample": "CNC"},
            {"name": "line_id", "type": "varchar", "key": "FK", "description": "所属产线ID", "sample": "L03"},
            {"name": "model", "type": "varchar", "key": "", "description": "设备型号", "sample": "VMC850E"},
            {"name": "purchase_date", "type": "date", "key": "", "description": "购置日期", "sample": "2024-03-15"},
            {"name": "status", "type": "varchar", "key": "", "description": "设备状态(运行/停机/维修)", "sample": "运行"},
        ],
    },
    {
        "table_name": "test_factories",
        "table_alias": "测试工厂表",
        "category": "dim",
        "description": "工厂信息表，记录工厂名称、所在城市、产能和成立日期。",
        "row_count": 3,
        "keywords": ["工厂", "厂房", "生产基地", "产能", "城市"],
        "related_tables": ["test_orders"],
        "fields": [
            {"name": "factory_id", "type": "integer", "key": "PK", "description": "工厂ID"},
            {"name": "factory_name", "type": "varchar", "key": "", "description": "工厂名称"},
            {"name": "city", "type": "varchar", "key": "", "description": "所在城市"},
            {"name": "capacity", "type": "integer", "key": "", "description": "产能"},
            {"name": "established", "type": "date", "key": "", "description": "成立日期"},
        ],
    },
    {
        "table_name": "test_orders",
        "table_alias": "测试订单表",
        "category": "fact",
        "description": "订单表，记录客户名称、工厂、产品、数量和金额等订单信息。",
        "row_count": 3,
        "keywords": ["订单", "客户", "采购", "购买", "产品", "金额", "价格"],
        "related_tables": ["test_factories"],
        "fields": [
            {"name": "order_id", "type": "integer", "key": "PK", "description": "订单ID"},
            {"name": "customer_name", "type": "varchar", "key": "", "description": "客户名称"},
            {"name": "factory_id", "type": "integer", "key": "FK", "description": "工厂ID → test_factories"},
            {"name": "product_name", "type": "varchar", "key": "", "description": "产品名称"},
            {"name": "quantity", "type": "integer", "key": "", "description": "数量"},
            {"name": "unit_price", "type": "numeric", "key": "", "description": "单价"},
            {"name": "order_date", "type": "date", "key": "", "description": "订单日期"},
            {"name": "status", "type": "varchar", "key": "", "description": "状态(completed/processing/pending)"},
        ],
    },
    {
        "table_name": "test_materials",
        "table_alias": "测试材料表",
        "category": "dim",
        "description": "原材料库存表，记录材料名称、供应商、库存量、安全库存和成本。",
        "row_count": 3,
        "keywords": ["材料", "物料", "原材料", "供应商", "库存", "采购", "成本"],
        "related_tables": [],
        "fields": [
            {"name": "material_id", "type": "integer", "key": "PK", "description": "材料ID"},
            {"name": "material_name", "type": "varchar", "key": "", "description": "材料名称"},
            {"name": "supplier", "type": "varchar", "key": "", "description": "供应商"},
            {"name": "unit", "type": "varchar", "key": "", "description": "单位"},
            {"name": "stock_qty", "type": "integer", "key": "", "description": "库存数量"},
            {"name": "safety_qty", "type": "integer", "key": "", "description": "安全库存"},
            {"name": "unit_cost", "type": "numeric", "key": "", "description": "单价成本"},
        ],
    },
    # ═══════════════ 123 库（factory schema，2026-08-11 补充）═══════════════
    {
        "table_name": "factory.workshop",
        "table_alias": "车间表",
        "category": "dim",
        "description": "车间维度表：记录工厂下属的各个车间（如冲压车间/焊接车间），是生产、人员归集的核心维度。",
        "row_count": 0,
        "keywords": ["车间", "workshop", "生产单元", "工厂"],
        "related_tables": ["factory.factory", "factory.department", "factory.production_line", "factory.employee"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "车间ID"},
            {"name": "factory_id", "type": "bigint", "key": "FK", "description": "所属工厂ID → factory.factory"},
            {"name": "department_id", "type": "bigint", "key": "FK", "description": "关联部门ID → factory.department"},
            {"name": "code", "type": "varchar", "key": "", "description": "车间编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "车间名称"},
            {"name": "manager", "type": "varchar", "key": "", "description": "车间主任"},
            {"name": "area_sqm", "type": "numeric", "key": "", "description": "车间面积(平方米)"},
            {"name": "shift_count", "type": "smallint", "key": "", "description": "班次数"},
            {"name": "status", "type": "varchar", "key": "", "description": "车间状态(运行/停产/改建)"},
        ],
    },
    {
        "table_name": "factory.department",
        "table_alias": "部门表",
        "category": "dim",
        "description": "组织架构部门维度表：记录公司各部门（如生产部/质量部/财务部），员工按部门归属。",
        "row_count": 0,
        "keywords": ["部门", "department", "组织", "编制"],
        "related_tables": ["factory.employee", "factory.factory", "factory.workshop"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "部门ID"},
            {"name": "factory_id", "type": "bigint", "key": "FK", "description": "所属工厂ID"},
            {"name": "parent_id", "type": "bigint", "key": "", "description": "上级部门ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "部门编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "部门名称"},
            {"name": "manager", "type": "varchar", "key": "", "description": "部门负责人"},
            {"name": "cost_center", "type": "varchar", "key": "", "description": "成本中心"},
        ],
    },
    {
        "table_name": "factory.employee",
        "table_alias": "员工表",
        "category": "dim",
        "description": "员工维度表：记录在职员工的个人资料、所属部门/车间、岗位、学历、薪资，是人事分析的核心表。",
        "row_count": 0,
        "keywords": ["员工", "employee", "人员", "工资", "薪资", "学历", "入职", "出勤"],
        "related_tables": ["factory.department", "factory.workshop", "factory.attendance"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "员工ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "工号"},
            {"name": "name", "type": "varchar", "key": "", "description": "姓名"},
            {"name": "gender", "type": "varchar", "key": "", "description": "性别(男/女)"},
            {"name": "birthday", "type": "date", "key": "", "description": "出生日期"},
            {"name": "hire_date", "type": "date", "key": "", "description": "入职日期"},
            {"name": "department_id", "type": "bigint", "key": "FK", "description": "所属部门ID → factory.department"},
            {"name": "workshop_id", "type": "bigint", "key": "FK", "description": "所属车间ID → factory.workshop"},
            {"name": "position", "type": "varchar", "key": "", "description": "岗位"},
            {"name": "education", "type": "varchar", "key": "", "description": "学历(本科/大专等)"},
            {"name": "salary", "type": "numeric", "key": "", "description": "月工资"},
            {"name": "status", "type": "varchar", "key": "", "description": "员工状态(在职/离职/试用)"},
        ],
    },
    {
        "table_name": "factory.attendance",
        "table_alias": "考勤表",
        "category": "fact",
        "description": "员工考勤记录：按日记录员工打卡/出勤状态（正常/迟到/缺勤等），是出勤与缺勤分析的事实表。",
        "row_count": 0,
        "keywords": ["考勤", "attendance", "出勤", "缺勤", "打卡"],
        "related_tables": ["factory.employee"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "考勤记录ID"},
            {"name": "employee_id", "type": "bigint", "key": "FK", "description": "员工ID → factory.employee"},
            {"name": "work_date", "type": "date", "key": "", "description": "工作日期"},
            {"name": "shift", "type": "varchar", "key": "", "description": "班次"},
            {"name": "check_in", "type": "time", "key": "", "description": "上班打卡时间"},
            {"name": "check_out", "type": "time", "key": "", "description": "下班打卡时间"},
            {"name": "status", "type": "varchar", "key": "", "description": "出勤状态(正常/迟到/早退/缺勤/请假)"},
        ],
    },
    {
        "table_name": "factory.factory",
        "table_alias": "工厂表",
        "category": "dim",
        "description": "工厂维度表：记录公司下属的各工厂基本信息（名称/地址/建厂时间），是多工厂架构的顶层维度。",
        "row_count": 0,
        "keywords": ["工厂", "factory", "厂区", "基地"],
        "related_tables": ["factory.workshop", "factory.department"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "工厂ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "工厂编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "工厂名称"},
            {"name": "province", "type": "varchar", "key": "", "description": "省份"},
            {"name": "city", "type": "varchar", "key": "", "description": "城市"},
            {"name": "manager", "type": "varchar", "key": "", "description": "负责人"},
            {"name": "established_date", "type": "date", "key": "", "description": "建厂日期"},
            {"name": "status", "type": "varchar", "key": "", "description": "工厂状态(启用/停用)"},
        ],
    },
    {
        "table_name": "factory.production_line",
        "table_alias": "产线表",
        "category": "dim",
        "description": "产线维度表：记录各车间下的生产线（装配线/焊接线等）及产能，是生产分析的核心维度。",
        "row_count": 0,
        "keywords": ["产线", "生产线", "production line", "line", "班组"],
        "related_tables": ["factory.workshop", "factory.production_record", "factory.work_order"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "产线ID"},
            {"name": "workshop_id", "type": "bigint", "key": "FK", "description": "所属车间ID → factory.workshop"},
            {"name": "code", "type": "varchar", "key": "", "description": "产线编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "产线名称"},
            {"name": "capacity_per_hr", "type": "numeric", "key": "", "description": "每小时产能"},
            {"name": "status", "type": "varchar", "key": "", "description": "产线状态(运行/调试/停机)"},
        ],
    },
    {
        "table_name": "factory.production_record",
        "table_alias": "生产记录表",
        "category": "fact",
        "description": "生产日报事实表：按产线、工单、日期记录实际产量、不良量、工时，是产量/不良率/工时分析的核心事实表。",
        "row_count": 0,
        "keywords": ["生产", "production", "产量", "不良", "工时", "日报", "产出"],
        "related_tables": ["factory.work_order", "factory.production_line", "factory.employee"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "生产记录ID"},
            {"name": "wo_id", "type": "bigint", "key": "FK", "description": "工单ID → factory.work_order"},
            {"name": "line_id", "type": "bigint", "key": "FK", "description": "产线ID → factory.production_line"},
            {"name": "employee_id", "type": "bigint", "key": "FK", "description": "员工ID → factory.employee"},
            {"name": "work_date", "type": "date", "key": "", "description": "生产日期"},
            {"name": "shift", "type": "varchar", "key": "", "description": "班次"},
            {"name": "qty_produced", "type": "numeric", "key": "", "description": "实际产量"},
            {"name": "qty_defect", "type": "numeric", "key": "", "description": "不良数量"},
            {"name": "work_hours", "type": "numeric", "key": "", "description": "工时(小时)"},
        ],
    },
    {
        "table_name": "factory.work_order",
        "table_alias": "工单表",
        "category": "fact",
        "description": "生产工单表：记录生产工单的计划/实际数量、起止日期、状态（已完成/进行中/延期等），是工单完成率与计划达成分析的事实表。",
        "row_count": 0,
        "keywords": ["工单", "work order", "工单号", "生产任务", "计划", "按期", "延期"],
        "related_tables": ["factory.product", "factory.production_line", "factory.production_record"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "工单ID"},
            {"name": "wo_no", "type": "varchar", "key": "", "description": "工单编号"},
            {"name": "product_id", "type": "bigint", "key": "FK", "description": "产品ID → factory.product"},
            {"name": "line_id", "type": "bigint", "key": "FK", "description": "产线ID → factory.production_line"},
            {"name": "plan_qty", "type": "numeric", "key": "", "description": "计划数量"},
            {"name": "produced_qty", "type": "numeric", "key": "", "description": "已完成数量"},
            {"name": "scrap_qty", "type": "numeric", "key": "", "description": "报废数量"},
            {"name": "status", "type": "varchar", "key": "", "description": "工单状态(计划/生产中/已完成/已关闭/暂停)"},
            {"name": "plan_start", "type": "date", "key": "", "description": "计划开始日期"},
            {"name": "plan_end", "type": "date", "key": "", "description": "计划完成日期"},
            {"name": "actual_start", "type": "date", "key": "", "description": "实际开始日期"},
            {"name": "actual_end", "type": "date", "key": "", "description": "实际完成日期"},
        ],
    },
    {
        "table_name": "factory.product",
        "table_alias": "产品表",
        "category": "dim",
        "description": "产品维度表：记录产品编码、名称、类别、规格、标准价，销售/生产/质检按产品归集。",
        "row_count": 0,
        "keywords": ["产品", "product", "成品", "类别", "sku"],
        "related_tables": ["factory.sales_order_item", "factory.work_order", "factory.quality_inspection"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "产品ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "产品编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "产品名称"},
            {"name": "category", "type": "varchar", "key": "", "description": "产品类别(如 A类/B类)"},
            {"name": "spec", "type": "varchar", "key": "", "description": "规格型号"},
            {"name": "unit", "type": "varchar", "key": "", "description": "计量单位"},
            {"name": "standard_price", "type": "numeric", "key": "", "description": "标准售价"},
            {"name": "lead_time_days", "type": "smallint", "key": "", "description": "生产周期(天)"},
        ],
    },
    {
        "table_name": "factory.material",
        "table_alias": "物料表",
        "category": "dim",
        "description": "物料维度表：记录原材料/辅料的编码、名称、类别、规格、安全库存、单价，采购与库存按物料归集。",
        "row_count": 0,
        "keywords": ["物料", "material", "原材料", "原料", "辅料", "安全库存"],
        "related_tables": ["factory.purchase_order_item", "factory.inventory", "factory.supplier"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "物料ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "物料编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "物料名称"},
            {"name": "category", "type": "varchar", "key": "", "description": "物料类别(原材料/辅料/包装等)"},
            {"name": "spec", "type": "varchar", "key": "", "description": "规格"},
            {"name": "unit", "type": "varchar", "key": "", "description": "计量单位"},
            {"name": "safety_stock", "type": "numeric", "key": "", "description": "安全库存量"},
            {"name": "unit_price", "type": "numeric", "key": "", "description": "采购单价"},
            {"name": "supplier_id", "type": "bigint", "key": "FK", "description": "主要供应商ID → factory.supplier"},
        ],
    },
    {
        "table_name": "factory.inventory",
        "table_alias": "库存表",
        "category": "fact",
        "description": "库存快照事实表：按仓库、物料/产品类型记录当前库存数量与安全库存，是库存水位、缺货预警分析的核心表。",
        "row_count": 0,
        "keywords": ["库存", "inventory", "存量", "安全库存", "缺货", "告急", "仓库"],
        # 注意：warehouse 是 varchar 仓库编码，不是外键；factory.warehouse 表不存在，
        # 之前误列为 related_tables 会诱导 LLM 尝试 JOIN factory.warehouse → 表不存在报错/编造字段。
        "related_tables": ["factory.material", "factory.product"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "库存记录ID"},
            {"name": "warehouse", "type": "varchar", "key": "", "description": "仓库编码/名称"},
            {"name": "item_type", "type": "varchar", "key": "", "description": "库存项类型：枚举值为 material(物料)/product(产品)，不要用中文'物料'过滤", "sample": "material"},
            {"name": "item_id", "type": "bigint", "key": "FK", "description": "物料或产品ID → factory.material / factory.product"},
            {"name": "quantity", "type": "numeric", "key": "", "description": "当前库存数量"},
            {"name": "unit", "type": "varchar", "key": "", "description": "计量单位"},
            {"name": "safe_stock", "type": "numeric", "key": "", "description": "安全库存量"},
            {"name": "last_update", "type": "timestamp", "key": "", "description": "最后更新时间"},
        ],
    },
    {
        "table_name": "factory.supplier",
        "table_alias": "供应商表",
        "category": "dim",
        "description": "供应商维度表：记录供应商基本资料、类别、评级、状态，采购订单按供应商归集。",
        "row_count": 0,
        "keywords": ["供应商", "supplier", "供货商", "评级", "采购"],
        "related_tables": ["factory.purchase_order", "factory.material"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "供应商ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "供应商编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "供应商名称"},
            {"name": "short_name", "type": "varchar", "key": "", "description": "简称"},
            {"name": "province", "type": "varchar", "key": "", "description": "省份"},
            {"name": "city", "type": "varchar", "key": "", "description": "城市"},
            {"name": "category", "type": "varchar", "key": "", "description": "供应商类别(原材料/零部件/服务等)"},
            {"name": "rating", "type": "smallint", "key": "", "description": "评级(1-5)"},
            {"name": "status", "type": "varchar", "key": "", "description": "供应商状态(合作中/暂停/淘汰)"},
        ],
    },
    {
        "table_name": "factory.purchase_order",
        "table_alias": "采购订单表",
        "category": "fact",
        "description": "采购订单事实表：记录采购单号、供应商、下单/到货日期、状态、总金额，是采购金额/采购周期分析的核心表。",
        "row_count": 0,
        "keywords": ["采购", "purchase", "采购单", "采购订单", "金额", "供应商"],
        "related_tables": ["factory.supplier", "factory.purchase_order_item"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "采购单ID"},
            {"name": "po_no", "type": "varchar", "key": "", "description": "采购单编号"},
            {"name": "supplier_id", "type": "bigint", "key": "FK", "description": "供应商ID → factory.supplier"},
            {"name": "order_date", "type": "date", "key": "", "description": "下单日期"},
            {"name": "expected_date", "type": "date", "key": "", "description": "预计到货日期"},
            {"name": "status", "type": "varchar", "key": "", "description": "采购单状态(草稿/已审核/部分收货/已收货/已关闭)"},
            {"name": "total_amount", "type": "numeric", "key": "", "description": "订单总金额"},
            {"name": "buyer", "type": "varchar", "key": "", "description": "采购员"},
        ],
    },
    {
        "table_name": "factory.purchase_order_item",
        "table_alias": "采购订单明细表",
        "category": "fact",
        "description": "采购订单明细事实表：按采购单逐行记录物料、数量、单价、金额、收货量，是采购金额拆解分析的事实表。",
        "row_count": 0,
        "keywords": ["采购明细", "purchase item", "采购金额", "物料"],
        "related_tables": ["factory.purchase_order", "factory.material"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "明细ID"},
            {"name": "po_id", "type": "bigint", "key": "FK", "description": "采购单ID → factory.purchase_order"},
            {"name": "material_id", "type": "bigint", "key": "FK", "description": "物料ID → factory.material"},
            {"name": "qty", "type": "numeric", "key": "", "description": "采购数量"},
            {"name": "unit_price", "type": "numeric", "key": "", "description": "单价"},
            {"name": "amount", "type": "numeric", "key": "", "description": "金额小计"},
            {"name": "received_qty", "type": "numeric", "key": "", "description": "已收货数量"},
        ],
    },
    {
        "table_name": "factory.customer",
        "table_alias": "客户表",
        "category": "dim",
        "description": "客户维度表：记录客户资料、行业、信用等级，销售订单按客户归集。",
        "row_count": 0,
        "keywords": ["客户", "customer", "买主", "信用"],
        "related_tables": ["factory.sales_order"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "客户ID"},
            {"name": "code", "type": "varchar", "key": "", "description": "客户编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "客户名称"},
            {"name": "province", "type": "varchar", "key": "", "description": "省份"},
            {"name": "city", "type": "varchar", "key": "", "description": "城市"},
            {"name": "industry", "type": "varchar", "key": "", "description": "所属行业"},
            {"name": "credit_level", "type": "varchar", "key": "", "description": "信用等级"},
            {"name": "status", "type": "varchar", "key": "", "description": "客户状态(合作中/流失/潜在)"},
        ],
    },
    {
        "table_name": "factory.sales_order",
        "table_alias": "销售订单表",
        "category": "fact",
        "description": "销售订单事实表：记录销售单号、客户、下单/交期、状态、总金额，是销售额/订单分析的核心表。",
        "row_count": 0,
        "keywords": ["销售", "sales", "订单", "销售额", "客户", "已交付"],
        "related_tables": ["factory.customer", "factory.sales_order_item"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "销售单ID"},
            {"name": "so_no", "type": "varchar", "key": "", "description": "销售单编号"},
            {"name": "customer_id", "type": "bigint", "key": "FK", "description": "客户ID → factory.customer"},
            {"name": "order_date", "type": "date", "key": "", "description": "下单日期"},
            {"name": "due_date", "type": "date", "key": "", "description": "交货日期"},
            {"name": "status", "type": "varchar", "key": "", "description": "销售单状态(草稿/已确认/发货中/已完成/已关闭)"},
            {"name": "total_amount", "type": "numeric", "key": "", "description": "订单总金额"},
            {"name": "salesman", "type": "varchar", "key": "", "description": "业务员"},
        ],
    },
    {
        "table_name": "factory.sales_order_item",
        "table_alias": "销售订单明细表",
        "category": "fact",
        "description": "销售订单明细事实表：按销售单逐行记录产品、数量、单价、金额、发货量，是销售额拆解分析的事实表。",
        "row_count": 0,
        "keywords": ["销售明细", "sales item", "销售额", "产品"],
        "related_tables": ["factory.sales_order", "factory.product"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "明细ID"},
            {"name": "so_id", "type": "bigint", "key": "FK", "description": "销售单ID → factory.sales_order"},
            {"name": "product_id", "type": "bigint", "key": "FK", "description": "产品ID → factory.product"},
            {"name": "qty", "type": "numeric", "key": "", "description": "销售数量"},
            {"name": "unit_price", "type": "numeric", "key": "", "description": "单价"},
            {"name": "amount", "type": "numeric", "key": "", "description": "金额小计"},
            {"name": "shipped_qty", "type": "numeric", "key": "", "description": "已发货数量"},
        ],
    },
    {
        "table_name": "factory.equipment",
        "table_alias": "设备表",
        "category": "dim",
        "description": "设备维度表：记录设备编码、名称、类别、型号、所属产线/车间、状态，是设备管理与停机分析的核心维度。",
        "row_count": 0,
        "keywords": ["设备", "equipment", "机器", "资产"],
        "related_tables": ["factory.production_line", "factory.workshop", "factory.equipment_maintenance"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "设备ID"},
            {"name": "line_id", "type": "bigint", "key": "FK", "description": "所属产线ID → factory.production_line"},
            {"name": "workshop_id", "type": "bigint", "key": "FK", "description": "所属车间ID → factory.workshop"},
            {"name": "code", "type": "varchar", "key": "", "description": "设备编码"},
            {"name": "name", "type": "varchar", "key": "", "description": "设备名称"},
            {"name": "category", "type": "varchar", "key": "", "description": "设备类别"},
            {"name": "model", "type": "varchar", "key": "", "description": "型号"},
            {"name": "manufacturer", "type": "varchar", "key": "", "description": "制造商"},
            {"name": "purchase_date", "type": "date", "key": "", "description": "购置日期"},
            {"name": "status", "type": "varchar", "key": "", "description": "设备状态(正常/维修中/闲置)"},
            {"name": "last_maintenance_date", "type": "date", "key": "", "description": "最近维护日期"},
        ],
    },
    {
        "table_name": "factory.equipment_maintenance",
        "table_alias": "设备维护记录表",
        "category": "fact",
        "description": "设备维护事实表：记录每次维护的类型（保养/维修）、日期、技术员、费用、停机时长，是维护成本与停机分析的事实表。",
        "row_count": 0,
        "keywords": ["维护", "maintenance", "保养", "维修", "设备", "停机"],
        "related_tables": ["factory.equipment"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "维护记录ID"},
            {"name": "equipment_id", "type": "bigint", "key": "FK", "description": "设备ID → factory.equipment"},
            {"name": "type", "type": "varchar", "key": "", "description": "维护类型(例行保养/预防性维护/故障维修/大修)"},
            {"name": "mdate", "type": "date", "key": "", "description": "维护日期"},
            {"name": "technician", "type": "varchar", "key": "", "description": "技术员"},
            {"name": "cost", "type": "numeric", "key": "", "description": "维护费用"},
            {"name": "downtime_hours", "type": "numeric", "key": "", "description": "停机时长(小时)"},
        ],
    },
    {
        "table_name": "factory.quality_inspection",
        "table_alias": "质检记录表",
        "category": "fact",
        "description": "质量检验事实表：按工单/产品记录抽检数、合格数、不良数、检验结论（合格/不合格），是合格率/不良率分析的核心事实表。",
        "row_count": 0,
        "keywords": ["质检", "quality", "检验", "合格率", "不良率", "抽检", "检测"],
        "related_tables": ["factory.work_order", "factory.product"],
        "fields": [
            {"name": "id", "type": "bigint", "key": "PK", "description": "质检记录ID"},
            {"name": "wo_id", "type": "bigint", "key": "FK", "description": "工单ID → factory.work_order"},
            {"name": "product_id", "type": "bigint", "key": "FK", "description": "产品ID → factory.product"},
            {"name": "inspect_date", "type": "date", "key": "", "description": "检验日期"},
            {"name": "inspector", "type": "varchar", "key": "", "description": "检验员"},
            {"name": "sample_qty", "type": "numeric", "key": "", "description": "抽检数量"},
            {"name": "pass_qty", "type": "numeric", "key": "", "description": "合格数量"},
            {"name": "fail_qty", "type": "numeric", "key": "", "description": "不合格数量"},
            {"name": "result", "type": "varchar", "key": "", "description": "检验结论(合格/不合格)"},
            {"name": "defect_type", "type": "varchar", "key": "", "description": "缺陷类型(功能失效/尺寸超差/材料缺陷/漏工序/焊点虚焊/表面划伤/装配不良)"},
        ],
    },
]


def find_table_by_name(name: str) -> dict | None:
    """精确匹配表名"""
    for t in TABLES:
        if t["table_name"] == name:
            return t
    return None


# 紧贴在关键词前的否定字。命中时若被它们修饰，这次出现不算"肯定命中"。
# 起因（2026-09-21）：「各产线的不可用分钟中，非计划部分的占比」里的「非计划」
# 被子串匹配当成「计划」命中 mes_work_order（它的关键词里有个裸词「计划」），
# 工单表挤掉正确的 eqp_downtime_record → LLM 拿工单表的 order_status 硬凑
# '非计划' → CASE WHEN 一条都不中 → 全 0 → 全 0 列不算数值列 → 图表不生成。
_NEG_PREFIXES = ("非", "不", "无", "未", "没")


def _keyword_hit(kw: str, query: str, query_lower: str) -> bool:
    """关键词是否在问句里"肯定地"出现（否定式不算）。

    逐次定位 kw 的每个出现位置，只要紧邻的前一个字是否定词就跳过这次出现。
    只处理紧邻前缀，覆盖「非/不/无/未/没」这几种最常见写法。
    「合格」在「不合格品」里不命中；「计划」在「非计划」里不命中；
    但若元数据里登记了完整词条「非计划」，它自己仍会正常命中。
    """
    start = 0
    while True:
        i = query.find(kw, start)
        if i < 0:
            break
        if i == 0 or query[i - 1] not in _NEG_PREFIXES:
            return True
        start = i + 1
    # 反向：问句本身是关键词的子串（如短句「非计划」对上词条「非计划停机」）
    return query_lower in kw


def search_tables(query: str) -> list[dict]:
    """关键词搜索匹配的表,返回匹配度排序列表

    排序的第一优先级是「**问句有没有明确点名这张表**」（写了表别名或表名），
    第二优先级才是关键词累计分，最后对同名包含关系取更长的别名（更具体的那个）。

    为什么要有第一优先级（2026-09-20 修复）：
      旧实现只判断"整句是不是表名/别名的子串"（`query_lower in table_alias`），方向反了 ——
      几乎永远不成立。于是「用生产工单表训练模型预测工单的实际产量」里完整的
      「生产工单表」不加一分，而「产量」「生产」两个关键词同时命中工序产量表、
      「工单」「生产」同时命中生产工单表，两表并列 12 分，再按定义顺序取胜 ——
      点名的表反而落选。现在点名即 +40，不会再被关键词巧合盖过。
    """
    results = []
    query_lower = query.lower()
    for idx, t in enumerate(TABLES):
        score = 0
        named = False
        alias = t["table_alias"] or ""
        tname = (t["table_name"] or "").lower()
        # 1) 明确点名：别名/表名整体出现在问句里
        if len(alias) >= 2 and alias in query:
            score += 40
            named = True
        if len(tname) >= 4 and tname in query_lower:
            score += 30
            named = True
        # 2) 简称：问句本身是别名/表名的子串（如"工序产量"）
        if len(query) >= 2 and (query in alias or query_lower in tname):
            score += 10
        # 3) 描述整体命中（保留原语义）
        if query_lower in t["description"]:
            score += 5
        # 4) 关键词命中；长关键词更具体，权重略高（"工单状态">"生产"）
        #    否定式不计分：「非计划」不给「计划」加分（走 _keyword_hit 逐次定位）
        for kw in t["keywords"]:
            if _keyword_hit(kw, query, query_lower):
                score += 4 + min(len(kw), 4)
        if score > 0:
            # (是否点名, 分数, 别名词长, 定义顺序) —— 全部降序，末位用 -idx 保证稳定
            results.append(((1 if named else 0, score, len(alias), -idx), t))
    results.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in results]
