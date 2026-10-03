"""权限模型存储（第六层语义层权限）— auth_permissions.json v2

模型结构（单文件、可版本化、可 diff）：
{
  "version": 2,
  "roles":    [{key, name, description, priority, system}],
  "datasets": [{key, name, tables[], description, sensitive}],
  "policies": {
      "<role_key>": {
          "datasets": ["sales"],                 # 数据集级授权
          "tables":   ["extra_table"],           # 额外单表授权（不属于任何数据集时用）
          "rows":     {"test_orders": {"expr": "factory_id = ${user.factory_id}", "enabled": true}},
          "columns":  {"test_orders": {"customer_name": {"mode": "mask", "mask": "partial_1_1"}}},
          "metrics":  {"产量": {"mode": "override", "sql_expression": "...", "formula": "...", "description": "..."}}
      }
  },
  "sensitive": {"columns": ["t.c"], "metrics": ["销售金额"], "datasets": ["sales"]},
  "updated_at": "...", "updated_by": "..."
}

兼容性：
- v1 旧格式（{role: {tables, row_filters, column_whitelist}}）在首次加载时自动迁移，
  原文件备份为 auth_permissions.v1.bak.json，viewer 等既有配置完整保留（零回归）。
- 权限**未配置** = 不限制（沿用旧语义）；显式配置才收紧。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_PERM_FILE = _BASE / "auth_permissions.json"
_BACKUP_FILE = _BASE / "auth_permissions.v1.bak.json"

_lock = threading.RLock()
_cache: dict | None = None

MODEL_VERSION = 2

# 系统内置角色（不可删除；admin 恒为超级权限，不受任何策略约束）
SYSTEM_ROLES = [
    {"key": "admin", "name": "超级管理员", "description": "系统最高权限，不受任何数据权限约束",
     "priority": 100, "system": True},
    {"key": "viewer", "name": "普通员工", "description": "只读查询，受数据权限约束",
     "priority": 20, "system": True},
]

# 列级权限模式
COLUMN_MODES = ("visible", "mask", "deny")
# 指标级权限模式
METRIC_MODES = ("allow", "override", "deny")
# 操作级权限（对标 FineBI 六类 / 观远「角色-数据-操作」三维；ChatBI 场景收敛为最核心三类）
#   export   导出查询结果 / 数据总览报告 / HTML 报告
#   share    分享图表 / 结果给他人
#   download 下载原始数据文件
ACTION_PERMS = ("export", "share", "download")


# ── 角色模板（权限页「新建角色」一键套用）──────────────────
# 模板 policies = 角色策略节（datasets/tables/rows/columns/metrics），
# 行级用声明式 rule（也可 expr 兼容），套用时经 _normalize_policy 规整。
ROLE_TEMPLATES: dict[str, dict] = {}



# ── 种子模型（首次运行 / 空文件时写入，贴合当前 MES 演示库表名）──

def _seed_model() -> dict:
    return {
        "version": MODEL_VERSION,
        "roles": [dict(r) for r in SYSTEM_ROLES] + [
            {"key": "region_manager", "name": "区域销售经理",
             "description": "只看本区域订单与工厂；客户名称脱敏；销售口径按合同总额",
             "priority": 50, "system": False},
        ],
        "datasets": [
            {"key": "prod_core", "name": "生产制造域",
             "tables": ["mes_process_output", "mes_work_order", "dim_product",
                        "dim_process", "dim_production_line"],
             "description": "MES 生产事实表与生产主数据", "sensitive": False},
            {"key": "quality", "name": "质量域",
             "tables": ["qms_inspection", "qms_defect_detail"],
             "description": "检验与不良明细", "sensitive": False},
            {"key": "equipment", "name": "设备域",
             "tables": ["dim_equipment", "eqp_downtime_record"],
             "description": "设备主数据与停机记录", "sensitive": False},
            {"key": "inventory", "name": "库存域",
             "tables": ["inv_inventory_snapshot", "test_materials"],
             "description": "库存快照与物料（含成本价，敏感）", "sensitive": True},
            {"key": "sales", "name": "销售域",
             "tables": ["test_orders", "test_factories"],
             "description": "订单与工厂（含客户、单价，敏感）", "sensitive": True},
        ],
        "policies": {
            "viewer": {
                # 普通员工默认不预授权任何表/字段：由管理员通过「员工数据授权」逐人配置，
                # 未授权即默认空（不展示任何表/字段，符合数据展示需求 #2）。
                "datasets": [],
                "tables": [],
                "rows": {},
                "columns": {
                    "dim_production_line": {"supervisor": {"mode": "mask", "mask": "partial_1_1",
                                                           "note": "负责人姓名脱敏"}},
                    "test_materials": {"unit_cost": {"mode": "deny", "note": "成本价对普通员工不可见"}},
                },
                "metrics": {},
            },
            "region_manager": {
                "datasets": ["sales", "prod_core"],
                "tables": [],
                "rows": {
                    "test_factories": {"expr": "city = ${user.region}", "enabled": True,
                                       "note": "只看本人所属区域的工厂"},
                    "test_orders": {
                        "expr": "factory_id IN (SELECT factory_id FROM test_factories WHERE city = ${user.region})",
                        "enabled": True, "note": "只看本区域工厂的订单"},
                },
                "columns": {
                    "test_orders": {
                        "customer_name": {"mode": "mask", "mask": "partial_1_1", "note": "客户名称脱敏"},
                    },
                },
                "metrics": {
                    "产量": {"mode": "override",
                             "sql_expression": "SUM(good_qty + defect_qty)",
                             "formula": "合格产出 + 不良产出（总产出口径）",
                             "description": "区域销售口径：产量按总产出计（含不良），用于产能承诺",
                             "note": "销售视角口径"},
                },
            },
        },
        "sensitive": {
            "columns": ["test_materials.unit_cost", "test_orders.unit_price",
                        "test_orders.customer_name", "dim_production_line.supervisor"],
            "metrics": ["产量"],
            "datasets": ["sales", "inventory"],
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system(seed)",
    }


def _empty_policy() -> dict:
    return {"datasets": [], "tables": [], "rows": {}, "columns": {}, "metrics": {}, "actions": {}}


def coerce_mask_key(value) -> str:
    """把列脱敏取值规整成算法 key（兼容历史脏数据，fail-loud 不静默）。

    历史上有三种落库形态，都视为合法输入：
      1) 裸 key：        "partial_1_1"
      2) 字典对象：      {"key": "partial_1_1", "label": "...", "example": "..."}
      3) 字典的字符串：  "{'key': 'partial_1_1', ...}"（下拉选项被整体序列化写入所致）

    前两种可直接取 key；第三种尝试按 JSON / Python repr 解析后取 key。
    实在解析不出、也不是 key 形态时，回退 "fixed"（enforcer 侧仍会兜底）。
    """
    if isinstance(value, dict):
        key = value.get("key")
        return str(key).strip() if key else "fixed"
    s = str(value if value is not None else "").strip()
    if s.startswith("{") and s.endswith("}"):
        parsed = None
        try:
            parsed = json.loads(s)                  # 双引号 JSON
        except (ValueError, TypeError):
            try:
                import ast
                parsed = ast.literal_eval(s)        # Python repr（单引号）
            except (ValueError, TypeError, SyntaxError):
                parsed = None
        if isinstance(parsed, dict):
            key = parsed.get("key")
            if key:
                return str(key).strip()
    return s or "fixed"


def _normalize_policy(raw) -> dict:
    """把任意历史/残缺形态的策略规整为 v2 policy 结构"""
    pol = _empty_policy()
    if isinstance(raw, list):  # 最早的格式：role -> [表名...]
        pol["tables"] = [str(t).split(".")[-1].strip().lower() for t in raw if t]
        return pol
    if not isinstance(raw, dict):
        return pol
    pol["datasets"] = [str(d) for d in (raw.get("datasets") or []) if d]
    pol["tables"] = [str(t).split(".")[-1].strip().lower() for t in (raw.get("tables") or []) if t]

    # 行策略：v2 rows 优先，其次 v1 row_filters；rule（声明式）与 expr 双轨兼容
    rows = raw.get("rows")
    if isinstance(rows, dict):
        for tbl, cfg in rows.items():
            key = str(tbl).split(".")[-1].strip().lower()
            if isinstance(cfg, str):
                cfg = {"expr": cfg, "enabled": True}
            if not isinstance(cfg, dict):
                continue
            expr = str(cfg.get("expr") or "").strip()
            rule = cfg.get("rule")
            if not expr and not (isinstance(rule, dict) and rule):
                continue  # 无 expr 也无 rule → 视为无效配置跳过
            item = {"expr": expr,
                    "enabled": bool(cfg.get("enabled", True)),
                    "note": str(cfg.get("note") or "")}
            if isinstance(rule, dict) and rule:
                item["rule"] = rule
            pol["rows"][key] = item
    for tbl, expr in (raw.get("row_filters") or {}).items():
        key = str(tbl).split(".")[-1].strip().lower()
        if expr and key not in pol["rows"]:
            pol["rows"][key] = {"expr": str(expr).strip(), "enabled": True, "note": "迁移自 v1 row_filters"}

    # 列策略：v2 columns 优先；v1 column_whitelist（白名单）转成「表内其余列 deny」需要真实列，
    # 这里保留白名单原语义存到 _whitelist，enforcer 一并处理（不丢配置、不误开权限）。
    cols = raw.get("columns")
    if isinstance(cols, dict):
        for tbl, cfg in cols.items():
            key = str(tbl).split(".")[-1].strip().lower()
            if not isinstance(cfg, dict):
                continue
            tbl_cfg: dict = {}
            for col, rule in cfg.items():
                if isinstance(rule, str):
                    rule = {"mode": rule}
                if not isinstance(rule, dict):
                    continue
                # 2026-10-03 修复：原实现只lower() 没strip()，且任何无法识别的 mode
                # 一律回落成 visible —— 这是 fail-open：「deny 」（表单尾随空格）、
                # 「denied」「禁止」等拼写问题都会把「禁止访问」变成「明文可见」，
                # 保存成功、界面回显正常、引擎层却完全没拦，且无任何报错。
                # 现在：先strip()；未知值按 deny 处理（收紧方向，宁可多挡）。
                mode = str(rule.get("mode") or "visible").strip().lower()
                if mode not in COLUMN_MODES:
                    mode = "deny"
                item = {"mode": mode, "note": str(rule.get("note") or "")}
                if mode == "mask":
                    item["mask"] = coerce_mask_key(rule.get("mask"))
                    item["agg_ok"] = bool(rule.get("agg_ok"))  # 聚合豁免：允许该列参与 SUM/AVG 等聚合
                tbl_cfg[str(col).strip()] = item
            if tbl_cfg:
                pol["columns"][key] = tbl_cfg
    wl = raw.get("column_whitelist") or {}
    if isinstance(wl, dict) and wl:
        pol["_whitelist"] = {str(k).split(".")[-1].strip().lower(): [str(c) for c in v]
                             for k, v in wl.items() if v}

    # 指标策略
    mets = raw.get("metrics")
    if isinstance(mets, dict):
        for name, rule in mets.items():
            if isinstance(rule, str):
                rule = {"mode": rule}
            if not isinstance(rule, dict):
                continue
            # 2026-10-03 修复：与上面columns 段同一个 bug，此前只修了列、漏了指标。
            # 原实现只lower() 没 strip()，且无法识别的 mode 一律回落成 allow——
            # 同样 是 fail-open：「deny 」（表单尾随空格）、「denied」会把「禁止访问」
            # 变成「完全放行」，引擎层不拦、界面回显正常、无任何报错。
            mode = str(rule.get("mode") or "allow").strip().lower()
            if mode not in METRIC_MODES:
                mode = "deny"
            item = {"mode": mode, "note": str(rule.get("note") or "")}
            if mode == "override":
                expr = str(rule.get("sql_expression") or "").strip()
                if not expr:
                    continue
                item["sql_expression"] = expr
                item["formula"] = str(rule.get("formula") or expr)
                item["description"] = str(rule.get("description") or "")
            pol["metrics"][str(name).strip()] = item

    # 操作级权限：{export: bool, share: bool, download: bool}，未配置 = 全部拒绝（fail-close）
    actions = raw.get("actions")
    if isinstance(actions, dict):
        for k in ACTION_PERMS:
            if k in actions:
                pol["actions"][k] = bool(actions[k])
    return pol


def _migrate(raw: dict) -> dict:
    """v1（role → 权限配置）→ v2（roles/datasets/policies/sensitive）"""
    model = _seed_model()
    # v1 里出现的角色若不在种子角色中，补一个自定义角色
    known = {r["key"] for r in model["roles"]}
    for role_key, cfg in raw.items():
        if role_key in ("version", "roles", "datasets", "policies", "sensitive",
                        "updated_at", "updated_by"):
            continue
        if role_key not in known:
            model["roles"].append({"key": role_key, "name": role_key,
                                   "description": "迁移自 v1 配置", "priority": 30, "system": False})
            known.add(role_key)
        # v1 配置优先级高于种子（保留用户既有设置）
        model["policies"][role_key] = _normalize_policy(cfg)
    model["updated_at"] = datetime.now(timezone.utc).isoformat()
    model["updated_by"] = "system(migrate v1→v2)"
    return model


def _write(model: dict) -> None:
    # 2026-10-03 修复：原来是裸 write_text（先 truncate 再写），写入途中崩溃会留下
    # 半截 JSON → 下次 load_model 读失败 → 又触发种子覆写 → 整套权限被永久抹掉。
    # 改用「同目录临时文件 + 原子替换」，与 database.py 的 _write_text_atomic 同一思路。
    tmp = _PERM_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _PERM_FILE)


def load_model(refresh: bool = False) -> dict:
    """加载权限模型（带内存缓存）；文件缺失/损坏 → 种子模型；v1 → 自动迁移并落盘备份"""
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return _cache
        raw = None
        # 2026-10-03 修复：原实现把「文件读不出来」（被占用/半截 JSON/磁盘错误）与
        # 「文件确实是空的」当同一件事 —— 任何一次读失败都会用 _seed_model() 覆写磁盘，
        # 管理员配的列 deny、行过滤、user_grants 全部消失，被替换成只含
        # admin/viewer/region_manager 的种子模型。丢 deny 意味着敏感列从「无权访问」
        # 变成「明文可见」，权限是往**放宽**方向静默滑坡的。
        # 现在：读失败时绝不覆写用户文件；有内存缓存就继续用旧模型，没有才退回种子
        # 且不落盘（并显式告警）。
        read_failed = False
        if _PERM_FILE.exists():
            try:
                raw = json.loads(_PERM_FILE.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = None
            except Exception as e:
                read_failed = True
                print(f"[perm-model] 读取 {_PERM_FILE} 失败（{e}）：沿用内存缓存，不覆写权限文件")
        if read_failed:
            model = _cache if _cache is not None else _seed_model()
            if _cache is not None:
                return _cache
            # 没有任何可用模型：返回种子供本次请求使用，但**不落盘**，
            # 避免把用户文件覆盖成种子
            _cache = model
            return model
        if not isinstance(raw, dict) or not raw:
            # 仅在「文件确实不存在/为空」时播种
            model = _seed_model()
            _write(model)
        elif int(raw.get("version") or 1) >= MODEL_VERSION and "policies" in raw:
            model = _ensure_shape(raw)
        else:
            try:
                if not _BACKUP_FILE.exists():
                    _BACKUP_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            model = _migrate(raw)
            _write(model)
        _cache = model
        return model


def _ensure_shape(model: dict) -> dict:
    """补齐缺失字段，规整每个角色策略（防手工编辑写坏文件导致 KeyError）"""
    model.setdefault("version", MODEL_VERSION)
    model.setdefault("roles", [dict(r) for r in SYSTEM_ROLES])
    model.setdefault("datasets", [])
    model.setdefault("user_grants", {})
    model.setdefault("strict_mode", False)  # 严格合并模式：列 deny>mask>visible、行 AND 收紧（默认关闭零回归）
    model.setdefault("sensitive", {"columns": [], "metrics": [], "datasets": []})
    for k in ("columns", "metrics", "datasets"):
        model["sensitive"].setdefault(k, [])
    # 系统角色必须存在
    have = {r.get("key") for r in model["roles"]}
    for r in SYSTEM_ROLES:
        if r["key"] not in have:
            model["roles"].append(dict(r))
    policies = model.get("policies") or {}
    model["policies"] = {str(k): _normalize_policy(v) for k, v in policies.items()}
    return model


def save_model(model: dict, actor: str = "") -> dict:
    """保存权限模型（写盘 + 缓存刷新 + 更新元信息）"""
    global _cache
    with _lock:
        model = _ensure_shape(model)
        model["version"] = MODEL_VERSION
        model["updated_at"] = datetime.now(timezone.utc).isoformat()
        if actor:
            model["updated_by"] = actor
        _write(model)
        _cache = model
        return model


# ── 读取辅助 ─────────────────────────────────────────────

def list_roles() -> list[dict]:
    return list(load_model().get("roles") or [])


def get_role(role_key: str) -> dict | None:
    for r in list_roles():
        if r.get("key") == role_key:
            return r
    return None


def list_datasets() -> list[dict]:
    return list(load_model().get("datasets") or [])


def get_policy(role_key: str) -> dict:
    """角色策略（缺省返回空策略 = 不限制）"""
    pol = (load_model().get("policies") or {}).get(role_key)
    return _normalize_policy(pol) if pol else _empty_policy()


def dataset_tables(keys: list[str]) -> set[str]:
    """数据集 key 列表 → 裸表名集合（小写）"""
    idx = {d.get("key"): d for d in list_datasets()}
    out: set[str] = set()
    for k in keys or []:
        for t in (idx.get(k, {}).get("tables") or []):
            out.add(str(t).split(".")[-1].strip().lower())
    return out


def sensitive_columns() -> set[str]:
    return {str(c).strip().lower() for c in (load_model().get("sensitive", {}).get("columns") or [])}


def sensitive_metrics() -> set[str]:
    return {str(m).strip() for m in (load_model().get("sensitive", {}).get("metrics") or [])}


def sensitive_datasets() -> set[str]:
    return {str(d).strip() for d in (load_model().get("sensitive", {}).get("datasets") or [])}


def is_strict_mode() -> bool:
    """严格合并模式开关：默认关闭（零回归）；开启后列权限 deny>mask>visible、行条件 AND 收紧"""
    return bool(load_model().get("strict_mode"))


def set_strict_mode(enabled: bool, actor: str = "") -> dict:
    model = json.loads(json.dumps(load_model()))
    model["strict_mode"] = bool(enabled)
    return save_model(model, actor)


def is_sensitive_change(kind: str, target: str) -> bool:
    """判断一次权限变更是否触及敏感对象（→ 需走审批流）

    kind: dataset | column | row | metric | role
    target: dataset_key / "table.column" / table / metric_name / role_key
    """
    t = str(target or "").strip().lower()
    if kind == "dataset":
        return t in {d.lower() for d in sensitive_datasets()}
    if kind == "column":
        return t in sensitive_columns()
    if kind == "metric":
        return str(target).strip() in sensitive_metrics()
    if kind == "row":
        # 行策略作用于表；表属于敏感数据集则视为敏感
        for d in list_datasets():
            if d.get("key") in sensitive_datasets():
                if t in {str(x).split(".")[-1].lower() for x in (d.get("tables") or [])}:
                    return True
        return False
    if kind == "role":
        return True  # 角色本身的授权变更一律视为敏感
    return False


# ── 写入辅助（供 API 调用；均返回新模型）──────────────────

def list_role_templates() -> list[dict]:
    """角色模板清单（供前端下拉；仅展示元信息，不含 policies 细节）"""
    return [{"key": k, "name": v.get("name", k), "description": v.get("description", ""),
             "priority": v.get("priority", 30)} for k, v in ROLE_TEMPLATES.items()]


def apply_role_template(role_key: str, template_key: str, actor: str = "") -> dict:
    """套用角色模板：生成/覆盖角色及其策略（角色定义变更，走审批流）。"""
    tpl = ROLE_TEMPLATES.get(str(template_key).strip())
    if not tpl:
        raise ValueError(f"角色模板不存在：{template_key}")
    key = str(role_key).strip()
    if not key:
        raise ValueError("角色 key 不能为空")
    model = json.loads(json.dumps(load_model()))
    roles = model.get("roles") or []
    found = False
    for r in roles:
        if r.get("key") == key:
            r["name"] = str(tpl.get("name") or key)
            r["description"] = str(tpl.get("description") or "")
            r["priority"] = int(tpl.get("priority") or 30)
            found = True
            break
    if not found:
        roles.append({"key": key, "name": str(tpl.get("name") or key),
                      "description": str(tpl.get("description") or ""),
                      "priority": int(tpl.get("priority") or 30), "system": False})
        model["roles"] = roles
    model.setdefault("policies", {})[key] = _normalize_policy(
        json.loads(json.dumps(tpl.get("policies") or {})))
    return save_model(model, actor)


def upsert_role(role: dict, actor: str = "") -> dict:
    model = json.loads(json.dumps(load_model()))
    key = str(role.get("key") or "").strip()
    if not key:
        raise ValueError("角色 key 不能为空")
    roles = model.get("roles") or []
    for i, r in enumerate(roles):
        if r.get("key") == key:
            if r.get("system") and role.get("priority") is not None and key == "admin":
                role["priority"] = 100  # admin 优先级固定
            roles[i] = {**r, **{k: v for k, v in role.items() if v is not None},
                        "system": bool(r.get("system"))}
            model["roles"] = roles
            return save_model(model, actor)
    roles.append({"key": key, "name": str(role.get("name") or key),
                  "description": str(role.get("description") or ""),
                  "priority": int(role.get("priority") or 30), "system": False})
    model["roles"] = roles
    model.setdefault("policies", {}).setdefault(key, _empty_policy())
    return save_model(model, actor)


def delete_role(role_key: str, actor: str = "") -> dict:
    model = json.loads(json.dumps(load_model()))
    target = get_role(role_key)
    if not target:
        raise ValueError(f"角色「{role_key}」不存在")
    if target.get("system"):
        raise ValueError(f"系统内置角色「{role_key}」不能删除")
    model["roles"] = [r for r in model.get("roles") or [] if r.get("key") != role_key]
    (model.get("policies") or {}).pop(role_key, None)
    return save_model(model, actor)


def upsert_dataset(ds: dict, actor: str = "") -> dict:
    model = json.loads(json.dumps(load_model()))
    key = str(ds.get("key") or "").strip()
    if not key:
        raise ValueError("数据集 key 不能为空")
    tables = [str(t).strip() for t in (ds.get("tables") or []) if str(t).strip()]
    entry = {"key": key, "name": str(ds.get("name") or key),
             "tables": tables, "description": str(ds.get("description") or ""),
             "sensitive": bool(ds.get("sensitive"))}
    datasets = model.get("datasets") or []
    for i, d in enumerate(datasets):
        if d.get("key") == key:
            datasets[i] = entry
            break
    else:
        datasets.append(entry)
    model["datasets"] = datasets
    # 敏感数据集标记同步
    sens = set(model["sensitive"].get("datasets") or [])
    sens.add(key) if entry["sensitive"] else sens.discard(key)
    model["sensitive"]["datasets"] = sorted(sens)
    return save_model(model, actor)


def delete_dataset(key: str, actor: str = "") -> dict:
    model = json.loads(json.dumps(load_model()))
    model["datasets"] = [d for d in model.get("datasets") or [] if d.get("key") != key]
    for pol in (model.get("policies") or {}).values():
        pol["datasets"] = [k for k in (pol.get("datasets") or []) if k != key]
    model["sensitive"]["datasets"] = [d for d in model["sensitive"].get("datasets") or [] if d != key]
    return save_model(model, actor)


def set_role_section(role_key: str, section: str, value, actor: str = "") -> dict:
    """整段替换角色策略的某一节：datasets / tables / rows / columns / metrics / actions"""
    if section not in ("datasets", "tables", "rows", "columns", "metrics", "actions"):
        raise ValueError(f"未知策略节：{section}")
    model = json.loads(json.dumps(load_model()))
    if not get_role(role_key):
        raise ValueError(f"角色「{role_key}」不存在")
    policies = model.setdefault("policies", {})
    pol = _normalize_policy(policies.get(role_key) or {})
    pol[section] = value
    policies[role_key] = _normalize_policy(pol)
    return save_model(model, actor)


def set_sensitive(kind: str, items: list[str], actor: str = "") -> dict:
    if kind not in ("columns", "metrics", "datasets"):
        raise ValueError(f"未知敏感对象类型：{kind}")
    model = json.loads(json.dumps(load_model()))
    model["sensitive"][kind] = sorted({str(i).strip() for i in items if str(i).strip()})
    return save_model(model, actor)


# ── 员工级数据授权（per-user 表/字段访问白名单）────────────
# 结构：{ "<username>": { "<table>": ["col1", "col2", ...] } }
# 与角色策略关系：per-user 授权是「员工可见表/字段」的权威来源；
# 一旦某员工存在 user_grants，其可见表 = 授权表集合，可见字段 = 授权字段集合
# （角色级的行/列脱敏/拒绝仍叠加生效，作为纵深防御）。
# 若某员工既无角色表授权、也无 user_grants → 视为「未授权」，引擎层默认空（不展示任何表）。

def get_user_grants(username: str) -> dict:
    """某员工的表→字段白名单：{table: [col, ...]}；无则返回空 dict。"""
    model = load_model()
    grants = (model.get("user_grants") or {}).get(str(username).strip())
    if not isinstance(grants, dict):
        return {}
    out: dict[str, list[str]] = {}
    for t, cols in grants.items():
        bare = str(t).split(".")[-1].strip().lower()
        if not bare:
            continue
        cl = [str(c).strip() for c in (cols or []) if str(c).strip()]
        out[bare] = cl
    return out


def set_user_grants(username: str, grants: dict, actor: str = "") -> dict:
    """整段替换某员工的表→字段白名单；空 dict 表示撤销授权（回到默认空/角色态）"""
    model = json.loads(json.dumps(load_model()))
    ug = model.setdefault("user_grants", {})
    clean_name = str(username).strip()
    norm: dict[str, list[str]] = {}
    for t, cols in (grants or {}).items():
        bare = str(t).split(".")[-1].strip().lower()
        if not bare:
            continue
        cl = [str(c).strip() for c in (cols or []) if str(c).strip()]
        norm[bare] = cl
    if norm:
        ug[clean_name] = norm
    else:
        ug.pop(clean_name, None)
    model["user_grants"] = ug
    return save_model(model, actor)


def list_user_grants() -> dict:
    """全部员工授权（用户名 → {table: [cols]}）"""
    model = load_model()
    raw = model.get("user_grants") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for u, g in raw.items():
        if isinstance(g, dict) and g:
            out[str(u)] = get_user_grants(str(u))
    return out
