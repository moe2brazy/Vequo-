import statistics
import os
import re
import tempfile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request, Header
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from database import get_db, get_db_type, quote_ident, import_csv_to_db, import_database_source, collect_import_candidate_files
from auth import require_roles, get_current_user
from agent.field_semantics import explain_field_detailed
from pydantic import BaseModel
from typing import Any, Optional, List
from security import enforcer

router = APIRouter(prefix="/api/tables", tags=["数据表"])

# 标识符校验：表名/列名只允许字母、数字、下划线（表名可含 schema 点号），杜绝注入
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _table_label_meta() -> dict:
    """从 db/metadata.py 取表中文别名与业务场景（表名 → {label, scene}）

    兼容两种表名形态：metadata 里的全名（factory.work_order）与
    inspector 返回的裸名（work_order，PG 已 SET search_path）。
    """
    try:
        from db.metadata import TABLES
        out = {}
        for t in TABLES:
            n = t.get("table_name", "")
            if not n:
                continue
            meta = {
                "label": t.get("table_alias") or n,
                "scene": _table_scene(n),
            }
            out[n] = meta
            bare = n.split(".")[-1]
            if bare != n:
                out.setdefault(bare, meta)
        return out
    except Exception:
        return {}


def _table_scene(table_name: str) -> str:
    """按表名推导业务场景（用于图谱节点分组着色）"""
    n = table_name.lower()
    base = n.split(".")[-1]
    # 按常见前缀
    if base.startswith(("mes_", "prod_")) or "work_order" in base or "process_output" in base \
            or base in ("workshop", "production_line", "production_record", "process", "工序", "产线", "车间", "工单"):
        return "生产"
    if base.startswith(("qms_", "quality")) or "inspect" in base or "defect" in base \
            or base in ("quality_inspection", "质检", "检验", "不良"):
        return "质量"
    if base.startswith(("eqp_", "equip", "machine")) or "downtime" in base or "alarm" in base \
            or "maintenance" in base or base in ("设备", "停机", "报警"):
        return "设备"
    if base.startswith(("inv_", "stock", "warehouse")) or "inventory" in base \
            or base in ("material", "materials", "物料", "库存", "仓库"):
        return "库存"
    if base.startswith(("sales_",)) or "sales_order" in base or "customer" in base or base.startswith("test_order"):
        return "销售"
    if "purchase" in base or "supplier" in base or "vendor" in base or base in ("采购", "供应商"):
        return "采购"
    if base.startswith(("hr_", "employee", "staff", "attendance")) or "工资" in base or base in ("员工", "考勤", "人事"):
        return "人事"
    if "finance" in base or "cost" in base or "invoice" in base or base in ("财务", "成本"):
        return "财务"
    if base.startswith("dim_") or base in ("product", "products", "factory", "factories",
                                           "department", "departments", "产品", "工厂", "部门", "主数据"):
        return "基础数据"
    if base.startswith("test_"):
        return "基础数据"
    return "其他"


def _assert_ident(name: str, what: str):
    if not name or not _IDENT_RE.match(name):
        raise HTTPException(status_code=400, detail=f"非法的{what}名: {name}")


def _quote_qualified(name: str) -> str:
    """带引号的标识符；遇 schema.table 先拆分再逐段 quote。

    quote_ident 不拆分点号，会把 'factory.work_order' 当成单个带点号标识符，
    多 schema 库（如 123 库的 factory schema）下查询/更新/注释会失败。
    """
    if "." in name:
        schema, table = name.split(".", 1)
        return f"{quote_ident(schema)}.{quote_ident(table)}"
    return quote_ident(name)


def _column_type_for_mysql(db: Session, table_name: str, column_name: str) -> str:
    """获取 MySQL 列的完整类型定义（用于 MODIFY COLUMN 保持类型不变）"""
    inspector = inspect(db.get_bind())
    for col in inspector.get_columns(table_name):
        if col["name"] == column_name:
            col_type = str(col["type"])
            nullable = "" if col.get("nullable") else " NOT NULL"
            default = ""
            if col.get("default") is not None:
                d = col["default"]
                if isinstance(d, str):
                    # 字符串默认值必须加引号并转义内部单引号，否则生成非法 SQL
                    default = f" DEFAULT '{d.replace(chr(39), chr(39) * 2)}'"
                else:
                    default = f" DEFAULT {d}"
            return f"{col_type}{nullable}{default}"
    raise HTTPException(status_code=404, detail=f"字段 '{column_name}' 不存在于表 '{table_name}' 中")


# ========== CSV / SQLite / ZIP / URL 数据导入 ==========

@router.post("/import-csv", dependencies=[Depends(require_roles("admin"))])
async def import_csv(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    table_name: str = Form(""),
    source_type: Optional[str] = Form("file"),
    source_url: Optional[str] = Form(None),
):
    """支持上传 CSV、SQLite、ZIP、文件夹或直接使用远程 URL 导入"""
    try:
        if source_type == "url" and source_url:
            # 仅允许 http/https 远程数据源；禁止把本地服务器文件路径当 URL 传入，
            # 否则会走 import_database_source 的本地文件/目录分支，造成任意文件读取。
            if not re.match(r"^https?://", source_url.strip(), re.IGNORECASE):
                raise HTTPException(status_code=400, detail="远程数据源仅支持 http/https URL")
            result = import_database_source(source_url, target_table_name=table_name or None)
            return {"success": True, **result}

        uploaded_files = list(files or [])
        if not uploaded_files:
            form = await request.form()
            uploaded_files = [file for file in form.getlist("file") if hasattr(file, "filename")]

        if not uploaded_files:
            raise ValueError("没有收到任何上传文件")

        if len(uploaded_files) == 1:
            upload_file = uploaded_files[0]
            if hasattr(upload_file, "filename"):
                filename = os.path.basename(upload_file.filename or "uploaded")
                content = await upload_file.read()
            else:
                filename = getattr(upload_file, "filename", "uploaded") or "uploaded"
                content = upload_file.read() if hasattr(upload_file, "read") else b""

            suffix = os.path.splitext(filename)[1].lower()
            with tempfile.TemporaryDirectory() as tmp_dir:
                temp_path = os.path.join(tmp_dir, filename or "uploaded")
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                with open(temp_path, "wb") as tmp:
                    tmp.write(content)

                if suffix == ".csv":
                    result = import_csv_to_db(temp_path, table_name or (os.path.splitext(os.path.basename(temp_path))[0]))
                else:
                    result = import_database_source(temp_path, target_table_name=table_name or None)
            return {"success": True, **result}

        with tempfile.TemporaryDirectory() as tmp_dir:
            for upload_file in uploaded_files:
                if not hasattr(upload_file, "filename"):
                    continue
                filename = upload_file.filename or "uploaded"
                filename = filename.replace("\\", "/").strip("/") or "uploaded"
                if any(part in {"", ".", ".."} for part in filename.split("/")):
                    raise ValueError("上传文件名包含不安全的路径")
                content = await upload_file.read()
                dest_path = os.path.join(tmp_dir, filename)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "wb") as tmp:
                    tmp.write(content)

            candidates = collect_import_candidate_files(tmp_dir)
            if not candidates:
                raise ValueError("文件夹中没有找到可导入的 CSV/SQLite/ZIP 文件")

            effective_table_name = (table_name or None) if len(candidates) == 1 else None
            imported = []
            for path in candidates:
                imported.append(import_database_source(path, target_table_name=effective_table_name))
            return {"success": True, "mode": "folder", "files": imported}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导入失败: {str(exc)}")


def get_dynamic_topics(db: Session, inspector) -> list[dict]:
    """根据当前数据库结构推导 Agent 实际具备的分析能力（按业务场景生成具体主题）。"""
    tables = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]
    schema = {
        table: inspector.get_columns(table)
        for table in tables
    }
    labels = {table: table for table in tables}
    try:
        metadata_columns = {column["name"] for column in inspector.get_columns("metadata_tables")}
        if {"table_name", "table_chinese_name"}.issubset(metadata_columns):
            result = db.execute(text("SELECT table_name, table_chinese_name FROM metadata_tables"))
            labels.update({row[0]: row[1] or row[0] for row in result})
    except Exception:
        pass
    # metadata.py 中文别名优先（兼容带 schema 前缀与裸名两种形态）
    try:
        from db.metadata import TABLES as _MD_TABLES
        for _t in _MD_TABLES:
            _tn = _t["table_name"]
            _alias = _t.get("table_alias") or _tn
            labels[_tn] = _alias
            _bare = _tn.split(".")[-1]
            if _bare != _tn:
                labels[_bare] = _alias
    except Exception:
        pass

    def matches(*words: str) -> list[tuple[str, str]]:
        found = []
        for table, columns in schema.items():
            table_text = table.lower()
            for column in columns:
                column_name = column["name"].lower()
                if any(word.lower() in table_text or word.lower() in column_name for word in words):
                    found.append((table, column["name"]))
        return found

    def topic(topic_id: str, name: str, icon: str, description: str, evidence, metrics, questions, kind="场景主题", chart_types=None):
        related_tables = sorted({table for table, _ in evidence})
        return {
            "id": topic_id,
            "name": name,
            "icon": icon,
            "description": description,
            "related_tables": related_tables,
            "related_metrics": metrics,
            "supported_questions": questions,
            "chart_types": chart_types or [],
            "evidence": [{"table": table, "field": field} for table, field in evidence[:12]],
            "table_labels": {table: labels.get(table, table) for table in related_tables},
            "kind": kind,
        }

    topics = []
    numeric_types = ("INT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "MONEY")
    numeric_fields = [
        (table, column["name"])
        for table, columns in schema.items()
        for column in columns
        if any(kind in str(column["type"]).upper() for kind in numeric_types)
    ]
    time_fields = matches("date", "time", "day", "month", "year", "日期", "时间")

    # 按业务场景分组 → 每个场景生成 1-3 个具体主题（名称/问题锚定具体表）
    scene_tables: dict[str, list[str]] = {}
    for table in tables:
        scene_tables.setdefault(_table_scene(table), []).append(table)

    # 场景主题模板：{场景: [(id, icon, 名称, 描述, 问题列表, 关键词, 建议图表)]}
    scene_templates = {
        "生产": [
            ("prod_yield", "📊", "产量排行与趋势", "按产线/工序/日期汇总产量，识别高产线与趋势变化，可对比良率",
             ["各产线产量排行？", "最近产量变化趋势？", "哪个产线/工序产量最高？", "产量对比良率如何？"],
             ["产量", "产出", "qty", "good_qty"], ["bar", "line", "pie"]),
            ("prod_wo", "📋", "工单执行分析", "工单计划/实际产量对比、按期与延期情况、在制负荷",
             ["工单完成率如何？", "有哪些延期工单？", "各状态工单分布？", "计划 vs 实际产量偏差？"],
             ["工单", "work_order", "plan_qty", "actual_qty"], ["bar", "pie"]),
        ],
        "质量": [
            ("qlty_defect", "⚠️", "缺陷 Pareto 分析", "按缺陷类型/工序汇总不良，聚焦 80/20 主要问题",
             ["主要缺陷类型有哪些？", "哪些工序不良最多？", "各缺陷类型占比？", "不良趋势如何？"],
             ["defect", "不良", "缺陷", "fail"], ["bar", "pie", "line"]),
            ("qlty_rate", "✅", "合格率与检验统计", "抽检合格率、检验结果分布、批次良率",
             ["各工序合格率？", "质检结论分布？", "合格率变化趋势？", "哪些批次合格率异常？"],
             ["inspection", "质检", "检验", "合格"], ["bar", "line", "pie"]),
        ],
        "设备": [
            ("eqp_downtime", "⏸️", "停机损失分析", "按设备/原因汇总停机时长与次数，定位最大损失源",
             ["哪些设备停机最多？", "停机主要原因？", "计划/非计划停机分布？", "停机时长趋势？"],
             ["downtime", "停机", "equipment", "设备"], ["bar", "pie", "line"]),
            ("eqp_util", "⚙️", "设备维护分析", "维护记录、维护类型分布与最近维护情况",
             ["设备维护类型分布？", "最近维护记录？", "哪些设备维护最多？"],
             ["maintenance", "维护", "equipment", "设备"], ["bar", "pie"]),
        ],
        "库存": [
            ("inv_level", "📦", "库存水位分析", "库存总量、各仓库分布与安全库存对比，识别缺货风险",
             ["当前库存总量？", "哪些物料低于安全库存？", "各仓库库存分布？", "库存告急的物料？"],
             ["inventory", "库存", "stock", "available_qty", "safe"], ["bar", "pie"]),
        ],
        "销售": [
            ("sales_trend", "📈", "销售趋势与客户贡献", "按日期/客户/产品汇总销售额，识别大客户与趋势",
             ["最近销售趋势？", "贡献最大的客户？", "各产品销售额排行？", "月度销售额对比？"],
             ["sales", "销售", "订单", "amount", "金额"], ["line", "bar", "pie"]),
        ],
        "采购": [
            ("pu_amount", "🛒", "采购金额分析", "按供应商/物料/季度汇总采购金额，识别主要供应商",
             ["采购总额？", "主要供应商占比？", "各季度采购金额？", "哪些物料采购最多？"],
             ["purchase", "采购", "supplier", "供应商"], ["bar", "pie", "line"]),
        ],
        "人事": [
            ("hr_headcount", "👥", "人力与薪酬分析", "部门人数、薪酬分布、入职趋势与缺勤情况",
             ["各部门人数？", "平均工资是多少？", "男女员工平均工资？", "本月缺勤天数？"],
             ["employee", "员工", "salary", "工资", "attendance", "考勤"], ["bar", "pie", "line"]),
        ],
        "财务": [
            ("fin_cost", "💹", "收入成本分析", "收入、成本与毛利变化，识别盈利结构",
             ["本期收入成本？", "毛利多少？", "成本构成如何？"],
             ["finance", "财务", "cost", "成本", "invoice"], ["line", "bar"]),
        ],
    }
    for scene, templates in scene_templates.items():
        scene_tbls = scene_tables.get(scene, [])
        if not scene_tbls:
            continue
        for tpl_id, icon, name, desc, questions, words, chart_types in templates:
            evidence = [(t, c) for t in scene_tbls for c in (c["name"] for c in schema[t])
                        if any(w.lower() in c.lower() for w in words)] or \
                       [(t, c["name"]) for t in scene_tbls for c in schema[t][:3]]
            if evidence:
                # 名称带上具体表的中文名（如"工序产量表产量分析"过于啰嗦，直接用主题名）
                topics.append(topic(
                    tpl_id, name, icon, desc, evidence,
                    [name], questions, kind="场景主题", chart_types=chart_types,
                ))

    # 通用能力主题（有数值字段才有意义）
    if numeric_fields:
        topics.append(topic(
            "aggregation", "指标统计与排名", "📊",
            "对数值字段进行求和、计数、平均、分组排名与占比分析。",
            numeric_fields, ["数量", "总和", "平均值", "排名", "占比"],
            ["哪些对象的数值最高？", "各类别的数量和占比是多少？"], kind="通用能力",
        ))
    if time_fields and numeric_fields:
        topics.append(topic(
            "trend", "趋势变化分析", "📈",
            "按日期/时间周期分析数值变化趋势。",
            time_fields + numeric_fields, ["同比", "环比", "趋势", "峰值"],
            ["最近一段时间的变化趋势如何？", "哪个时间段变化最明显？"], kind="通用能力",
        ))

    if not topics:
        topics.append(topic(
            "data-overview", "数据概览分析", "🔎",
            "系统可以读取当前数据库结构，查看表、字段、数据量和基础分布。",
            [(table, column["name"]) for table, columns in schema.items() for column in columns[:2]],
            ["表数量", "字段数量", "数据行数"], ["当前数据库有哪些数据？", "各数据表规模如何？"],
            kind="通用能力",
        ))
    return topics


# ========== 请求模型 ==========

class CommentUpdate(BaseModel):
    comment: str


class UpdateCellRequest(BaseModel):
    table_name: str
    column_name: str
    row_id: str
    id_column: str
    new_value: Optional[Any] = None


class AnalysisRequest(BaseModel):
    question: str


def build_dimension_label_map(db, inspector, group_column: str) -> dict:
    """将分组字段的编码值翻译成业务中文名，例如 PR06 -> 功能测试。"""
    mappings = [
        (("process_id", "process_code"), "dim_process", "process_id", "process_name"),
        (("product_id", "product_code"), "dim_product", "product_id", "product_name"),
        (("equipment_id", "equipment_code"), "dim_equipment", "equipment_id", "equipment_name"),
        (("line_id", "line_code"), "dim_production_line", "line_id", "line_name"),
    ]
    for group_fields, dim_table, key_field, name_field in mappings:
        if group_column not in group_fields:
            continue
        try:
            dim_columns = {column["name"] for column in inspector.get_columns(dim_table)}
            if {key_field, name_field}.issubset(dim_columns):
                q = quote_ident
                result = db.execute(text(f'SELECT {q(key_field)}, {q(name_field)} FROM {q(dim_table)}'))
                return {str(row[0]): str(row[1] or row[0]) for row in result}
        except Exception:
            pass
    return {}


METRIC_BUSINESS_MAP = {
    "defect_qty": ("缺陷数量", "件"),
    "good_qty": ("合格数量", "件"),
    "good_qty + defect_qty": ("产量", "件"),
    "downtime_minutes": ("停机时长", "分钟"),
    "standard_yield_rate": ("良率", "%"),
    "available_qty": ("可用库存", "件"),
    "input_qty": ("投入数量", "件"),
    "frozen_qty": ("冻结数量", "件"),
    "safety_stock_qty": ("安全库存", "件"),
    "sample_qty": ("抽样数量", "件"),
    "plan_qty": ("计划数量", "件"),
    "actual_qty": ("实际数量", "件"),
    "produce_qty": ("产出数量", "件"),
    "output_qty": ("产出数量", "件"),
    "quantity": ("数量", "件"),
}


def metric_business_name(column: str) -> tuple:
    """把指标字段名转成可读的中文业务名称与单位。"""
    if column in METRIC_BUSINESS_MAP:
        return METRIC_BUSINESS_MAP[column]
    lower = column.lower()
    if lower.endswith("_qty") or lower.endswith("_count") or lower == "quantity":
        return (column.replace("_qty", "").replace("_count", ""), "件")
    if lower.endswith("_amount"):
        return (column.replace("_amount", ""), "元")
    if lower.endswith("_minutes"):
        return (column.replace("_minutes", ""), "分钟")
    if lower.endswith("_rate"):
        return (column.replace("_rate", ""), "%")
    return (column, "数量")

DIMENSION_BUSINESS_MAP = {
    "process_id": "工序",
    "process_code": "工序",
    "product_id": "产品",
    "product_code": "产品",
    "equipment_id": "设备",
    "equipment_code": "设备",
    "line_id": "产线",
    "line_code": "产线",
    "warehouse_code": "仓库",
    "defect_type": "缺陷类型",
    "inspection_result": "检验结果",
    "order_status": "工单状态",
    "equipment_status": "设备状态",
    "shift_code": "班次",
    "work_order_no": "工单编号",
    "inspection_no": "检验编号",
    "product_name": "产品",
    "process_name": "工序",
    "equipment_name": "设备",
    "line_name": "产线",
    "warehouse_name": "仓库",
}


def dimension_business_name(column: str) -> str:
    """把分组字段名转成可读的中文业务维度名。"""
    if column in DIMENSION_BUSINESS_MAP:
        return DIMENSION_BUSINESS_MAP[column]
    lower = column.lower()
    if lower.endswith("_no"):
        return "编号"
    if lower.endswith("_code"):
        return "代码"
    if lower.endswith("_name"):
        return "名称"
    if lower.endswith("_type"):
        return "类型"
    if lower.endswith("_status"):
        return "状态"
    if lower.endswith("_category"):
        return "类别"
    return column


# ========== 获取所有表 ==========

@router.get("/")
def get_all_tables(authorization: str = Header(None), db: Session = Depends(get_db)):
    """获取数据库中所有表及其字段信息（走 10 分钟 TTL 缓存 + 批量行数估算，避免每次全量重建）

    权限：已登录非管理员按生效 ACL 过滤可见表与字段（与 /api/permission/data-catalog 同源）；
    guest（开放模式匿名）与超级管理员不受限。
    """
    try:
        from routers.knowledge import _cached
        data = _cached("tables_list", _build_tables_list, db)
    except Exception:
        data = _build_tables_list(db)
    return _acl_filter_tables(data, authorization)


def _bare(name) -> str:
    """跨数据源表名统一取裸名小写（test_orders / factory.sales_order → 同名小写）"""
    return str(name).split(".")[-1].lower()


def _acl_visible(authorization: str):
    """返回 (full, allowed)：
    full=True 表示不受限（guest 开放模式 / 超级管理员 / 未配置表授权），直接返回全量；
    否则 full=False，allowed 为当前用户可见表的裸名小写集合。"""
    u = get_current_user(authorization)
    if u["role"] == "guest":
        return True, None
    ctx = enforcer.build_acl_context(u)
    if ctx.superuser:
        return True, None
    allowed = ctx.allowed_tables  # None = 未配置 = 不限制
    if allowed is None:
        return True, None
    return False, allowed


def _acl_filter_tables(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 裁剪表列表的可见表与字段，并标记脱敏字段（不修改缓存对象）。

    字段级可见性（对齐观远单元格级体验）：
    - deny 字段：直接隐藏，界面上不出现；
    - mask 字段：保留但标记 masked=True，前端显示「🔒 脱敏」标识，让用户提前知道该字段会被打码。
    """
    u = get_current_user(authorization)
    if u["role"] == "guest":
        return data
    ctx = enforcer.build_acl_context(u)
    if ctx.superuser:
        return data
    allowed = ctx.allowed_tables  # None = 角色未配置表授权 = 不限制
    out = []
    for t in (data.get("tables") or []):
        bare = str(t.get("table_name") or "").split(".")[-1].lower()
        if allowed is not None and bare not in allowed:
            continue  # 无权表：整表隐藏（需求：未授权员工默认全空）
        wl = ctx.column_whitelist.get(bare)
        denies = ctx.column_denies.get(bare, set())
        masks = ctx.column_masks.get(bare, {})
        if wl is not None or denies or masks:
            cols = []
            for c in (t.get("columns") or []):
                cl = str(c.get("name") or "").lower()
                if (wl is not None and cl not in wl) or cl in denies:
                    continue  # 白名单外 / 拒绝字段：隐藏
                c2 = dict(c)
                if cl in masks:
                    c2["masked"] = True  # 脱敏字段：保留但标记
                cols.append(c2)
            t = {**t, "columns": cols}
        out.append(t)
    return {"tables": out}


def _build_tables_list(db: Session):
    """构建全部表的字段/翻译/行数信息（供缓存生产）"""
    inspector = inspect(db.get_bind())
    tables_info = []

    # 表中文名映射（metadata_tables 有 table_name -> table_chinese_name；没有则留空）
    table_labels = {}
    try:
        metadata_columns = {column["name"] for column in inspector.get_columns("metadata_tables")}
        if {"table_name", "table_chinese_name"}.issubset(metadata_columns):
            result = db.execute(text("SELECT table_name, table_chinese_name FROM metadata_tables"))
            table_labels = {row[0]: (row[1] or "") for row in result}
    except Exception:
        pass
    # metadata.py 中文别名兜底（123 库 factory.* 等）
    try:
        from db.metadata import TABLES as _MD_TABLES
        for _t in _MD_TABLES:
            _tn = _t["table_name"]
            table_labels.setdefault(_tn, _t.get("table_alias") or "")
            _bare = _tn.split(".")[-1]
            if _bare != _tn:
                table_labels.setdefault(_bare, _t.get("table_alias") or "")
    except Exception:
        pass

    row_counts = get_row_counts_fast(db)  # 一次查询估算全部表行数

    for table_name in inspector.get_table_names():
        # 跳过系统表
        if table_name.startswith("_") or table_name.startswith("pg_") or table_name.startswith("metadata_"):
            continue

        # 获取主键列名集合
        pk_constraint = inspector.get_pk_constraint(table_name)
        pk_columns = set(pk_constraint.get("constrained_columns", []))

        columns = []
        for column in inspector.get_columns(table_name):
            columns.append({
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": column.get("nullable", True),
                "default": str(column.get("default", "")) if column.get("default") else "",
                "comment": column.get("comment", "") or "",
                "primary_key": column["name"] in pk_columns,
                # 字段翻译：同名字段不同表含义（status 是生产状态还是机械状态）+ 详细解释
                "translation": explain_field_detailed(
                    table_name, column["name"], str(column["type"]), column.get("comment") or ""),
            })

        tables_info.append({
            "table_name": table_name,
            "chinese_name": table_labels.get(table_name, "") or "",
            "columns": columns,
            "row_count": row_counts[table_name] if table_name in row_counts else get_row_count(db, table_name),
        })

    return {"tables": tables_info}


# ========== 获取行数 ==========

def get_row_count(db: Session, table_name: str) -> int:
    """获取表的行数（单表，兜底用；批量场景用 get_row_counts_fast）"""
    try:
        result = db.execute(text(f'SELECT COUNT(*) FROM {_quote_qualified(table_name)}'))
        return result.scalar()
    except Exception:
        return 0


def get_row_counts_fast(db: Session) -> dict:
    """批量估算全部表行数：PG 用 pg_class.reltuples，MySQL 用 information_schema.table_rows，
    一次查询替代逐表 COUNT（20 表串行 COUNT 是表列表慢的主因之一）"""
    try:
        from database import get_db_type
        if get_db_type() == "mysql":
            result = db.execute(text(
                "SELECT table_name, table_rows FROM information_schema.tables "
                "WHERE table_schema=DATABASE()"))
            return {r[0]: max(0, int(r[1] or 0)) for r in result}
        result = db.execute(text(
            "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind='r'"))
        return {r[0]: max(0, int(r[1] or 0)) for r in result}
    except Exception:
        return {}


# ========== 更新字段备注 ==========

@router.patch("/tables/{table_name}/columns/{column_name}/comment", dependencies=[Depends(require_roles("admin"))])
def update_column_comment(
    table_name: str,
    column_name: str,
    data: CommentUpdate,
    db: Session = Depends(get_db)
):
    """
    更新数据表字段的备注（PostgreSQL）
    """
    try:
        _assert_ident(table_name, "表")
        _assert_ident(column_name, "字段")
        # 先验证字段是否存在
        inspector = inspect(db.get_bind())
        columns = inspector.get_columns(table_name)
        column_names = [col["name"] for col in columns]
        
        if column_name not in column_names:
            raise HTTPException(status_code=404, detail=f"字段 '{column_name}' 不存在于表 '{table_name}' 中")
        
        # 更新字段注释（PostgreSQL / MySQL 各自语法）
        # 注意：COMMENT/ALTER 是 utility 语句，PG 不支持绑定参数（IS :comment 会语法错误），
        # 必须内联为转义后的字符串字面量（' → ''；MySQL 额外转义反斜杠）
        from database import get_db_type
        _val = str(data.comment)
        if get_db_type() == "mysql":
            _lit = "'" + _val.replace("\\", "\\\\").replace("'", "''") + "'"
            comment_sql = text(
                f'ALTER TABLE {_quote_qualified(table_name)} MODIFY COLUMN {quote_ident(column_name)} '
                f'{_column_type_for_mysql(db, table_name, column_name)} COMMENT {_lit}'
            )
        else:
            _lit = "'" + _val.replace("'", "''") + "'"
            comment_sql = text(f'COMMENT ON COLUMN {_quote_qualified(table_name)}.{quote_ident(column_name)} IS {_lit}')
        db.execute(comment_sql)
        db.commit()
        
        return {"success": True, "comment": data.comment}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"更新备注失败: {str(e)}")


# ========== 更新样例数据单元格 ==========

@router.patch("/data/update", dependencies=[Depends(require_roles("admin"))])
def update_cell(
    req: UpdateCellRequest,
    authorization: str = Header(None),
    db: Session = Depends(get_db)
):
    """
    更新数据表中某个单元格的值（支持双击编辑样例数据）

    权限（P1 修复）：在角色门槛之上叠加 ACL —— 表白名单 + 敏感列 deny/mask +
    行级过滤，防止越权修改无权表/敏感列/行过滤范围之外的数据。
    """
    try:
        from security.enforcer import build_acl_context
        acl = build_acl_context(get_current_user(authorization))
        _assert_ident(req.table_name, "表")
        _assert_ident(req.column_name, "字段")
        _assert_ident(req.id_column, "主键字段")
        table_name = req.table_name
        column_name = req.column_name
        id_column = req.id_column
        row_id = req.row_id
        new_value = req.new_value
        tbl_key = table_name.split(".")[-1].lower()

        # 表级白名单（fail-closed）：白名单非空且目标表不在其中 → 拒绝
        if acl.allowed_tables is not None and not acl.superuser:
            allowed_lower = {t.split(".")[-1].lower() for t in acl.allowed_tables}
            if tbl_key not in allowed_lower:
                raise HTTPException(status_code=403,
                                    detail="权限不足：当前角色无权修改该表数据")

        # 列级 deny / mask：敏感字段不可写（含脱敏字段，写回会覆盖真实值）
        if (column_name.lower() in acl.column_denies.get(tbl_key, set())
                or column_name.lower() in acl.column_masks.get(tbl_key, {})):
            raise HTTPException(status_code=403,
                                detail="权限不足：当前角色无权修改该敏感字段")

        # 获取表结构信息，判断字段类型
        inspector = inspect(db.get_bind())
        columns = inspector.get_columns(table_name)
        
        # 找到目标列的类型
        col_type = None
        col_nullable = True
        for col in columns:
            if col["name"] == column_name:
                col_type = str(col["type"]).upper()
                col_nullable = col.get("nullable", True)
                break
        
        if col_type is None:
            raise HTTPException(status_code=404, detail=f"字段 '{column_name}' 不存在")

        # 根据类型格式化值
        # 数值类型判断：取类型基名（去括号参数）精确匹配，避免 "INT" 子串误命中
        # INTERVAL/TIMESTAMP 等含 INT 的类型（曾把时间列当数字强转）
        _base_type = re.split(r"[\(\s]", col_type)[0].upper()
        is_numeric = _base_type in ("INT", "INTEGER", "SMALLINT", "BIGINT", "TINYINT",
                                    "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE",
                                    "DOUBLE PRECISION", "REAL", "MONEY", "NUMBER")
        is_bool = _base_type in ("BOOL", "BOOLEAN", "BIT")
        if new_value is None or new_value == '':
            if not col_nullable:
                # 如果字段不允许为空，空值转为空字符串（数字类型转为0）
                if is_numeric:
                    formatted_value = '0'
                elif is_bool:
                    formatted_value = 'false'
                else:
                    formatted_value = "''"
            else:
                formatted_value = 'NULL'
        elif is_numeric:
            # 数字类型
            try:
                formatted_value = str(float(new_value)) if '.' in str(new_value) else str(int(float(new_value)))
            except (ValueError, TypeError):
                formatted_value = '0'
        elif is_bool:
            # 布尔类型
            val_str = str(new_value).lower()
            formatted_value = 'true' if val_str in ('true', 't', '1', 'yes', 'y') else 'false'
        elif 'DATE' in col_type or 'TIMESTAMP' in col_type:
            # 日期时间类型，加引号
            escaped = str(new_value).replace("'", "''")
            formatted_value = f"'{escaped}'"
        else:
            # 字符串类型，转义单引号
            escaped = str(new_value).replace("'", "''")
            formatted_value = f"'{escaped}'"
        
        # 构建 UPDATE SQL（表名/字段名按数据库类型加引号，schema.table 拆开引用）
        # 行级过滤（P1 修复）：ACL row_filters 是 enforcer 渲染好的安全条件，追加进 WHERE
        where_clause = f"{quote_ident(id_column)} = :row_id"
        row_cond = acl.row_filters.get(tbl_key)
        if row_cond:
            where_clause += f" AND ({row_cond})"
        sql = text(f"""
            UPDATE {_quote_qualified(table_name)} 
            SET {quote_ident(column_name)} = {formatted_value}
            WHERE {where_clause}
        """)
        
        db.execute(sql, {"row_id": row_id})
        db.commit()
        
        # 返回更新后的值（便于前端同步）
        return {"success": True, "message": "更新成功", "new_value": new_value}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


# ========== 总览数据 ==========

@router.get("/overview")
def get_overview(authorization: str = Header(None), db: Session = Depends(get_db)):
    """获取总览页统计数据（走 10 分钟 TTL 缓存；切库时 clear_knowledge_cache 会自动失效）

    权限：已登录非管理员按生效 ACL 过滤可见表的统计（表数/行数/字段数/关系数/主题数），
    不泄露无权表的存在与规模；guest（开放模式匿名）与超级管理员不受限。
    """
    try:
        from routers.knowledge import _cached
        data = _cached("overview", _build_overview, db)
    except Exception:
        data = _build_overview(db)
    data = _acl_filter_overview(data, authorization)

    # 关系数 / 主题数：与各自列表接口（/relationships、/topics）同源，按授权重算，
    # 保证概览数字与用户实际能打开的内容数量一致；全量用户（guest/admin）保持原值。
    full, _ = _acl_visible(authorization)
    if not full:
        try:
            from routers.knowledge import _cached
            rel = _cached("relationships", _build_relationships, db)
            data["data"]["relationship_count"] = len(
                _acl_filter_relationships(rel, authorization)["relationships"])
        except Exception:
            pass
        try:
            from routers.knowledge import _cached, _build_scenes
            scenes = _cached("scenes", _build_scenes, db)
            topics = _scenes_to_topics(scenes)
            data["data"]["topic_count"] = len(
                _acl_filter_topics({"topics": topics}, authorization)["topics"])
        except Exception:
            pass
    return data


def _acl_filter_overview(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤总览统计（缓存全量，返回层过滤）"""
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    inner = dict(data.get("data") or {})
    trc = dict(inner.get("table_row_counts") or {})
    colc = dict(inner.get("table_column_counts") or {})
    # 可见表集合（跨数据源表名统一按裸名小写匹配）
    visible = {t for t in trc if _bare(t) in allowed}
    visible |= {t for t in colc if _bare(t) in allowed}
    inner["table_count"] = len(visible)
    inner["total_rows"] = sum(trc.get(t, 0) for t in visible)
    inner["total_columns"] = sum(colc.get(t, 0) for t in visible)
    inner["table_row_counts"] = {t: trc.get(t, 0) for t in visible}
    inner["table_column_counts"] = {t: colc.get(t, 0) for t in visible}
    return {"data": inner}


def _acl_filter_relationships(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤表间关系图（节点 + 关系），只保留可见表及其关系"""
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    nodes = [n for n in (data.get("nodes") or []) if _bare(n.get("id", "")) in allowed]
    rels = [r for r in (data.get("relationships") or [])
            if _bare(r.get("source_table", "")) in allowed and _bare(r.get("target_table", "")) in allowed]
    return {"nodes": nodes, "relationships": rels}


def _acl_filter_topics(data: dict, authorization: str) -> dict:
    """按当前用户生效 ACL 过滤分析主题：只保留所有相关表均可见的主题
    （无绑定表的通用主题保留）"""
    full, allowed = _acl_visible(authorization)
    if full:
        return data
    out = []
    for t in (data.get("topics") or []):
        rts = [str(x) for x in (t.get("related_tables") or []) if str(x).strip()]
        if not rts or all(_bare(x) in allowed for x in rts):
            out.append(t)
    return {"topics": out}


def _build_overview(db: Session) -> dict:
    """构建总览统计数据（供缓存生产）"""
    inspector = inspect(db.get_bind())
    
    # 1. 数据表总数（排除系统表）
    all_tables = [t for t in inspector.get_table_names() if not t.startswith("_") and not t.startswith("pg_")]
    table_count = len(all_tables)
    
    # 2. 字段总数（同时记录每表字段数，供 ACL 过滤时重算）
    total_columns = 0
    table_column_counts: dict[str, int] = {}
    for table_name in all_tables:
        columns = inspector.get_columns(table_name)
        total_columns += len(columns)
        table_column_counts[table_name] = len(columns)
    
    # 3. 总数据行数（在下方 table_row_counts 估算后统一求和）
    total_rows = 0
    
    # 4. 表间关系数量（真实外键 + 预定义业务关系；metadata_relationships 可能为空表，
    #    不能只查它——否则永远 0，取两者之和）
    relationship_count = 0
    try:
        fk_count = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.table_constraints tc
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys', 'pg_catalog')
              AND tc.table_schema NOT LIKE 'pg_%'
        """)).scalar() or 0
        relationship_count += int(fk_count)
    except Exception:
        pass
    try:
        meta_count = db.execute(text('SELECT COUNT(*) FROM metadata_relationships')).scalar() or 0
        relationship_count += int(meta_count)
    except Exception:
        pass
    
    # 5. 分析主题数量：轻量调用主题引擎（只数主题数，不构建对象/字段翻译层级，
    #    避免 overview 每次请求都要全库生成字段翻译导致十几秒卡顿 → 前端卡片显示 0）
    try:
        from agent.topic_engine import detect_topics
        _fmap = {}
        for _t in all_tables:
            try:
                _fmap[_t] = [c["name"] for c in inspector.get_columns(_t)]
            except Exception:
                _fmap[_t] = []
        topic_count = len(detect_topics(all_tables, _fmap))
    except Exception:
        topic_count = len(get_dynamic_topics(db, inspector))
    
    # 6. 各表行数（估算：PG 用 pg_class.reltuples，MySQL 用 information_schema.table_rows；
    #    一次查询，避免逐表 COUNT 串行扫描；不依赖具体业务表名，跨库通用）
    table_row_counts = {}
    try:
        from database import get_db_type
        if get_db_type() == "mysql":
            result = db.execute(text(
                "SELECT table_name, table_rows FROM information_schema.tables "
                "WHERE table_schema=DATABASE()"))
            table_row_counts = {r[0]: max(0, int(r[1] or 0)) for r in result}  # reltuples -1（未分析）按 0
        else:
            result = db.execute(text(
                "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind='r'"))
            table_row_counts = {r[0]: max(0, int(r[1] or 0)) for r in result}  # reltuples -1（未分析）按 0
        total_rows = sum(table_row_counts.values())
    except Exception:
        pass
    
    return {
        "data": {
            "table_count": table_count,
            "total_columns": total_columns,
            "total_rows": total_rows,
            "relationship_count": relationship_count,
            "topic_count": topic_count,
            "table_row_counts": table_row_counts,
            "table_column_counts": table_column_counts,
        }
    }
# ========== 获取表间关系 ==========

def _acl_table_filter(allowed_tables: set[str] | None, superuser: bool = False):
    """按 ACL 白名单生成表名过滤函数（None/superuser = 不过滤）。

    安全：非 superuser 且有白名单时只放行白名单内的裸表名（fail-closed 方向）。
    """
    if superuser or allowed_tables is None:
        return lambda t: True
    allowed_lower = {t.split(".")[-1].lower() for t in allowed_tables}
    return lambda t: t.split(".")[-1].lower() in allowed_lower


@router.get("/attention-points")
def get_attention_points(authorization: str = Header(None), db: Session = Depends(get_db)):
    """基于当前数据库的真实数据计算，动态诊断数据中存在的潜在问题，随连接的数据库变化而刷新。

    权限（P0 修复）：必须登录，且表清单按当前用户 ACL 白名单过滤，
    防止低权限用户通过该接口读取全库聚合业务数据。
    """
    from security.enforcer import build_acl_context
    acl = build_acl_context(get_current_user(authorization))
    table_ok = _acl_table_filter(acl.allowed_tables, acl.superuser)
    inspector = inspect(db.get_bind())
    points = []
    tables = [
        t for t in inspector.get_table_names()
        if not t.startswith(("_", "pg_", "metadata_")) and table_ok(t)
    ]
    schema = {t: inspector.get_columns(t) for t in tables}

    numeric_types = ("INT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "MONEY")
    numeric_fields = [
        (t, c["name"], str(c["type"]).upper())
        for t, cols in schema.items()
        for c in cols
        if any(x in str(c["type"]).upper() for x in numeric_types)
    ]
    time_fields = [
        (t, c["name"])
        for t, cols in schema.items()
        for c in cols
        if any(x in c["name"].lower() for x in ("date", "time", "day", "month"))
    ]
    text_fields = [
        (t, c["name"])
        for t, cols in schema.items()
        for c in cols
        if any(x in c["name"].lower() for x in ("name", "type", "category", "status", "code", "no"))
        or c["name"].lower().endswith("_id")
    ]

    quote = quote_ident

    metric_priority = ("qty", "amount", "count", "total", "defect", "downtime", "数量", "金额", "停机", "rate", "ratio")
    used_tables = set()

    # 明确不作为业务指标的字段：主键 / 外键 / 序号 / 排序 / 版本等
    non_metric_tokens = ("_id", "_seq", "_no", "_order", "_sort", "_version", "_status_code")

    def is_metric_column(name: str) -> bool:
        lower = name.lower()
        if lower == "id" or any(lower.endswith(tok) for tok in non_metric_tokens):
            return False
        if lower in ("seq", "sequence", "ordinal", "sort_order", "version"):
            return False
        return True

    def pick_metric(table: str):
        cols = [c for t, c, _ in numeric_fields if t == table]
        business_cols = [c for c in cols if is_metric_column(c)]
        if not business_cols:
            return None
        return next((c for c in business_cols if any(w in c.lower() for w in metric_priority)), business_cols[0])

    def pick_group(table: str):
        group_candidates = [f for f in text_fields if f[0] == table]
        readable_candidates = [f for f in group_candidates if any(
            x in f[1].lower() for x in ("name", "type", "category", "status", "code", "no", "名称", "类型", "类别", "状态", "编号")
        ) and not f[1].lower().endswith("_id")]
        group_source = readable_candidates if readable_candidates else [f for f in group_candidates if not f[1].lower().endswith("_id")]
        if not group_source:
            group_source = group_candidates
        return group_source[0][1] if group_source else None

    # 各类诊断的配额：趋势 2 条、分布 2 条、质量 1 条、安全线 1 条、异常占比 2 条
    kind_quota = {"trend": 2, "volatility": 2, "concentration": 2, "imbalance": 2, "quality": 1, "safety": 1, "abnormal": 2}
    kind_count = {}

    def add_point(kind, table, tone, icon, title, detail, value, unit, rule, question):
        if len(points) >= 6:
            return False
        if kind_count.get(kind, 0) >= kind_quota.get(kind, 0):
            return False
        kind_count[kind] = kind_count.get(kind, 0) + 1
        points.append({
            "id": f"{kind}-{table}-{len(points)}",
            "category": table,
            "icon": icon,
            "tone": tone,
            "title": title,
            "detail": detail,
            "value": value,
            "unit": unit,
            "rule": rule,
            "question": question,
        })
        return True

    # ---------- 规则 1：时间趋势诊断（近期骤升 / 骤降 / 大幅波动） ----------
    quote = quote_ident
    for table, time_column in time_fields:
        if len(points) >= 6 or kind_count.get("trend", 0) + kind_count.get("volatility", 0) >= 2:
            break
        if table in used_tables:
            continue
        metric = pick_metric(table)
        if not metric:
            continue
        metric_business, metric_unit = metric_business_name(metric)
        try:
            rows = db.execute(text(
                f"SELECT {quote(time_column)}, SUM({quote(metric)}) AS value "
                f"FROM {quote(table)} GROUP BY {quote(time_column)} "
                f"ORDER BY {quote(time_column)}"
            )).fetchall()
            if len(rows) < 4:
                continue
            values = [float(r[1] or 0) for r in rows]
            if sum(values) == 0:
                continue
            overall_avg = sum(values) / len(values)
            recent = values[-3:]
            recent_avg = sum(recent) / len(recent)
            cv = statistics.pstdev(values) / overall_avg if overall_avg else 0
            if recent_avg > overall_avg * 1.25:
                if add_point(
                    "trend", table, "red", "📈",
                    f"{metric_business}近期骤升",
                    f"最近{len(recent)}个周期的{metric_business}均值为 {recent_avg:,.0f} {metric_unit}，较整体均值 {overall_avg:,.0f} {metric_unit} 上升 {((recent_avg / overall_avg) - 1) * 100:.0f}%，增长异常，需排查原因。",
                    round(recent_avg, 2), metric_unit,
                    f"对比最近{len(recent)}期与整体均值的{metric_business}，上升超过 25% 判定为骤升",
                    f"分析{metric_business}近期骤升的原因",
                ):
                    used_tables.add(table)
            elif recent_avg < overall_avg * 0.75:
                if add_point(
                    "trend", table, "orange", "📉",
                    f"{metric_business}近期骤降",
                    f"最近{len(recent)}个周期的{metric_business}均值为 {recent_avg:,.0f} {metric_unit}，较整体均值 {overall_avg:,.0f} {metric_unit} 下降 {((1 - recent_avg / overall_avg)) * 100:.0f}%，下滑明显，需排查原因。",
                    round(recent_avg, 2), metric_unit,
                    f"对比最近{len(recent)}期与整体均值的{metric_business}，下降超过 25% 判定为骤降",
                    f"分析{metric_business}近期骤降的原因",
                ):
                    used_tables.add(table)
            elif cv > 0.8:
                if add_point(
                    "volatility", table, "amber", "🎢",
                    f"{metric_business}波动剧烈",
                    f"{metric_business}的变异系数达到 {cv:.1f}，各周期数值波动很大，运行稳定性较差。",
                    round(cv, 2), "",
                    "变异系数 = 标准差 / 均值，超过 0.8 判定为波动剧烈",
                    f"分析{metric_business}波动剧烈的原因",
                ):
                    used_tables.add(table)
        except Exception as e:
            print(f"趋势诊断 {table} 失败: {e}")

    # ---------- 规则 2：分布集中诊断（指标高度集中于单一分组 / 分组间差距悬殊） ----------
    for table in tables:
        if len(points) >= 6 or kind_count.get("concentration", 0) + kind_count.get("imbalance", 0) >= 2:
            break
        if table in used_tables:
            continue
        column = pick_metric(table)
        if not column:
            continue
        group_column = pick_group(table)
        if not group_column or group_column == column:
            continue
        metric_business, metric_unit = metric_business_name(column)
        dimension_business = dimension_business_name(group_column)
        label_map = build_dimension_label_map(db, inspector, group_column)
        try:
            rows = db.execute(text(
                f"SELECT {quote(group_column)}, SUM({quote(column)}) AS value "
                f"FROM {quote(table)} GROUP BY {quote(group_column)} "
                "ORDER BY value DESC"
            )).fetchall()
            if len(rows) < 3:
                continue
            total = sum(float(r[1] or 0) for r in rows)
            if total <= 0:
                continue
            top = float(rows[0][1] or 0)
            top_label = label_map.get(str(rows[0][0]), str(rows[0][0]))
            share = top / total * 100
            min_val = float(rows[-1][1] or 0)
            ratio = (top / min_val) if min_val > 0 else 0
            if share >= 50:
                if add_point(
                    "concentration", table, "orange", "⚖️",
                    f"{metric_business}高度集中",
                    f"{metric_business}中 {top_label} 占比高达 {share:.0f}%（共 {len(rows)} 个{dimension_business}），高度集中，一旦异常影响面大。",
                    round(share, 1), "%",
                    f"按{dimension_business}汇总{metric_business}，最高分组占比 ≥ 50% 判定为高度集中",
                    f"分析{metric_business}为何高度集中在{top_label}",
                ):
                    used_tables.add(table)
            elif ratio >= 8:
                if add_point(
                    "imbalance", table, "amber", "🔀",
                    f"{metric_business}分布失衡",
                    f"{metric_business}最高的{top_label}与最低分组相差约 {ratio:.0f} 倍，各{dimension_business}间差距悬殊。",
                    round(ratio, 1), "倍",
                    f"按{dimension_business}汇总{metric_business}，最高/最低 ≥ 8 倍判定为失衡",
                    f"分析各{dimension_business}的{metric_business}差距为何悬殊",
                ):
                    used_tables.add(table)
        except Exception as e:
            print(f"分布诊断 {table} 失败: {e}")

    # ---------- 规则 3：数据质量诊断（零值 / 空值占比过高） ----------
    for table, column, _ in numeric_fields:
        if len(points) >= 6 or kind_count.get("quality", 0) >= 1:
            break
        if table in used_tables or not is_metric_column(column):
            continue
        metric_business, _ = metric_business_name(column)
        try:
            total_row = db.execute(text(f'SELECT COUNT(*) FROM {quote(table)}')).scalar() or 0
            if total_row == 0:
                continue
            zero_count = db.execute(text(
                f'SELECT COUNT(*) FROM {quote(table)} WHERE {quote(column)} = 0 OR {quote(column)} IS NULL'
            )).scalar() or 0
            zero_share = zero_count / total_row * 100
            if zero_share >= 30:
                if add_point(
                    "quality", table, "amber", "⚠️",
                    f"{metric_business}数据缺失偏高",
                    f"{table} 中 {metric_business} 存在 {zero_share:.0f}% 的零值或空值（{zero_count} 条），数据可能未完整采集。",
                    round(zero_share, 1), "%",
                    f"统计 {metric_business} 为零或空的记录占比，≥ 30% 判定为数据缺失",
                    f"检查{metric_business}缺失数据的原因",
                ):
                    used_tables.add(table)
        except Exception as e:
            print(f"质量诊断 {table} 失败: {e}")

    # ---------- 规则 4：安全线诊断（可用量低于安全库存） ----------
    safety_pairs = []
    for table, cols in schema.items():
        col_names = {c["name"] for c in cols}
        for avail in ("available_qty", "available", "stock_qty", "qty_available"):
            for safe in ("safety_stock_qty", "safety_stock", "min_stock", "safe_stock"):
                if avail in col_names and safe in col_names:
                    safety_pairs.append((table, avail, safe))
                    break
            else:
                continue
            break
    for table, avail_col, safe_col in safety_pairs:
        if len(points) >= 6 or kind_count.get("safety", 0) >= 1:
            break
        if table in used_tables:
            continue
        avail_business, avail_unit = metric_business_name(avail_col)
        try:
            rows = db.execute(text(
                f"SELECT {quote(avail_col)}, {quote(safe_col)} FROM {quote(table)}"
            )).fetchall()
            low = [(float(r[0] or 0), float(r[1] or 0)) for r in rows if r[0] is not None and r[1] is not None and float(r[0]) < float(r[1])]
            if low:
                worst = min(low, key=lambda x: x[0] - x[1])
                add_point(
                    "safety", table, "red", "🛑",
                    "库存低于安全线",
                    f"存在 {len(low)} 条{avail_business}低于安全库存的记录，最严重的一条 {avail_business} {worst[0]:,.0f} {avail_unit} < 安全线 {worst[1]:,.0f} {avail_unit}，存在断供风险。",
                    round(worst[0], 2), avail_unit,
                    f"逐条检查 {avail_col} < {safe_col} 的记录，存在即告警",
                    "分析库存低于安全线的物料和原因",
                )
                used_tables.add(table)
        except Exception as e:
            print(f"安全线诊断 {table} 失败: {e}")

    # ---------- 规则 5：异常占比诊断（状态/结果类字段中异常值占比过高） ----------
    # 例如 inspection_result 中 fail 占比高、status 中异常状态占比高等
    abnormal_tokens = ("fail", "ng", "error", "abnormal", "reject", "不合格", "异常", "故障", "停机", "报警")
    status_like = ("result", "status", "state", "mark", "flag", "type", "code")
    for table, cols in schema.items():
        if len(points) >= 6 or kind_count.get("abnormal", 0) >= 2:
            break
        if table in used_tables:
            continue
        for col in cols:
            col_name = col["name"]
            lower_name = col_name.lower()
            col_type = str(col["type"]).upper()
            # 只看文本/枚举类字段（非时间、非大文本）
            if not any(tok in col_type for tok in ("CHAR", "TEXT", "VARCHAR", "ENUM")):
                continue
            if not any(tok in lower_name for tok in status_like):
                continue
            try:
                rows = db.execute(text(
                    f"SELECT {quote(col_name)}, COUNT(*) FROM {quote(table)} GROUP BY {quote(col_name)}"
                )).fetchall()
                total = sum(int(r[1] or 0) for r in rows)
                if total < 20:
                    continue
                bad = sum(
                    int(r[1] or 0) for r in rows
                    if any(tok in str(r[0]).lower() for tok in abnormal_tokens)
                )
                bad_share = bad / total * 100
                if bad_share >= 25:
                    col_business = dimension_business_name(col_name)
                    add_point(
                        "abnormal", table, "red", "🚨",
                        f"{col_business}异常占比偏高",
                        f"{table} 的{col_business}中，异常/不合格记录占 {bad_share:.0f}%（{bad}/{total} 条），异常比例偏高，需重点关注。",
                        round(bad_share, 1), "%",
                        f"统计 {col_name} 中异常值记录占比，≥ 25% 判定为异常偏高",
                        f"分析{table}异常占比偏高的原因",
                    )
                    used_tables.add(table)
                    break
            except Exception as e:
                print(f"异常占比诊断 {table}.{col_name} 失败: {e}")

    # ---------- 兜底：没有发现显著问题时的中性提示 ----------
    if not points:
        points.append({
            "id": "no-points",
            "category": "数据底座",
            "icon": "✅",
            "tone": "cyan",
            "title": "暂未发现明显异常",
            "detail": "对当前数据库的趋势、分布、数据完整性与安全线进行诊断后，未发现需要优先关注的显著问题。",
            "value": 0,
            "unit": "",
            "rule": "检查了趋势、分布、数据完整性与安全线四类规则",
            "question": "查看当前数据库的整体数据概况",
        })

    return {"points": points}

@router.get("/relationships")
def get_table_relationships(authorization: str = Header(None), db: Session = Depends(get_db)):
    """获取数据库中所有表间关系（外键关系 + 预定义业务关系）。

    走 10 分钟 TTL 缓存（表结构/外键短时间不变），弹窗秒开。
    权限：已登录非管理员只返回可见表之间的关系统；guest/超级管理员不受限。
    """
    try:
        from routers.knowledge import _cached
        data = _cached("relationships", _build_relationships, db)
    except Exception:
        data = _build_relationships(db)
    return _acl_filter_relationships(data, authorization)


@router.get("/search")
def search_tables_api(q: str = "", limit: int = 8):
    """按关键词搜索表（命令面板 Ctrl+K 用）：匹配表名/别名/描述/字段注释。

    纯确定性关键词打分（复用 db.tools.match_tables_by_query），零 LLM、零缓存依赖，
    前端输入即搜。返回 [{table_name, label, description, keywords_matched}]。
    """
    q = (q or "").strip()
    if not q:
        return {"success": True, "tables": []}
    try:
        from db.tools import match_tables_by_query
        hits = match_tables_by_query(q)[:max(1, min(limit, 20))]
        out = []
        for t in hits:
            name = str(t.get("table_name") or "")
            if not name:
                continue
            out.append({
                "table_name": name,
                "label": str(t.get("table_alias") or name.split(".")[-1] or name),
                "description": str(t.get("description") or "")[:120],
                "keywords_matched": [str(k) for k in (t.get("keywords_matched") or [])[:5]],
            })
        return {"success": True, "tables": out}
    except Exception as e:
        return {"success": False, "tables": [], "error": str(e)[:120]}


def _build_relationships(db: Session) -> dict:
    """构建表间关系（供缓存生产）"""
    inspector = inspect(db.get_bind())
    relationships = []
    table_labels = {}
    _label_meta = _table_label_meta()

    try:
        metadata_columns = {column["name"] for column in inspector.get_columns("metadata_tables")}
        if {"table_name", "table_chinese_name"}.issubset(metadata_columns):
            result = db.execute(text("SELECT table_name, table_chinese_name FROM metadata_tables"))
            table_labels = {row[0]: row[1] or row[0] for row in result}
    except Exception:
        pass
    
    # 1. 从 information_schema 获取外键关系（覆盖所有业务 schema：public + factory 等）
    try:
        # PostgreSQL / MySQL 通用：排除系统 schema，其余全部纳入
        result = db.execute(text("""
            SELECT
                tc.table_name AS source_table,
                kcu.column_name AS source_column,
                ccu.table_name AS target_table,
                ccu.column_name AS target_column,
                tc.constraint_name AS constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys', 'pg_catalog')
              AND tc.table_schema NOT LIKE 'pg_%'
            ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
        """))
        
        for row in result:
            relationships.append({
    "source_table": row[0],
    "source_column": row[1],
    "target_table": row[2],
    "target_column": row[3],
    "type": "foreign_key",
    "constraint_name": row[4],
    "description": f"外键：{row[0]}.{row[1]} → {row[2]}.{row[3]}"
})
    except Exception as e:
        print(f"获取外键关系失败: {e}")
        try:
            db.rollback()  # 事务终止（25P02）后必须回滚，否则后续查询全部被拒
        except Exception:
            pass
    
    # 2. 如果外键关系较少，补充预定义的业务关系
    if len(relationships) < 3:
        # 从 metadata_relationships 表读取
        try:
            result = db.execute(text("""
                SELECT from_table, from_field, to_table, to_field, relationship_type
                FROM metadata_relationships
                ORDER BY id
            """))
            for row in result:
                relationships.append({
                    "source_table": row[0],
                    "source_column": row[1],
                    "target_table": row[2],
                    "target_column": row[3],
                    "type": row[4] or "business",
                    "constraint_name": None,
                    "description": f"业务关系：{row[0]}.{row[1]} → {row[2]}.{row[3]}"
                })
        except Exception:
            pass
    
    table_names = [
        name for name in inspector.get_table_names()
        if not name.startswith(("_", "pg_", "metadata_"))
    ]

    # 对显式外键较少的数据库，基于同名 ID 字段补充低置信度的推导关系。
    # 收紧规则：外键关系充足(≥10)时不再推导；只推导 xxx_id 字段（排除裸 id → 否则
    # 每张表的 id 会连到所有表主键 id，产生几百条荒谬关系把 ER 图糊成一坨）。
    if len(relationships) < 10:
        known_pairs = {
            (r["source_table"], r["source_column"], r["target_table"], r["target_column"])
            for r in relationships
        }
        primary_keys = {}
        for table in table_names:
            try:
                primary_keys[table] = set(inspector.get_pk_constraint(table).get("constrained_columns", []))
            except Exception:
                primary_keys[table] = set()
        for source_table in table_names:
            try:
                source_columns = inspector.get_columns(source_table)
            except Exception:
                continue
            for source_column in source_columns:
                source_name = source_column["name"]
                if not (source_name.endswith("_id") and source_name != "id"):
                    continue
                for target_table in table_names:
                    if source_table == target_table:
                        continue
                    if source_name in primary_keys.get(target_table, set()):
                        pair = (source_table, source_name, target_table, source_name)
                        if pair not in known_pairs:
                            relationships.append({
                                "source_table": source_table,
                                "source_column": source_name,
                                "target_table": target_table,
                                "target_column": source_name,
                                "type": "inferred",
                                "constraint_name": None,
                                "description": f"推导关系：{source_table}.{source_name} → {target_table}.{source_name}",
                            })
                            known_pairs.add(pair)

    nodes = [
        {
            "id": table_name,
            "name": table_name,
            "label": _label_meta.get(table_name, {}).get("label")
                     or table_labels.get(table_name)
                     or table_name,
            "columns": len(inspector.get_columns(table_name)),
            "scene": _label_meta.get(table_name, {}).get("scene") or _table_scene(table_name),
            "nodeType": "业务对象",
            "connected": any(
                relation["source_table"] == table_name or relation["target_table"] == table_name
                for relation in relationships
            ),
        }
        for table_name in table_names
    ]

    # 关系明细描述用中文表名替换，提升可读性（只替换"完整表名."形态）：
    # 负向前瞻 (?<![A-Za-z0-9_.]) 确保 tn 是独立 token——排除 dim_product 里的 product、
    # 以及 factory.work_order 里被 schema 点分隔的 work_order（避免误改成"工厂表.产品表."）
    _re_desc = re
    for r in relationships:
        if r.get("description"):
            desc = r["description"]
            for tn, meta in _label_meta.items():
                if tn in desc:
                    desc = _re_desc.sub(r"(?<![A-Za-z0-9_.])" + _re_desc.escape(tn) + r"\.", meta["label"] + ".", desc)
            r["description"] = desc

    return {"nodes": nodes, "relationships": relationships}


# ========== 获取单个表详情 ==========

# ========== 获取分析主题列表 ==========

@router.get("/analysis-examples")
def get_analysis_examples(authorization: str = Header(None), db: Session = Depends(get_db)):
    """根据当前数据库生成快捷提问，且只保留点击后能直接产出结果的问题。

    权限（P0 修复）：必须登录（会触发示例问题分析，涉及库表结构）。
    走 10 分钟 TTL 缓存：7 个示例问题串行分析约 5s，缓存后秒回。
    """
    get_current_user(authorization)
    try:
        from routers.knowledge import _cached
        return _cached("analysis_examples", _build_analysis_examples, db)
    except Exception:
        return {"examples": _build_analysis_examples(db, inspect(db.get_bind()))}


def _build_analysis_examples(db: Session, inspector=None) -> dict:
    inspector = inspector or inspect(db.get_bind())
    candidate_questions = [
        "各工序的缺陷数量如何？",
        "各工序的良率如何？",
        "各工序的产量如何？",
        "设备的停机时长如何？",
        "库存的可用数量如何？",
        "缺陷数量的变化趋势如何？",
        "产量的变化趋势如何？",
    ]
    examples = []
    for question in candidate_questions:
        result = _run_analysis(db, inspector, question)
        payload = result.get("result", {})
        # 只有能真实产出数据的才保留；跳过需要澄清、缺少字段、分析失败的情况
        if payload.get("series"):
            examples.append({
                "id": payload.get("id", "example"),
                "question": question,
                "title": payload.get("title", "分析结果"),
                "unit": payload.get("unit", "分析值"),
                "unit_label": payload.get("unit_label", ""),
                "chart_type": payload.get("chart_type", "bar"),
                "series": payload.get("series", []),
            })
    return {"examples": examples}


def _run_analysis(db, inspector, question: str,
                  allowed_tables: set[str] | None = None, superuser: bool = False) -> dict:
    """根据当前数据库结构执行可解释的基础自然语言分析。"""
    table_ok = _acl_table_filter(allowed_tables, superuser)
    tables = [t for t in inspector.get_table_names()
              if not t.startswith(("_", "pg_")) and table_ok(t)]
    schema = {t: inspector.get_columns(t) for t in tables}
    all_columns = [(t, c["name"], str(c["type"]).upper()) for t in tables for c in schema[t]]

    numeric_types = ("INT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "MONEY")
    numeric = [(t, c, typ) for t, c, typ in all_columns if any(x in typ for x in numeric_types)]
    time_fields = [
        (t, c, typ) for t, c, typ in all_columns
        if any(word in c.lower() for word in ("date", "time", "day", "month", "year", "日期", "时间"))
    ]
    text_fields = [
        (t, c, typ)
        for t, c, typ in all_columns
        if any(x in c.lower() for x in ("name", "type", "category", "status", "code"))
        or c.lower().endswith("_id")
    ]
    keywords = question.lower()

    if not numeric:
        return {"result": {
            "id": "schema-overview", "question": question, "title": "当前数据库暂缺可聚合数值字段",
            "unit": "结果", "series": [], "explanation": "请提供包含数值字段的表后再进行统计分析。"
        }}

    intent_terms = {
        "产量": ("output", "good_qty", "input_qty", "产量", "产出"),
        "生产": ("output", "production", "process", "产量", "工序"),
        "不良": ("defect", "fail", "不良", "缺陷"),
        "质量": ("quality", "inspection", "defect", "质量", "检验"),
        "良率": ("good", "yield", "良率", "合格"),
        "停机": ("downtime", "minutes", "停机"),
        "设备": ("equipment", "machine", "设备"),
        "库存": ("inventory", "stock", "available", "库存"),
        "金额": ("amount", "price", "cost", "金额", "费用"),
    }
    question_terms = [term for key, terms in intent_terms.items() if key in keywords for term in terms]
    if "工序" in keywords:
        question_terms.extend(("process", "工序"))
    if "产线" in keywords or "生产线" in keywords:
        question_terms.extend(("line", "产线"))
    if "产品" in keywords:
        question_terms.extend(("product", "产品"))
    if "设备" in keywords:
        question_terms.extend(("equipment", "设备"))

    intent_names = []
    if any(word in keywords for word in ("产量", "产出", "生产", "工序")):
        intent_names.append("生产产出")
    if any(word in keywords for word in ("不良", "缺陷", "质量", "检验", "良率")):
        intent_names.append("质量表现")
    if any(word in keywords for word in ("设备", "停机", "报警", "运行")):
        intent_names.append("设备运行")
    if any(word in keywords for word in ("库存", "物料", "仓库", "安全库存")):
        intent_names.append("库存水位")
    if any(word in keywords for word in ("金额", "费用", "成本", "收入", "利润")):
        intent_names.append("金额统计")
    if any(word in keywords for word in ("趋势", "变化", "时间", "最近", "周期")):
        intent_names.append("趋势变化")

    if not intent_names:
        return {"result": {
            "id": "need-clarification", "question": question, "title": "还需要明确分析目标",
            "unit": "待确认", "series": [],
            "explanation": "我没有找到与当前数据库字段明确对应的业务意图，因此不会强行选择字段生成图表。请说明要分析的指标、对象或时间范围，例如“分析库存数量”“查看各工序不良数量”。",
            "analysis": {
                "intent": "未识别",
                "source_tables": [],
                "fields": [],
                "calculation": "未执行",
                "grouping": "未执行",
                "limitation": "问题缺少可匹配的分析目标。"
            }
        }}

    if any(word in keywords for word in ("趋势", "变化", "时间", "最近", "周期")) and not any(
        word in keywords for word in ("产量", "产出", "不良", "缺陷", "良率", "停机", "设备", "库存", "金额", "数量", "比例")
    ):
        return {"result": {
            "id": "need-metric", "question": question, "title": "趋势分析还缺少指标",
            "unit": "待确认", "series": [],
            "explanation": "你描述了时间变化，但没有说明要观察什么指标。请补充“产量、不良数量、良率、停机时长、库存数量”等指标。",
            "analysis": {
                "intent": "趋势变化",
                "source_tables": [], "fields": [], "calculation": "未执行",
                "grouping": "未执行", "limitation": "缺少要观察的业务指标。"
            }
        }}

    if any(word in keywords for word in ("趋势", "变化", "时间", "最近", "周期")) and not time_fields:
        return {"result": {
            "id": "missing-time-field", "question": question, "title": "当前数据库缺少时间字段",
            "unit": "无法生成趋势", "series": [],
            "explanation": "这个问题要求按时间观察变化，但当前数据库没有识别到日期或时间字段，因此不能生成趋势图。",
            "analysis": {
                "intent": "、".join(intent_names),
                "source_tables": [],
                "fields": [],
                "calculation": "未执行",
                "grouping": "按时间分组（不可用）",
                "limitation": "当前数据库没有可用的日期/时间字段。"
            }
        }}

    def score(item):
        table, column, _ = item
        table_text = table.lower()
        column_text = column.lower()
        base_score = sum(
            3 if term.lower() in column_text else 1 if term.lower() in table_text else 0
            for term in question_terms
        )
        if column_text.endswith("_id") or column_text == "id":
            base_score -= 0.5
        return base_score

    quality_requested = any(word in keywords for word in ("不良", "缺陷", "检验结果", "问题分布"))
    if quality_requested:
        quality_candidates = [
            item for item in numeric
            if any(word in f"{item[0]} {item[1]}".lower() for word in ("defect", "fail", "inspection", "quality", "不良", "缺陷", "检验"))
        ]
        preferred = sorted(quality_candidates or numeric, key=score, reverse=True)
    else:
        preferred = sorted(numeric, key=score, reverse=True)
    value_table, value_column, _ = preferred[0]

    # 对常见制造业问题使用已确认存在的字段表达式，避免把 output_id/input_qty 当作产量。
    if "产量" in keywords or "产出" in keywords:
        production_fields = {column for table, column, _ in numeric if table == value_table}
        if {"good_qty", "defect_qty"}.issubset(production_fields):
            value_column = "good_qty + defect_qty"
        elif "output_qty" in production_fields:
            value_column = "output_qty"
    elif "不良" in keywords or "缺陷" in keywords:
        defect_fields = {column for table, column, _ in numeric if table == value_table}
        if "defect_qty" in defect_fields:
            value_column = "defect_qty"
    elif "停机" in keywords:
        downtime_fields = {column for table, column, _ in numeric if table == value_table}
        if "downtime_minutes" in downtime_fields:
            value_column = "downtime_minutes"
    elif "良率" in keywords:
        yield_fields = {column for table, column, _ in numeric if table == value_table}
        if "standard_yield_rate" in yield_fields:
            value_column = "standard_yield_rate"
        elif {"good_qty", "defect_qty"}.issubset(yield_fields):
            value_column = "good_qty"
    group_candidates = [item for item in text_fields if item[0] == value_table and item[1] != value_column]
    if not group_candidates:
        group_candidates = text_fields
    if not group_candidates:
        return {"result": {
            "id": "numeric-summary", "question": question, "title": f"{value_table}.{value_column} 汇总",
            "unit": value_column, "series": [{"label": value_table, "value": 0}],
            "explanation": "当前数据库没有可用于分组展示的文本字段。"
        }}

    def group_score(item):
        table, column, _ = item
        text_value = f"{table} {column}".lower()
        preferred_words = ("process", "line", "product", "equipment", "warehouse", "category", "name", "status", "code", "工序", "产线", "产品", "设备", "仓库", "类别", "名称")
        result = sum(1 for word in preferred_words if word in text_value) + score((table, column, ""))
        if "工序" in keywords and "process" in column:
            result += 10
        if "产线" in keywords and "line" in column:
            result += 10
        if "设备" in keywords and "equipment" in column:
            result += 10
        if "停机" in keywords and column == "equipment_id":
            result += 10
        if "产品" in keywords and "product" in column:
            result += 10
        if quality_requested and column in ("defect_type", "defect_code", "inspection_result", "process_id", "responsible_process_id"):
            result += 20
        if quality_requested and column in ("shift_code", "output_id"):
            result -= 10
        if column in ("output_id", "inspection_id", "snapshot_id", "downtime_id"):
            result -= 10
        return result

    group_table, group_column, _ = sorted(group_candidates, key=group_score, reverse=True)[0]
    quote = quote_ident

    # 业务化翻译：指标名、单位、分组维度中文名、编码值 -> 中文名
    metric_business, metric_unit = metric_business_name(value_column)
    dimension_business = dimension_business_name(group_column)
    label_map = build_dimension_label_map(db, inspector, group_column)
    is_trend = any(word in keywords for word in ("趋势", "变化", "时间", "最近", "周期"))

    safe_value_expression = value_column if " + " in value_column else quote(value_column)
    aggregate_function = "AVG" if value_column == "standard_yield_rate" else "SUM"
    is_yield = aggregate_function == "AVG" and value_column == "standard_yield_rate"

    # 趋势类问题：优先按时间维度生成折线趋势图
    if is_trend:
        time_field = None
        for column_info in schema.get(value_table, []):
            column_name = column_info["name"].lower()
            if any(word in column_name for word in ("date", "time", "day", "month")):
                time_field = column_info["name"]
                break
        if time_field:
            trend_query = text(
                f"SELECT {quote(time_field)}, {aggregate_function}({safe_value_expression}) AS value "
                f"FROM {quote(value_table)} GROUP BY {quote(time_field)} "
                f"ORDER BY {quote(time_field)}"
            )
            try:
                trend_rows = db.execute(trend_query)
                trend_series = []
                for row in trend_rows:
                    raw_value = float(row[1] or 0)
                    display_value = round(raw_value * 100, 2) if is_yield else round(raw_value, 2)
                    trend_series.append({
                        "label": str(row[0])[:10],
                        "value": display_value,
                    })
                return {"result": {
                    "id": "trend-analysis", "chart_type": "trend",
                    "question": question,
                    "title": f"{metric_business}变化趋势",
                    "unit": metric_business, "unit_label": metric_unit,
                    "series": trend_series,
                    "explanation": f"按时间统计{metric_business}的变化趋势。",
                    "analysis": {
                        "metric": metric_business,
                        "dimension": "时间",
                        "unit_label": metric_unit,
                        "calculation": f"按日期统计{metric_business}",
                        "grouping": "按时间分组",
                        "limitation": "当前版本使用字段语义进行匹配，复杂问题后续由 Agent 进一步处理。"
                    }
                }}
            except Exception as exc:
                return {"result": {
                    "id": "analysis-error", "chart_type": "bar", "question": question,
                    "title": "暂时无法生成趋势图", "unit": "结果", "series": [],
                    "explanation": f"无法按时间统计：{exc}"
                }}

    query = text(
        f"SELECT {quote(group_column)}, {aggregate_function}({safe_value_expression}) AS value "
        f"FROM {quote(value_table)} GROUP BY {quote(group_column)} "
        "ORDER BY value DESC LIMIT 12"
    )
    try:
        rows = db.execute(query)
        series = []
        for row in rows:
            raw_value = float(row[1] or 0)
            display_value = round(raw_value * 100, 2) if is_yield else round(raw_value, 2)
            series.append({
                "label": label_map.get(str(row[0]), str(row[0])),
                "value": display_value,
            })
        title = f"各{dimension_business}{metric_business}对比"
        if aggregate_function == "AVG":
            calculation = f"各{dimension_business}{metric_business}取平均"
        else:
            calculation = f"各{dimension_business}{metric_business}求和"
        if value_column == "good_qty + defect_qty":
            calculation = "各工序产量 = 合格数量 + 不良数量"
        return {"result": {
            "id": "dynamic-analysis", "chart_type": "bar",
            "question": question, "title": title,
            "unit": metric_business, "unit_label": metric_unit, "series": series,
            "explanation": f"按{dimension_business}统计{metric_business}，并从高到低排列。",
            "analysis": {
                "intent": "、".join(intent_names),
                "source_tables": [value_table, group_table] if value_table != group_table else [value_table],
                "metric": metric_business,
                "dimension": dimension_business,
                "unit_label": metric_unit,
                "calculation": calculation,
                "grouping": f"按{dimension_business}分组",
                "limitation": "当前版本使用字段语义进行匹配，复杂问题后续由 Agent 进一步处理。"
            }
        }}
    except Exception as exc:
        return {"result": {
            "id": "analysis-error", "chart_type": "bar", "question": question,
            "title": "暂时无法完成这项分析",
            "unit": "结果", "series": [], "explanation": str(exc)
        }}


@router.post("/analyze")
def analyze_question(request: AnalysisRequest, authorization: str = Header(None),
                     db: Session = Depends(get_db)):
    """根据当前数据库结构执行可解释的基础自然语言分析。

    权限（P0 修复）：必须登录，且候选表按当前用户 ACL 白名单过滤。
    """
    from security.enforcer import build_acl_context
    acl = build_acl_context(get_current_user(authorization))
    inspector = inspect(db.get_bind())
    return _run_analysis(db, inspector, request.question.strip(),
                         allowed_tables=acl.allowed_tables, superuser=acl.superuser)


@router.get("/topics")
def get_analysis_topics(authorization: str = Header(None), db: Session = Depends(get_db)):
    """获取当前数据库动态推导出的分析主题。

    与知识页「业务场景」同源（复用 knowledge._build_scenes 的动态主题引擎），
    每个主题带完整层级：涉及对象（表+字段翻译）→ 核心指标 → 判定规则 → 支持问题，
    并对齐角色（管理员/viewer）与热度。
    权限：已登录非管理员只返回「所有相关表均可见」的主题；guest/超级管理员不受限。
    """
    try:
        # 走 knowledge 的 _cached（60s TTL）——与知识页 /scenes 共享同一份缓存，
        # 且能被后端启动预热填充，避免每次请求全库重建（首次约 17s → 命中 <1s）
        from routers.knowledge import _build_scenes, _cached
        data = _cached("scenes", _build_scenes, db)
        topics = _scenes_to_topics(data)
        return _acl_filter_topics({"topics": topics}, authorization)
    except Exception as e:
        print(f"统一主题源失败，回退旧实现: {e}")
        inspector = inspect(db.get_bind())
        return _acl_filter_topics({"topics": get_dynamic_topics(db, inspector)}, authorization)


def _scenes_to_topics(data: dict) -> list[dict]:
    """把 knowledge._build_scenes 的返回转换为 /topics 接口的主题列表"""
    topics = []
    for s in (data.get("scenes") or {}).values():
        objs = s.get("objects") or []
        sub = s.get("topics") or []
        questions = []
        for t in sub:
            for q in (t.get("questions") or [])[:2]:
                if q not in questions:
                    questions.append(q)
        if not questions:
            questions = ["分析该主题的关键变化？", "哪些记录最需要关注？"]
        chart_types = []
        for t in sub:
            if t.get("kind") == "趋势" or "趋势" in t.get("name", ""):
                chart_types.append("line")
            if "占比" in t.get("desc", "") or "分布" in t.get("name", "") or "Pareto" in t.get("name", ""):
                chart_types.append("pie")
        if not chart_types:
            chart_types = ["bar", "line"]
        topics.append({
            "id": s["key"],
            "name": s["name"],
            "icon": s["icon"],
            "description": s["desc"],
            "kind": "业务场景",
            "roles": s.get("roles", []),
            "related_tables": [o.get("table") for o in objs],
            "table_labels": {o.get("table"): o.get("label") for o in objs},
            "objects": objs,
            "metrics": s.get("metrics", []),
            "rules": s.get("rules", []),
            "topics": sub,
            "supported_questions": questions,
            "evidence": [
                {"table": o.get("table"), "field": f}
                for o in objs for f in (o.get("fields") or [])[:3]
            ][:12],
            "chart_types": chart_types,
        })
    return topics


@router.get("/{table_name}")
def get_table_detail(
    table_name: str,
    page: int = 1,
    page_size: int = 100,
    time_column: str | None = None,
    start_date: str = "",
    end_date: str = "",
    order_column: str | None = None,
    order_dir: str = "asc",
    db: Session = Depends(get_db),
    authorization: str = Header(None),
):
    """获取单个表的详细信息和数据（分页返回，避免大表全量拉取卡顿）

    time_column/start_date/end_date：可选时间范围过滤（前端「全部数据」的近/远筛选）。
    半开区间 [start, end+1天)：start/end 传 'YYYY-MM-DD'；time_column 必须存在且为
    date/time 类列（否则忽略过滤），列名走标识符白名单校验防注入，值走 SQL 绑定参数。
    order_column/order_dir：可选列排序（asc/desc）。列名白名单 + 存在性校验，方向白名单，防注入。

    权限（与 /api/permission/data-catalog 同源）：
    - guest（开放模式匿名）与超级管理员：完整返回。
    - 已登录非管理员：无权表 → 403；可见表按 per-user 字段白名单 / 角色列拒绝
      裁剪字段，敏感列按脱敏表达式改写 SQL（样例数据视图同样脱敏）。
    """
    # 标识符校验：表名只允许字母数字下划线（可带 schema 点号），杜绝注入与异常参数
    _assert_ident(table_name, "表")
    bare = table_name.split(".")[-1].lower()
    inspector = inspect(db.get_bind())

    # ── 权限：表级访问校验（guest / superuser 放行）──
    u = get_current_user(authorization)
    ctx = None
    if u["role"] != "guest":
        ctx = enforcer.build_acl_context(u)
        if ctx.superuser:
            ctx = None  # 超级管理员不限制
        else:
            ok, _denied = enforcer.check_table_access(ctx, [bare])
            if not ok:
                raise HTTPException(
                    status_code=403,
                    detail=f"无权访问表「{table_name}」：当前账号未被授权查看该表，可在数据页点击「反馈/申请授权」向管理员申请")

    # 获取主键列名集合（表不存在 → 404，而非 500）
    try:
        pk_constraint = inspector.get_pk_constraint(table_name)
        pk_columns = set(pk_constraint.get("constrained_columns", []))
        columns = []
        for column in inspector.get_columns(table_name):
            columns.append({
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": column.get("nullable", True),
                "comment": column.get("comment", "") or "",
                "primary_key": column["name"] in pk_columns,
            })
    except Exception as exc:
        # NoSuchTableError（sqlalchemy.exc.NoSuchTableError）等反射失败 → 404
        if "NoSuchTableError" in type(exc).__name__ or "does not exist" in str(exc).lower():
            raise HTTPException(status_code=404, detail=f"表 '{table_name}' 不存在") from exc
        raise

    # ── 权限：字段级裁剪（per-user 白名单 / 角色列拒绝；脱敏字段标记 masked）──
    masks: dict[str, str] = {}
    if ctx is not None:
        wl = ctx.column_whitelist.get(bare)
        denies = ctx.column_denies.get(bare, set())
        masks = ctx.column_masks.get(bare, {})
        visible = []
        for c in columns:
            cl = c["name"].lower()
            if cl in denies:
                continue
            if wl is not None and cl not in wl:
                continue
            if cl in masks:
                c = {**c, "masked": True}  # 脱敏字段：标记供前端显示 🔒 标识
            visible.append(c)
        columns = visible

    # 分页参数安全限制
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 500)

    # 行级权限：注入该表的行过滤条件（已渲染好的 SQL 条件串，与 Agent 查询链路
    # rewrite_sql 同源），防止数据表页绕过行级权限泄露全部行。
    row_cond = ctx.row_filters.get(bare) if ctx is not None else ""
    where_clause = f" WHERE {row_cond}" if row_cond else ""
    _ep: dict = {}  # 时间过滤的绑定参数（无过滤时为空）

    # 时间范围过滤（可选）：校验列存在且为 date/time 类；区间右端开区间到 end+1 天
    _tc = (time_column or "").strip()
    if _tc and (start_date or end_date):
        _assert_ident(_tc, "时间列")
        _tcbare = _tc.lower()
        _tcol = next((c for c in columns if c["name"].lower() == _tcbare), None)
        if _tcol is not None and any(
                k in str(_tcol["type"]).lower() for k in ("date", "time")):
            _tq = quote_ident(_tcol["name"])
            _pg = "postgres" in (get_db_type() or "")
            _extra = ""
            if (start_date or "").strip():
                _ep["_dstart"] = str(start_date).strip()[:10]
                _extra += f" AND {_tq} >= :_dstart"
            _e = (end_date or "").strip()[:10]
            if _e:
                _ep["_dend"] = _e
                _extra += (f" AND {_tq} < (CAST(:_dend AS DATE) + INTERVAL '1 day')"
                           if _pg else
                           f" AND {_tq} < DATE_ADD(CAST(:_dend AS DATE), INTERVAL 1 DAY)")
            if _extra:
                where_clause = (where_clause + _extra) if where_clause \
                    else " WHERE " + _extra[len(" AND "):]

    # 列排序（可选）：列名存在校验 + 白名单；方向白名单
    order_sql = ""
    _oc = (order_column or "").strip()
    _od = (order_dir or "").strip().lower()
    if _oc:
        _assert_ident(_oc, "排序列")
        _ocb = _oc.lower()
        if any(c["name"].lower() == _ocb for c in columns) and _od in ("asc", "desc"):
            order_sql = f" ORDER BY {quote_ident(_oc)} {_od.upper()}"

    # 总行数
    total_count = 0
    try:
        total_result = db.execute(text(f'SELECT COUNT(*) FROM {_quote_qualified(table_name)}{where_clause}'), _ep)
        total_count = int(total_result.scalar() or 0)
    except Exception:
        pass

    # 当前页数据
    page_data = []
    try:
        offset = (page - 1) * page_size
        if columns:
            if ctx is not None:
                # 受控视图：仅查询可见列；敏感列按脱敏表达式改写（样例数据同样脱敏）
                dialect = "postgres" if "postgres" in (get_db_type() or "") else "mysql"
                select_parts = []
                for c in columns:
                    cl = c["name"].lower()
                    m = masks.get(cl)
                    if m:
                        select_parts.append(
                            enforcer.mask_expression(quote_ident(c["name"]), m, dialect)
                            + f" AS {quote_ident(c['name'])}")
                    else:
                        select_parts.append(quote_ident(c["name"]))
                select_sql = ", ".join(select_parts)
            else:
                select_sql = "*"
            result = db.execute(text(
                f'SELECT {select_sql} FROM {_quote_qualified(table_name)}{where_clause}{order_sql} '
                f'LIMIT {page_size} OFFSET {offset}'
            ), _ep)
            page_data = [dict(row._mapping) for row in result]
    except Exception as e:
        print(f"获取表数据失败: {e}")

    return {
        "table_name": table_name,
        "columns": columns,
        "sample_data": page_data,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "is_full_data": True,
    }


# ===== AI 字段翻译（非 yans 库专用）=====
@router.post("/{table_name}/ai-translate-fields", dependencies=[Depends(require_roles("admin"))])
def ai_translate_fields(
    table_name: str,
    dry_run: int = 0,
    db: Session = Depends(get_db),
):
    """把字段名/英文备注用当前 AI 模型翻译为简短中文业务名，并写回该列的备注。

    规则（用户约定）：
    - yans 库：一律不执行（该库翻译以人工备注为准，"按备注的来"不能动）；
    - 其它库：仅处理「备注为空或纯英文」的列，已有中文备注的列跳过（不覆盖人工内容）；
    - 产出短中文名（≤12 字，可含 ID/编号/数量等），写回 database comment，
      「全部数据」表头/字段说明随之显示中文。
    dry_run=1：只返回拟翻译结果、不写库（用于测试 LLM 效果）。
    """
    from database import get_database_config
    _cfg = get_database_config() or {}
    _dbn = str(_cfg.get("name") or "").lower()
    if "yans" in _dbn:
        return {"ok": False, "yans": True,
                "message": "yans 库的字段翻译以人工备注为准，不执行 AI 翻译。",
                "translated": [], "skipped": []}
    _assert_ident(table_name, "表")
    inspector = inspect(db.get_bind())
    try:
        columns = inspector.get_columns(table_name)
    except Exception as exc:
        if "NoSuchTableError" in type(exc).__name__ or "does not exist" in str(exc).lower():
            raise HTTPException(status_code=404, detail=f"表 '{table_name}' 不存在") from exc
        raise HTTPException(status_code=500, detail=f"读取表结构失败: {exc}") from exc
    need = []
    for c in columns:
        cm = str(c.get("comment") or "").strip()
        if re.search(r"[\u4e00-\u9fff]", cm):
            continue  # 已有中文 → 不动
        need.append({"name": c["name"], "type": str(c.get("type")), "comment": cm})
    if not need:
        return {"ok": True, "yans": False, "translated": [], "skipped": 0,
                "message": "所有字段都已有中文备注，无需翻译。"}
    need = need[:100]  # 单次上限，防 prompt 过长

    # —— 调当前 AI 模型翻译 ——
    import json as _json
    from langchain_core.messages import HumanMessage
    from agent.llm_service import _make_llm
    _rows = "\n".join(
        f"- {i['name']} ({i['type']}) 备注: {i['comment'] or '无'}" for i in need)
    _prompt = (
        "你是制造业/企业数据库字段翻译员。把下列字段翻译成简洁的中文业务字段名"
        "（≤12 字，允许含 ID/编号/数量/金额/时间/状态 等词；看不懂的保留原样不翻译）。"
        "只输出一个 JSON 对象，键必须与原列名完全一致，值是对应的中文名；不要解释、不要 markdown。\n"
        f"字段列表：\n{_rows}")
    translated = []
    llm_error = ""
    try:
        _llm = _make_llm(temp=0.0, max_tokens=1600, json_mode=True, timeout=60, max_retries=0)
        _r = _llm.invoke([HumanMessage(content=_prompt)])
        _txt = str(getattr(_r, "content", "") or "").strip()
        if _txt.startswith("```"):
            import re as _re
            _txt = _re.sub(r"^```(?:json)?\s*|\s*```$", "", _txt).strip()
        _map = _json.loads(_txt) if _txt else {}
        if not isinstance(_map, dict):
            _map = {}
        for it in need:
            cn = str(_map.get(it["name"]) or "").strip()
            if cn and re.search(r"[\u4e00-\u9fff]", cn) and cn != it["name"]:
                translated.append({"name": it["name"], "type": it["type"],
                                   "comment": it["comment"], "cn": cn})
    except Exception as e:
        llm_error = f"{type(e).__name__}: {str(e)[:180]}"

    if dry_run or llm_error or not translated:
        msg = (f"AI 翻译{'（试运行，未写库）' if dry_run else '未生效'}: 拟翻译 {len(translated)} 个字段。"
               if translated else ("AI 翻译失败：" + llm_error if llm_error else "AI 未返回可用翻译，请检查模型配置/余额。"))
        return {"ok": bool(translated), "dry_run": bool(dry_run),
                "translated": translated, "skipped": 0, "message": msg,
                "error": llm_error or None}

    # —— 写回列备注 ——
    from database import get_db_type
    _pg = get_db_type() != "mysql"
    try:
        for o in translated:
            _lit = "'" + o["cn"].replace("\\", "\\\\").replace("'", "''") + "'"
            if _pg:
                _sql = text(f'COMMENT ON COLUMN {_quote_qualified(table_name)}.{quote_ident(o["name"])} IS {_lit}')
            else:
                _sql = text(f'ALTER TABLE {_quote_qualified(table_name)} MODIFY COLUMN {quote_ident(o["name"])} '
                            f'{_column_type_for_mysql(db, table_name, o["name"])} COMMENT {_lit}')
            db.execute(_sql)
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "translated": [], "skipped": 0,
                "message": f"写回备注失败: {str(e)[:160]}"}
    return {"ok": True, "yans": False, "dry_run": False,
            "translated": translated, "skipped": 0,
            "message": f"已翻译并写入 {len(translated)} 个字段的中文备注。"}
