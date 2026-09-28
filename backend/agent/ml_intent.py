"""ML 意图生成 Agent — 受限输出，不接触真实数据"""

import json, logging, re
from langchain_core.messages import HumanMessage, SystemMessage, AIMessageChunk

logger = logging.getLogger(__name__)

# ====== 白名单 ============================================

# 允许的特征工程操作
ALLOWED_OPERATIONS = {
    "dropna":       {"desc": "删除含空值行",   "params": {"max_drop_ratio": {"type": "number", "min": 0.0, "max": 0.5}}},
    "log_transform": {"desc": "对数变换",       "params": {"columns": {"type": "list[str]"}}},
    "standardize":   {"desc": "标准化",         "params": {"columns": {"type": "list[str]"}}},
    "label_encode":  {"desc": "标签编码",       "params": {"columns": {"type": "list[str]"}}},
    "resample":      {"desc": "时间重采样",     "params": {"rule": {"type": "str"}, "agg": {"type": "str"}}},
}

# 允许的模型 + 参数约束
ALLOWED_MODELS = {
    "LinearRegression":      {"name": "线性回归",        "task": "regression",      "params": {}},
    "LogisticRegression":    {"name": "逻辑回归",        "task": "classification",  "params": {"max_iter": {"min": 100, "max": 5000}}},
    "DecisionTreeClassifier": {"name": "决策树分类",     "task": "classification",  "params": {"max_depth": {"min": 1, "max": 20}}},
    "DecisionTreeRegressor":  {"name": "决策树回归",     "task": "regression",      "params": {"max_depth": {"min": 1, "max": 20}}},
    "RandomForestClassifier": {"name": "随机森林分类",   "task": "classification",  "params": {"n_estimators": {"min": 10, "max": 500}, "max_depth": {"min": 1, "max": 20}}},
    "RandomForestRegressor":  {"name": "随机森林回归",   "task": "regression",      "params": {"n_estimators": {"min": 10, "max": 500}, "max_depth": {"min": 1, "max": 20}}},
    "KMeans":                {"name": "KMeans聚类",      "task": "clustering",       "params": {"n_clusters": {"min": 2, "max": 20}}},
    "IsolationForest":      {"name": "孤立森林",        "task": "anomaly_detection","params": {"n_estimators": {"min": 10, "max": 500}, "contamination": {"min": 0.01, "max": 0.5}}},
}

# 允许的重采样聚合函数
ALLOWED_RESAMPLE_AGGS = {"mean", "sum", "min", "max", "count", "std", "first", "last"}


# ====== Prompt ===========================================

ML_INTENT_PROMPT = """你是一个机器学习建模助手。根据用户需求和可用数据，生成一个严格符合 JSON Schema 的建模意图。

## 可用数据源
**你只能使用下面这一张表，表名已经是固定的！不要改表名！**

{schema_context}

## JSON Schema（必须严格遵守）

{{
  "intent_type": "train",
  "user_summary": "一句话概括用户的建模需求",
  "data_request": {{
    "table": "{table_name}",
    "fields": ["从上方表中选择的字段名"],
    "target": "目标字段名（选一个数值字段作为预测目标，聚类/异常检测留空）",
    "filter": "",
    "limit": 5000
  }},
  "feature_engineering": [
    {{"op": "操作名", "params": {{...}}}}
  ],
  "model_training": {{
    "model": "模型名（从允许列表选）",
    "params": {{...}}
  }},
  "output_spec": {{
    "metrics": ["指标名"],
    "max_rows": 100
  }}
}}

## 你只能使用的字段（从上方复制）
## 表名固定为：{table_name}

任务：从上方字段中选择合适的 feature 和 target 字段，选择模型，设计特征工程步骤。

## 允许的模型
{model_list}

## 允许的特征工程步骤（只能用下面这些 op，名字必须一模一样，不要自己造名字）
{fe_list}

## 关键规则
1. 只输出 JSON
2. table 字段必须是 "{table_name}"，不要修改
3. **绝对不要改写字段名！字段名必须和上面「字段列表」中的一模一样，直接复制粘贴！例如上面写的是 line_id 你就填 line_id，不要写成 production_line**
4. 聚类/异常检测时 target 留空字符串 ""
5. 如果用户需求模糊，选择最合理的默认值
6. 特征工程中的 columns 也要用和字段列表一致的名字
7. **fields 只放能当特征用的业务字段**：不要放结尾是 _id 的主键/外键（如 output_id、work_order_id），不要放日期时间字段（如 stat_date）；两者系统都会剔除
8. 没有合适的特征工程步骤时，feature_engineering 直接返回空数组 []
9. **「根据 A 预测 B」语序不要弄反**：B 是用户要预测的那个，必须填 target；A 是拿来做依据的，
   必须放进 fields 当特征。例：「根据计划产量预测实际产量」→ target 是"实际产量"对应的列，
   fields 里放"计划产量"对应的列。
10. **用户点名的算法要照用**：说了"随机森林"就填 RandomForestRegressor（预测数值）或
    RandomForestClassifier（预测类别），说了"线性回归"就填 LinearRegression，不要换别的。
11. **上面字段列表里没有的列，一个都不许出现在 fields/target 里**。这张表里没有的字段，
    宁可把 target 留空也不要编一个名字出来。

用户需求: {query}

输出 JSON:"""


# ====== 校验 =============================================

def _llm(temp: float = 0.0, max_tokens: int = 4096, json_mode: bool = False,
         thinking_off: bool = True):
    # 复用统一 LLM 工厂（含 Kimi temperature 兜底等兼容逻辑）
    #
    # max_tokens 默认 4096（原 2048，取自 _make_llm 默认值）。
    # 2026-09-20 实测：当前模型 deepseek-v4.1-flash 默认「先想后答」，**思考 token 计入
    # max_tokens 预算**，本 prompt（长 schema + 长规则）下思考能把 4096 全吃光 →
    # content 恒空 → `_parse_ml_intent("")` 报 "Expecting value: line 1 column 1 (char 0)"，
    # 现场表现就是那句「方案生成异常」外加一条乱猜出来的模型。实测首轮 104s 后仍 0 字。
    #
    # 所以这里对**方案生成这一处**关掉思考链：它是结构化 JSON 输出，不需要长思考链，
    # 而 SQL 生成那侧"思考链是正确性来源"的结论（见 llm_providers 注释）不适用于此。
    # 只对已实测支持 enable_thinking 的 dashscope 端点注入，其它网关保持原样。
    from agent.llm_service import _make_llm
    from config import LLM_CONFIG
    extra = None
    if thinking_off:
        try:
            if "dashscope" in (LLM_CONFIG.get("base_url") or "").lower():
                extra = {"enable_thinking": False}
        except Exception:
            extra = None
    return _make_llm(temp=temp, max_tokens=max_tokens, json_mode=json_mode, extra_body=extra)


def validate_ml_intent(intent: dict) -> tuple[bool, str, dict]:
    """校验 ML 意图 JSON 的合法性，返回 (有效?, 错误信息, 修正后的意图)"""
    errors = []
    dropped_ops: list[str] = []   # 未支持的特征工程算子（丢弃，不算错误）

    # 1. 基本结构
    if not isinstance(intent, dict):
        return False, "意图必须是 JSON 对象", intent
    if intent.get("intent_type") not in ("train", "predict"):
        errors.append("intent_type 必须是 train 或 predict")
    if "data_request" not in intent:
        errors.append("缺少 data_request")

    dr = intent.get("data_request", {})
    if not isinstance(dr, dict):
        errors.append("data_request 必须是对象")
    else:
        if not dr.get("table"):
            errors.append("data_request.table 不能为空")
        if not dr.get("fields") or not isinstance(dr["fields"], list):
            errors.append("data_request.fields 必须是非空数组")

    # 2. 特征工程校验
    fe_steps = intent.get("feature_engineering", [])
    if not isinstance(fe_steps, list):
        errors.append("feature_engineering 必须是数组")
    else:
        valid_steps = []
        for i, step in enumerate(fe_steps):
            # LLM 偶发输出字符串/None 等非 dict 元素，直接 step.get 会抛 AttributeError
            # 且未被捕获，导致整个意图校验失败而非降级。这里显式拦截。
            if not isinstance(step, dict):
                errors.append(f"feature_engineering[{i}] 必须是对象")
                continue
            op = step.get("op", "")
            if op not in ALLOWED_OPERATIONS:
                # 非致命，只丢弃（2026-09-20 修复）：
                # 执行器本身就是静默跳过的（ml/executor.py 里 `_audit("fe_skip")` + continue），
                # 旧实现却把它塞进 errors → 整个意图被判定为无效 → 界面弹「方案生成异常…不在白名单中」，
                # 而训练其实照常跑完。两处口径不一致，用户看到的是一句假故障。
                logger.warning("ML 意图含未支持的特征工程算子，已丢弃: %s", op)
                dropped_ops.append(op)
                continue
            params = step.get("params", {})
            if op == "dropna":
                ratio = params.get("max_drop_ratio", 0.3)
                try:
                    ratio = float(ratio)
                except (TypeError, ValueError):
                    ratio = 0.3  # LLM 输出非数字 → 用默认值，不中断流程
                if not (0.0 <= ratio <= 0.5):
                    params["max_drop_ratio"] = max(0.0, min(0.5, ratio))
                else:
                    params["max_drop_ratio"] = ratio
            elif op == "resample":
                if params.get("rule") not in ("1D", "1H", "1W", "1M", "1T"):
                    errors.append(f"resample rule='{params.get('rule')}' 不支持")
                if params.get("agg") not in ALLOWED_RESAMPLE_AGGS:
                    errors.append(f"resample agg='{params.get('agg')}' 不支持")
            valid_steps.append({"op": op, "params": params})
        intent["feature_engineering"] = valid_steps

    # 3. 模型校验
    mt = intent.get("model_training", {})
    if not isinstance(mt, dict):
        errors.append("model_training 必须是对象")
    else:
        model_name = mt.get("model", "")
        if model_name not in ALLOWED_MODELS:
            errors.append(f"模型 '{model_name}' 不在白名单中")
        else:
            model_def = ALLOWED_MODELS[model_name]
            user_params = mt.get("params", {})
            # 校验参数范围
            sanitized_params = {}
            for pname, pdef in model_def["params"].items():
                if pname in user_params:
                    try:
                        val = float(user_params[pname])
                        val = max(pdef["min"], min(pdef["max"], val))
                        sanitized_params[pname] = int(val) if isinstance(pdef["min"], int) else val
                    except (ValueError, TypeError):
                        errors.append(f"参数 {pname}={user_params[pname]} 不是合法数值")
                # 使用默认值填充必需参数
            if "n_clusters" in model_def["params"] and "n_clusters" not in sanitized_params:
                sanitized_params["n_clusters"] = 3
            if "contamination" in model_def["params"] and "contamination" not in sanitized_params:
                sanitized_params["contamination"] = 0.1
            mt["params"] = sanitized_params

    # 4. 输出规格校验
    # 注意：写回要用上面 .get() 拿到的那份对象本身，不能再用 intent["output_spec"] 取 ——
    # LLM 漏掉 output_spec / model_training 这两个"可选"键时，.get() 返回的是新的空 dict，
    # 而 intent["output_spec"] 会直接 KeyError。这个异常发生在流的消费侧、
    # 且 _parse_ml_intent 调用处没有 try 包住 → 整轮建模会以未捕获异常收场。
    os_spec = intent.get("output_spec", {})
    if not isinstance(os_spec, dict):
        errors.append("output_spec 必须是对象")
    else:
        max_rows = os_spec.get("max_rows", 100)
        try:
            max_rows = int(max_rows)
            os_spec["max_rows"] = max(1, min(max_rows, 1000))
        except (ValueError, TypeError):
            os_spec["max_rows"] = 100

    if dropped_ops:
        intent["_dropped_ops"] = dropped_ops
    if errors:
        return False, "; ".join(errors), intent
    return True, "", intent


# ====== 主函数 ===========================================

def _build_ml_prompt(query: str, schema_context: str, table_name: str = "") -> str:
    """构建 ML 意图生成 Prompt（流式/普通共用）"""
    model_list = "\n".join(
        f"- {name}: {info['name']} ({info['task']}), 参数约束: {info['params'] or '无'}"
        for name, info in ALLOWED_MODELS.items()
    )
    # 把算子白名单也写进 Prompt（2026-09-20 修复）：
    # 旧版只给模型清单、不给算子清单，模型只能靠猜，于是每次都编出
    # fill_missing / one_hot_encode / date_features 这类名字 —— 校验器把它当错误上报
    # （界面弹「方案生成异常」），而执行器其实是静默跳过的。现在名字对得上，两边都不再吵。
    fe_list = "\n".join(
        f"- {op}: {info['desc']}, params: {info['params'] or '无'}"
        for op, info in ALLOWED_OPERATIONS.items()
    ) or "- （无可用算子）"
    return ML_INTENT_PROMPT.format(
        schema_context=schema_context,
        model_list=model_list,
        fe_list=fe_list,
        query=query,
        table_name=table_name,
    )


def _parse_ml_intent(raw: str) -> tuple[dict, str]:
    """从 LLM 输出解析并校验 ML 意图，返回 (intent, error)"""
    raw_json = ""
    src = raw or ""
    # 清理 markdown 代码块（旧写法 rstrip("```") 是按字符集剥，会把结尾的 } 一起啃掉）
    if src.lstrip().startswith("```"):
        lines = src.split("\n")
        src = "\n".join(lines[1:])
        if src.rstrip().endswith("```"):
            src = src.rstrip()[:-3]

    # 提取 JSON 对象
    match = re.search(r'\{[\s\S]*\}', src)
    if not match:
        # 输出里连一个花括号都没有（空输出 / 被截断 / 只回了段中文话术）——
        # 这条信息比 json.loads("") 的 "Expecting value: line 1 column 1" 有用得多
        return {}, f"LLM 未返回 JSON（输出 {len(src)} 字：{src[:80]!r}）"
    raw_json = match.group(0)

    try:
        intent = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return {}, f"LLM 生成的 JSON 格式无效: {str(e)}"

    valid, err, corrected = validate_ml_intent(intent)
    if not valid:
        return corrected, err
    return corrected, ""


def generate_ml_intent(query: str, schema_context: str, table_name: str = "") -> dict:
    """根据用户自然语言 + 可用表结构，生成受限 ML 意图 JSON（一次性）"""
    prompt = _build_ml_prompt(query, schema_context, table_name)

    try:
        llm = _llm(temp=0.0)
        resp = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=query)])
        intent, err = _parse_ml_intent(resp.content.strip())
    except Exception as e:
        return {
            "valid": False,
            "error": f"意图生成失败: {str(e)}",
            "raw": "",
        }

    if err:
        return {
            "valid": True,
            "warning": err,
            "intent": intent,
        }

    return {
        "valid": True,
        "warning": "",
        "intent": intent,
    }


def generate_ml_intent_stream(query: str, schema_context: str, table_name: str = ""):
    """流式生成 ML 意图：yield 思考事件，最后 yield {type:'intent', ...}

    yield 事件:
      {'type': 'thought', 'step': 'ML方案设计', 'text': '...'}   LLM token 流
      {'type': 'thought_done', 'step': 'ML方案设计'}              生成结束
      {'type': 'intent', 'valid': bool, 'intent': dict|None, 'error': str}
    """
    prompt = _build_ml_prompt(query, schema_context, table_name)
    full = ""
    json_started = False   # 关掉思考链后，吐出来的是 JSON 本体，不该当"思考过程"铺到界面上
    try:
        llm = _llm(temp=0.0)
        for chunk in llm.stream([SystemMessage(content=prompt), HumanMessage(content=query)]):
            if isinstance(chunk, AIMessageChunk) and chunk.content:
                t = chunk.content
                full += t
                if not json_started:
                    if "{" in t:
                        json_started = True
                    else:
                        yield {"type": "thought", "step": "ML方案设计", "text": t}
    except Exception as e:
        yield {"type": "intent", "valid": False, "intent": None, "error": f"意图生成失败: {str(e)}"}
        return

    yield {"type": "thought_done", "step": "ML方案设计"}

    intent, err = _parse_ml_intent(full.strip())
    if not intent:
        # 首轮没拿到可用方案 —— 最常见的两种：思考 token 吃光 max_tokens（content 恒空）、
        # 上游抖动返回空串。再试一次：不流式、预算加大、开 JSON 模式（dashscope/deepseek
        # 支持 response_format=json_object，provider 表里 json_mode=True）。
        # 这次失败也不影响可用性：上层 `_respond_ml` 还有 agent/ml_plan 的规则兜底。
        logger.warning("ML 意图首轮失败(%s)，原始输出 %d 字，重试一次", err, len(full))
        retry_text = ""
        try:
            resp = _llm(temp=0.0, max_tokens=6144, json_mode=True).invoke(
                [SystemMessage(content=prompt), HumanMessage(content=query)])
            retry_text = (resp.content or "").strip()
        except Exception as e:
            logger.warning("ML 意图重试调用异常: %s", e)
        if retry_text:
            intent2, err2 = _parse_ml_intent(retry_text)
            if intent2:
                yield {"type": "thought", "step": "ML方案设计", "text": "（首轮无输出，已重试成功）"}
                intent, err = intent2, err2

    if err:
        yield {"type": "intent", "valid": True, "intent": intent, "error": err, "warning": err}
    else:
        yield {"type": "intent", "valid": True, "intent": intent, "error": ""}
