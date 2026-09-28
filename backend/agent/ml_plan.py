"""建模方案的确定性解析 / 兜底（**不依赖 LLM**）

为什么要这个模块（2026-09-20 第二次报障，用户贴出运行结果）：

    问「用生产工单表训练一个随机森林模型，根据计划产量预测实际产量」，界面出来的是
        · 预测目标 plan_qty          ← 把用户指定要"拿来当依据"的那一列当成了目标
        · 特征 work_order_id / work_order_no / product_id / line_id / order_status
        · 线性回归  R²=0.0031  · 特征重要性一正一负（其实是线性系数，不是重要性）
        · 顶部弹「方案生成异常: LLM 生成的 JSON 格式无效: Expecting value: line 1 column 1」

    链路是：方案设计那次 LLM 调用返回了空内容（思考 token 把 max_tokens 吃干净、
    content 恒空，这个失败模式在 `_make_llm` 的注释里已经写过），`_parse_ml_intent("")`
    返回 `{}`，而 `_respond_ml` 拿到 `{}` 后**当成一份"什么都没指定的方案"继续往下跑**，
    于是一路启发式瞎猜：目标按关键词顺序撞上 plan_qty，特征退化成一排 ID 列。

    问题不在"哪条启发式写错了"，而在**方案只有 LLM 一条路**。本模块补上第二条路：
    从问句本身把「表 / 依据的特征 / 要预测的目标 / 用户点名的算法」解析出来。

还有一个真实数据事实（2026-09-20 查库确认，别记错）：
    `mes_work_order` 只有 8 列 —— work_order_id / work_order_no / product_id / line_id /
    plan_qty / start_date / end_date / order_status，**唯一的数值列是 plan_qty**，
    "实际产量"根本不在这张表里（它在 mes_process_output.good_qty，按 work_order_id 汇总）。
    所以「用工单表预测实际产量」单表无解，必须跨表：
        mes_work_order ⋈ mes_process_output  ON work_order_id
        target = SUM(good_qty)   feature = plan_qty
    这条路由 `try_cross_table_target()` 负责。

本模块纯函数、无 IO、无 LLM，可直接单测（见 backend/_probe/probe_mlplan.py）。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ====== 中文业务词 → 英文列名片段 =========================================
# 只放"业务量"的词，不放表名/实体名：把「工单」映射成 work_order 会让
# 「工单的实际产量」误命中 work_order_id。命中数越多越可信（见 resolve_column）。
CN_EN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("实际",   ("actual", "real", "finished")),
    ("计划",   ("plan",)),
    ("目标",   ("target", "plan")),
    ("合格",   ("good", "qualified", "ok")),
    ("良品",   ("good",)),
    ("不良",   ("defect", "bad", "ng")),
    ("缺陷",   ("defect",)),
    ("次品",   ("defect",)),
    ("返工",   ("rework",)),
    ("报废",   ("scrap",)),
    ("投入",   ("input",)),
    ("投料",   ("input", "feed")),
    ("产出",   ("output", "qty")),
    ("产量",   ("qty", "output")),
    ("数量",   ("qty", "count")),
    ("库存",   ("inventory", "stock", "qty")),
    ("可用",   ("available",)),
    ("冻结",   ("frozen",)),
    ("在途",   ("transit",)),
    ("安全",   ("safety",)),
    ("缺货",   ("shortage", "lack")),
    ("停机",   ("downtime",)),
    ("时长",   ("minutes", "duration")),
    ("次数",   ("count",)),
    ("抽检",   ("sample",)),
    ("检验",   ("sample", "inspection")),
    ("批次",   ("batch",)),
    ("金额",   ("amount",)),
    ("价格",   ("price",)),
    ("成本",   ("cost",)),
    ("工资",   ("salary",)),
    ("温度",   ("temp",)),
    ("寿命",   ("life",)),
    ("序列",   ("seq",)),
    ("率",     ("rate", "ratio")),
    # ── 类别型业务词（"根据抽检数量和检验结论预测不良数量"里的"检验结论"要靠它落地）──
    # 它们同时是限定词：出现在目标短语里却没落点，就说明目标不在这张表。
    ("结论",   ("result", "conclusion")),
    ("结果",   ("result",)),
    ("状态",   ("status", "state")),
    ("类型",   ("type", "category")),
    ("类别",   ("category", "type")),
    ("等级",   ("level", "grade", "severity")),
    ("严重",   ("severity", "level")),
    ("班次",   ("shift",)),
    ("原因",   ("reason",)),
    ("编码",   ("code",)),
    ("编号",   ("no", "code")),
    ("名称",   ("name",)),
)

# 打平时（多个列命中同一个词，如"产量"同时命中 good_qty / input_qty / defect_qty）
# 按这个顺序偏好"更像产出"的那一列，避免把不良数当产量。
_QUANTITY_PREFERENCE = ("actual", "output", "good", "finished", "qualified", "total",
                        "qty", "count", "amount")

# 只表"量"不表"哪一种量"的词：光靠它们命中一列不算数（"产量"命中 plan_qty 只是撞上了 qty）
_GENERIC_QUANTITY_WORDS = {"产量", "数量", "产出", "库存", "次数", "时长", "批次",
                           "序列", "消耗", "率"}

# 用户点名算法 → (trainer model_type, 白名单模型名前缀, 是否有监督)
ALGO_HINTS: tuple[tuple[tuple[str, ...], str, str, bool], ...] = (
    (("随机森林", "random forest", "randomforest"),  "random_forest", "RandomForest", True),
    (("决策树", "decision tree", "decisiontree"),    "decision_tree", "DecisionTree", True),
    (("逻辑回归", "logistic"),                       "logistic",      "LogisticRegression", True),
    (("线性回归", "linear regression"),              "linear",        "LinearRegression", True),
    (("回归", "regression"),                         "linear",        "LinearRegression", True),
    (("kmeans", "k-means", "k均值"),                 "kmeans",        "KMeans", False),
    (("聚类", "cluster"),                            "kmeans",        "KMeans", False),
    (("孤立森林", "isolation"),                      "isolation",     "IsolationForest", False),
    (("异常检测", "离群", "异常点", "outlier"),       "isolation",     "IsolationForest", False),
)

# 不作为特征的列：行标识 / 日期时间
_ID_SUFFIX = ("_id",)
_DATE_TYPE_KEYS = ("date", "time", "timestamp")
_NUMERIC_TYPES = ("integer", "bigint", "numeric", "real", "double precision",
                  "smallint", "int", "float", "decimal", "double")

_TREE_MODEL_NAMES = {
    "random_forest": ("RandomForestRegressor", "RandomForestClassifier"),
    "decision_tree": ("DecisionTreeRegressor", "DecisionTreeClassifier"),
}


def is_numeric_type(dtype: str) -> bool:
    return str(dtype or "").strip().lower().split("(")[0].strip() in _NUMERIC_TYPES


def is_date_like(col_name: str, dtype: str = "") -> bool:
    t = str(dtype or "").lower()
    if any(k in t for k in _DATE_TYPE_KEYS):
        return True
    return str(col_name or "").lower().endswith(("_date", "_time", "_at"))


def is_id_like(col_name: str) -> bool:
    return str(col_name or "").lower().endswith(_ID_SUFFIX)


def col_type(columns: list[dict], name: str) -> str:
    return next((c.get("type") or "" for c in columns if c.get("name") == name), "")


def feature_pool(columns: list[dict]) -> list[str]:
    """可当特征的列：去掉 *_id 行标识与日期时间列（与前端展示口径一致）"""
    out = []
    for c in columns:
        name = c.get("name") or ""
        if not name or is_id_like(name) or is_date_like(name, c.get("type")):
            continue
        out.append(name)
    return out


def numeric_feature_pool(columns: list[dict]) -> list[str]:
    pool = feature_pool(columns)
    return [c["name"] for c in columns
            if c.get("name") in pool and is_numeric_type(c.get("type"))]


# ====== 问句解析 =========================================================

_FEATURE_MARKERS = ("根据", "依据", "按照", "基于", "以", "看")
_CLAUSE_SPLIT = re.compile(r"[，,。;；!！?？\n]")
_PREDICT_WORDS = ("预测", "预估", "推算")


def _clause_with_predict(query: str) -> str:
    for cl in _CLAUSE_SPLIT.split(query or ""):
        if any(w in cl for w in _PREDICT_WORDS):
            return cl
    return query or ""


def split_predict_grammar(query: str) -> tuple[str, str]:
    """解析「（根据/依据/以）A 预测 B」→ (A 短语, B 短语)。取不到就返回 ("", "")。

    这里**不做词表外的猜测**，宁可不给也不要给错。
    """
    clause = _clause_with_predict(query or "")
    m = re.search(r"(预测|预估|推算)", clause)
    if not m:
        return "", ""
    head, tail = clause[:m.start()], clause[m.end():]
    # 目标短语：掐掉"预测出/得到/一下"这类助词，剩下的交给列名命中计数
    # （"预测工单的实际产量" → "工单的实际产量"，由 resolve_column 命中 actual+qty 落到 actual_qty）
    target = re.sub(r"^(的|出|得到|得出|一下|它|这个|一个|每|各)", "", tail.strip())
    target = re.sub(r"[。，,；;]+$", "", target)
    feat = ""
    for mk in _FEATURE_MARKERS:
        idx = head.rfind(mk)
        if idx < 0:
            continue
        cand = head[idx + len(mk):].strip()
        # "用生产工单表训练一个随机森林模型" 这类：名词里带"表"/"模型"，是表不是列
        if cand and "表" not in cand and "模型" not in cand:
            feat = cand
            break
    return feat, target


def _token_match(needle: str, colname: str) -> bool:
    """英文词片段按"下划线分词整词"匹配，不做裸子串匹配。

    裸子串会闹笑话：「计划产量」的 plan 撞上 eqp_downtime_record.is_planned（planned≠plan），
    于是"缺计划产量"被判成"找到了 is_planned"，训练出一个布尔目标。按分词整词就干净了。
    """
    nl = (colname or "").lower()
    return needle == nl or needle in nl.split("_")


def _quantity_rank(colname: str) -> int:
    """越像"产出量"排得越靠前（打平时的确定序，避免结果随表顺序漂移）"""
    nl = (colname or "").lower()
    return next((i for i, k in enumerate(_QUANTITY_PREFERENCE) if k in nl), 50)


def unmatched_discriminator(phrase: str, column: str) -> str:
    """短语里的"限定词"（实际/计划/合格/不良/投入/可用…）在这列上有没有落点。

    返回第一个没落点的限定词（"实际产量"对上 plan_qty → 返回 "实际"），
    没有则返回空串。**这是把「目标列不在这张表里」判出来的关键**：
    "实际产量"只靠"产量"撞上 plan_qty 的 qty 是假命中，实际量根本不在这张表。
    """
    nl = (column or "").lower()
    for cn, needles in CN_EN_HINTS:
        if cn in _GENERIC_QUANTITY_WORDS or cn not in (phrase or ""):
            continue
        if not any(_token_match(n, nl) for n in needles):
            return cn
    return ""


def resolve_column(phrase: str, columns: list[dict], numeric_only: bool = False,
                   strict: bool = True) -> tuple[str, int]:
    """把中文短语落到真实列名上，返回 (列名, 命中分)。命中分 0 表示没把握。

    评分：每个词表命中 +1；列名原样出现在短语里 +3（用户直接写了英文列名）。
    打平时按 `_QUANTITY_PREFERENCE` 偏好"更像产出"的列。
    strict=True（默认）时跳过"有限定词没落点"的列 —— 见 unmatched_discriminator。
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return "", 0
    pl = phrase.lower()
    best, best_score, best_rank = "", 0, 99
    for c in columns:
        name = c.get("name") or ""
        if not name or (numeric_only and not is_numeric_type(c.get("type"))):
            continue
        if strict and unmatched_discriminator(phrase, name):
            continue
        nl = name.lower()
        score = 3 if (nl and nl in pl) else 0
        for cn, needles in CN_EN_HINTS:
            if cn in phrase and any(_token_match(n, nl) for n in needles):
                score += 1
        if not score:
            continue
        rank = _quantity_rank(name)
        if score > best_score or (score == best_score and rank < best_rank):
            best, best_score, best_rank = name, score, rank
    return best, best_score


def infer_model_name(query: str, target_numeric: bool) -> tuple[str, str]:
    """用户点名的算法 → (白名单模型名, 说明)。没点名返回 ("", "")。"""
    q = (query or "").lower()
    for words, mtype, prefix, supervised in ALGO_HINTS:
        if not any(w in q for w in words):
            continue
        if not supervised:
            return prefix, f"点名算法为「{words[0]}」"
        if mtype in _TREE_MODEL_NAMES:
            reg, clf = _TREE_MODEL_NAMES[mtype]
            return (reg if target_numeric else clf), f"点名算法为「{words[0]}」"
        if mtype == "logistic":
            return "LogisticRegression", f"点名算法为「{words[0]}」"
        return "LinearRegression", f"点名算法为「{words[0]}」"
    return "", ""


def _unsupervised_model(query: str) -> str:
    q = (query or "").lower()
    for words, mtype, prefix, supervised in ALGO_HINTS:
        if not supervised and any(w in q for w in words):
            return prefix
    return ""


def phrase_has_hint(phrase: str) -> bool:
    """短语里是否出现了词表里的业务词（有词才有"该不该在本表"的判据）。

    "工单状态"没有任何词表命中 → 不做判断，把选择权留给 LLM（那可能是分类任务）；
    "实际产量"命中 实际+产量 → 有判据，命不中真实列就能判定"本表没有这个量"。
    """
    return any(cn in (phrase or "") for cn, _ in CN_EN_HINTS)


_FEATURE_SPLIT = re.compile(r"[和与及跟、,，+＋]|以及")


def resolve_feature_columns(phrase: str, columns: list[dict]) -> list[str]:
    """把「根据 A 和 B 预测 C」里的 A、B 分别落到列上（列可以是类别列）。

    「根据抽检数量和检验结论预测不良数量」→ [sample_qty, inspection_result]；
    只当特征用，所以这里不要求数值：trainer 会把类别列做标签编码。
    """
    out: list[str] = []
    for part in _FEATURE_SPLIT.split(phrase or ""):
        part = (part or "").strip()
        if not part:
            continue
        col, _ = resolve_column(part, columns, numeric_only=False)
        if col and col not in out:
            out.append(col)
    if not out:
        col, _ = resolve_column(phrase or "", columns, numeric_only=False)
        if col:
            out.append(col)
    return out


# ====== 方案构造 =========================================================

def build_fallback_intent(query: str, table: str, columns: list[dict]) -> dict:
    """不依赖 LLM 的兜底方案：从问句 + 真实列结构直接拼一份 intent。

    契约与 LLM 版本一致，可交给 `validate_ml_intent` 校验后再执行。
    """
    numeric = numeric_feature_pool(columns)
    pool = feature_pool(columns)
    feat_phrase, tgt_phrase = split_predict_grammar(query)
    notes: list[str] = []

    target, t_score = resolve_column(tgt_phrase, columns, numeric_only=True)
    unresolved = ""
    if target:
        notes.append(f"目标来自问句「{tgt_phrase}」→ {target}")
    elif tgt_phrase and phrase_has_hint(tgt_phrase):
        # 用户明确说了要预测什么，但这张表里没有 → **绝不改成别的列充数**，
        # 挂 `_target_unresolved` 交给上游去别的表找或如实告知。
        unresolved = tgt_phrase
        notes.append(f"问句要预测的「{tgt_phrase}」在本表找不到对应数值列")
    if not target and not unresolved:
        for kw in ("actual", "output", "qty", "amount", "count", "total"):
            hit = [c for c in numeric if kw in c.lower()]
            if hit:
                target = hit[0]
                notes.append(f"目标由数值列兜底 → {target}")
                break
        if not target and numeric:
            target = numeric[0]
            notes.append(f"目标由首个数值列兜底 → {target}")

    # 特征：问句点名的"依据列"打头（可能有多个，用"和/与/、"连接），再补其它数值列
    features: list[str] = []
    if feat_phrase:
        for fcol in resolve_feature_columns(feat_phrase, columns):
            if fcol != target and fcol not in features:
                features.append(fcol)
        if features:
            notes.append(f"特征来自问句「{feat_phrase}」→ {'、'.join(features)}")
    for c in numeric:
        if c != target and c not in features:
            features.append(c)
    if not features:
        features = [c for c in pool if c != target][:8]
    features = features[:8]

    unsup = _unsupervised_model(query)
    # 目标列待跨表解析时也算有监督任务：别按"没目标就是聚类"来理解，
    # 否则 _respond_ml 会把刚刚跨表求出来的目标又抹掉（踩过）。
    target_type = col_type(columns, target)
    assume_numeric = (not target_type) or is_numeric_type(target_type)
    named_model, why = infer_model_name(query, target_numeric=assume_numeric)
    model_name = named_model
    if unsup:
        # 聚类/异常检测没有目标列，target 必须留空，否则训练器会当成有监督任务
        model_name, target, why = unsup, "", "问句为无监督任务（聚类/异常检测）"
    if not model_name:
        model_name = "RandomForestRegressor" if (target or unresolved) else "KMeans"
        why = f"未点名算法，按任务选 {model_name}"
    notes.append(f"算法：{model_name}（{why}）")

    intent = {
        "intent_type": "train",
        "user_summary": f"对 {table} 建模" + (f"：预测 {target}" if target else "：无监督分析"),
        "data_request": {"table": table, "fields": features, "target": target,
                         "filter": "", "limit": 5000},
        "feature_engineering": [],
        "model_training": {"model": model_name, "params": {}},
        "output_spec": {"metrics": [], "max_rows": 100},
        "_source": "rule_fallback",
        "_notes": notes,
    }
    if unresolved:
        intent["_target_unresolved"] = unresolved
    logger.info("ML 规则兜底方案: table=%s target=%s features=%s model=%s unresolved=%s",
                table, target, features, model_name, unresolved or "-")
    return intent


def apply_query_override(intent: dict, query: str, columns: list[dict]) -> tuple[dict, str]:
    """用问句里的「根据A预测B / 点名算法」纠正一份方案（LLM 的或兜底的）。

    只做有把握的干预：
      1. B（要预测的）在真实列里命中分 ≥2 → 强制 target=B；
         命不中但用户明说了 B → 记 `_target_unresolved`，不让别的列顶替；
      2. A（依据）命得中 → 保证它在 features 里且排最前；
      3. 特征里的目标列 / *_id / 日期列一律剔除（目标列当特征＝自证，标识列无信息量）；
      4. 用户点名了算法 → 按 target 是否数值覆盖模型选择。
    """
    if not isinstance(intent, dict):
        return intent, ""
    dr = intent.get("data_request")
    if not isinstance(dr, dict):
        return intent, ""

    feat_phrase, tgt_phrase = split_predict_grammar(query)
    notes: list[str] = []

    tgt_col, t_score = resolve_column(tgt_phrase, columns, numeric_only=True)
    if not tgt_col:
        tgt_col, t_score = resolve_column(tgt_phrase, columns, numeric_only=False)
    if tgt_col:
        old = dr.get("target") or ""
        if old != tgt_col:
            dr["target"] = tgt_col
            notes.append(f"目标校正 {old or '空'} → {tgt_col}")
        intent.pop("_target_unresolved", None)
    elif tgt_phrase and phrase_has_hint(tgt_phrase):
        # 有词表判据却在本表命不中（"实际产量"对工单表）→ 挂起，交给上层跨表解析。
        # 没有任何词表命中时（如"工单状态"）不动手：那可能是分类目标，选择权留给 LLM。
        dr["target"] = ""
        intent["_target_unresolved"] = tgt_phrase
        notes.append(f"问句要预测的「{tgt_phrase}」在本表无对应列，已挂起待跨表解析")
    target = dr.get("target") or ""

    feat_cols = resolve_feature_columns(feat_phrase, columns) if feat_phrase else []
    feat_cols = [c for c in feat_cols if c != target]
    if feat_cols:
        fields = [f for f in (dr.get("fields") or []) if isinstance(f, str)]
        dr["fields"] = feat_cols + [f for f in fields if f not in feat_cols and f != target]
        notes.append(f"特征补入 {'、'.join(feat_cols)}")

    real = {c.get("name") for c in columns}
    drop_set = {c.get("name") for c in columns
                if is_id_like(c.get("name")) or is_date_like(c.get("name"), c.get("type"))}
    kept = [f for f in (dr.get("fields") or []) if f in real and f != target and f not in drop_set]
    if kept != list(dr.get("fields") or []):
        notes.append("剔除特征中的目标列 / 标识列 / 日期列")
    dr["fields"] = kept

    mt = intent.get("model_training")
    if not isinstance(mt, dict):
        mt = {}
        intent["model_training"] = mt
    target_type = col_type(columns, target)
    # 目标待跨表解析时类型未知 → 按数值回归算（跨表找到的目标列一定是数值列），
    # 否则"随机森林"会被选成分类器，等跨表拿到数值目标时已经晚了。
    assume_numeric = (not target_type) or is_numeric_type(target_type)
    named, why = infer_model_name(query, target_numeric=assume_numeric)
    if named and named != mt.get("model"):
        notes.append(f"模型校正 {mt.get('model') or '空'} → {named}")
        mt["model"] = named
    elif why:
        notes.append(why)

    if notes:
        logger.info("ML 方案按问句校正: %s", "；".join(notes))
    return intent, "；".join(notes)


# ====== 跨表：目标列不在这张表里 =========================================

def _pick_join_key(base_columns: list[str], other_columns: list[str], base_table: str) -> str:
    """在两个表的公共 *_id 列里挑连接键。

    优先"键名是基表名的一部分"（work_order_id ⊂ mes_work_order），
    这样 mes_work_order ⋈ mes_process_output 会选 work_order_id 而不是 product_id。
    """
    shared = [c for c in other_columns if c in set(base_columns) and is_id_like(c)]
    if not shared:
        return ""
    base = base_table.split(".")[-1].lower()
    for k in shared:
        stem = k.lower()[:-3]
        if stem and stem in base:
            return k
    return shared[0]


def find_target_elsewhere(phrase: str, schema_map: dict, exclude: tuple[str, ...] = ()) -> list[tuple]:
    """在全部表的**数值列**里找这个短语，返回 [(表, 列, 命中分), ...]。

    这里刻意用 strict=False：本表要严格（"实际产量"在工单表里没有就是没有），
    但去别的表找恰恰要能接受"最接近那个量"（工序产量表的 good_qty 就是工单的实际产出）。
    拼不拼得成由调用方的连接键唯一性把关。

    schema_map: {表名: {"columns": [列名...], "numeric": [数值列名...]}}
    传进来的列没有类型，这里用 numeric 列表当类型信息。
    """
    hits = []
    for tname, meta in (schema_map or {}).items():
        if tname in exclude:
            continue
        cols = [{"name": c, "type": "integer" if c in set(meta.get("numeric") or []) else "text"}
                for c in (meta.get("columns") or [])]
        col, score = resolve_column(phrase, cols, numeric_only=True, strict=False)
        if col and score >= 1:
            hits.append((tname, col, score, _quantity_rank(col)))
    # 先按语义命中分、再按"像不像产出"排，保证结果不随表的枚举顺序漂移
    hits.sort(key=lambda x: (-x[2], x[3], x[0], x[1]))
    return [(t, c, s) for t, c, s, _ in hits]


def try_cross_table_target(query: str, table: str, columns: list[dict],
                           schema_map: dict, key_check=None) -> tuple[dict, str, str]:
    """目标列不在本表时，试着从别的表按公共主键汇总过来。

    返回 (join_spec | None, 说明, 失败时的建议话术)。
    join_spec = {"table": 汇总表, "col": 数值列, "agg": "SUM", "on": 连接键}

    key_check(table, key) -> bool：连接键在**基表**里是否唯一。唯一才说明基表是
    "一条记录一行"的主体侧，对多行明细表做汇总才有意义（工单 ⋈ 工序产量）。
    不唯一时（设备停机记录按 line_id 去 aggregation 产量）就是硬凑，宁可不出方案。
    """
    _, tgt_phrase = split_predict_grammar(query)
    if not tgt_phrase:
        return None, "", ""
    hits = find_target_elsewhere(tgt_phrase, schema_map, exclude=(table,))
    if not hits:
        return None, "", ""
    base_cols = [c.get("name") for c in columns]
    for other, col, _score in hits:
        meta = schema_map.get(other) or {}
        other_cols = meta.get("columns") or []
        key = _pick_join_key(base_cols, other_cols, table)
        if not key:
            continue
        if key_check is not None:
            try:
                if not key_check(table, key):
                    logger.info("ML 跨表候选被否（%s.%s 不唯一，主体侧不成立）", table, key)
                    continue
            except Exception as e:
                logger.info("ML 跨表键唯一性检查失败，放弃：%s", e)
                continue
        spec = {"table": other, "col": col, "agg": "SUM", "on": key}
        note = (f"「{tgt_phrase}」不在 {table} 里，已按 {key} 从 {other} 汇总 {col}"
                f"（{spec['agg']}）后作为预测目标")
        logger.info("ML 跨表取数: %s", note)
        return spec, note, ""
    return None, "", ""


def cross_table_suggestion(phrase: str, table: str, schema_map: dict) -> str:
    """跨表也拼不出来时，给用户一句能照抄的问法。"""
    hits = find_target_elsewhere(phrase or "数值", schema_map, exclude=(table,))
    if not hits:
        return ""
    other, col, _ = hits[0]
    numeric = (schema_map.get(other) or {}).get("numeric") or []
    feat = [c for c in numeric if c != col][:1]
    feat_txt = f"，根据 {feat[0]}" if feat else ""
    return f"改问：用 {other} 训练模型预测 {col}{feat_txt}"
