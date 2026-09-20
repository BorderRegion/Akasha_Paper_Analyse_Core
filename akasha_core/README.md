# Akasha 后端

负责论文导入、证据提取、多 agent 分析、核查和检索，通过 FastAPI 向前端提供接口。

## 运行结构

- **API**：接收请求，提供数据与 `/app/` 页面。
- **worker**：执行提取、分析、核查等任务。
- **beat**：定时派发数据库中的待执行任务，只启动一份。
- **PostgreSQL / pgvector**：保存论文、证据、结论、任务与检索数据。
- **Redis**：为 Celery 提供消息队列；PDF 等文件保存在本地 `data/`。

内部 Python 模块名为 `paperintel`，命令行为 `paperctl`，环境变量使用 `PAPERINTEL_*`。

## 1. 安装

以下命令适用于 Linux，需要 Python 3.12+ 和 Docker Compose。从仓库根目录开始：

```bash
cd akasha_core
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d --build
cp .env.example .env
cp config/providers.example.yaml config/providers.yaml
```

Compose 只启动数据库和 Redis，不启动应用。用 `docker compose ps` 确认服务健康；默认占用本机 5432、6379 端口。

## 2. 配置

编辑 `akasha_core/.env`：

| 配置 | 用途 |
| --- | --- |
| `API_TOKEN` | 自己设置的网页登录口令，不是模型密钥 |
| `LLM_BASE_URL`、`LLM_API_KEY` | OpenAI 兼容模型接口地址与密钥 |
| `LLM_ANALYST_MODEL` | 论文分析模型 |
| `LLM_VERIFIER_MODEL` | 结论核查模型 |
| `LLM_SYNTH_MODEL` | 综合整理模型 |
| `PAPERINTEL_DATA_DIR` | 本地文件目录，默认 `./data` |
| `DATABASE_URL`、`REDIS_URL`、`CELERY_*` | 数据库与任务队列连接 |

三个模型角色可以使用同一个模型，名称以服务商实际提供的为准。例如 DeepSeek 的接口地址可设为 `https://api.deepseek.com/v1`。

生成应用 token：

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把输出填入 `.env` 的 `API_TOKEN`。启动脚本会读取这份文件；包含空格或 shell 特殊字符的值请用单引号包住。不要提交 `.env` 或真实密钥。

`config/providers.yaml` 定义模型角色、超时与并发。只处理有文本层的 PDF 时，可删除其中的 `ocr_primary` 块；扫描版需要配置 Paddle OCR HTTP 服务及 `PADDLE_OCR_*`。`providers.mock.yaml` 只用于离线开发，不产生真实论文分析。

## 3. 启动

回到仓库根目录，初始化数据库并启动 API：

```bash
cd ..
bash start-local.sh migrate
bash start-local.sh api
```

另开两个终端，在仓库根目录分别运行：

```bash
bash start-local.sh worker
```

```bash
bash start-local.sh beat
```

三个进程共用 `akasha_core/.env` 和同一个数据目录。终端关闭会停止相应进程。

- 工作台：`http://127.0.0.1:8420/app/`
- API 文档：`http://127.0.0.1:8420/docs`
- 环境检查：`bash start-local.sh doctor`

启动脚本用 `PAPERINTEL_WEB_DIST` 指向前端 `dist/`。默认只监听本机；远程访问需自行配置 HTTPS 与访问控制，不要直接暴露数据库和 Redis。

## 管线与代码位置

| 阶段 | 主要工作 | 模块 |
| --- | --- | --- |
| 导入与提取 | 指纹去重、PDF 检查、原生文本提取及 OCR 补充 | `ingest`、`extraction`、`ocr` |
| 结构与证据 | 重建章节，保存可定位的证据 | `structure`、`evidence` |
| 分析 | 按深度分配预算，分析问题、贡献、方法、实验、结果与局限 | `triage`、`agents` |
| 核查与综合 | 解析元数据，检查结论与证据，再综合整理 | `providers`、`verification`、`agents` |
| 组织与检索 | 连接实体、构建搜索索引，支持跨论文查询 | `knowledge`、`graph`、`search` |

上述模块位于 `src/paperintel/`；任务状态与重试由 `workflow/` 管理。任务失败时，先在前端运行页面查看错误，再决定是否重试或重新分析。

## 开发与维护

在 `akasha_core/` 中运行离线测试：

```bash
.venv/bin/pytest tests/unit tests/contract tests/golden
.venv/bin/paperctl --help
```

手动运行 CLI 时不会自动读取 `.env`，需要先在当前终端加载配置。数据库集成测试会创建临时测试库，应在独立测试环境执行。`paperctl selftest` 需要保留源码中的 `tests/fixtures/`。

数据库默认账号和密码均为 `paperintel`，仅供本机使用。修改密码时同步修改 Compose 与 `DATABASE_URL`。升级、迁移前备份 PostgreSQL 和 `data/`；日常停止数据库用 `docker compose stop`，不要删除数据卷。
