"""模型 Provider 注册表（P0-3a）— _make_llm 的可插拔 provider 抽象

把散落在 `_make_llm` 里的 provider 特殊处理（如 Kimi temperature 必须为 1）收敛到
统一注册表，好处：
- 新增模型只加一条配置，不改工厂逻辑
- 前端/配置可按能力位展示（json_mode / 温度规则）
- 后续可扩展成 provider 级差异化参数（超时、重试、JSON 模式开关）

纯数据 + 纯函数，可单测，不依赖 LLM。
"""
import os

# temperature 规则：clamp=0~2 自由（配置里已钳制）；force_1=模型强制为 1（Kimi）
# example_model：各厂商 2026 年当前使用人数最多的官方模型（旧名已退役的不再使用）
_PROVIDERS: list[dict] = [
    {
        "name": "deepseek",
        "label": "DeepSeek",
        "models": ["deepseek"],
        "hosts": ["deepseek.com"],
        "base_url": "https://api.deepseek.com/v1",
        # V4.1-Flash 为官方现行主力（模型 ID 已改为 deepseek-flash；
        # 旧的 deepseek-v4-flash 对应模型已于 2026-09-10 下线，
        # deepseek-chat/reasoner 亦于 2026-07-24 退役）
        "example_model": "deepseek-flash",
        "temperature": "clamp",
        "json_mode": True,
    },
    {
        "name": "kimi",
        "label": "Moonshot Kimi",
        "models": ["kimi", "moonshot"],
        "hosts": ["moonshot.cn", "moonshot.ai"],
        "base_url": "https://api.moonshot.cn/v1",
        # K3 为最新主力（moonshot-v1 系列与 kimi-k2.5 于 2026-08-31 下线）
        "example_model": "kimi-k3",
        "temperature": "force_1",
        "json_mode": True,
    },
    {
        "name": "zhipu",
        "label": "智谱 GLM",
        "models": ["glm-", "chatglm", "zhipu"],
        "hosts": ["bigmodel.cn", "zhipuai.cn"],
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # GLM-4.6 为当前主流（glm-4-plus 为旧代模型）
        "example_model": "glm-4.6",
        "temperature": "clamp",
        "json_mode": True,
    },
    {
        "name": "qwen",
        "label": "通义千问",
        "models": ["qwen"],
        "hosts": ["dashscope"],
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        # qwen-plus 为 DashScope 稳定主力（Qwen3 系列通用名）
        "example_model": "qwen-plus",
        "temperature": "clamp",
        "json_mode": True,
    },
    {
        "name": "openai",
        "label": "OpenAI",
        "models": ["gpt-", "o1", "o3", "o4"],
        "hosts": ["openai.com"],
        "base_url": "https://api.openai.com/v1",
        # GPT-5.2 为当前默认主力（GPT-4o/4.1 已于 2026-02 起逐步退役）
        "example_model": "gpt-5.2",
        "temperature": "clamp",
        "json_mode": True,
    },
    {
        "name": "ollama",
        "label": "Ollama（本地模型）",
        "models": ["qwen2", "qwen2.5", "llama3", "llama3.1", "deepseek-r1", "gemma", "mistral"],
        "hosts": ["localhost", "127.0.0.1", "ollama"],
        "base_url": "http://localhost:11434/v1",
        # qwen2.5:7b 为 Ollama 库下载量最高的中文场景模型
        "example_model": "qwen2.5:7b",
        "temperature": "clamp",
        "json_mode": True,
        "local": True,   # 本地部署标记：数据主权 / 零成本场景
    },
]

_GENERIC = {"name": "generic", "label": "通用 OpenAI 兼容", "base_url": "", "temperature": "clamp", "json_mode": True}


def detect_provider(model: str = "", base_url: str = "") -> dict:
    """根据 base_url / model 识别 provider，返回其配置（无匹配返回 generic）。

    先按 host（base_url）匹配——base_url 是确定端点，能可靠区分供应商；
    再按 model 名匹配兜底（如 Ollama 跑 qwen2.5 时 base_url 为 localhost，优先命中 ollama）。
    """
    m = (model or "").lower()
    u = (base_url or "").lower()
    for p in _PROVIDERS:
        if any(h in u for h in p["hosts"]):
            return p
    for p in _PROVIDERS:
        if any(mm in m for mm in p["models"]):
            return p
    return dict(_GENERIC)


def apply_temperature_rule(provider: dict, temp: float) -> float:
    """按 provider 温度规则调整 temperature。force_1 模型（Kimi）强制 ≥1。"""
    if provider.get("temperature") == "force_1":
        return max(temp, 1.0)
    return temp


# ── 思考链关闭规则（SQL 生成 / 口径推断类任务不需要思考链）──────────────
# 背景：混合思考模型默认「先想后答」，**思考 token 计入 max_tokens 预算**。
# 预算被思考烧光时 content 恒为 0 字、finish_reason=length，外层表现成
# 「AI 未能根据问题生成查询 SQL」——看着像模型不会答，实际是预算被思考吃干净。
#
# ⚠️ 2026-09-15 A/B 实测结论（yans 高价值题库 6 题，同一套代码只切换本开关）：
#   思考 OFF → 6 题全部生成出 SQL 且只要 3~17s，但**语义正确率 0/6**
#              （如「停机时长最长的前5台」被答成 `SELECT * FROM eqp_downtime_record LIMIT 20`）
#   思考 ON  → 3/6 严格通过 + 4/6 宽松通过，单题 44~85s
#   ⇒ 对**本模型（deepseek-v4.1-flash）**而言，思考链是复杂 SQL（EXISTS/窗口/HAVING）
#     的正确性来源，**不能靠关思考来解决"生成失败"**；真正的瓶颈是墙钟预算
#     （见 llm_service._SQL_GEN_WALL_S：40s 装不下「思考 + 出 SQL」→ 流被截断 → 空输出）。
#   所以此处**只保留已验证有益的 qwen3 / GLM 两档**，不覆盖 deepseek。
#   确有速度诉求时用环境变量 LLM_THINKING_OFF=force 手动打开（代价见上）。
_THINKING_OFF_RULES: list[tuple] = [
    # (base_url 关键字, model 关键字, 关闭载荷)
    # DashScope：Qwen3 系混合思考模型默认开思考（2026-09-11 实测 81s→2.2s）
    (("dashscope",), ("qwen3", "qwen-3"), {"enable_thinking": False}),
    # 智谱 GLM-4.5+ / GLM-5 用 thinking 字段关闭（不是 enable_thinking）
    (("bigmodel", "zhipuai"), ("glm-4.5", "glm-4.6", "glm-5", "glm-z1"),
     {"thinking": {"type": "disabled"}}),
    # DeepSeek-V4-Pro（dashscope）：pro 档模型本身已足够强，思考链反而是负担。
    # 2026-09-16 实测（同一 BIRD 题）：pro 开思考 8.4s / reasoning 1242 字 / 答案仍错；
    # 关思考 1.9s / 0 字 / 答案正确。注意只匹配 deepseek-v4-pro，**不含 flash**——
    # 上方注释已说明 flash 的思考链是复杂 SQL 的正确性来源，不可关闭。
    (("dashscope",), ("deepseek-v4-pro",), {"enable_thinking": False}),
]


def thinking_off_payload(model: str = "", base_url: str = "") -> dict | None:
    """返回关闭思考链所需的额外请求载荷；无需关闭时返回 None。

    环境变量 LLM_THINKING_OFF：
      - `force` / `1` / `true` / `on` → 对任意模型强制关闭（自建网关想关思考时用）
      - `0` / `false` / `off` / `no`  → 全局不注入（排障回退用）
      - 未设 / 其他                   → 按 _THINKING_OFF_RULES 精确匹配
    """
    env = (os.getenv("LLM_THINKING_OFF") or "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return None
    if env in ("force", "1", "true", "on", "yes"):
        return {"enable_thinking": False}
    m = (model or "").lower()
    u = (base_url or "").lower()
    for hosts, models, payload in _THINKING_OFF_RULES:
        if any(h in u for h in hosts) and any(mm in m for mm in models):
            return dict(payload)
    return None


def list_providers() -> list[dict]:
    """枚举已注册 provider（供前端/配置展示能力位与 base_url/model 预设）。"""
    return [
        {"name": p["name"], "label": p["label"],
         "base_url": p.get("base_url", ""),
         "example_model": p.get("example_model", ""),
         "local": p.get("local", False),
         "temperature": p["temperature"], "json_mode": p["json_mode"]}
        for p in _PROVIDERS
    ]
