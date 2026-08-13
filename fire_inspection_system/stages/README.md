# 十一阶段主流程

`main.py` 只负责命令行参数、阶段顺序和最终提示。业务阶段位于本目录，每个文件提供一个公开的 `run_stage(...)` 接口。

十一个阶段文件已经包含各自所需的完整实现源码，不在运行时读取或导入原来的算法 `.py`。为避免合并后大量同名私有函数互相覆盖，每个旧实现被迁入阶段文件内的独立源码命名空间；这些命名空间只存在于内存中，其函数代码位置仍是对应的 `stage_*.py`。

| 阶段 | 文件 | 主要输入 | 主要输出 |
|---|---|---|---|
| 1 | `stage_01_cad_input.py` | DWG/DXF、输出目录 | 可用 DXF、本次运行目录 |
| 2 | `stage_02_cad_inventory.py` | DXF | 带缓存的 CAD 图元清单 |
| 3 | `stage_03_floor_preprocess.py` | DXF、图元清单 | 图幅、楼层、inspection region、审核 DXF |
| 4 | `stage_04_inspection_objects.py` | 图元清单、楼层区域 | 巡检对象识别结果 |
| 5 | `stage_05_obstacles.py` | 图元清单、楼层区域 | 障碍物及其并集 |
| 6 | `stage_06_navigation_graph.py` | 楼层、障碍物、巡检对象 | 自由空间、Portal AreaGraph、中轴导航图 |
| 7 | `stage_07_connector_metric_closure.py` | 中轴导航图 | Connector 修正物理图、有效自由空间、Metric Closure 准备 |
| 8 | `stage_08_semantic_rgcn.py` | 修正物理图、巡检目标、权重 | A/N/C、候选目标、全楼层 R-GCN 推荐 |
| 9 | `stage_09_dual_graph_planning.py` | R-GCN 推荐、物理图 | 所有识别楼层的目标顺序和值图 |
| 10 | `stage_10_route_outputs.py` | 目标顺序、认证物理图、DXF | 矢量路径、最终标注 DXF、访问顺序、主摘要 |
| 11 | `stage_11_acceptance_reports.py` | 楼层目标池、控制访问计划、实际访问事件 | 每楼层 Markdown 验收报告、报告索引、完成率摘要 |

## Metric Closure 的边界

物理 Metric Closure 必须在目标接入点锁定后构建。因此阶段 7 负责 Connector 修正和物理输入准备；A/N/C 使用的上下文 Closure 在阶段 8 依据候选目标接入点生成；最终认证物理图的规划 Closure 在阶段 9 生成。这样避免为了形式拆分而增加一套未被使用的重复 Closure。

## 运行方式

完整运行：

```powershell
python fire_inspection_system\main.py --input D:\drawing.dxf
```

只复用已有识别结果执行阶段 7～11：

```powershell
python fire_inspection_system\main.py --path-planning-only --output-dir D:\run_dir
```

每个阶段模块可以独立导入，输入输出均为显式参数或结果对象，便于后续接入任务队列、HTTP API、容器作业或分阶段重试。

## 上线必须保留的非 Python 资源

“只保留 11 个阶段 Python 文件和 `main.py`”只针对项目 Python 源码。以下数据和模型仍必须保留：

- `configs/inspection_object_aliases.json`
- `configs/inspection_object_keyword_patterns.json`
- `fire_inspection_system/configs/semantic_inspection_constraints.json`
- `datasets/semantic_navigation_reachability80_v1/`
- 冻结 R-GCN 权重和路线头权重
- `inspection_route_rules.json` 等业务规则数据
- CAD 文件、输出目录及 ODA File Converter

`tools/build_consolidated_stages.py` 是本次迁移期间使用的生成工具，不是生产运行依赖；清理旧文件时可以与旧 Python 源码一起处理。

## DXF 输出策略

Portal AreaGraph 和规则导航图审阅 DXF 已关闭。Connector 修正审阅图 `area_graph_navigation_refined/refined_navigation_review.dxf` 与最终路线标注图仍保留。
