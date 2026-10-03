"""Vequo 客服中心 — AI 客服 + 人工客服工单（服务端持久化）

与「智能问析」链路（LLMService 全量 SQL 分析）的边界：
  - 本模块的 AI 客服只回答「平台使用 / 业务知识 / 账户」类日常问题，
    携带会话内多轮记忆（前端传 history），不查数据库、不写对话存储；
  - AI 自评（need_human）：遇到权限审批、账号异常、投诉或超出知识范围的问题，
    返回 need_human=True，由前端展示「转人工」入口；
  - 人工客服为服务端文件存储（support_tickets.json），用户与管理员真正互通，
    含双向已读进度（user_read_ts / admin_read_ts）用于未读红点计数。
"""

import json
import os
import re
import threading
import time
import uuid

_LOCK = threading.Lock()
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_STORE_PATH = os.path.join(_DATA_DIR, "support_tickets.json")

# 单会话消息上限（防缓存/文件无限膨胀）
MAX_MSGS_PER_CONVO = 200
# 单条消息长度上限
MAX_TEXT_LEN = 2000

# 消息类型：text = 普通对话；feedback = 「业务知识有误」反馈（在会话流里渲染为大号待办卡）
MSG_TEXT = "text"
MSG_FEEDBACK = "feedback"
# 反馈处理状态
FB_OPEN = "open"            # 未处理
FB_DONE = "done"            # 已处理（管理员已修正知识）
FB_REJECT = "rejected"      # 反馈不成立（经核实该知识无误）
# 仍算「待处理」的状态（用于会话列表的待办角标）
FB_PENDING = (FB_OPEN,)
# 全部合法状态
FB_STATES = (FB_OPEN, FB_DONE, FB_REJECT)
# AI 客服携带的历史轮数上限（服务端二次裁剪，前端已裁一次）
MAX_AI_HISTORY = 12


# ── 存储层 ────────────────────────────────────────────────

def _load() -> list:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def _save(convos: list) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(convos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _STORE_PATH)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _msg_brief(m: dict) -> str:
    """消息摘要（会话列表一行用）。反馈消息显示成「【业务知识反馈】条目名」便于管理员一眼识别。"""
    if m.get("kind") == MSG_FEEDBACK:
        title = str((m.get("meta") or {}).get("item_title") or "").strip()[:60]
        return f"【业务知识反馈】{title}" if title else "【业务知识反馈】"
    return str(m.get("text") or "")[:80]


def _convo_summary(c: dict, unread_who: str = "") -> dict:
    """列表页用的会话摘要（不含消息体，减小载荷）"""
    out = {
        "id": c["id"],
        "user_key": c["user_key"],
        "user_name": c["user_name"],
        "status": c["status"],
        "created_at": c["created_at"],
        "updated_at": c["updated_at"],
        "last_text": _msg_brief(c["messages"][-1]) if c["messages"] else "",
        "message_count": len(c["messages"]),
    }
    if unread_who == "user":
        out["unread"] = sum(1 for m in c["messages"] if m["sender"] == "agent" and m["ts"] > c.get("user_read_ts", 0))
    elif unread_who == "admin":
        out["unread"] = sum(1 for m in c["messages"] if m["sender"] == "user" and m["ts"] > c.get("admin_read_ts", 0))
        # 待处理的知识反馈数（管理员会话列表角标：提醒还有几条「业务知识有误」没处理）
        out["pending_feedback"] = sum(1 for m in c["messages"] if is_feedback_open(m))
    return out


def is_feedback_open(m: dict) -> bool:
    return m.get("kind") == MSG_FEEDBACK and str((m.get("meta") or {}).get("status") or FB_OPEN) == FB_OPEN


def list_convos() -> list:
    with _LOCK:
        convos = _load()
    return sorted(convos, key=lambda c: c["updated_at"], reverse=True)


def get_convo(cid: str) -> dict | None:
    with _LOCK:
        for c in _load():
            if c["id"] == cid:
                return c
    return None


def create_convo(user_key: str, user_name: str, seed_text: str = "") -> dict:
    """创建工单会话；seed_text 用于「转人工」时携带 AI 阶段的上下文摘要"""
    now = time.time() * 1000
    convo = {
        "id": f"sup-{_uid()}",
        "user_key": user_key,
        "user_name": user_name,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "user_read_ts": now,
        "admin_read_ts": 0,
        "messages": [],
    }
    if seed_text:
        convo["messages"].append({
            "id": _uid(), "sender": "user", "sender_name": user_name,
            "text": seed_text[:MAX_TEXT_LEN], "ts": now,
        })
    with _LOCK:
        convos = _load()
        # 已有未关闭会话则复用（避免同一用户重复开单）
        cur = next((c for c in convos if c["user_key"] == user_key and c["status"] == "open"), None)
        if cur:
            if seed_text:
                cur["messages"].append(convo["messages"][0])
                cur["updated_at"] = now
                cur["messages"] = cur["messages"][-MAX_MSGS_PER_CONVO:]
            _save(convos)
            return cur
        convos.append(convo)
        _save(convos)
    return convo


def add_message(cid: str, sender: str, sender_name: str, text: str,
                kind: str = MSG_TEXT, meta: dict | None = None) -> dict | None:
    """sender: 'user'（工单归属者本人）| 'agent'（人工客服/管理员）
    kind: 'text' 普通对话 | 'feedback' 业务知识反馈（前端渲染为大号待办卡）
    """
    text = (text or "").strip()[:MAX_TEXT_LEN]
    if not text:
        return None
    now = time.time() * 1000
    msg = {"id": _uid(), "sender": sender, "sender_name": sender_name, "text": text, "ts": now}
    if kind and kind != MSG_TEXT:
        msg["kind"] = kind
    if meta:
        msg["meta"] = meta
    with _LOCK:
        convos = _load()
        c = next((c for c in convos if c["id"] == cid), None)
        if not c:
            return None
        c["messages"].append(msg)
        c["messages"] = c["messages"][-MAX_MSGS_PER_CONVO:]
        c["updated_at"] = now
        if sender == "user":
            c["user_read_ts"] = now  # 自己发的自动已读
        else:
            c["admin_read_ts"] = now
        _save(convos)
    return msg


def add_feedback(user_key: str, user_name: str, item: dict, note: str) -> dict:
    """业务知识「有误」反馈 —— 不新开聊天框，直接作为一条大号待办消息追加进该用户的人工客服会话。

    item: {key, kind, title, scene, table, desc}（来自业务知识详情面板）
    note: 用户填写的问题说明
    返回 {conversation, message}
    """
    note = (note or "").strip()[:MAX_TEXT_LEN]
    convo = create_convo(user_key, user_name)          # 已有未关闭会话则复用
    meta = {
        "item_key": str(item.get("key") or "")[:200],
        "item_kind": str(item.get("kind") or "")[:40],
        "item_title": str(item.get("title") or "")[:200],
        "item_scene": str(item.get("scene") or "")[:80],
        "item_table": str(item.get("table") or "")[:120],
        "item_desc": str(item.get("desc") or "")[:600],
        "note": note,
        "status": FB_OPEN,
        "admin_note": "",
        "handled_at": 0,
        "handled_by": "",
    }
    text = note or "（用户未填写具体说明，请查看知识条目后核实）"
    msg = add_message(convo["id"], "user", convo["user_name"], text,
                      kind=MSG_FEEDBACK, meta=meta)
    if not msg:  # 理论上不会发生（text 非空），兜底重建
        return {"conversation": get_convo(convo["id"]) or convo, "message": None}
    return {"conversation": get_convo(convo["id"]) or convo, "message": msg}


def set_feedback_status(mid: str, status: str, admin_note: str = "", admin_name: str = "",
                        reply: str = "") -> dict | None:
    """管理员处理业务知识反馈：标记 已处理(done) / 未处理(open) / 反馈不成立(rejected)。

    按消息 id 全局定位（管理员不必先选中会话），返回 {conversation, message, reply_message}。

    reply：**给用户的回复**。反馈可能是误报（用户看错了口径）——此时管理员选「知识无误」，
    这段回复会作为一条「人工客服」消息真正发进会话，用户能看到并收到未读提醒；
    不传则不产生回复消息，只改状态。
    """
    status = status if status in FB_STATES else FB_OPEN
    admin_note = (admin_note or "").strip()[:MAX_TEXT_LEN]
    reply = (reply or "").strip()[:MAX_TEXT_LEN]
    now = time.time() * 1000
    with _LOCK:
        convos = _load()
        for c in convos:
            for m in c["messages"]:
                if m.get("id") != mid or m.get("kind") != MSG_FEEDBACK:
                    continue
                meta = m.setdefault("meta", {})
                meta["status"] = status
                meta["admin_note"] = admin_note
                meta["handled_at"] = now if status != FB_OPEN else 0
                meta["handled_by"] = admin_name if status != FB_OPEN else ""
                reply_msg = None
                if reply:
                    # 直接构造消息（不能用 add_message：它内部会再次取 _LOCK，普通 Lock 不可重入）
                    reply_msg = {
                        "id": _uid(), "sender": "agent",
                        "sender_name": admin_name or "人工客服",
                        "text": reply, "ts": now,
                    }
                    c["messages"].append(reply_msg)
                    c["messages"] = c["messages"][-MAX_MSGS_PER_CONVO:]
                    c["admin_read_ts"] = now      # 自己发的自动已读（用户侧未读会 +1）
                c["updated_at"] = now
                _save(convos)
                return {"conversation": c, "message": m, "reply_message": reply_msg}
    return None


def mark_read(cid: str, who: str) -> bool:
    now = time.time() * 1000
    with _LOCK:
        convos = _load()
        c = next((c for c in convos if c["id"] == cid), None)
        if not c:
            return False
        key = "user_read_ts" if who == "user" else "admin_read_ts"
        c[key] = now
        _save(convos)
    return True


def close_convo(cid: str) -> bool:
    with _LOCK:
        convos = _load()
        c = next((c for c in convos if c["id"] == cid), None)
        if not c:
            return False
        c["status"] = "closed"
        _save(convos)
    return True


def unread_total(who: str, user_key: str = "") -> int:
    """who='user'：某用户的客服回复未读数；who='admin'：管理员侧所有用户新消息未读数"""
    with _LOCK:
        convos = _load()
    total = 0
    for c in convos:
        if who == "user":
            if c["user_key"] != user_key:
                continue
            total += sum(1 for m in c["messages"] if m["sender"] == "agent" and m["ts"] > c.get("user_read_ts", 0))
        else:
            if c["status"] != "open":
                continue
            total += sum(1 for m in c["messages"] if m["sender"] == "user" and m["ts"] > c.get("admin_read_ts", 0))
    return total


# ── AI 客服 ───────────────────────────────────────────────

SUPPORT_SYSTEM_PROMPT = """你是「Vequo 维阔」平台的智能客服，只负责解答用户日常使用问题。

你了解的平台功能（回答时以此为准）：
- 总览：系统运行状态与数据概览、运营报告。
- 智能问析：核心功能，自然语言提问 → 自动生成 SQL 查询真实数据，支持多轮对话、表格/图表展示、归因分析、ML 预测。
- 业务知识：业务场景/对象/规则/术语管理，条目可星标收藏，可提交纠错反馈。
- 账户设置：改昵称、头像、密码。
- 管理专区（侧边栏独立分组，只有管理员看得见，普通员工没有这些页面）：
  · 数据资源：维护数据源、切换/导入/删除数据库、浏览表结构与字段说明、双击单元格改样例数据。
  · 指标口径：固定指标计算规则（如良率统计范围），保证回答口径一致。
  · 权限管理：数据集/行/列/指标四级权限，敏感变更走审批。
  · 系统设置：模型参数与数据源配置。
  普通员工问「怎么看数据库、表在哪、字段什么意思」时，不要引导他去管理专区 ——
  他看不到这些页面；引导他用「智能问析」直接提问，或去「业务知识」页查业务对象与术语。

回答要求：
1. 简洁友好，中文回答，分步骤说明操作路径（如「进入 XX 页 → 点击 XX」）。
2. 只答平台使用、业务知识概念、账户相关的问题。用户问具体数据（如"上个月产量多少"）时，
   不要编造数据，引导其前往「智能问析」提问。
3. 不确定或超出能力范围时如实说明，不要猜测。

需要转人工客服（need_human）的判定，以下情况必须为 true：
- 用户明确要求人工客服/真人客服；
- 涉及账号异常（被锁定/禁用/无法登录且演示账号无法解决）、数据权限开通与审批、投诉、
  数据错误纠责、企业内部流程等需要管理员处理的事务；
- 用户在你解释后仍表示不满，或问题你确实无法给出可靠答案。"""

# 纯文本模式（JSON mode 不被支持时的兜底）：正文 + 末行标记
SUPPORT_PLAIN_PROMPT = SUPPORT_SYSTEM_PROMPT + """

输出格式（务必严格遵守）：
- 先直接输出给用户的回答正文，不要输出 JSON、不要输出任何解释；
- 仅当「用户本人的诉求」属于上面转人工条件时，才在正文最后另起一行单独输出标记 [[HUMAN]]；
  不需要则不要输出该标记。

关于 [[HUMAN]] 的判定尺度（重要，不要过度触发）：
- 你在正文里顺口建议用户「去申请权限 / 联系管理员开通 / 找管理员办理」，
  这类只是常规指引，**不算**转人工，不要输出 [[HUMAN]]；
- 只有用户本人的问题本身就是需要管理员介入的事务（明确要真人、账号被锁定等异常、
  投诉或数据纠责、你确实无可靠答案且已重复解释过）才输出 [[HUMAN]]。
- 平台功能咨询（如「支持哪些数据库」「能不能上传 Excel」「怎么导出」）属于正常解答范围，
  即使你无法 100% 确认细节，也只如实说明并给出查看路径，**不要**输出 [[HUMAN]]。"""

# 显式转人工关键词（命中直接转，不再走 LLM）。
# 刻意不含「人工」裸词——否则「人工智能是什么」会被误判为转人工需求。
_HUMAN_KEYWORDS = re.compile(
    r"人工客服|人工服务|转人工|转接人工|转接客服|帮我转接|真人客服|找真人|找人工|要人工"
    r"|人工在吗|有人工吗|人工怎么联系|联系客服|客服在吗|找客服|客服电话"
)

_FALLBACK_ANSWER = (
    "抱歉，客服 AI 这次没能生成回答。\n"
    "可以换一种说法再问一次；权限申请、账号异常等需要管理员处理的问题，建议直接转接人工客服留言。"
)

_LOG_DIR = os.path.join(os.path.dirname(_DATA_DIR), "logs")


def _cap_json(entry: dict, limit: int = 4000) -> str:
    """把日志条目序列化成**一行合法 JSON**（support_ai.log 是 JSONL）。

    原先 `json.dumps(entry)[:4000]` 是字符级硬切，会切出非法 JSON，读取端
    （read_diag_log）解析失败后只能退化成 {"raw_line": ...}，丢掉结构。
    现改为：超长时逐步折半「最长的字符串字段」（模型原始返回/报错文本通常就是
    那个超长字段），始终保证输出可被 json.loads 解析。
    """
    s = json.dumps(entry, ensure_ascii=False)
    if len(s) <= limit:
        return s
    work = dict(entry)
    for _ in range(24):
        k = max((kk for kk, vv in work.items() if isinstance(vv, str)),
                key=lambda kk: len(work[kk]), default=None)
        if k is None or len(work[k]) <= 1:
            break
        work[k] = work[k][: max(1, len(work[k]) // 2)]
        s = json.dumps(work, ensure_ascii=False)
        if len(s) <= limit:
            return s
    # 兜底：仍放不下（如嵌套结构巨大）→ 只保留定位信息，仍是合法 JSON
    return json.dumps({"ts": work.get("ts"), "kind": work.get("kind"),
                       "note": "entry too large; long fields truncated"},
                      ensure_ascii=False)


def _log_failure(kind: str, detail: dict) -> None:
    """把 AI 调用/解析失败的原始信息落盘（backend/logs/support_ai.log），便于排查"""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **detail}
        with open(os.path.join(_LOG_DIR, "support_ai.log"), "a", encoding="utf-8") as f:
            f.write(_cap_json(entry, 4000) + "\n")
    except Exception:
        pass


def read_diag_log(limit: int = 20) -> list:
    """读取客服 AI 失败日志尾部（供管理员排查：模型原始返回/异常原因）"""
    path = os.path.join(_LOG_DIR, "support_ai.log")
    out: list = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f.readlines()[-max(1, min(limit, 100)):]:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({"raw_line": line})
    except FileNotFoundError:
        return []
    except Exception:
        pass
    return out


def _extract_text(resp) -> str:
    """取 LLM 响应正文，兼容三种形态：
    ① content 为字符串；② content 为内容块列表；
    ③ content 为空但 reasoning_content 里有内容（推理模型把 token 预算耗在思考上，
       或中转把正文塞进 reasoning_content）——否则会被误判成"AI 返回为空"
    """
    raw = getattr(resp, "content", "")
    if isinstance(raw, list):
        parts: list[str] = []
        for b in raw:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        raw = "".join(parts)
    text = str(raw or "")
    if text.strip():
        return text
    for attr in ("additional_kwargs", "response_metadata"):
        d = getattr(resp, attr, None)
        if not isinstance(d, dict):
            continue
        for k in ("reasoning_content", "reasoning", "content", "text"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return text


def _ask_llm(history: list[dict], json_mode: bool, max_tokens: int = 1400) -> str:
    """调用一次客服 LLM 返回原始文本。max_tokens 给足，避免推理模型思考吃满后正文为空。

    注意（2026-09-10 实测阿里云百炼 DashScope）：模型是推理模型（返回 reasoning_content），
    且使用 response_format=json_object 时，厂商会校验 **非 system 消息**里必须出现 "json" 字样，
    否则直接 400：'messages' must contain the word 'json' …。系统提示词里写 "JSON" 不算数，
    因此 json_mode=True 时在最后一条用户消息末尾显式补一句 json 要求。
    """
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
    from agent.llm_service import _make_llm
    llm = _make_llm(temp=0.3, max_tokens=max_tokens, json_mode=json_mode,
                    timeout=60, max_retries=1)
    msgs: list = [SystemMessage(content=SUPPORT_SYSTEM_PROMPT if json_mode else SUPPORT_PLAIN_PROMPT)]
    for m in history[-MAX_AI_HISTORY:]:
        content = str(m.get("content", ""))[:MAX_TEXT_LEN]
        if not content:
            continue
        role = m.get("role")
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    if not any(isinstance(x, HumanMessage) for x in msgs):
        msgs.append(HumanMessage(content="你好"))
    if json_mode:
        # 满足厂商 json_object 校验（必须是 user/assistant 消息里出现 json 字样）
        hint = '\n\n（要求：直接返回 json，形如 {"answer": "给用户的回答", "need_human": false, "reason": "简短原因"}，不要输出其他内容）'
        last_user = [i for i, x in enumerate(msgs) if isinstance(x, HumanMessage)][-1]
        msgs[last_user] = HumanMessage(content=str(msgs[last_user].content) + hint)
    resp = llm.invoke(msgs)
    return _extract_text(resp)


def ai_support_reply(history: list[dict]) -> dict:
    """客服 AI：携带会话内多轮记忆；返回 {answer, need_human, reason}

    两段式（顺序很重要）：
      ① 纯文本 + [[HUMAN]] 标记 —— 主通道。不依赖厂商对 response_format 的支持，
         实测阿里云百炼该模型此方式稳定返回正文；
      ② JSON 模式 —— 兜底。仅当纯文本拿不到正文时使用（此时会在用户消息里补 json 字样）。
    两段都失败才给兜底文案（need_human=True，仍提供人工入口）。
    """
    # 规则短路：明确要人工直接转，省一次 LLM 调用
    last_user = next((m.get("content", "") for m in reversed(history) if m.get("role") == "user"), "")
    if last_user and _HUMAN_KEYWORDS.search(last_user):
        return {"answer": "好的，正在为你转接人工客服。你可以直接描述问题，管理员会尽快回复；也可以点击下方按钮进入人工客服会话。", "need_human": True, "reason": "用户明确要求人工客服"}

    # ① 纯文本 + [[HUMAN]] 标记（主通道）
    try:
        text = _ask_llm(history, json_mode=False).strip()
        need_human = "[[HUMAN]]" in text.upper()
        answer = text.replace("[[HUMAN]]", "").replace("[[human]]", "").strip()
        if not answer:
            # 模型可能仍按 JSON 包了一层
            data = _loads_lenient(text)
            answer = str(data.get("answer") or "").strip()
            need_human = need_human or bool(data.get("need_human"))
        if answer:
            return {"answer": answer, "need_human": need_human, "reason": "plain-mode"}
        _log_failure("plain_empty", {"raw": text[:1200], "last_user": last_user[:200]})
    except Exception as e:
        _log_failure("plain_error", {"error": str(e)[:500], "last_user": last_user[:200]})

    # ② JSON 模式兜底
    try:
        raw = _ask_llm(history, json_mode=True)
        data = _loads_lenient(raw)
        answer = str(data.get("answer") or "").strip()
        if answer:
            return {"answer": answer, "need_human": bool(data.get("need_human")),
                    "reason": str(data.get("reason") or "").strip()}
        _log_failure("json_empty", {"raw": raw[:1200], "last_user": last_user[:200]})
    except Exception as e:
        _log_failure("json_error", {"error": str(e)[:500], "last_user": last_user[:200]})

    return {"answer": _FALLBACK_ANSWER, "need_human": True, "reason": "AI 未返回可用内容，已提供人工入口"}


def _loads_lenient(text: str):
    """宽容解析 LLM 输出的 JSON（截取首尾花括号、剥 markdown 代码块）"""
    if not text:
        return {}
    text = text.strip()
    # 剥 ```json ... ```
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass
    return {}
