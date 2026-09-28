"""ML 建模引擎 — 分类 / 回归 / 聚类 / 异常检测"""

import json, io, logging
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor, IsolationForest
from sklearn.cluster import KMeans
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, accuracy_score, f1_score, silhouette_score
from sklearn.preprocessing import LabelEncoder, StandardScaler
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

from db.executor import execute_sql

_zh_font = None
_MODEL_STORE = {}  # 内存模型存储: {name: {model, encoder, scaler, ...}}

def _get_zh_font():
    global _zh_font
    if _zh_font is not None: return _zh_font
    for p in ["C:/Windows/Fonts/msyh.ttc","C:/Windows/Fonts/simhei.ttf"]:
        import os
        if os.path.exists(p): _zh_font = FontProperties(fname=p); return _zh_font
    _zh_font = FontProperties(); return _zh_font

MODEL_TYPES = {
    "linear":       {"name": "线性回归",    "type": "regression"},
    "decision_tree": {"name": "决策树",      "type": "both"},
    "random_forest": {"name": "随机森林",    "type": "both"},
    "logistic":     {"name": "逻辑回归",    "type": "classification"},
    "kmeans":       {"name": "KMeans聚类",  "type": "clustering"},
    "isolation":    {"name": "孤立森林",     "type": "anomaly"},
}

# ML 训练参数白名单 + 取值范围钳制（对齐 ml/executor 的安全约束，防止恶意参数打爆资源）
_ALLOWED_TRAIN_PARAMS = {
    "n_clusters": (1, 100),        # KMeans 聚类数：1~100
    "contamination": (0.01, 0.5),  # IsolationForest 异常比例：(0, 0.5]
}

def _clamp_train_params(params: dict | None) -> dict:
    """只透传白名单内参数，并对数值做 min/max 钳制（未知/非法参数丢弃，用默认值）"""
    if not params:
        return {}
    cleaned = {}
    for key, (lo, hi) in _ALLOWED_TRAIN_PARAMS.items():
        if key not in params:
            continue
        try:
            val = float(params[key])
        except (ValueError, TypeError):
            continue  # 非数值直接丢弃
        cleaned[key] = max(lo, min(hi, val))
    return cleaned


def _split_table_ref(table_name: str) -> str:
    """把 'schema.table' 拆分为带引号的 schema.table 引用（不带 schema 时直接引用表名）"""
    from database import quote_ident
    if "." in table_name:
        schema, tbl = table_name.split(".", 1)
        return f"{quote_ident(schema)}.{quote_ident(tbl)}"
    return quote_ident(table_name)


def _load_data(table_name: str, columns: list[str], limit: int = 5000) -> pd.DataFrame:
    from database import quote_ident
    cols = ", ".join(quote_ident(c) for c in columns)
    result = execute_sql(f"SELECT {cols} FROM {_split_table_ref(table_name)} LIMIT {limit}")
    if not result["success"] or not result["rows"]:
        raise ValueError(f"数据加载失败: {result.get('error','')}")
    return pd.DataFrame(result["rows"])


def _load_data_joined(base_table: str, features: list[str], target: str,
                      spec: dict, limit: int = 5000) -> pd.DataFrame:
    """跨表取数：基表按公共主键 JOIN 汇总表，对目标列做聚合（SUM）。

    为什么需要它：工单表里只有计划量，实际产出记在工序产量表里（一行一道工序）。
    「用工单表预测实际产量」单表无解，正确的做法是先按工单号把产出汇总回来再建模。
    注意 GROUP BY 只按特征分组、目标列走聚合 —— 特征必须是基表的分组列。
    """
    from database import quote_ident
    other = spec.get("table") or ""
    col = spec.get("col") or target
    agg = str(spec.get("agg") or "SUM").upper()
    key = spec.get("on") or ""
    if agg not in ("SUM", "AVG", "MAX", "MIN", "COUNT"):
        raise ValueError(f"不支持的跨表聚合方式: {agg}")
    if not (other and col and key) or not features:
        raise ValueError("跨表取数参数不完整（需要 表/列/连接键/特征）")

    sel = [f"b.{quote_ident(c)} AS {quote_ident(c)}" for c in features]
    sel.append(f"COALESCE({agg}(o.{quote_ident(col)}), 0) AS {quote_ident(target)}")
    # 分组必须带上连接键：只按特征分组会把"计划量相同的不同工单"合成一行
    # （344 张工单会被压成几十个计划量取值，样本数和结论全错）
    group = ", ".join([f"b.{quote_ident(key)}"] + [f"b.{quote_ident(c)}" for c in features])
    sql = (f"SELECT {', '.join(sel)} FROM {_split_table_ref(base_table)} AS b "
           f"JOIN {_split_table_ref(other)} AS o "
           f"ON b.{quote_ident(key)} = o.{quote_ident(key)} "
           f"GROUP BY {group} LIMIT {limit}")
    result = execute_sql(sql)
    if not result["success"] or not result["rows"]:
        raise ValueError(f"跨表取数失败: {result.get('error', '')}")
    _audit_train("load_joined", {"base": base_table, "other": other, "on": key,
                                 "value_col": col, "agg": agg, "rows": len(result["rows"])})
    return pd.DataFrame(result["rows"])


# 敏感列自动剥离 + 训练审计（对齐 ml/executor 的安全约束）
_SENSITIVE_COLUMNS = {"password", "secret", "token", "key", "phone", "email",
                      "id_card", "ssn", "address", "ip_address"}
_TRAIN_AUDIT: list[dict] = []

def _audit_train(event: str, detail: dict):
    from datetime import datetime as _dt
    _TRAIN_AUDIT.append({"timestamp": _dt.now().isoformat(), "event": event, "detail": detail})
    if len(_TRAIN_AUDIT) > 1000:  # 审计日志限长，防止无限增长
        del _TRAIN_AUDIT[:len(_TRAIN_AUDIT) - 1000]
    logging.getLogger("ml_trainer").info(f"[TRAIN-AUDIT] {event}: {json.dumps(detail, ensure_ascii=False, default=str)}")


def train_model(table: str, target: str, features: list[str], model_type: str,
                params: dict = None, join_spec: dict = None) -> dict:
    """训练模型,返回指标和可视化

    join_spec: 目标列不在 table 里时，按公共主键从另一张表汇总（见 _load_data_joined）。
    """
    params = _clamp_train_params(params)
    if model_type not in MODEL_TYPES:
        raise ValueError(f"不支持的模型类型 '{model_type}'，可用: {', '.join(MODEL_TYPES)}")
    info = MODEL_TYPES[model_type]
    task = info["type"]
    all_cols = features + [target] if target and task != "clustering" else features
    if join_spec:
        df = _load_data_joined(table, features, target, join_spec)
    else:
        df = _load_data(table, all_cols)
    df = df.dropna()
    # 脱敏：剥离敏感列（对齐 ml/executor）；目标/特征列涉敏则直接拒绝训练
    dropped = [c for c in df.columns if any(s in c.lower() for s in _SENSITIVE_COLUMNS)]
    if dropped:
        missing = [c for c in (features + ([target] if target else [])) if c in dropped]
        if missing:
            raise ValueError(f"目标/特征列涉及敏感字段，禁止训练: {missing}")
        df = df.drop(columns=dropped)
        _audit_train("strip_sensitive", {"dropped_cols": dropped})
    if len(df) < 10:
        raise ValueError(f"有效数据不足({len(df)}行)，至少需要10行")
    _audit_train("train", {"table": table, "model_type": model_type, "samples": len(df)})

    X = df[features].copy()
    le_target = None
    scaler = StandardScaler()

    # 编码分类/日期特征（任何非数值列统一转字符串再编码）
    encoders = {}
    for col in X.columns:
        if not pd.api.types.is_numeric_dtype(X[col]):
            le = LabelEncoder()
            X[col] = le.fit_transform(X[col].astype(str))
            encoders[col] = le
    X_scaled = scaler.fit_transform(X)

    y = None
    if target and task != "clustering":
        y_raw = df[target]
        if task == "classification" or (task == "both" and not pd.api.types.is_numeric_dtype(y_raw)):
            le_target = LabelEncoder()
            y = le_target.fit_transform(y_raw.astype(str))
        else:
            y = y_raw.values

    # 选模型
    if model_type == "linear":
        m = LinearRegression()
        X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
        m.fit(X_train, y_train)
        y_pred = m.predict(X_test)
        metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "decision_tree":
        is_clf = task == "classification" or (le_target is not None)
        if is_clf:
            m = DecisionTreeClassifier(max_depth=5, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
        else:
            m = DecisionTreeRegressor(max_depth=5, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "random_forest":
        is_clf = task == "classification" or (le_target is not None)
        if is_clf:
            m = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
        else:
            m = RandomForestRegressor(n_estimators=100, max_depth=8, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "logistic":
        m = LogisticRegression(max_iter=1000, random_state=42)
        X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
        m.fit(X_train, y_train)
        y_pred = m.predict(X_test)
        metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}

    elif model_type == "kmeans":
        k = params.get("n_clusters", 3) if params else 3
        k = max(1, min(int(k), len(X_scaled) - 1))  # 聚类数须 < 样本数（silhouette_score 要求），且避免过大
        m = KMeans(n_clusters=k, random_state=42, n_init=10)
        m.fit(X_scaled)
        y_pred = m.labels_
        n_labels = len(set(y_pred))
        # silhouette_score 要求标签数在 2 ~ n_samples-1 之间，否则返回 -1
        sil = silhouette_score(X_scaled, y_pred) if 1 < n_labels < len(y_pred) else -1
        metrics = {"轮廓系数": round(sil, 4), "聚类数": k}
        le_target = None  # 无监督，不存

    elif model_type == "isolation":
        contam = params.get("contamination", 0.1) if params else 0.1
        m = IsolationForest(contamination=contam, random_state=42)
        m.fit(X_scaled)
        y_pred = m.predict(X_scaled)  # 1=正常, -1=异常
        n_outliers = int((y_pred == -1).sum())
        metrics = {"异常比例": round(n_outliers / len(y_pred), 4), "异常数": int(n_outliers), "总样本": len(y_pred)}
        le_target = None

    else:
        raise ValueError(f"未知模型类型: {model_type}")

    # 特征重要性
    importance = {}
    if hasattr(m, "feature_importances_"):
        importance = {features[i]: round(m.feature_importances_[i], 4) for i in range(len(features))}
    elif hasattr(m, "coef_"):
        coef = m.coef_[0] if m.coef_.ndim > 1 else m.coef_
        importance = {features[i]: round(coef[i], 4) for i in range(len(features))}

    # 保存模型（key 按库隔离，切库后旧库模型不可见）
    name = f"{model_type}_{table}_{target or 'nosup'}"
    _MODEL_STORE[_store_key(name)] = {
        "model": m, "encoders": encoders, "scaler": scaler,
        "le_target": le_target, "features": features, "model_type": model_type,
        "task": task,
    }

    # 可视化
    charts = {}
    charts["importance"] = _plot_importance(importance, info["name"])
    if model_type not in ("kmeans", "isolation") and y_test is not None and y_pred is not None:
        charts["pred_vs_actual"] = _plot_pred_vs_actual(y_test[:50], y_pred[:50])

    return {
        "model_name": name,
        "model_type": model_type,
        "model_label": info["name"],
        "metrics": metrics,
        "importance": importance,
        "features": features,
        "target": target,
        "samples": len(df),
        "charts": charts,
        # 跨表取数说明（单表训练为空串）：结果卡上原样展示，方便演示时讲清数据从哪来
        "join_note": (f"跨表取数：{table} ⋈ {join_spec.get('table')}"
                      f"（按 {join_spec.get('on')} 汇总 {join_spec.get('col')}）"
                      if join_spec else ""),
    }

def _ident_ok(name: str) -> bool:
    """表名/列名白名单校验（拼进 SQL 前的最后一道，和 llm_service._get_db_columns 同口径）"""
    import re
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))


def is_unique_column(table: str, column: str, limit: int = 5000) -> bool:
    """该列在表里是否**唯一**（能否当"一对多"的主体侧）。

    跨表汇总目标列时用它把关：工单表的 work_order_id 唯一 → 工单是主体、工序产量是明细，
    按工单号汇总产出成立；设备停机记录的 line_id 不唯一 → 拿它去汇总产量就是硬凑，
    宁可不给方案也不要给一个语义站不住的模型。取不到信息时返回 False（保守拒绝）。
    注意 limit 上限 5000 是执行器沙箱的硬约束（>5000 直接被 block）。
    """
    from database import quote_ident
    col_name = (column or "").split(".")[-1]
    if not _ident_ok(table) or not _ident_ok(col_name):
        return False
    try:
        col = quote_ident(col_name)
        sql = (f"SELECT COUNT(*) AS n, COUNT(DISTINCT {col}) AS d FROM "
               f"(SELECT {col} FROM {_split_table_ref(table)} LIMIT {limit}) t")
        r = execute_sql(sql)
        if not r["success"] or not r["rows"]:
            return False
        row = r["rows"][0]
        n, d = int(row.get("n") or 0), int(row.get("d") or 0)
        return n > 0 and n == d
    except Exception:
        return False


def _model_db_key() -> str:
    try:
        from database import get_database_config
        c = get_database_config()
        return f"{c.get('db_type')}:{c.get('host')}:{c.get('port')}:{c.get('name')}"
    except Exception:
        return "default"


def _store_key(name: str) -> str:
    return f"{_model_db_key()}|{name}"


def predict(model_name: str, data: dict) -> dict:
    """单条推理"""
    store = _MODEL_STORE.get(_store_key(model_name))
    if not store:
        raise ValueError(f"模型 {model_name} 不存在，请先训练")
    m = store["model"]
    encoders = store["encoders"]
    scaler = store["scaler"]
    le_target = store["le_target"]
    features = store["features"]

    X = []
    for f in features:
        val = data.get(f, 0)
        if f in encoders:
            try:
                val = encoders[f].transform([str(val)])[0]
            except ValueError:
                # 训练时未出现的类别：无法编码，回退到该列最常见的类别（而非静默当作 0 类）
                le = encoders[f]
                val = le.transform([le.classes_[0]])[0]
        try:
            X.append(float(val) if val is not None else 0)
        except (ValueError, TypeError):
            raise ValueError(f"预测输入字段 '{f}' 的值无法转为数值: {val!r}")
    X_scaled = scaler.transform([X])

    model_type = store["model_type"]
    if model_type == "kmeans":
        pred = int(m.predict(X_scaled)[0])
        return {"prediction": pred, "label": f"簇 {pred}"}
    elif model_type == "isolation":
        pred = int(m.predict(X_scaled)[0])
        return {"prediction": pred, "label": "正常" if pred == 1 else "异常"}
    elif model_type in ("linear",):
        pred = float(m.predict(X_scaled)[0])
        return {"prediction": round(pred, 4), "label": str(round(pred, 4))}
    else:
        y_pred = m.predict(X_scaled)
        if le_target:
            label = le_target.inverse_transform([int(y_pred[0])])[0]
            return {"prediction": int(y_pred[0]), "label": str(label)}
        return {"prediction": float(y_pred[0]), "label": str(round(float(y_pred[0]), 4))}

def _plot_importance(importance: dict, title: str) -> str:
    if not importance: return ""
    font = _get_zh_font()
    items = sorted(importance.items(), key=lambda x: abs(x[1]))
    labels = [i[0] for i in items]
    vals = [abs(i[1]) for i in items]
    fig, ax = plt.subplots(figsize=(5, max(2.5, len(labels)*0.35)))
    colors = ["#ef4444" if importance[l] < 0 else "#3b82f6" for l in labels]
    ax.barh(range(len(labels)), vals, color=colors)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontproperties=font, fontsize=9)
    ax.set_title(f"{title} — 特征重要性", fontproperties=font, fontsize=11)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    buf = io.BytesIO(); fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True); plt.close(fig)
    buf.seek(0); svg = buf.read().decode()
    return svg[svg.index("<svg"):] if svg.startswith("<?xml") else svg

def _plot_pred_vs_actual(y_true, y_pred) -> str:
    font = _get_zh_font()
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.scatter(range(len(y_true)), y_true, c="#3b82f6", s=20, alpha=0.7, label="真实值")
    ax.scatter(range(len(y_pred)), y_pred, c="#ef4444", s=20, alpha=0.7, label="预测值")
    ax.set_title("预测 vs 真实 (前50条)", fontproperties=font, fontsize=11)
    ax.legend(prop=font)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False); ax.grid(alpha=0.3)
    buf = io.BytesIO(); fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True); plt.close(fig)
    buf.seek(0); svg = buf.read().decode()
    return svg[svg.index("<svg"):] if svg.startswith("<?xml") else svg

def list_trained_models() -> list[dict]:
    # 只列出当前库的模型（key 带 db_key 前缀，切库后自动隔离）
    prefix = _model_db_key() + "|"
    return [{"name": k[len(prefix):], "type": v["model_type"], "features": v["features"]}
            for k, v in _MODEL_STORE.items() if k.startswith(prefix)]

def get_numeric_tables() -> list[dict]:
    """返回可建模的数值表列表（行数用估算值，避免逐表 COUNT 串行扫描）。
    PG 遍历 search_path 内全部 schema（含业务 schema，如 123 库的 factory.*），非 public 表带 schema 前缀。"""
    tables = []
    try:
        from database import get_db_type
        is_mysql = get_db_type() == "mysql"
        schema_clause = "table_schema=DATABASE()" if is_mysql else "table_schema = ANY (current_schemas(false))"
    except Exception:
        is_mysql = False
        schema_clause = "table_schema = ANY (current_schemas(false))"
    # 一次查询所有表行数（估算）
    est_rows = {}
    try:
        if is_mysql:
            r0 = execute_sql(
                "SELECT table_name, table_rows FROM information_schema.tables "
                "WHERE table_schema=DATABASE()"
            )
        else:
            r0 = execute_sql(
                "SELECT n.nspname AS schema_name, c.relname AS table_name, c.reltuples::bigint AS row_count "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = ANY (current_schemas(false)) AND c.relkind='r'"
            )
        if r0["success"]:
            for row in r0["rows"]:
                tn = row["table_name"]
                if not is_mysql:
                    sch = row.get("schema_name") or "public"
                    if sch != "public":
                        tn = f"{sch}.{tn}"
                # reltuples 对未 ANALYZE 的表为 -1（未知），按 0 处理
                est_rows[tn] = max(0, int(row.get("row_count") or row.get("table_rows") or 0))
    except Exception:
        pass
    # 一次查询所有表的列（参数化缺失场景：不再对表名做字符串插值）
    NUM_TYPES = ("integer", "bigint", "numeric", "real", "double precision",
                 "smallint", "int", "float", "decimal")
    cols_by_table: dict[str, list[tuple]] = {}
    try:
        if is_mysql:
            rc = execute_sql(
                f"SELECT table_name, column_name, data_type FROM information_schema.columns "
                f"WHERE {schema_clause} ORDER BY table_name, ordinal_position"
            )
        else:
            rc = execute_sql(
                f"SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                f"WHERE {schema_clause} ORDER BY table_schema, table_name, ordinal_position"
            )
        if rc["success"]:
            for row in rc["rows"]:
                tn = row["table_name"]
                if not is_mysql:
                    sch = row.get("table_schema") or "public"
                    if sch != "public":
                        tn = f"{sch}.{tn}"
                cols_by_table.setdefault(tn, []).append((row["column_name"], row["data_type"]))
    except Exception:
        pass
    for tn, col_list in cols_by_table.items():
        numeric = [c for c, dt in col_list if dt in NUM_TYPES]
        if len(numeric) >= 2 and len(col_list) >= 3:
            tables.append({"table": tn, "columns": [c for c, _ in col_list],
                           "numeric_columns": numeric, "rows": est_rows.get(tn, 0)})
    return tables
