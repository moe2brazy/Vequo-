"""ML 安全执行层 — 参数化查询 / 受限特征工程 / 模型管控 / 结果脱敏"""

import json, logging, re, signal
from collections import deque
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor, IsolationForest
from sklearn.cluster import KMeans
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, accuracy_score, f1_score, silhouette_score
from sklearn.preprocessing import LabelEncoder, StandardScaler

from db.executor import execute_sql
from agent.ml_intent import ALLOWED_MODELS, ALLOWED_OPERATIONS, ALLOWED_RESAMPLE_AGGS

logger = logging.getLogger("ml_executor")

# ====== 常量 ==============================================

MAX_ROWS = 5000          # 单次查询最大行数
MAX_TRAIN_SECONDS = 30   # 训练超时
MAX_OUTPUT_ROWS = 1000   # 结果最大行数
SENSITIVE_COLUMNS = {     # 自动脱敏列名（包含这些关键词的列会被剥离）
    "password", "secret", "token", "key", "phone", "email",
    "id_card", "ssn", "address", "ip_address",
}

_AUDIT_LOG: deque = deque(maxlen=1000)  # 有界双端队列：自动淘汰最旧，避免并发下手工 del 切片竞态


# ====== 审计日志 ==========================================

def _audit(event: str, detail: dict):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event,
        "detail": detail,
    }
    _AUDIT_LOG.append(entry)  # deque.append 线程安全（GIL），maxlen 自动限长
    logger.info(f"[ML-AUDIT] {event}: {json.dumps(detail, ensure_ascii=False, default=str)}")

def get_audit_log(limit: int = 50) -> list[dict]:
    return list(_AUDIT_LOG)[-limit:]


# ====== 超时保护 ==========================================

class TrainingTimeout(Exception):
    pass

def _timeout_handler(signum, frame):
    raise TrainingTimeout()

def _with_timeout(func, seconds: int, *args, **kwargs):
    """在超时保护下执行函数（仅 Unix，Windows 跳过并记录告警）"""
    import os
    if os.name == "nt":
        # Windows 无 SIGALRM：软超时不可用，直接执行并告警（避免静默失效误导运维）
        logger.warning("ML 训练超时保护在 Windows 上不可用（无 SIGALRM），训练将以无超时方式执行")
        return func(*args, **kwargs)
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(seconds)
        result = func(*args, **kwargs)
        signal.alarm(0)
        return result
    except TrainingTimeout:
        raise TimeoutError(f"操作超时（>{seconds}秒）")
    finally:
        signal.alarm(0)


# ====== 列类型判定（pandas 3 兼容）==========================
# pandas 3.x 起，字符串列默认 dtype 为 "str"（StringDtype）而非 "object"，
# 旧写法 `df[col].dtype == object` 会漏判全部文本列 → 文本特征未编码，
# sklearn 直接报 "could not convert string to float: 'L02'"。
# 统一用 pandas 官方类型判定函数，兼容 object / str / string / category。

def _is_text_col(series: "pd.Series") -> bool:
    """文本/分类列（需要 label encode）"""
    return bool(
        pd.api.types.is_object_dtype(series)
        or pd.api.types.is_string_dtype(series)
        or isinstance(series.dtype, pd.CategoricalDtype)
    )


def _is_numeric_col(series: "pd.Series") -> bool:
    """数值列（可直接进 scaler / 模型）"""
    return bool(pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series))


# ====== 数据获取 ==========================================

def _resolve_data(data_request: dict) -> pd.DataFrame:
    """安全数据获取：白名单表名校验 + 参数化查询"""
    table = data_request.get("table", "")
    fields = data_request.get("fields", [])
    target = data_request.get("target", "")
    filter_cond = data_request.get("filter", "")
    limit = max(1, min(int(data_request.get("limit", MAX_ROWS)), MAX_ROWS))

    # 1. 表名白名单校验
    allowed_tables = _get_allowed_tables()
    if table not in allowed_tables:
        raise ValueError(f"表 '{table}' 不在允许列表中。可用表: {list(allowed_tables.keys())}")

    # 2. 字段白名单校验
    allowed_fields = allowed_tables[table]
    all_requested = list(fields)
    if target:
        all_requested.append(target)
    for f in all_requested:
        if f not in allowed_fields:
            raise ValueError(f"字段 '{f}' 不在表 '{table}' 的允许列表中")

    # 3. 构建参数化 SQL（按数据库方言引用标识符：MySQL 默认 sql_mode 下双引号是字符串字面量，必须用反引号）
    from database import quote_ident
    cols = ", ".join(quote_ident(f) for f in sorted(set(all_requested)))
    sql = f'SELECT {cols} FROM {quote_ident(table)}'

    # filter 安全处理：白名单模式，仅允许「字段 运算符 值」单一简单比较。
    # 字段必须通过 allowed_fields 白名单，值只接受数字或单引号字符串（正则已排除内嵌单引号），
    # 字段名走 quote_ident 转义 → 从根本上杜绝 SQL 注入（原黑名单方式可被 ' OR 1=1 绕过）。
    if filter_cond and filter_cond.strip():
        fc = filter_cond.strip()
        m = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_]*)\s*(=|!=|<>|>=|<=|>|<|LIKE)\s*('[^']*'|-?\d+(?:\.\d+)?)",
            fc, re.IGNORECASE,
        )
        if not m:
            raise ValueError("filter 仅支持单个简单条件（如 field = 值 / field LIKE 'x%'），且字段须为表字段")
        col, op, val = m.group(1), m.group(2), m.group(3)
        if col not in allowed_fields:
            raise ValueError(f"filter 字段 '{col}' 不在表 '{table}' 的允许列表中")
        sql += f" WHERE {quote_ident(col)} {op} {val}"

    sql += f" LIMIT {limit}"

    _audit("data_fetch", {"table": table, "fields": all_requested, "limit": limit, "sql": sql})

    # 4. 执行查询
    result = execute_sql(sql)
    if not result["success"]:
        raise ValueError(f"数据查询失败: {result.get('error', '未知错误')}")
    if not result["rows"]:
        raise ValueError(f"查询无数据: 表 '{table}' 可能为空")

    df = pd.DataFrame(result["rows"])

    # 5. 自动剥离敏感列
    df = _strip_sensitive_columns(df)

    _audit("data_loaded", {"rows": len(df), "columns": list(df.columns)})
    return df


def _get_allowed_tables() -> dict[str, set[str]]:
    """动态获取所有可用表及其字段（白名单）"""
    try:
        from database import get_db_type
        schema_clause = "table_schema=DATABASE()" if get_db_type() == "mysql" else "table_schema='public'"
        result = execute_sql(
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE {schema_clause} ORDER BY table_name, ordinal_position"
        )
        if result["success"] and result["rows"]:
            tables = {}
            for r in result["rows"]:
                tn = r["table_name"]
                if tn not in tables:
                    tables[tn] = set()
                tables[tn].add(r["column_name"])
            return tables
    except Exception:
        pass

    # 回退：使用元数据
    from db.metadata import TABLES
    return {t["table_name"]: {f["name"] for f in t["fields"]} for t in TABLES}


def _strip_sensitive_columns(df: pd.DataFrame) -> pd.DataFrame:
    """剥离包含敏感关键词的列"""
    cols_to_drop = []
    for col in df.columns:
        col_lower = col.lower()
        for sensitive in SENSITIVE_COLUMNS:
            if sensitive in col_lower:
                cols_to_drop.append(col)
                break
    if cols_to_drop:
        _audit("strip_sensitive", {"dropped_cols": cols_to_drop})
        df = df.drop(columns=cols_to_drop)
    return df


# ====== 特征工程 ==========================================

def _run_feature_engineering(df: pd.DataFrame, steps: list[dict]) -> tuple[pd.DataFrame, dict]:
    """执行白名单操作序列，返回 (处理后的DataFrame, 元信息)"""
    meta = {"original_rows": len(df), "original_cols": len(df.columns), "steps_applied": []}

    for i, step in enumerate(steps):
        op = step.get("op", "")
        params = step.get("params", {})

        if op not in ALLOWED_OPERATIONS:
            _audit("fe_skip", {"step": i, "reason": f"操作 '{op}' 不在白名单"})
            continue

        before_rows = len(df)

        try:
            if op == "dropna":
                max_ratio = float(params.get("max_drop_ratio", 0.3))
                before = len(df)
                df = df.dropna()
                dropped = before - len(df)
                if dropped / before > max_ratio:
                    raise ValueError(f"dropna 删除比例 {dropped/before:.2%} 超过上限 {max_ratio:.0%}，拒绝执行")
                meta["steps_applied"].append({"op": op, "dropped": dropped, "remaining": len(df)})

            elif op == "log_transform":
                columns = params.get("columns", [])
                for col in columns:
                    if col in df.columns:
                        min_val = df[col].min()
                        offset = abs(min_val) + 1 if min_val <= 0 else 0
                        df[col] = np.log(df[col] + offset)
                meta["steps_applied"].append({"op": op, "columns": columns})

            elif op == "standardize":
                columns = params.get("columns", [])
                scaler = StandardScaler()
                valid_cols = [c for c in columns if c in df.columns and _is_numeric_col(df[c])]
                if valid_cols:
                    df[valid_cols] = scaler.fit_transform(df[valid_cols])
                meta["steps_applied"].append({"op": op, "columns": valid_cols})

            elif op == "label_encode":
                columns = params.get("columns", [])
                for col in columns:
                    if col in df.columns and _is_text_col(df[col]):
                        le = LabelEncoder()
                        df[col] = le.fit_transform(df[col].astype(str))
                meta["steps_applied"].append({"op": op, "columns": columns})

            elif op == "resample":
                # 需要日期列作为索引
                date_cols = [c for c in df.columns if "date" in c.lower() or "time" in c.lower()]
                if not date_cols:
                    raise ValueError("resample 需要日期/时间列，但未找到")
                rule = params.get("rule", "1D")
                agg = params.get("agg", "mean")
                if agg not in ALLOWED_RESAMPLE_AGGS:
                    raise ValueError(f"不支持的聚合函数: {agg}")
                df[date_cols[0]] = pd.to_datetime(df[date_cols[0]])
                df = df.set_index(date_cols[0])
                df = df.resample(rule).agg(agg)
                df = df.reset_index()
                meta["steps_applied"].append({"op": op, "rule": rule, "agg": agg, "date_col": date_cols[0]})

            _audit("fe_step", {"step": i, "op": op, "before": before_rows, "after": len(df)})

        except Exception as e:
            _audit("fe_error", {"step": i, "op": op, "error": str(e)})
            raise ValueError(f"特征工程步骤 [{op}] 执行失败: {str(e)}")

    meta["final_rows"] = len(df)
    meta["final_cols"] = len(df.columns)
    return df, meta


# ====== 模型训练 ==========================================

def _train_model_safe(df: pd.DataFrame, model_spec: dict) -> dict:
    """在白名单约束下训练模型，返回结果"""
    model_name = model_spec.get("model", "")
    params = model_spec.get("params", {})
    target = model_spec.get("target", "")

    if model_name not in ALLOWED_MODELS:
        raise ValueError(f"模型 '{model_name}' 不在白名单中")

    model_def = ALLOWED_MODELS[model_name]
    task = model_def["task"]

    # 校验参数：不在白名单的参数直接忽略（LLM 可能输出 random_state 等常见默认参数）
    for pname, pval in list(params.items()):
        if pname not in model_def["params"]:
            params.pop(pname)
            continue
        pdef = model_def["params"][pname]
        try:
            pnum = float(pval)
        except (TypeError, ValueError):
            # LLM 输出非数字参数 → 用默认值，不中断训练
            params.pop(pname, None)
            continue
        if not (pdef["min"] <= pnum <= pdef["max"]):
            params[pname] = max(pdef["min"], min(pdef["max"], pnum))

    _audit("train_start", {"model": model_name, "params": params, "task": task, "rows": len(df)})

    # 准备特征
    feature_cols = [c for c in df.columns if c != target]
    if not feature_cols:
        raise ValueError("没有可用的特征列")

    X = df[feature_cols].copy()
    le_target = None
    y = None

    if target and target in df.columns and task != "clustering":
        y_raw = df[target]
        if _is_text_col(y_raw) or task == "classification":
            le_target = LabelEncoder()
            y = le_target.fit_transform(y_raw.astype(str))
            task = "classification"
        else:
            y = y_raw.values

    # 编码分类特征
    encoders = {}
    for col in X.columns:
        if _is_text_col(X[col]):
            le = LabelEncoder()
            X[col] = le.fit_transform(X[col].astype(str))
            encoders[col] = le

    # 训练函数
    def _do_train():
        if model_name == "LinearRegression":
            m = LinearRegression()
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "LogisticRegression":
            max_iter = int(params.get("max_iter", 1000))
            m = LogisticRegression(max_iter=max_iter, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4),
                       "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "DecisionTreeClassifier":
            max_depth = int(params.get("max_depth", 5))
            m = DecisionTreeClassifier(max_depth=max_depth, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4),
                       "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "DecisionTreeRegressor":
            max_depth = int(params.get("max_depth", 5))
            m = DecisionTreeRegressor(max_depth=max_depth, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "RandomForestClassifier":
            n_est = int(params.get("n_estimators", 100))
            max_depth = int(params.get("max_depth", 8))
            m = RandomForestClassifier(n_estimators=n_est, max_depth=max_depth, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"准确率": round(accuracy_score(y_test, y_pred), 4),
                       "F1": round(f1_score(y_test, y_pred, average="weighted"), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "RandomForestRegressor":
            n_est = int(params.get("n_estimators", 100))
            max_depth = int(params.get("max_depth", 8))
            m = RandomForestRegressor(n_estimators=n_est, max_depth=max_depth, random_state=42)
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            m.fit(X_train, y_train)
            y_pred = m.predict(X_test)
            metrics = {"R²": round(r2_score(y_test, y_pred), 4)}
            return m, metrics, y_test, y_pred, None

        elif model_name == "KMeans":
            n_clusters = int(params.get("n_clusters", 3))
            m = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            m.fit(X)
            labels = m.labels_
            sil = silhouette_score(X, labels) if len(set(labels)) > 1 else -1
            metrics = {"轮廓系数": round(sil, 4), "聚类数": n_clusters}
            df["_cluster"] = labels
            return m, metrics, None, labels, df["_cluster"].value_counts().to_dict()

        elif model_name == "IsolationForest":
            n_est = int(params.get("n_estimators", 100))
            contam = float(params.get("contamination", 0.1))
            m = IsolationForest(n_estimators=n_est, contamination=contam, random_state=42)
            preds = m.fit_predict(X)
            n_outliers = int((preds == -1).sum())
            metrics = {"异常比例": round(n_outliers / len(preds), 4),
                       "异常数": n_outliers, "总样本": len(preds)}
            df["_anomaly"] = (preds == -1).astype(int)
            return m, metrics, None, preds, {"labels": ["正常", "异常"], "counts": [len(preds) - n_outliers, n_outliers]}

    try:
        model, metrics, y_test, y_pred, extra = _do_train()
    except Exception as e:
        _audit("train_error", {"model": model_name, "error": str(e)})
        raise ValueError(f"模型训练失败: {str(e)}")

    # 提取特征重要性
    importance = {}
    if hasattr(model, "feature_importances_"):
        importance = {feature_cols[i]: round(model.feature_importances_[i], 4) for i in range(len(feature_cols))}
    elif hasattr(model, "coef_"):
        coef = model.coef_[0] if model.coef_.ndim > 1 else model.coef_
        importance = {feature_cols[i]: round(coef[i], 4) for i in range(len(feature_cols))}

    # 预测结果（前50条，脱敏）
    pred_samples = []
    if task not in ("clustering", "anomaly_detection") and y_test is not None:
        for i in range(min(50, len(y_test))):
            pred_samples.append({"actual": float(y_test[i]), "predicted": float(y_pred[i])})
    elif task == "clustering":
        for i in range(min(50, len(df))):
            pred_samples.append({"cluster": int(df["_cluster"].iloc[i])})

    _audit("train_done", {"model": model_name, "metrics": metrics})

    return {
        "model_name": model_name,
        "model_label": model_def["name"],
        "task": task,
        "features": feature_cols,
        "target": target,
        "metrics": metrics,
        "importance": importance,
        "samples": len(df),
        "pred_samples": pred_samples,
        "extra": extra,
    }


# ====== 结果封装 ==========================================

def _package_result(train_result: dict, fe_meta: dict, output_spec: dict) -> dict:
    """安全封装结果：截断行数 + 脱敏"""
    max_rows = min(int(output_spec.get("max_rows", 100)), MAX_OUTPUT_ROWS)
    requested_metrics = output_spec.get("metrics", [])

    # 过滤指标
    all_metrics = train_result.get("metrics", {})
    if requested_metrics:
        filtered_metrics = {k: v for k, v in all_metrics.items() if k in requested_metrics}
        if not filtered_metrics:
            filtered_metrics = all_metrics
    else:
        filtered_metrics = all_metrics

    # 截断预测样本
    pred_samples = train_result.get("pred_samples", [])[:max_rows]

    result = {
        "success": True,
        "model": {
            "name": train_result["model_name"],
            "label": train_result["model_label"],
            "task": train_result["task"],
            "features": train_result["features"],
            "target": train_result["target"],
        },
        "metrics": filtered_metrics,
        "importance": dict(sorted(train_result.get("importance", {}).items(), key=lambda x: abs(x[1]), reverse=True)),
        "feature_engineering": fe_meta,
        "samples": train_result["samples"],
        "pred_samples": pred_samples,
    }

    if train_result.get("extra"):
        result["extra"] = train_result["extra"]

    _audit("result_packaged", {"metrics": filtered_metrics, "samples": len(pred_samples)})
    return result


# ====== 主入口 ============================================

def execute_ml_intent(intent: dict) -> dict:
    """执行 ML 意图 — 唯一对外入口"""

    intent_type = intent.get("intent_type", "train")
    user_summary = intent.get("user_summary", "")
    data_request = intent.get("data_request", {})
    fe_steps = intent.get("feature_engineering", [])
    model_training = intent.get("model_training", {})
    output_spec = intent.get("output_spec", {})

    _audit("execute_start", {"type": intent_type, "summary": user_summary})

    try:
        # 1. 安全获取数据
        df = _resolve_data(data_request)

        # 2. 特征工程
        df, fe_meta = _run_feature_engineering(df, fe_steps)

        if len(df) < 5:
            raise ValueError(f"特征工程后有效数据不足（{len(df)}行），至少需要5行")

        # 3. 模型训练（接入超时保护；Windows 无 signal 支持时退化为直接执行）
        model_training["target"] = data_request.get("target", "")
        train_result = _with_timeout(_train_model_safe, MAX_TRAIN_SECONDS, df, model_training)

        # 4. 结果封装
        result = _package_result(train_result, fe_meta, output_spec)

        _audit("execute_done", {"success": True})
        return result

    except ValueError as e:
        _audit("execute_error", {"error": str(e), "type": "ValueError"})
        return {
            "success": False,
            "error": str(e),
            "error_type": "validation_error",
            "hint": "请检查数据和参数是否正确。",
        }
    except TimeoutError as e:
        _audit("execute_error", {"error": str(e), "type": "TimeoutError"})
        return {
            "success": False,
            "error": "模型训练超时，请减少数据量或选择更简单的模型。",
            "error_type": "timeout",
        }
    except Exception as e:
        _audit("execute_error", {"error": str(e), "type": type(e).__name__})
        return {
            "success": False,
            "error": "系统内部错误，请联系管理员。",
            "error_type": "internal_error",
        }
