# Akasha

一个面向个人使用的论文阅读与分析工作台。

导入 PDF，整理研究问题、方法、实验与结论。分析附带原文证据，阅读时可以回到对应页面，留下笔记，再决定哪些结论值得相信。

## 能做什么

- **阅读与整理**：文献搜索、收藏、标签、合集、笔记和阅读进度。
- **论文分析**：按分析深度调用多个 agent，梳理贡献、方法、结果、局限与复现条件。
- **证据核查**：查看结论的支持证据、反证和核查记录，定位 PDF 原文。
- **跨论文研究**：对比论文、浏览技术与实体关系，导出研究材料。
- **运行管理**：查看任务、模型服务、工作进程和存储状态。

## 处理管线

```mermaid
flowchart LR
    A[导入 PDF 与指纹去重] --> B[页面检查与文本提取]
    B --> C[章节重建与证据索引]
    C --> D[分析深度与预算分配]
    D --> E[多 agent 分析]
    E --> F[元数据补全与结论核查]
    F --> G[综合整理]
    G --> H[关系连接与搜索索引]
```

有文本层的页面优先使用原生提取；需要 OCR 的页面依赖另行配置的识别服务。分析阶段按深度和预算执行，不是每篇论文都运行全部 agent。生成的结论是阅读线索，不替代原文。

## 前后端

| 部分 | 职责 | 技术与入口 |
| --- | --- | --- |
| [后端](akasha_core/README.md) | PDF 处理、分析管线、任务调度、数据与 API | Python / FastAPI / Celery，`akasha_core/` |
| [前端](akasha_core_front/README.md) | 文献库、阅读器、证据核查、研究与运行界面 | React / TypeScript / Vite，`akasha_core_front/apps/web/` |
| 存储与队列 | 结构化数据、向量能力、任务消息与本地文件 | PostgreSQL / pgvector / Redis / 本地 `data/` |

## 开始使用

```bash
git clone https://github.com/BorderRegion/Akasha_Paper_Analyse_Core.git
cd Akasha_Paper_Analyse_Core
```

1. 按 [后端说明](akasha_core/README.md) 安装依赖、配置模型服务并初始化数据库。
2. 启动 API、worker 和 beat。仓库已有前端构建文件，日常使用无需单独启动前端。
3. 打开 `http://127.0.0.1:8420/app/`，输入自己配置的应用 token。
4. 在文献库导入 PDF，进入论文工作台阅读分析、查看证据和记录笔记。需要调整深度时，选择论文后重新分析。

修改界面或单独运行开发服务器，见 [前端说明](akasha_core_front/README.md)。

论文、笔记和分析结果存放在本机。启用外部模型或 OCR 时，相关论文内容会发送给所配置的服务；请自行确认隐私要求与调用费用。

[MIT License](LICENSE)
