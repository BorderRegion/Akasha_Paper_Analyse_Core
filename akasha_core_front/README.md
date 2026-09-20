# Akasha 前端

阅读、整理、核对。界面负责把分析结果放回论文旁边。

前端使用 React、TypeScript、Vite、TanStack Query 与 PDF.js。应用代码位于 `apps/web/`，通过 `/v1/ui/*` 访问后端，不在浏览器中保存模型 API Key。

## 页面与使用流程

| 页面 | 用途 |
| --- | --- |
| 首页 | 继续阅读，查看最近活动 |
| 文献库 | 导入 PDF，搜索、筛选、收藏和批量操作 |
| 论文工作台 | 查看方法、结果与结论，对照 PDF，定位证据、写笔记 |
| 核查 | 查看结论的支持情况与反证，记录个人判断 |
| 合集、技术与对比 | 整理主题，比较论文，浏览关联信息 |
| 运行状态 | 查看任务、服务、工作进程和存储情况 |

先登录，再导入论文。分析未完成时可到运行页面查看进度；完成后从结论进入证据，回到原文核对。调整分析深度本身不会重新生成结果，需要再执行「重新分析」。

## 直接使用

仓库的 `apps/web/dist/` 已包含可用页面。按 [后端说明](../akasha_core/README.md) 启动服务后访问：

```text
http://127.0.0.1:8420/app/
```

登录填写后端 `.env` 中的 `API_TOKEN`。不需要启动 Vite，也不需要安装 Node。

## 本地开发

需要 Node 20.19.x、npm 10.8.2+（10.x），并先启动后端 API。从仓库根目录执行：

```bash
cd akasha_core_front/apps/web
npm ci
npm run dev
```

访问 `http://localhost:5273/app/`。开发服务器将 `/v1` 请求转发到 `http://127.0.0.1:8420`。后端地址不同，可在启动时指定：

```bash
PAPERINTEL_API_BASE=http://127.0.0.1:8421 npm run dev
```

## 构建与检查

在 `apps/web/` 中执行：

```bash
npm run typecheck
npm run lint:boundaries
npm test
npm run build
```

构建结果写入 `dist/`，由后端在 `/app/` 下提供。构建后刷新浏览器即可；若更改了后端静态目录配置，需要重启 API。`npm run preview` 仅预览静态构建，不等于启动完整应用。

## 目录

- `apps/web/src/app/`：入口、路由与会话。
- `apps/web/src/features/`：各页面和业务交互。
- `apps/web/src/components/`：公共界面组件。
- `apps/web/src/api/`：请求、接口类型与错误处理。
- `apps/web/src/design/`：颜色、字体、间距与动效。
- `contracts/`、`design/status-map.json`：接口合同与状态映射。
- `apps/web/tests/`：组件、交互及接口测试。
