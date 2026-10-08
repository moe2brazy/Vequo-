<div align="center">

# Vequo 维阔 · 数据工作台

**制造业 NL2SQL 智能问析 Agent 系统**

用自然语言问数据，系统自己找表、选口径、拼 SQL、算结果、出图表和结论。

Vue 3 + FastAPI + PostgreSQL｜确定性口径编译 + 行列级权限引擎

</div>

---

## 这是什么

企业里问数据通常卡在两件事：**业务问题不知道怎么翻译成 SQL**，以及**每个人该看到的数据不一样**。

Vequo 维阔把这两件事拆开各自解决，再接到一个对话入口上。

- 问「L04 产线这个月良率多少」，走已注册口径的确定性编译路径，毫秒级返回，零 LLM 调用
- 问「停机都卡在哪些环节」，走 AI 生成 SQL，四重校验后才落库执行
- 同一句问法，质检岗看到的和设备岗看到的不一样——行过滤、列隐藏、列脱敏在 SQL 改写层统一生效，不靠前端藏

## 核心能力

| 能力 | 说明 |
|---|---|
| 智能问析 | 自然语言取数，SSE 流式返回思考过程与结果，自动配图 |
| 口径注册表 | 163 个指标口径（68 内置 + 95 团队自建），业务术语与字段中文名一一绑定 |
| 行列级权限 | 4 角色 / 行过滤 / 列隐藏 / 列脱敏，SQL AST 就地改写，fail-close |
| 血缘与归因 | 算子级血缘、指标异动归因树、维度下钻 |
| 数据洞察 | 自动异常扫描、TOP-N 榜单、趋势对比 |
| 机器学习 | 6 类模型（线性回归 / 决策树 / 随机森林 / 逻辑回归 / KMeans / 孤立森林），带 R²、特征重要性、训练集测试集分离 |
| 报告生成 | 定时报告、Word / PDF 导出、报告资产库 |
| 数据资源 | 表结构、字段字典、样例数据、表间关系图谱 |
| 业务知识 | 术语管理、知识图谱、LightRAG 导出与可视化 |

## 架构

```
前端  Vue 3.5 + TypeScript 6 + Vite 8 + Tailwind 3.4 + ECharts 6
      8 个页面 / 26 个组件，SSE 逐 token 渲染

                    ↓ /api (Vite dev proxy → :8010)

后端  FastAPI 0.104 + SQLAlchemy 2.0
      main.py            205 个 API 端点
      routers/           知识库、权限、可视化、配置、MCP
      agent/             62 个模块，核心在 llm_service.py
        ├─ orchestrator.py   任务编排（确定性词表任务链）
        ├─ metric_compiler.py 口径 → SQL 确定性编译器
        ├─ metric_registry.py 指标注册表读写
        ├─ sql_validator.py    SQL 校验
        ├─ investigator.py    异常扫描
        ├─ attribution*.py     归因树
        └─ ml/                 建模
      security/         权限引擎
        ├─ context.py    AclContext 构建
        ├─ enforcer.py  rewrite_sql（行过滤 + 列隐藏 + 列脱敏）
        ├─ model.py     权限模型存储 + 悬空列体检
        └─ approval.py  高危操作审批
      db/               executor.py 只读闸门 / metadata.py schema 源

数据源  PostgreSQL（主）/ MySQL，运行时可切换
```

### 双路径问数

这是整个系统最核心的设计。

**路径一 · 确定性编译**（口径已注册）

自然语言 → MQL 结构化推断（只让模型选「指标 + 维度 + 筛选」）→ 确定性编译器按真实 schema 拼装 SQL。

列名、关联键、单位换算由代码把关，模型没有机会编错。零 LLM 调用（除 MQL 推断那一次），毫秒级。

**路径二 · AI 生成 SQL**（口径未注册）

模型直生 SQL，然后过四道闸门：

1. 只读校验 —— `db/executor.py` 正则 + 关键字闸门，拦截写操作、`SELECT ... INTO`、文件写入
2. 权限校验 —— `security/enforcer.py` 用 sqlglot 解析 AST，缺权限直接拒
3. EXPLAIN 干跑 —— 估算行数超阈值就拒绝并回灌模型改写
4. 结果质量 —— 空结果 / 单值 / 比例异常时走预设路径，不把垃圾结果丢给用户

### 权限模型

权限判定收敛在一个出口：`security.enforcer.rewrite_sql()`。

- **行过滤**：把 `region = '华东'` 这类行规则编译成 SQL 谓词，注入 WHERE
- **列隐藏**：无权限的列直接从 AST 里删掉，不是查出来再删
- **列脱敏**：命中脱敏配置的列替换为掩码表达式（姓名 → 张\*、手机 → 138\*\*\*\*1234）

任何一层判定失败都是**拒绝**，不是放行。表级白名单由各调用方另做。

## 技术栈

**前端**

```
Vue 3.5 · TypeScript 6 · Vite 8 · Tailwind CSS 3.4
ECharts 6 · AntV G2Plot 2 · vis-network
```

**后端**

```
FastAPI 0.104 · SQLAlchemy 2.0 · uvicorn 0.24
sqlglot        SQL 解析与 AST 改写（权限引擎核心）
langchain      LLM 调用编排
scikit-learn  机器学习
pandas         数据处理
sentence-transformers  本地语义向量（bge-small-zh-v1.5，随仓库）
psycopg2 / PyMySQL   数据库驱动
```

向量检索是 numpy 点积 + BM25，没有引入向量数据库。

## 快速开始

### 环境要求

- Python 3.11
- Node.js 20+
- PostgreSQL 12+ 或 MySQL 8+

### 1. 拉代码

```bash
git clone https://github.com/moe2brazy/Vequo-.git
cd Vequo-
```

### 2. 后端

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

配置环境变量：

```bash
cp .env.example .env
```

`.env` 至少要填这几项：

```ini
DB_TYPE=postgresql
DB_HOST=localhost
DB_PORT=5432
DB_NAME=你的库名
DB_USER=你的用户名
DB_PASSWORD=你的密码

LLM_MODEL=deepseek-flash
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.deepseek.com/v1
```

`.env` 含密钥，已被 `.gitignore` 挡住，不要提交。

启动：

```bash
python main.py
```

后端监听 `0.0.0.0:8010`。健康检查：

```bash
curl http://127.0.0.1:8010/
# {"message":"ok"}
```

> 默认单进程运行（`WORKERS=1`）。系统支持运行时动态切库，切库改的是进程内全局 engine，开多进程会导致各 worker 指向不同库。确认不需要动态切库再用 `WORKERS` 调高。

### 3. 前端

```bash
npm install
npm run dev
```

开发服务器监听 `5173`，`/api` 自动代理到 `127.0.0.1:8010`。

浏览器打开 `http://localhost:5173`。

### 4. 本地语义模型（仓库已含，权重走 Git LFS）

语义缓存、相似问题推荐、记忆检索需要一个中文向量模型。仓库已含 `backend/models/bge-small-zh-v1.5`，但 **91.4MB 的权重由 Git LFS 托管**，所以 clone 之前先装 LFS：

```bash
git lfs install
git clone https://github.com/moe2brazy/Vequo-.git
# 若已 clone 过却没拉到权重：git lfs pull
```

没装 LFS 也能 clone，只是 `model.safetensors` 只剩 130 字节的 pointer 文件。此时 `agent/embeddings.py` 加载失败会降级到 OpenAI 兼容接口，再降级为 n-gram 哈希向量——**系统照常启动**，只是同义改写命中不了语义缓存（例如「各产线的产量」与「每个产线的产量」余弦 0.9252，降级后只有字面重复能命中）。

检出后还需 `pip install sentence-transformers torch`（约 2GB 依赖，首次加载 9~12 秒，已计入后端启动预热）。

不想背这个体积就设 `EMBEDDING_DISABLE=1`，显式关闭并直接走 n-gram 兜底。

### 5. 构建部署

```bash
npm run build        # 产物在 dist/，含 .gz 预压缩
```

`deploy/nginx.conf.example` 是一份可直接改用的 Nginx 配置，覆盖 Gzip 预压缩伺服、带 hash 静态资源强缓存、API 反代、SSE 缓冲禁用。

**SSE 必须 `proxy_buffering off`**，否则流式响应会被 Nginx 攒到结束一次性吐出，前端表现为「打字机不逐字出」。

## 目录结构

```
.
├── src/                    前端
│   ├── pages/              8 个页面
│   ├── components/         26 个组件
│   ├── charts/             图表封装
│   └── stores/             状态管理
├── backend/
│   ├── main.py             API 入口
│   ├── routers/            模块化路由
│   ├── agent/              62 个 Agent 模块
│   ├── security/           权限引擎
│   ├── db/                 执行器与元数据
│   ├── ml/                 建模
│   ├── models/             本地语义模型（92MB）
│   ├── tests/              53 个测试
│   └── .env.example        环境变量模板
├── deploy/                 Nginx 配置示例
└── dist/                   构建产物（不入库）
```

代码量：后端 6.1 万行 Python，前端 3.2 万行 TS / Vue。

## 测试

`backend/tests/` 下 53 个测试文件。**多数是脚本式**（直接 `python 文件名` 运行），少数是 pytest 风格。

```bash
cd backend

# 逐个直接跑（正确姿势）
for f in tests/test_*.py; do .venv/Scripts/python.exe "$f"; done

# 不要用这个 —— 脚本式测试里含 sys.exit(0)，pytest 收集时会 INTERNALERROR
# pytest tests/
```

当前状态：**49 通过 / 4 失败**。4 个失败项（`test_action_agent`、`test_confirm_security`、`test_metric_compiler_adv`、`test_semantic_memory_report`）经基线对照确认是历史遗留，非新引入。

## 已知约束

- 语义缓存按表类型分级 TTL：维度表 24h，事实表 15min。事实表数据变动频繁，长 TTL 会给出过期答案
- 非管理员账号下，编译产物如果同时含「多表 JOIN + ORDER BY 裸别名」，会被判列归属歧义并静默走兜底。维度字段放在事实表内才安全（停机原因、班次、工单状态、缺陷类型、严重度、仓库）；落在维表需显式限定
- 本地语义模型首次加载 9~12 秒（约 2GB 依赖）。部署环境不想背这个体积，设 `EMBEDDING_DISABLE=1` 走 n-gram 哈希兜底——代价是同义改写命中不了语义缓存
- `httpx` 必须锁 `<0.28`。httpx 0.28 移除了 `Client(app=...)`，会让 `starlette 0.27` 的 TestClient 崩

## 环境变量

完整清单见 `backend/.env.example`，每个可选项都带用途说明。常用的几组：

| 组 | 变量 | 说明 |
|---|---|---|
| 数据库 | `DB_TYPE` `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD` | 支持 PostgreSQL / MySQL |
| LLM | `LLM_MODEL` `LLM_API_KEY` `LLM_BASE_URL` `LLM_SQL_MODEL` | `LLM_SQL_MODEL` 单独给 SQL 生成链路用更强的模型，留空则跟随 |
| 生成预算 | `SQL_GEN_WALL_S` `SQL_GEN_INFER_RATIO` | 整条 SQL 生成链的墙钟上限，以及分给 MQL 推断的比例 |
| 护栏 | `EXPLAIN_GATE_ENABLED` `EXPLAIN_ROW_THRESHOLD` | 执行前的行数闸门 |
| 缓存 | `SEMANTIC_CACHE_*` `REDIS_URL` `CACHE_TTL_SECONDS` | 不配 Redis 则内存回退 |
| 企业认证 | `OIDC_ENABLED` `LDAP_ENABLED` | 默认关闭，惰性 import |
| 开关 | `EMBEDDING_DISABLE` `ANALYSIS_CONFIRM_ENABLED` | 语义向量 / 口径二次确认 |

## License

赛事参赛作品。Vequo 维阔，命题名称「企业数据底座智能问析 Agent 系统」。
