# Vequo 维阔 后端架构（2026-08 更新）

## 主链路（唯一主线）

`main.py` → `agent/llm_service.py::LLMService.run()` → 流式 NL2SQL 流水线

```
意图分类 → 表匹配 → Schema 构建 → SQL 生成(流式) → SQL 执行 → 结果校验 → 图表生成 → 响应
```

所有查询类请求（`/api/agent/ask`、`/api/agent/stream`）只走这一条主线。

## 目录说明

| 模块 | 职责 | 状态 |
|---|---|---|
| `agent/llm_service.py` | LLMService 主流水线 + 选表/意图/缓存/日志 | **主链路** |
| `agent/prompt_builder.py` | SQL 生成 Prompt（Vanna 风格 few-shot） | 使用中 |
| `agent/time_expr.py` | 中文时间表达式 → SQL 日期区间（最近7天/本周/上月等） | 使用中 |
| `agent/chart_agent.py` | 图表生成 | 使用中 |
| `agent/memory.py` | 相似 SQL 示例记忆（faiss） | 使用中 |
| `agent/stream.py` | SSE 流式桥接 | 使用中 |
| `agent/predict.py` | 趋势预测（使用 llm_service 的 LLM 工厂） | 使用中 |
| ~~`agent/multi_agent.py`~~ | 早期多智能体原型（无外部引用） | ✅ 已删除 2026-08-10 |
| ~~`agent/rag.py`~~ | 早期检索实现（仅被 multi_agent 引用） | ✅ 已删除 2026-08-10 |
| ~~`agent/graph.py`~~ | 早期 LangGraph 原型（无引用） | ✅ 已删除 2026-08-10 |
| `db/tools.py` | 表发现/字段/外键查询 | 使用中 |
| `db/executor.py` | SQL 执行器（安全护栏：超时/行数上限/危险拦截） | 使用中 |
| `db/metadata.py` | postgres 演示库 14 表硬编码元数据 | 使用中 |
| `config.py` | LLM/DB 配置（读 .env） | 使用中 |

## 安全护栏（db/executor.py）

- 仅允许 SELECT（含 WITH...SELECT CTE）
- 拦截写操作/管理语句/危险函数（DROP/INSERT/LOCK/pg_sleep/COPY 等）
- 拦截分号拼接的多条语句
- `statement_timeout` 10s 硬超时
- 结果行数硬上限 5000（超出截断并标记 `truncated`）

## 准确率增强（2026-08 评测后新增）

1. **Prompt 规则 8**：问"各XX/按XX统计"时 GROUP BY 只放业务维度列，不混入主键 id
2. **外键自动注入**：schema context 自动携带真实外键（JOIN 键），多表 JOIN 只能用它
3. **时间表达式解析**：`agent/time_expr.py` 把"最近7天/本周/上月/去年"翻译成确定性日期区间注入 prompt
4. **JSON mode**：选表/SQL 生成调用开启 `response_format={"type":"json_object"}`，解析更稳
5. **结果缓存 TTL**：5 分钟有效期 + 50 条容量上限，切换库自动清空
6. **可观测性日志**：每轮运行写 `logs/nl2sql.log`（intent/选表/SQL/耗时/是否 refine）

## 评测

`eval/` 目录下有自动化评测集（gold SQL + 结果比对），改一次 prompt/代码跑一遍即知准确率变化：

```bash
cd eval
python eval.py           # 跑完整评测（真实 LLM 调用）
python eval.py --no-llm  # 只重算已存 SQL 的判定（不调 LLM，秒级）
```

## 环境注意

- 后端端口 8010；DB 驱动 pg8000（仅支持 `%s` 占位符）
- `.env` 含 LLM/DB 密钥，已被 .gitignore 忽略，禁止提交
- 项目 venv 已损坏时，可用独立 venv 运行评测脚本（见 eval/README）

## 扩展新数据源（Phase 5.3，可选）

当前 `database.py::build_engine` 支持 PostgreSQL（pg8000）与 MySQL（pymysql）。扩展新库只需两步：

1. `database.py::build_engine` 增加分支（如 ClickHouse）：
   ```python
   if db_type == "clickhouse":
       from sqlalchemy import create_engine
       return create_engine(f"clickhouse://{config['user']}:{password}@{host}:{port}/{database}", ...)
   ```
   （需安装对应驱动：`clickhouse-connect` / `clickhouse-sqlalchemy`）

2. `.env` 设 `DB_TYPE=clickhouse`（或前端"数据库连接"配置里选新类型）。

注意：切换数据库后 `/api/database/switch` 会清空 schema/结果缓存（`clear_cache`），
新库类型需通过 `db/executor.py::_is_mysql`（按 `get_db_type()` 判断）对应的方言分支适配。
