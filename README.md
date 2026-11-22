# 特征工程与数据挖掘工作台

面向特征构建、特征选择、数据切片、模型训练与挖掘结果管理的数据分析特征工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 特征源管理 | `feature_source` | 特征源 | 源编号、源名称、数据表名 |
| 特征定义 | `feature_define` | 特征定义 | 特征编号、特征名称、特征类型 |
| 特征组管理 | `feature_group` | 特征组 | 组编号、组名称、包含特征 |
| 数据切片配置 | `data_slice` | 切片配置 | 切片编号、切片名称、数据来源 |
| 特征计算任务 | `feature_compute` | 计算任务 | 任务编号、关联特征、数据切片 |
| 特征筛选策略 | `feature_selection` | 筛选策略 | 策略编号、特征集、筛选方法 |
| 特征重要性分析 | `feature_iv` | 重要性评估 | 评估编号、关联特征、信息值IV |
| 特征快照管理 | `feature_snapshot` | 特征快照 | 快照编号、特征集、快照日期 |
| 训练流水线 | `train_pipeline` | 流水线配置 | 流水线编号、流水线名称、特征集 |
| 模型注册中心 | `model_registry` | 模型注册 | 模型编号、模型名称、算法类型 |
| 批量预测任务 | `predict_batch` | 预测任务 | 任务编号、模型编号、数据切片 |
| 挖掘结果管理 | `mining_result` | 挖掘结果 | 结果编号、分析主题、算法类型 |
| 聚类分析配置 | `cluster_config` | 聚类配置 | 配置编号、特征集、算法类型 |
| 特征相关性矩阵 | `feature_corr` | 相关性矩阵 | 矩阵编号、特征集、计算方法 |
| 特征漂移监控 | `drift_monitor` | 漂移监控 | 监控编号、关联特征、基准分布 |
| 数据导出管理 | `data_export` | 导出任务 | 任务编号、数据来源、导出范围 |
| 流水线调度 | `pipeline_schedule` | 调度配置 | 调度编号、关联流水线、Cron表达式 |
| 业务场景标注 | `scenario_label` | 场景标注 | 标注编号、场景名称、关联特征 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 切片复算分区清单

数据切片模块在通用列表之外提供「切片复算分区清单」能力，解决切片详情只有汇总行数、
无法按日期检索缺失分区并发起复算的问题。前端入口：
数据切片配置页底部「切片复算分区清单」面板；特征计算任务页底部「复算任务与回填核对」面板。

领域存储在 `backend/app/partition_catalog.py`（分区版本链、检索快照、复算清单、计算回执、
回填登记），专用路由在 `backend/app/routers/recompute.py`（先于通用资源路由注册）。

关键接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/data_slice/{id}/recompute/partitions` | 按日期窗口分页检索可复算（缺数）分区，同条件翻页复用 `search_id` |
| `POST /api/data_slice/{id}/recompute/snapshots` | 勾选即固化当时见到的分区版本，翻页与迟到修订不改写快照 |
| `POST /api/data_slice/recompute/check-snapshots` | 核对已勾选日期在迟到修订/补报后是否仍可用于本轮回填 |
| `POST /api/data_slice/{id}/recompute/revisions` | 登记迟到修订（revision）或缺失补报（backfill） |
| `POST /api/data_slice/{id}/recompute/plans` | 提交复算清单：按日期去重、拒绝跨切片/未勾选分区、拦截失效快照 |
| `POST /api/feature_compute/recompute/tasks` | 计算任务读取复算清单发起复算，输入分区逐日期锁定版本 |
| `GET /api/feature_compute/recompute/tasks/{id}/receipt` | 进度回执：实际处理的业务日期、输入版本、产出版本 |
| `GET /api/feature_compute/recompute/tasks/{id}/coverage` | 出数后回填覆盖核对：漏回填与清单外产出 |

守卫规则：同一业务日期在清单里只能出现一次；未勾选的分区不能进入计算输入，
计算引擎处理清单外日期会被拒绝；迟到修订后旧快照标记冲突，必须重新检索选择才能提交。

集成测试：`backend/tests/test_recompute_chains.py`（6 个场景，含分页固化、冲突核对、
两页勾选三片、回执与覆盖核对、输入校验、路由注册顺序）。

领域：数据工程
子方向：数据分析与数据应用
