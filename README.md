# Vequo 维阔 · 数据工作台

一个面向制造业的数据分析与问答平台。用户用自然语言提问，系统自动完成表匹配、SQL 生成、数据查询与可视化，并支持知识图谱、指标管理、权限控制等功能。

## 软件架构

- **前端**：Vue 3 + TypeScript + Vite + Tailwind CSS
- **后端**：FastAPI (Python) + PostgreSQL
- **大模型**：DeepSeek API（可切换）
- **向量检索**：bge-small-zh-v1.5

目录结构：

```
├── backend/         后端代码
│   ├── agent/       智能体逻辑（SQL 生成、检索、报告等）
│   ├── routers/     接口路由
│   ├── models/      本地模型文件
│   └── ...
├── src/             前端代码
│   ├── components/  组件
│   ├── pages/       页面
│   └── ...
└── package.json
```

## 环境要求

- Node.js 18+
- Python 3.10+
- PostgreSQL 14+

## 安装教程

### 1. 克隆仓库

```bash
git clone git@gitee.com:Vequo/vequo-viqueo.git
cd vequo-viqueo
```

### 2. 后端

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # Windows
# source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
pip install python-multipart
python main.py
```

后端默认运行在 `http://127.0.0.1:8010`。

### 3. 前端

```bash
cd 项目根目录
npm install
npm run dev
```

前端默认运行在 `http://localhost:5173`。

## 环境变量配置

在 `backend/` 目录下新建 `.env` 文件，填入以下配置。

**注意：`.env` 内含真实密钥，不要提交到仓库（已在 `.gitignore` 排除）。**

### 数据库配置

```
DB_TYPE=postgresql
DB_HOST=localhost
DB_PORT=5432
DB_NAME=你的数据库名
DB_USER=你的数据库用户名
DB_PASSWORD=你的数据库密码
```

> 每人本地数据库名可能不同，按自己的填。

### 大模型配置

```
LLM_MODEL=deepseek-v4.1-flash
LLM_API_KEY=你的APIKey
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_TEMPERATURE=0.0
LLM_MAX_TOKENS=8192

LLM_DEFAULT_MODEL=deepseek-v4-flash
LLM_DEFAULT_API_KEY=你的默认APIKey
LLM_DEFAULT_BASE_URL=https://api.deepseek.com/v1
LLM_DEFAULT_TEMPERATURE=0.2
LLM_DEFAULT_MAX_TOKENS=8192
```

> `LLM_API_KEY` 找项目负责人要，或用自己的大模型 Key。

### 本地语义向量

```
EMBEDDING_MODEL=./models/bge-small-zh-v1.5
HF_HUB_OFFLINE=1
LLM_SQL_MODEL=
```

> 路径用**相对路径**，不要写自己电脑的绝对路径。

### 邮箱验证码（注册用）

```
MAIL_DEBUG=0
MAIL_SMTP_HOST=smtp.qq.com
MAIL_SMTP_PORT=465
MAIL_SMTP_USER=你的QQ邮箱@qq.com
MAIL_SMTP_PASSWORD=你的QQ邮箱授权码
MAIL_FROM=你的QQ邮箱@qq.com
MAIL_FROM_NAME=Vequo 维阔
```

> - 建议每人**用自己的 QQ 邮箱**发信，避免共用同一邮箱被限流。
> - 授权码获取：QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务 → 生成授权码。
> - 本地调试时可设 `MAIL_DEBUG=1`，验证码只打印到后端日志，不真实发信。

## 使用说明

1. 启动后端和前端
2. 浏览器打开 `http://localhost:5173`
3. 注册账号并登录
4. 在工作台输入自然语言问题，如「分析各工序的良率」
5. 系统自动生成 SQL 并返回图表结果

## 参与贡献

1. 开工前先 `git pull`，拉取最新代码
2. 改完代码后：`git add .` → `git commit -m "说明"` → `git push`
3. 提交说明写清楚改了什么
4. 不要提交 `.env`、密钥等敏感文件
5. 不要提交运行时生成的文件（已在 `.gitignore` 排除）