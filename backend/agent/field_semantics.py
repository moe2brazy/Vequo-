"""字段语义翻译层 — 让非专业人员看懂字段含义

需求背景（文档《业务知识页面主题展示问题》）：同一个字段名在不同表里含义完全不同
（如 status 在工单表是「工单状态」、在设备表是「设备运行状态」），是数据底座最大的坑。

设计：
- explain_field(table, field, comment="") 统一翻译入口；
- 优先级：精确匹配「表.字段」语义 > 表级通用 > 字段通用语义 > 数据库注释 > 英文直译兜底；
- 语义来源：内置种子（代码内置，覆盖常见制造业字段）+ backend/field_semantics.json
  （用户可扩展：{"table.field": "含义", "field": "通用含义"}）。
"""

from __future__ import annotations

import functools
import json
import re
import threading
from pathlib import Path

_SEMANTICS_PATH = Path(__file__).resolve().parent.parent / "field_semantics.json"
# 字段名 → 短中文业务名（表头翻译用），见 field_cn.json 头部说明
_FIELD_CN_PATH = Path(__file__).resolve().parent.parent / "field_cn.json"
# AI 补全的翻译缓存（词典未命中时由当前模型补齐，落盘复用，避免每次问都调模型）
_FIELD_CN_AI_PATH = Path(__file__).resolve().parent.parent / "field_cn_ai.json"

# ── 内置种子：表级语义（解决同名不同义，优先级最高）──
# 2026-09-12 修正：原值凭通用经验编写，与评测库真实枚举值不符（如 mes_work_order.status
# 实际是"进行中/已完成/已取消"），反而诱导 LLM 写错值（status='running'/'in_progress'）。
# 以下按本机库 DISTINCT 实测值修正；不存在的列（eqp_downtime_record.status 等）已删除。
_BUILTIN_TABLE_SEMANTICS: dict[str, str] = {
    "mes_work_order.status": "工单状态（进行中/已完成/已取消）",
    "factory.work_order.status": "工单状态（计划/生产中/已完成/已关闭/暂停）",
    "dim_equipment.status": "设备状态（运行/停机/维修/空闲）",
    "factory.equipment.status": "设备状态（正常/维修中/闲置）",
    "qms_inspection.result": "检验结论（合格/不合格）",
    "quality_inspection.result": "检验结论（合格/不合格）",
    "factory.sales_order.status": "订单状态（草稿/已确认/发货中/已完成/已关闭）",
    "factory.purchase_order.status": "采购单状态（草稿/已审核/部分收货/已收货/已关闭）",
    "factory.employee.status": "员工状态（在职/试用/离职）",
    "factory.supplier.status": "供应商合作状态（合作中/暂停/淘汰）",
    "factory.supplier.rating": "供应商评级（1-5 整数，数字越大评级越高；不是 category）",
    "factory.supplier.category": "供应商所属行业类别（不是评级，评级用 rating 列）",
    "factory.attendance.status": "考勤状态（正常/迟到/早退/请假/缺勤）",
    "factory.quality_inspection.defect_type": "缺陷类型（焊点虚焊/尺寸超差/材料缺陷/表面划伤/功能失效/漏工序/装配不良，可为空）",
}

# ── 内置种子：字段通用语义（同名字段兜底）──
_BUILTIN_FIELD_SEMANTICS: dict[str, str] = {
    "status": "状态字段（不同表含义不同，见对应表注释）",
    "type": "类型字段（分类标识）",
    "result": "结果字段（判定结论）",
    "category": "类别（业务分类）",
    "level": "等级（优先级/严重度分级）",
    "flag": "标志位（是/否标记）",
    "is_deleted": "逻辑删除标记（1=已删除，0=正常）",
    "remark": "备注（补充说明文字）",
    "comment": "备注/说明",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "create_time": "创建时间",
    "update_time": "更新时间",
    "created_by": "创建人",
    "updated_by": "更新人",
    "version": "版本号（乐观锁）",
    "snapshot_date": "快照日期（该记录对应的数据时点）",
    "stat_date": "统计日期（数据归属日期）",
    "date": "日期",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "duration": "持续时间（分钟）",
    "duration_minutes": "持续时间（分钟）",
    "qty": "数量",
    "quantity": "数量",
    "amount": "金额",
    "price": "单价",
    "total_amount": "总金额",
    "unit": "单位",
    "name": "名称",
    "code": "编码（唯一标识）",
    "product_id": "产品ID（关联产品主数据）",
    "product_name": "产品名称",
    "product_code": "产品编码",
    "warehouse_code": "仓库编码",
    "available_qty": "可用库存数量",
    "safety_stock_qty": "安全库存数量（低于该值触发补货预警）",
    "input_qty": "投入数量（投产的原料/在制品数量）",
    "good_qty": "合格数量（良品产出）",
    "defect_qty": "不良数量（缺陷/不合格产出）",
    "fail_qty": "不合格数量",
    "sample_qty": "抽样数量（质检抽取的样本数）",
    "yield_rate": "良率（合格产出/总产出）",
    "std_yield_rate": "标准良率（工序预设的标准合格率，区别于实测计算良率）",
    "process_id": "工序ID（关联工序主数据）",
    "process_name": "工序名称",
    "equipment_id": "设备ID（关联设备主数据）",
    "equipment_status": "设备状态（运行/维修/停机）",
    "line_id": "产线ID（关联产线主数据）",
    "production_line": "产线",
    "order_status": "订单/工单状态",
    "order_type": "订单类型",
    "order_id": "工单/订单ID",
    "work_order_id": "工单ID",
    "employee_id": "员工ID（关联员工主数据）",
    "department_id": "部门ID（关联部门主数据）",
    "supplier_id": "供应商ID",
    "customer_id": "客户ID",
}


def _load_user_semantics() -> dict[str, str]:
    try:
        if _SEMANTICS_PATH.exists():
            with open(_SEMANTICS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


_fs_cache: dict[tuple, str] = {}
_FS_MAX = 4096


def _fs_mtime() -> float:
    """用户语义 JSON 的修改时间：编辑文件后缓存失效（lru_cache 无感知会导致扩展不生效）"""
    try:
        return _SEMANTICS_PATH.stat().st_mtime
    except Exception:
        return 0.0


def explain_field(table_name: str, field_name: str, comment: str = "") -> str:
    """返回字段的业务含义翻译。

    优先级：精确「表.字段」> 用户 JSON 字段通用 > 内置表级 > 内置字段通用 > 数据库注释 > 英文直译。
    带 mtime 感知缓存：用户编辑 field_semantics.json 后立即生效。
    """
    key = (_fs_mtime(), table_name, field_name, comment)
    if key in _fs_cache:
        return _fs_cache[key]

    bare_table = table_name.split(".", 1)[-1].lower()
    field = field_name.lower()
    user = _load_user_semantics()

    # 1. 精确 表.字段（用户 JSON 优先）
    for k in (f"{bare_table}.{field}", f"{table_name}.{field_name}", f"{table_name}.{field_name.lower()}"):
        if k in user:
            return _cache_fs(key, user[k])
    for k in (f"{bare_table}.{field}", f"{table_name}.{field}"):
        if k in _BUILTIN_TABLE_SEMANTICS:
            return _cache_fs(key, _BUILTIN_TABLE_SEMANTICS[k])

    # 2. 用户 JSON 字段通用
    if field in user:
        return _cache_fs(key, user[field])

    # 3. 内置字段通用
    if field in _BUILTIN_FIELD_SEMANTICS:
        return _cache_fs(key, _BUILTIN_FIELD_SEMANTICS[field])

    # 4. 数据库注释
    if comment and comment.strip():
        return _cache_fs(key, comment.strip())

    # 5. 直译兜底
    parts = field_name.split("_")
    cn = "".join(_PART_CN.get(p, p) for p in parts)
    return _cache_fs(key, cn or field_name)


def table_specific_semantics(table_name: str, field_name: str) -> str | None:
    """返回「表.字段」精确语义（内置表级 + 用户 JSON 表级）；无则 None。

    供 schema 上下文构建器调用：命中时说明该字段在此业务上下文里有唯一释义
    （如 mes_work_order.status vs dim_equipment.status），值得注入消歧。
    """
    bare_table = table_name.split(".", 1)[-1].lower()
    field = field_name.lower()
    user = _load_user_semantics()
    for k in (f"{bare_table}.{field}", f"{table_name}.{field_name}", f"{table_name}.{field_name.lower()}"):
        if k in user:
            return user[k]
    for k in (f"{bare_table}.{field}", f"{table_name}.{field}"):
        if k in _BUILTIN_TABLE_SEMANTICS:
            return _BUILTIN_TABLE_SEMANTICS[k]
    return None


# ─────────────────────────────────────────────────────────────
# 结果表头翻译：field_label —— 把英文字段名翻成「短中文名」
# 与 explain_field 的区别：explain_field 输出「含义长句」（给 LLM prompt 用），
# field_label 输出「短名」（给结果表头用，如 rework_qty → 返工数量）。
# 没把握时返回空串（宁可不标注，也不输出「rework数量」这种半成品）。
# ─────────────────────────────────────────────────────────────

_HAS_CN_RE = re.compile(r"[\u4e00-\u9fff]")
_CN_SEPS = ("（", "(", "，", ",", "、", "。", "；", ";", "：", ":", "\n", "\t")


def has_chinese(text: str) -> bool:
    return bool(_HAS_CN_RE.search(text or ""))


def short_cn(text: str) -> str:
    """取「短名」：截到第一个括号/逗号/分号等分隔符之前，并去掉尾部标点。

    '返工数量（yans 实测该列存在）' → '返工数量'
    '工单ID，主键' → '工单ID'
    """
    t = (text or "").strip()
    if not t:
        return ""
    for sep in _CN_SEPS:
        i = t.find(sep)
        if i > 0:
            t = t[:i]
    return t.strip().strip("-—·. ")


_field_cn_cache: dict | None = None
_field_cn_mtime: float = -1.0


def _load_field_cn() -> dict:
    """加载 field_cn.json（字段名短中文词典），带 mtime 感知缓存。"""
    global _field_cn_cache, _field_cn_mtime
    try:
        m = _FIELD_CN_PATH.stat().st_mtime
    except Exception:
        return _field_cn_cache or {}
    if _field_cn_cache is not None and m == _field_cn_mtime:
        return _field_cn_cache
    data: dict = {}
    try:
        with open(_FIELD_CN_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue          # 跳过 _comment 等说明键
                data[k.lower()] = {str(fk).lower(): str(fv) for fk, fv in v.items()}
    except Exception:
        data = {}
    _field_cn_cache = data
    _field_cn_mtime = m
    return data


def _builtin_label(field_name: str) -> str:
    """英文直译兜底：所有片段都认得才给中文，否则返回空（避免「rework数量」）。"""
    field = (field_name or "").lower()
    if not field or field == "*":
        return ""
    parts = [p for p in field.split("_") if p]
    if not parts:
        return ""
    out = []
    for p in parts:
        cn = _PART_CN.get(p)
        if not cn:
            return ""              # 有一段认不出 → 整体放弃，交给 AI
        out.append(cn)
    return "".join(out)


def field_label(table_name: str, field_name: str, comment: str = "") -> str:
    """结果表头用的短中文名（无可靠中文时返回空串）。

    优先级：用户语义 JSON「表.字段」> field_cn.json 词典 > 内置表级语义 >
    内置/用户字段通用语义 > 库表注释/元数据描述 > 英文直译（全片段可译才用）。
    """
    field = (field_name or "").strip()
    if not field or field == "*":
        return ""
    bare = (table_name or "").split(".")[-1].lower()
    key = field.lower()
    user = _load_user_semantics()

    # 1. 用户自定义语义（表.字段）
    for k in (f"{bare}.{key}", f"{table_name}.{field}"):
        v = user.get(k)
        if v and has_chinese(v):
            return short_cn(str(v))

    # 2. 字段短名词典（人工/文档维护）
    for tbl in (bare, (table_name or "").lower()):
        hit = (_load_field_cn().get(tbl) or {}).get(key)
        if hit:
            return short_cn(hit)

    # 3. 内置表级语义（同名字段不同义）
    for k in (f"{bare}.{key}", f"{table_name}.{field}"):
        v = _BUILTIN_TABLE_SEMANTICS.get(k)
        if v:
            return short_cn(v)

    # 4. 用户 / 内置字段通用语义
    v = user.get(key) or _BUILTIN_FIELD_SEMANTICS.get(key)
    if v and has_chinese(str(v)):
        return short_cn(str(v))

    # 5. 库表注释 / 元数据描述（仅当确实是中文）
    if comment and has_chinese(comment):
        return short_cn(comment)

    # 6. 英文直译兜底
    return _builtin_label(field)


# ── AI 补全：词典没收录的字段，用当前大模型补一个短中文名并缓存 ──

_ai_cn_lock = threading.Lock()
_ai_cn_cache: dict | None = None


def _load_ai_cn() -> dict:
    global _ai_cn_cache
    if _ai_cn_cache is not None:
        return _ai_cn_cache
    data: dict = {}
    try:
        if _FIELD_CN_AI_PATH.exists():
            with open(_FIELD_CN_AI_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {str(k).lower(): str(v) for k, v in raw.items() if v}
    except Exception:
        data = {}
    _ai_cn_cache = data
    return data


def _save_ai_cn(cache: dict) -> None:
    """写盘 + 同步内存缓存。

    缺陷修复（P0）：原实现只落盘，从不更新全局 `_ai_cn_cache`；
    而 `_load_ai_cn()` 在缓存非 None 时直接返回旧 dict，于是刚翻译过的字段
    下一次判定仍是"未命中 → todo"，**同一个字段每次问答都会重新调用一次大模型**
    （注释承诺的"同一个字段只花一次模型调用"完全失效，15s 级延迟与成本被无限放大）。
    """
    global _ai_cn_cache
    try:
        with open(_FIELD_CN_AI_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
        # 关键：回写内存缓存，让新条目在当前进程内立即可见
        _ai_cn_cache = dict(cache)
    except Exception:
        pass


def ai_fill_field_labels(pairs, timeout: float = 15.0, limit: int = 12) -> dict:
    """对词典未命中的字段，用当前 AI 模型补全短中文名。

    参数 pairs: 可迭代的 (table, field)（table 可为空）。
    返回 {f"{table}.{field}": 中文短名}（仅含成功翻译的项）。
    结果写入 field_cn_ai.json 复用——同一个字段只花一次模型调用。

    设计约束：任何异常都吞掉返回 {}，绝不阻塞 / 拖垮主问答链路。
    """
    cache = _load_ai_cn()
    todo: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tbl, fld in pairs or []:
        f = str(fld or "").strip()
        t = str(tbl or "").strip()
        if not f or f == "*" or not re.search(r"[A-Za-z]", f):
            continue
        k = f"{t}.{f}".lower()
        if k in cache or k in seen:
            continue
        if field_label(t, f):
            continue              # 词典/内置语义已能翻 → 不必再花模型调用
        seen.add(k)
        todo.append((t, f))
        if len(todo) >= max(1, int(limit)):
            break
    if not todo:
        return {}

    lines = [f"- {f}" + (f"（表 {t}）" if t else "") for t, f in todo]
    prompt = (
        "你是制造业/企业数据库字段翻译员。把下列英文字段名翻译成简洁的中文业务字段名"
        "（≤12 字，允许含 ID/编号/数量/金额/时间/状态/率 等词；看不懂的不要瞎猜，值留空字符串）。"
        "只输出一个 JSON 对象，键必须与给出的字段名完全一致，值是中文名；不要解释、不要 markdown。\n"
        "字段列表：\n" + "\n".join(lines))
    try:
        from langchain_core.messages import HumanMessage
        from agent.llm_service import _make_llm
        llm = _make_llm(temp=0.0, max_tokens=800, json_mode=True,
                        timeout=timeout, max_retries=0)
        resp = llm.invoke([HumanMessage(content=prompt)])
        txt = str(getattr(resp, "content", "") or "").strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
        mapping = json.loads(txt) if txt else {}
        if not isinstance(mapping, dict):
            mapping = {}
    except Exception:
        return {}

    wanted = {f"{t}.{f}".lower() for t, f in todo}
    with _ai_cn_lock:
        cache = dict(_load_ai_cn())      # 重新读，避免并发写互相覆盖
        for t, f in todo:
            cn = mapping.get(f)
            if cn is None:
                for mk, mv in mapping.items():   # 容忍模型带表名前缀/大小写差异
                    if str(mk).lower().endswith(f.lower()):
                        cn = mv
                        break
            cn = short_cn(str(cn or "").strip())
            if not cn or not has_chinese(cn) or cn.lower() == f.lower():
                continue
            cache[f"{t}.{f}".lower()] = cn
        _save_ai_cn(cache)
    return {k: v for k, v in cache.items() if k in wanted}


def enrich_lineage_labels(lineage: dict, use_ai: bool = True) -> dict:
    """给血缘结构里的每个结果列补 cn（短中文名），原地修改并返回。

    build_lineage 已经把词典/内置语义能翻的都填进 cn 了；这里只处理仍为空的
    列——调用一次 AI 批量补全（带缓存），失败就保持空串（前端退化为原始列名）。
    """
    try:
        cols = (lineage or {}).get("columns") or []
        need = []
        for c in cols:
            if not isinstance(c, dict) or c.get("cn"):
                continue
            f = str(c.get("field") or "")
            if not f or f == "*":
                continue
            if has_chinese(str(c.get("output") or "")):
                continue          # 结果列名本身已是中文（SQL 里已 AS 中文别名）
            need.append((str(c.get("table") or ""), f))
        if need and use_ai:
            filled = ai_fill_field_labels(need)
            for c in cols:
                if not isinstance(c, dict) or c.get("cn"):
                    continue
                k = f"{c.get('table') or ''}.{c.get('field') or ''}".lower()
                if filled.get(k):
                    c["cn"] = filled[k]
    except Exception:
        pass
    return lineage


def _cache_fs(key: tuple, value: str) -> str:
    if len(_fs_cache) >= _FS_MAX:
        _fs_cache.clear()
    _fs_cache[key] = value
    return value


# 常见英文片段 → 中文（直译兜底用）
_PART_CN = {
    "id": "ID", "name": "名称", "code": "编码", "status": "状态", "type": "类型",
    "qty": "数量", "amount": "金额", "date": "日期", "time": "时间", "desc": "描述",
    "count": "数量", "total": "合计", "avg": "平均", "max": "最大", "min": "最小",
    "rate": "率", "ratio": "比率", "price": "单价", "cost": "成本", "num": "编号",
    "defect": "缺陷", "fault": "故障", "yield": "良率", "order": "工单/订单",
    "record": "记录", "process": "工序/流程", "output": "产出", "input": "投入",
    "inspection": "检验", "quality": "质量", "equipment": "设备", "downtime": "停机",
    "warehouse": "仓库", "stock": "库存", "inventory": "库存", "material": "物料",
    "employee": "员工", "attendance": "考勤", "leave": "请假", "product": "产品",
    "supplier": "供应商", "customer": "客户", "purchase": "采购", "sales": "销售",
    "finance": "财务", "project": "项目", "task": "任务",
    "quantity": "数量", "number": "编号", "serial": "序号", "remark": "备注",
    "comment": "备注", "reason": "原因", "category": "类别", "level": "等级",
    "flag": "标志", "grade": "等级", "state": "状态", "result": "结果",
    "start": "开始", "end": "结束", "begin": "开始", "finish": "完成",
    "plan": "计划", "planned": "计划", "actual": "实际", "safety": "安全", "available": "可用",
    "scheduled": "排程", "estimated": "预估", "expected": "预期", "target": "目标",
    "snapshot": "快照", "daily": "每日", "monthly": "每月", "weekly": "每周",
    "line": "产线/行", "machine": "机器", "workshop": "车间", "shift": "班次",
    "batch": "批次", "sample": "抽样", "fail": "不合格", "good": "合格",
    "pass": "通过", "qualified": "合格", "unqualified": "不合格", "defective": "不良",
    "normal": "正常", "abnormal": "异常", "department": "部门", "org": "组织",
    "gender": "性别", "age": "年龄", "phone": "电话", "email": "邮箱",
    "address": "地址", "city": "城市", "country": "国家", "currency": "货币",
    "payment": "付款", "delivery": "交货/配送", "shipment": "发货", "transport": "运输",
}


def _usage_for(field_name: str) -> str:
    """按字段名特征生成「用途」说明（详细解释的一部分）"""
    f = field_name.lower()
    if f.endswith("_id") or f.endswith("_key"):
        return "用于关联其他业务表的主键，是表间 JOIN 的关键字段，可按其筛选与分组统计。"
    if any(k in f for k in ("status", "state", "flag", "result", "type", "category", "level", "grade", "reason", "mode")):
        return "用于标识业务对象的分类或状态，可按此字段筛选、分组统计与异常预警。"
    if any(k in f for k in ("qty", "count", "num", "quantity", "amount", "price", "cost", "value", "duration")):
        return "用于度量业务规模、金额或时长，可做求和、均值、趋势与占比分析。"
    if any(k in f for k in ("rate", "ratio", "yield", "percent", "ppm", "score")):
        return "用于衡量业务比率或质量水平，是核心 KPI，可做环比、同比与目标对比分析。"
    if any(k in f for k in ("date", "time", "day", "month", "year", "at")):
        return "记录业务发生或归属的时间点，可按日/周/月聚合做趋势与周期分析。"
    if any(k in f for k in ("name", "desc", "comment", "remark", "title", "label")):
        return "记录业务对象的名称或描述信息，用于识别、搜索与展示。"
    if f in ("created_at", "created_by", "updated_at", "updated_by", "version", "is_deleted"):
        return "系统审计字段：记录数据的创建/更新时间、操作人或版本，用于数据管理与追溯。"
    return "记录业务过程信息，可按此字段进行查询与统计分析。"


@functools.lru_cache(maxsize=4096)
def explain_field_detailed(table_name: str, field_name: str, col_type: str = "", comment: str = "") -> str:
    """生成字段的详细解释：业务含义 + 用途 + 所属表 + 类型。

    用于前端「字段总览/表卡片」展示，让非专业人员也能看懂每个字段。
    （LLM prompt 注入仍用 explain_field 短语义，避免上下文膨胀）
    """
    base = explain_field(table_name, field_name, comment)
    usage = _usage_for(field_name)
    return f"{base}。用途：{usage}所属表：{table_name}；类型：{col_type or '未知'}。"
