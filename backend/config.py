"""项目配置 — 从 .env 加载敏感信息，支持环境变量覆盖"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ENV_PATH = Path(__file__).with_name(".env")

# ── LLM 配置 (支持 OpenAI 兼容接口, 如 DeepSeek) ──
LLM_CONFIG = {
    "model":       os.getenv("LLM_MODEL", "deepseek-v4-flash"),
    "api_key":     os.getenv("LLM_API_KEY", ""),
    "base_url":    os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
    "max_tokens":  int(os.getenv("LLM_MAX_TOKENS", "8192")),
    # SQL 生成/口径推断专用模型（2026-09-14，模型分工）：
    # 空 = 跟随 model（旧行为，零回归）。设置后仅 SQL 生成链路改用它 ——
    # 意图分类、摘要、推荐追问等轻任务仍用 model，避免用 flash 档模型去干
    # "手写整条 SQL" 这种最难的任务（实测会编造列名/猜错外键/漏 LIMIT）。
    "sql_model":   os.getenv("LLM_SQL_MODEL", ""),
}

# ── 系统内置默认（冻结，用户在前端切换不影响此处；点“恢复默认”时回写 LLM_CONFIG）──
LLM_DEFAULT = {
    "model":       os.getenv("LLM_DEFAULT_MODEL", "deepseek-v4-flash"),
    "api_key":     os.getenv("LLM_DEFAULT_API_KEY", ""),
    "base_url":    os.getenv("LLM_DEFAULT_BASE_URL", "https://api.deepseek.com/v1"),
    "temperature": float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.2")),
    "max_tokens":  int(os.getenv("LLM_DEFAULT_MAX_TOKENS", "8192")),
    "sql_model":   os.getenv("LLM_DEFAULT_SQL_MODEL", ""),
}


def get_llm_config() -> dict:
    """返回当前 LLM 配置（运行时可变，供各模块读取）"""
    return LLM_CONFIG


def get_llm_default() -> dict:
    """返回系统内置默认配置（冻结副本，调用方不应修改）"""
    return dict(LLM_DEFAULT)


def restore_llm_default() -> None:
    """将当前生效配置恢复为系统内置默认（冻结的 LLM_DEFAULT），并持久化。

    用户在前端切换到其他模型后，可随时点“恢复默认”回到出厂 DeepSeek 配置，
    且不会破坏 LLM_DEFAULT_*（冻结区）。
    """
    for key in ("model", "api_key", "base_url", "temperature", "max_tokens", "sql_model"):
        LLM_CONFIG[key] = LLM_DEFAULT[key]
    _persist_llm_config()


def update_llm_config(partial: dict) -> None:
    """运行时更新 LLM 配置（热生效，无需重启）。

    所有模块通过 `from config import LLM_CONFIG` 持有同一引用，
    因此 update 后立即全局生效。同时持久化到 .env，重启后保持一致。
    """
    allowed = {"model", "api_key", "base_url", "temperature", "max_tokens", "sql_model"}
    for key, value in partial.items():
        if key in allowed and value is not None and str(value).strip() != "":
            if key == "temperature":
                # 钳制到 [0, 2]：避免异常值（如负数或过大）导致模型调用失败
                LLM_CONFIG[key] = min(2.0, max(0.0, float(value)))
            elif key == "max_tokens":
                # 钳制到 [256, 32768]：避免异常值（负数/超大）导致模型调用失败或资源浪费
                LLM_CONFIG[key] = max(256, min(32768, int(value)))
            else:
                LLM_CONFIG[key] = str(value).strip()
    _persist_llm_config()


def _persist_llm_config() -> None:
    """把当前 LLM_CONFIG 写入 .env（LLM_ 前缀）"""
    updates = {
        "LLM_MODEL": LLM_CONFIG["model"],
        "LLM_API_KEY": LLM_CONFIG["api_key"],
        "LLM_BASE_URL": LLM_CONFIG["base_url"],
        "LLM_TEMPERATURE": str(LLM_CONFIG["temperature"]),
        "LLM_SQL_MODEL": LLM_CONFIG.get("sql_model", ""),
        "LLM_MAX_TOKENS": str(LLM_CONFIG["max_tokens"]),
    }

    lines: list[str] = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    existing_keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in existing_keys:
            new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def test_llm_connection() -> tuple[bool, str]:
    """测试当前 LLM 配置是否可用（发一次极短调用验证 key/地址）

    使用配置中的 temperature；若因温度约束被拒（如 Kimi 要求 temperature=1），
    自动以 temperature=1 重试一次，避免用户在「测试连接」阶段被模型温度限制卡住。
    """
    def _try(temperature: float) -> tuple[bool, str]:
        try:
            if not LLM_CONFIG["api_key"]:
                return False, "API Key 为空"
            # 用原生 OpenAI SDK 做连通性测试（不用 ChatOpenAI.invoke：某些代理返回
            # 非标准响应时 langchain 解析会抛 "model_dump" 之类误导性错误）
            from openai import OpenAI
            client = OpenAI(
                api_key=LLM_CONFIG["api_key"],
                base_url=LLM_CONFIG["base_url"],
                timeout=30,
                max_retries=0,
            )
            resp = client.chat.completions.create(
                model=LLM_CONFIG["model"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=256,
                temperature=temperature,
                timeout=90,  # 慢速上游代理（如 geek2api 的 gpt 单轮实测 ~28s）需放宽，30s 会误报超时
            )
            try:
                text = ((resp.choices[0].message.content) or "").strip()
            except Exception:
                text = ""
            if not text:
                return False, "模型返回为空，请检查 base_url 与 model"
            return True, f"连接成功，模型响应: {text[:50]}"
        except Exception as exc:
            msg = str(exc)
            # 精简错误信息
            if "401" in msg or "Unauthorized" in msg or "authentication" in msg.lower():
                return False, "API Key 无效或已过期 (401)"
            if "404" in msg:
                return False, "模型不存在或 base_url 路径错误 (404，可尝试在地址末尾加 /v1)"
            if "Connection" in msg or "Max retries" in msg or "Failed to resolve" in msg:
                return False, "无法连接，请检查 base_url 或网络"
            if "model_dump" in msg or "not a valid" in msg or "Insufficient Balance" in msg:
                return False, ("模型服务商返回异常：接口可能不兼容或余额不足。"
                               "请确认 base_url 是否需以 /v1 结尾、model 名称是否存在。")
            return False, f"连接失败: {msg[:160]}"

    ok, msg = _try(float(LLM_CONFIG["temperature"]))
    # 温度约束导致失败时，自动以 temperature=1 重试（兼容 Kimi 等强制温度为 1 的模型）
    if not ok and ("temperature" in msg.lower() or "temperature" in str(msg).lower()):
        ok2, msg2 = _try(1.0)
        if ok2:
            return True, msg2 + "（已自动以 temperature=1 重试通过）"
    return ok, msg


# ── 服务配置 ──
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "5173"))

# ── 权限模式（P4）─────────────────────────────────────
# 完整模式（默认 1）：敏感权限变更 → 待审批单，管理员在审批中心处理。
# 轻量模式（0）：敏感变更直接落地（仍写 auto_approved 审计单，不留 pending），
# 适合内部/演示环境；前端同步隐藏「审批中心」。
AUTH_FULL_MODE = os.getenv("AUTH_FULL_MODE", "1") == "1"

# ── 结果-问题一致性 LLM 校验（独立校验 Agent / Critic）────────────
# 规则校验通过后，再加一轮独立 LLM 判定"这个 SQL + 结果能回答用户的问题吗"。
# 模式：off=关闭（仅规则校验）/ on=强制开启 / auto=仅复杂查询自动开启（默认）。
# auto 下简单查询不额外调用 LLM（控成本与延迟），复杂查询（JOIN/聚合/子查询）自动加保险。
#
# ⚠️ 2026-09-28 实测复审（dataherald 对照 + 本机 A/B）后补充的限制说明：
# Critic 与生成用的是**同一个模型**，只换了 system prompt。这带来两个已知代价：
#   ① 同源共偏——生成模型系统性写错的写法，Critic 往往也认为"没问题"（拦不住）；
#   ② 反向误杀——生成正确的 SQL 被 Critic 判为"不对"→ 触发改写 → 改写产物更差，
#      且旧产物能通过 _validate_result，于是**正确答案被覆盖**（见 run() Step 5.5
#      注释里记录的事故：首版正确 SQL 被改写成裸明细 LIMIT 20）。
# 现在 run() 已加 _output_quality_reason 二次闸门兜住 ②，但 ① 无法靠同模型解决。
# 结论：Critic 的净收益集中在「复杂 JOIN / 多表 / 子查询」这类结构错高风险场景，
# 对「单表聚合」类它更多是在制造改写噪音。因此把 auto 的触发面收窄：
# 只有出现 JOIN/UNION/子查询（结构复杂度）才开；纯 GROUP BY/聚合不再触发。
RESULT_LLM_CHECK_MODE = os.getenv("RESULT_LLM_CHECK_MODE", "auto").strip().lower()
ENABLE_RESULT_LLM_CHECK = RESULT_LLM_CHECK_MODE in ("on", "auto")
# auto 模式下是否把「纯聚合（GROUP BY/HAVING/SUM/COUNT，但无 JOIN）」也算作复杂查询。
# 默认 0（不算）：这类查询结构简单、错法集中在口径而非结构，而口径已由
# 10 道确定性前置校验 + 指标注册表把关，再叠一次同模型 LLM 复查性价比低。
# 置 1 恢复旧的宽松判定（任何聚合都触发）。
RESULT_CHECK_AGG_COUNTS_COMPLEX = os.getenv("RESULT_CHECK_AGG_COUNTS_COMPLEX", "0") == "1"

# ── 结果评价 Agent（Evaluator）────────────────────────────
# 白泽式四 Agent 闭环的第四环：生成→校验→修正→**评价**。
# 评价 Agent 对最终结果打分（0-100），低分或 Critic/评价分歧时触发多候选交叉比对。
# 模式：off=关闭 / on=强制 / auto=仅复杂查询（默认，控成本）。
#
# ⚠️ 2026-09-28：Evaluator 的**主要用途**是驱动 _cross_validate（低分→多候选重选），
# 而 _cross_validate 内部要再跑一次 _generate_candidates + 逐个 _llm_result_evaluate。
# 也就是「低分」这个信号本身要花 1 次调用取得，触发后又要花 1+N 次。实测门槛偏松时
# 很容易形成「打分→低分→重选→再打分」的长链。默认把门槛由 60 提到 70：
# 60 分档里混有大量"能用但写法不漂亮"的结果，把它们送进重选是负收益。
RESULT_EVAL_MODE = os.getenv("RESULT_EVAL_MODE", "auto").strip().lower()
# 低于该分数判定为"结果不可靠"，触发多候选交叉比对重选
RESULT_EVAL_MIN_SCORE = int(os.getenv("RESULT_EVAL_MIN_SCORE", "70"))
# 低分交叉比对的开关与候选数（0 = 关闭，省掉打分→重选整条链）
RESULT_CROSS_VALIDATE = os.getenv("RESULT_CROSS_VALIDATE", "1") == "1"
RESULT_CROSS_VALIDATE_N = int(os.getenv("RESULT_CROSS_VALIDATE_N", "2"))
# 确定性编译命中的查询是否跳过 LLM 复查（Critic + Evaluator）。
# 默认 1（跳过）：编译 SQL 的口径由指标注册表保证，让 LLM 再审一遍口径、甚至在它
# 判定"不对"时改写，反而会让结果偏离注册口径，与「确定性编译为主」的约定冲突；
# 同时省掉 2.8~5.3s 的两次 LLM 往返（实测编译命中查询从 ~5s 降到 ~0.2s）。
# 置 0 可恢复全量复查（排查复杂问题时用）。规则校验（确定性）无论何种情况都执行。
RESULT_CHECK_SKIP_COMPILED = os.getenv("RESULT_CHECK_SKIP_COMPILED", "1") == "1"

# ── NL2Python 受限沙箱（对标 Fabric Code Interpreter / Vanna Python 沙箱）──
SANDBOX_TIMEOUT_SEC = float(os.getenv("SANDBOX_TIMEOUT_SEC", "8"))
# 注入沙箱的最大行数（防大结果集打爆内存）；超出截断，代码可见截断提示
SANDBOX_MAX_ROWS = int(os.getenv("SANDBOX_MAX_ROWS", "20000"))

# ── PostgreSQL 数据库配置 ──
DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "postgres"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
}

# ── 前端静态文件路径 ──
UI_DIR = os.getenv("UI_DIR", os.path.join(os.path.dirname(__file__), "static"))
