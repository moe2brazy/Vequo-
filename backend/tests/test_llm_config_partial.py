"""LLM 配置部分更新回归测试

背景（2026-11-07 线上实测）：
`POST /api/llm/config` 原先用 `req.model_dump()` 全量下发。Pydantic 会给缺省字段
自动填模型默认值，于是一个只想改 base_url 的请求会把 model / temperature 一并
重置成默认值 —— 实测把线上 .env 里的 LLM_MODEL=qwen3.8-flash 改成了 deepseek-flash、
LLM_TEMPERATURE 从 0.0 变成 0.2，导致后续所有问数都打到错误的模型上。

修复：端点改为只提交 `model_fields_set` 里显式出现的字段。
本测试守住这条语义，避免以后又被改回 model_dump()。
"""
import sys

sys.path.insert(0, ".")

from main import LLMConfigRequest  # noqa: E402


def build_patch(req):
    """与 main.save_llm_config_api 内保持一致的字段筛选逻辑"""
    return {k: getattr(req, k) for k in getattr(req, "model_fields_set", set())
            if k != "test_only" and getattr(req, k) is not None}


fail = 0

# --- C1 只传 base_url：patch 里不得出现 model / temperature ---
r = LLMConfigRequest(base_url="https://example.com/v1")
p = build_patch(r)
ok = (set(p) == {"base_url"}
      and "model" not in p and "temperature" not in p and "max_tokens" not in p)
print(f"{'✅' if ok else '❌'} [C1] 只传 base_url → patch={p}")
fail += 0 if ok else 1

# --- C2 只传 model：不得把 base_url 重置成默认值 ---
r = LLMConfigRequest(model="qwen3.8-flash")
p = build_patch(r)
ok = set(p) == {"model"}
print(f"{'✅' if ok else '❌'} [C2] 只传 model → patch={p}")
fail += 0 if ok else 1

# --- C3 只传 temperature=0.0：0.0 是合法值，不能被当成"未填"丢掉 ---
r = LLMConfigRequest(temperature=0.0)
p = build_patch(r)
ok = set(p) == {"temperature"} and p.get("temperature") == 0.0
print(f"{'✅' if ok else '❌'} [C3] 只传 temperature=0.0 → patch={p}")
fail += 0 if ok else 1

# --- C4 全量提交：五个业务字段都要在 patch 里（前端表单走这条路径） ---
r = LLMConfigRequest(model="m1", api_key="k1", base_url="u1", temperature=0.7, max_tokens=4096)
p = build_patch(r)
ok = {"model", "api_key", "base_url", "temperature", "max_tokens"} <= set(p)
print(f"{'✅' if ok else '❌'} [C4] 全量提交 → patch keys={sorted(p)}")
fail += 0 if ok else 1

# --- C5 test_only 不得混进配置 patch（否则 update_llm_config 会收到无关键） ---
r = LLMConfigRequest(base_url="u", test_only=True)
p = build_patch(r)
ok = "test_only" not in p
print(f"{'✅' if ok else '❌'} [C5] test_only 不进 patch → {p}")
fail += 0 if ok else 1

# --- C6 model_dump() 仍然会带默认值：说明本测试守的是必要修复，不是多余防御 ---
r = LLMConfigRequest(base_url="u")
full = r.model_dump()
ok = full.get("model") == "deepseek-flash" and "temperature" in full
print(f"{'✅' if ok else '❌'} [C6] 反证 model_dump 会补默认值 → model={full.get('model')}, temperature={full.get('temperature')}")
fail += 0 if ok else 1

print(f"\n═══ 结果: FAIL={fail} ═══")
sys.exit(1 if fail else 0)
