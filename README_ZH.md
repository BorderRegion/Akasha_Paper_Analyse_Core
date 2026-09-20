# Akasha

包含当前修复后的前后端源码，以及最新浅青绿前端的完整构建。前端已有流程图示、统计图、移动端排版和阅读器交互修复。入口是 `/app/`，不是旧的 `/ui` 运维页。

交付包和解压根目录统一为 `Akasha`，页面、API 文档及导出的显示名称也使用 Akasha。已有 Python 模块名 `paperintel`、环境变量 `PAPERINTEL_*` 和 `paperctl` 命令保留兼容，启动时按本文命令即可。

## 包里有什么

- `akasha_core/`：后端源码、数据库迁移、测试、配置示例和数据库容器配置。
- `akasha_core_front/`：最新前端源码、测试、依赖锁文件和必要的接口合同、状态映射；`apps/web/dist/` 可直接由后端提供。
- `start-local.sh`：以同一份配置启动 API、worker 或 beat。
- `MANIFEST.json`、`verify_package.py`：逐文件 SHA-256 校验。

没有打包应用 token、模型密钥、SSH 凭据、证书、隧道配置、数据库、私人论文、笔记、调试截图、历史日志、旧设计包、原型、审查报告、任务卡或旧门禁脚本。虚拟环境和 node_modules 也不在包内，需要安装依赖。测试中的假 token、假密钥和本地开发数据库默认值不是私人凭据。

旧规格仅保留测试实际读取的五份 JSON/YAML 示例，位于 `akasha_core/spec/paperintel_final_spec/templates/`；不是整套规格书。依赖旧报告、旧基线、旧门禁脚本、规格书比对或指定私人 PDF 的历史检查不随包提供，其余功能回归测试保留。文件清单与交付时调整的文件记录在 `MANIFEST.json`。

## 本机启动（Linux）

需要 Python 3.12+、Docker Compose。使用包内现成前端时不需要 Node；修改前端需要 Node 20.19.x 和 npm 10.8.2+（10.x）。

在解压目录执行：

```bash
python3 verify_package.py
cd akasha_core
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d --build
cp .env.example .env
```

编辑 `.env`，把 `API_TOKEN` 填为自己生成的随机字符串。可用 `.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成。网页登录填这个 token；`LLM_API_KEY` 是模型服务的密钥，两者不同。启动脚本会读取 `.env`；如值包含空格或 shell 特殊字符，请用单引号包住。

配置 DeepSeek 等兼容接口时，填写 `LLM_BASE_URL`、`LLM_API_KEY` 和三个 `LLM_*_MODEL`。在控制台确认当前可用模型名。纯文字 PDF 可先只配置模型服务：

```bash
cp config/providers.example.yaml config/providers.yaml
```

从 `config/providers.yaml` 删除暂不使用的 `ocr_primary` 配置块即可；扫描版 PDF 需要另行配置真实 OCR 服务。不要把 mock 识别结果当成真实分析。仅预览界面或离线开发时，可改为复制 `config/providers.mock.yaml`，它不连接真实模型。

等待数据库和 Redis 健康后，回到解压目录：

```bash
cd ..
bash start-local.sh migrate
bash start-local.sh api
```

另开两个终端，在同一解压目录分别执行：

```bash
bash start-local.sh worker
```

```bash
bash start-local.sh beat
```

打开 `http://127.0.0.1:8420/app/`。三个进程必须共用同一个数据目录和配置；beat 只启动一份。退出终端会停止对应进程。启动已有数据库前请先备份数据库和 `data/`。

Compose 只提供 PostgreSQL/pgvector 和 Redis；默认数据库用户名、密码都是 `paperintel`，端口仅绑定本机。若修改数据库密码，需要同步调整 Compose 和 `.env` 的 `DATABASE_URL`。包内没有原部署的内网穿透配置，请按新机器重新配置。

## 改前端

```bash
cd akasha_core_front/apps/web
npm ci
npm run typecheck
npm run build
```

重新启动 API 后访问 `/app/`。开发服务器可用 `npm run dev`，默认将 API 请求转发到 `127.0.0.1:8420`。Python 历史验证版本清单在 `akasha_core/requirements-tested.txt`，用于复现参考；默认安装要求以 `pyproject.toml` 为准。

## 运行测试

后端单元、合同和合成 PDF 回归测试（不需要私人论文或真实模型）：

```bash
cd akasha_core
.venv/bin/pytest tests/unit tests/contract tests/golden
```

前端测试与代码边界检查：

```bash
cd akasha_core_front/apps/web
npm test
npm run lint:boundaries
```

`akasha_core/tests/integration/` 另含数据库/API 集成测试，需要独立测试环境，会创建和删除临时测试数据库；不要对生产库执行。真实模型专项测试需要额外配置，不属于上述离线测试。包内没有伪造的历史通过报告，文件完整性检查也不等于端到端分析已通过。
