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
    "n_clusters": (2, 100),        # KMeans 聚类数：2~100（k=1 无意义，轮廓系数恒 -1）
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
    # 2026-10-04 修复（真实用户可见错误，实测复现）：
    # features 为空时原实现直接往下走 → `_load_data(table, [target])` 拼出 `SELECT , FROM t`
    # 之类的坏 SQL，或让 sklearn拿到空数组后抛
    #   ValueError: at least one array or dtype is required
    # （sklearn/utils/validation.py:check_array）。该 ValueError 被 main.py 的
    # `except ValueError → HTTPException(400, detail=str(e))` 原样透出，
    # 于是管理员建模页传空特征 → 界面显示 sklearn 英文内部报错。
    # 此前从未暴露：TrainRequest.features 没有长度约束，且没人真的提交过空列表。
    if not features or not [c for c in features if str(c).strip()]:
        raise ValueError("请至少选择一个特征字段（features 不能为空）")
    if model_type not in MODEL_TYPES:
        raise ValueError(f"不支持的模型类型 '{model_type}'，可用: {', '.join(MODEL_TYPES)}")
    info = MODEL_TYPES[model_type]
    task = info["type"]
    all_cols = features + [target] if target and task != "clustering" else features
    if join_spec:
        df = _load_data_joined(table, features, target, join_spec)
    else:
        df = _load_data(table, all_cols)
    # 2026-10-01 修复（P1）：dropna 原先无任何提示——某列 NULL 率高时 5000 行可能掉到
    # 几十行，用户只看到"有效数据不足"，不知道是哪列导致。记录行数变化与缺失最多的列，
    # 通过 data_note 透出到结果卡。
    # 2026-10-03 修复（P0）：df.dropna() 无参数 =丢弃**任一列**含 NaN 的行。特征里只要
    # 有一列（如order_status 这类文本列）缺失率高，5000 行会被砍到几十行，且 len(df)<10
    # 的门槛拦不住"砍到 200 行"这种更糟的情况。改为按任务区分：有监督只补齐不删行，
    # 无监督才清理数值列。
    #
    # 追加修复（同一处的注释前提是错的）：
    #   原注释称「树模型/线性模型容忍 NaN」—— 实测（sklearn 1.9.0）**不成立**：
    #     LinearRegression    → ValueError: Input X contains NaN.
    #     LogisticRegression  → ValueError: Input X contains NaN.
    #     DecisionTree        → OK
    #   且**全 NaN 列**的 median() 返回 NaN，`fillna(NaN)` 是空操作 → NaN 原样进模型
    #   → 英文异常经 llm_service `"建模执行失败: {e}"` 原样糊到界面，而 data_note 里
    #   还写着「缺失值已按列中位数补齐」，自相矛盾。
    # 现在：全 NaN 列直接剔除；补齐后再校验，仍有 NaN 的列也剔除。
    rows_before = len(df)
    _na_counts = df.isna().sum()
    _num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if task in ("clustering", "anomaly"):
        # 无监督：KMeans 不接受 NaN，必须清理，且只清数值列（文本列后面要 LabelEncoder）
        df = df.dropna(subset=_num_cols) if _num_cols else df.dropna()
    else:
        # 有监督：树模型容忍 NaN，线性模型不容忍 → 统一用中位数补齐；
        # 全 NaN 列 median 仍是 NaN，必须剔除而非补齐。
        _allna_cols = [c for c in _num_cols if df[c].isna().all()]
        if _allna_cols:
            df = df.drop(columns=_allna_cols)
            _num_cols = [c for c in _num_cols if c not in _allna_cols]
        for c in _num_cols:
            if df[c].isna().any():
                med = df[c].median()
                df[c] = df[c].fillna(0.0 if pd.isna(med) else med)
        # 线性模型不容忍 NaN：补齐后仍有缺失的列直接剔除
        if str(model_type or "").lower() in ("linear", "logistic", "ridge", "lasso"):
            _still = [c for c in _num_cols if df[c].isna().any()]
            if _still:
                df = df.drop(columns=_still)
                _num_cols = [c for c in _num_cols if c not in _still]
    data_note = ""
    if rows_before - len(df) > 0:
        _worst = _na_counts.idxmax() if _na_counts.max() > 0 else ""
        data_note = (f"原始 {rows_before} 行，剔除含缺失值记录后剩余 {len(df)} 行"
                     + (f"（缺失最多：{_worst}）" if _worst else ""))
    elif task not in ("clustering", "anomaly") and _na_counts.max() > 0:
        _worst = _na_counts.idxmax()
        data_note = f"原始 {rows_before} 行，缺失值已按列中位数补齐（缺失最多：{_worst}）"
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

    y = None
    is_clf = False
    if target and task != "clustering":
        y_raw = df[target]
        if task == "classification" or (task == "both" and not pd.api.types.is_numeric_dtype(y_raw)):
            le_target = LabelEncoder()
            y = le_target.fit_transform(y_raw.astype(str))
            is_clf = True
        else:
            y = y_raw.values

    # 2026-10-01 修复（P0 数据泄漏）：原实现 scaler.fit_transform(X) 在 train_test_split
    # 之前对全量数据拟合，测试集的均值/方差信息泄漏进训练 → 展示的 R²/准确率系统性虚高，
    # 现场换一批数据就复现不出来。改为：监督任务先划分原始 X，scaler 只在训练段 fit，
    # 测试段只 transform；无监督任务（聚类/异常检测）无训练测试之分，保持全量拟合。
    X_train = X_test = y_train = y_test = None
    if task in ("clustering", "anomaly"):
        X_scaled = scaler.fit_transform(X)
    else:
        # 2026-10-01 修复（P1）：分类任务分层抽样（每类≥5条才启用，小类别不强切），
        # 避免类别不均衡时训练集只剩单类 → 逻辑回归直接抛英文堆栈
        strat = None
        if is_clf:
            from collections import Counter as _Counter
            cnt = _Counter(y)
            if len(cnt) < 2:
                raise ValueError(
                    f"分类目标「{target}」只有 1 个类别（{list(cnt)[0]}），无法训练分类模型，"
                    f"请换一个取值更丰富的目标字段，或改用回归模型")
            if min(cnt.values()) >= 5:
                strat = y
        try:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=0.2, random_state=42, stratify=strat)
        except ValueError:
            # 极小样本下分层切分仍可能失败（某类样本分不进测试集），回退普通切分
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        if is_clf and len(set(y_train)) < 2:
            raise ValueError(
                f"训练集中「{target}」只覆盖到 1 个类别，样本量太少无法训练分类模型，请补充数据后重试")
        X_train = scaler.fit_transform(X_train)
        X_test = scaler.transform(X_test)

    # 选模型
    if model_type == "linear":
        m = LinearRegression()
        m.fit(X_train, y_train)
        y_pred = m.predict(X_test)
        metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "decision_tree":
        is_clf = task == "classification" or (le_target is not None)
        if is_clf:
            m = DecisionTreeClassifier(max_depth=5, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
        else:
            m = DecisionTreeRegressor(max_depth=5, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "random_forest":
        is_clf = task == "classification" or (le_target is not None)
        if is_clf:
            m = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
        else:
            m = RandomForestRegressor(n_estimators=100, max_depth=8, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}

    elif model_type == "logistic":
        m = LogisticRegression(max_iter=1000, random_state=42)
        m.fit(X_train, y_train)
        y_pred = m.predict(X_test)
        metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4), "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}

    elif model_type == "kmeans":
        k = params.get("n_clusters", 3) if params else 3
        k = max(2, min(int(k), len(X_scaled) - 1))  # 聚类数须 ≥2 且 < 样本数（silhouette_score 要求）
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

    # 2026-10-03 修复（P0）：小表（10~20 行）时测试段只有 2 行，r2_score 在目标值为
    # 常数时返回 NaN（0/0，sklearn 只发 UndefinedMetricWarning 不抛异常），
    # round(nan,4) 仍是 nan → 一路进 metrics → json.dumps 默认 allow_nan=True 把它
    # 序列化成**裸 NaN 字面量**（不合法 JSON）→ 前端 JSON.parse 直接抛错、界面卡在
    # 「建模中」。这里统一做有限性校验，NaN/inf 转 None（前端显示为空，不崩）。
    metrics = _safe_metrics(metrics)

    # 特征重要性
    importance = {}
    if hasattr(m, "feature_importances_"):
        importance = {features[i]: round(m.feature_importances_[i], 4) for i in range(len(features))}
    elif hasattr(m, "coef_"):
        # 2026-10-01 修复（P1）：多分类逻辑回归的 coef_ 形状是 (n_classes, n_features)，
        # 原实现只取第 0 类系数（=「类0 vs 其余」的判别方向），标题却是「特征重要性」，
        # 大小和方向都可能误导。多分类按各类系数绝对值取均值聚合。
        import numpy as _np
        coef = _np.abs(m.coef_).mean(axis=0) if m.coef_.ndim > 1 else _np.abs(m.coef_)
        importance = {features[i]: round(float(coef[i]), 4) for i in range(len(features))}

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
        # 2026-10-03 修复：分类任务的 y 是 LabelEncoder 编码后的 0..K-1 整数，
        # 直接画图横轴是「0/1/2」而不是「合格/不合格/缺陷」，用户完全看不懂。
        # 这里先还原成原始类名再画。
        if le_target is not None:
            try:
                _yt = [str(x) for x in le_target.inverse_transform(y_test[:50])]
                _yp = [str(x) for x in le_target.inverse_transform(y_pred[:50])]
            except Exception:
                _yt, _yp = list(y_test[:50]), list(y_pred[:50])
        else:
            _yt, _yp = list(y_test[:50]), list(y_pred[:50])
        charts["pred_vs_actual"] = _plot_pred_vs_actual(_yt, _yp)

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
        # 缺失值剔除说明（无剔除为空串）：让"有效数据不足"类问题可解释
        "data_note": data_note,
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
    warnings = []
    for f in features:
        if f not in data:
            # 2026-10-01 修复（P0）：缺特征原来静默填 0（标准化后≈均值、标签编码后=第一个
            # 类别），会得到"看似合理"的错误预测且毫无提示。现仍按默认值推理（保证"只给
            # 部分字段"的问法能出结果），但缺失项记入 warnings 由结果卡透出。
            val = 0
            warnings.append(f"缺少输入「{f}」，已按默认值 0 处理")
        else:
            val = data[f]
        if f in encoders:
            try:
                val = encoders[f].transform([str(val)])[0]
            except ValueError:
                # 训练时未出现的类别：无法编码，回退到第一个类别，并明确告知（不再静默）
                le = encoders[f]
                warnings.append(f"「{f}」的取值 {val!r} 未在训练数据中出现过，已按类别「{le.classes_[0]}」处理")
                val = le.transform([le.classes_[0]])[0]
        try:
            X.append(float(val) if val is not None else 0)
        except (ValueError, TypeError):
            raise ValueError(f"预测输入字段 '{f}' 的值无法转为数值: {val!r}")
    # 传 DataFrame 保持与训练时 fit 的特征名一致（传 list 会触发 sklearn 的
    # "X does not have valid feature names" 警告刷屏控制台）
    X_scaled = scaler.transform(pd.DataFrame([X], columns=features))

    model_type = store["model_type"]
    if model_type == "kmeans":
        pred = int(m.predict(X_scaled)[0])
        return {"prediction": pred, "label": f"簇 {pred}", "warnings": warnings}
    elif model_type == "isolation":
        pred = int(m.predict(X_scaled)[0])
        return {"prediction": pred, "label": "正常" if pred == 1 else "异常", "warnings": warnings}
    elif model_type in ("linear",):
        pred = float(m.predict(X_scaled)[0])
        return {"prediction": round(pred, 4), "label": str(round(pred, 4)), "warnings": warnings}
    else:
        y_pred = m.predict(X_scaled)
        if le_target:
            label = le_target.inverse_transform([int(y_pred[0])])[0]
            return {"prediction": int(y_pred[0]), "label": str(label), "warnings": warnings}
        return {"prediction": float(y_pred[0]), "label": str(round(float(y_pred[0]), 4)), "warnings": warnings}

def _safe_metrics(d: dict) -> dict:
    """把指标里的 NaN/inf 转成 None（2026-10-03）。

    背景：Python json 默认 allow_nan=True，会把 float('nan') 序列化成裸 `NaN`
    字面量——这不是合法 JSON，前端 JSON.parse 直接抛错，整条 ml_result 事件
    解析失败、界面卡在「建模中」。这里在序列化前把非有限值统一收敛为 None。
    """
    import math as _math
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, float) and not _math.isfinite(v):
            out[k] = None
        else:
            out[k] = v
    return out


def _plot_importance(importance: dict, title: str) -> str:
    if not importance: return ""
    font = _get_zh_font()
    items = sorted(importance.items(), key=lambda x: abs(x[1]))
    labels = [i[0] for i in items]
    vals = [abs(i[1]) for i in items]
    fig, ax = plt.subplots(figsize=(5, max(2.5, len(labels)*0.35)))
    # 2026-10-03 修复：原来 plt.close 在最后一行，前面任一步抛异常（最常见是
    # 中文字体缺失导致 set_yticklabels 报错）就跳过 close，matplotlib 的 Agg
    # 全局 figure 注册表持续堆积，内存单调上涨。用 try/finally 保证回收。
    try:
        colors = ["#ef4444" if importance[l] < 0 else "#3b82f6" for l in labels]
        ax.barh(range(len(labels)), vals, color=colors)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontproperties=font, fontsize=9)
        ax.set_title(f"{title} — 特征重要性", fontproperties=font, fontsize=11)
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        buf = io.BytesIO(); fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
    finally:
        plt.close(fig)
    buf.seek(0); svg = buf.read().decode()
    return svg[svg.index("<svg"):] if svg.startswith("<?xml") else svg

def _plot_pred_vs_actual(y_true, y_pred) -> str:
    font = _get_zh_font()
    fig, ax = plt.subplots(figsize=(5, 4))
    try:  # 同上：异常路径也必须 close，否则 figure 句柄泄漏
        ax.scatter(range(len(y_true)), y_true, c="#3b82f6", s=20, alpha=0.7, label="真实值")
        ax.scatter(range(len(y_pred)), y_pred, c="#ef4444", s=20, alpha=0.7, label="预测值")
        ax.set_title("预测 vs 真实 (前50条)", fontproperties=font, fontsize=11)
        ax.legend(prop=font)
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False); ax.grid(alpha=0.3)
        buf = io.BytesIO(); fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
    finally:
        plt.close(fig)
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
