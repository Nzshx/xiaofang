# 外墙拓扑修复审核

该工具读取已有运行结果，不调用豆包或其他视觉 API，不修改源 CAD。
它生成独立的障碍物补全 GeoJSON、逐层审核 PNG 和叠加标注 DXF。
目前是审核阶段，不自动覆盖原识别结果、导航图、路线或后端接口数据。

## 本次数据与规则

- 当前运行：`outputs/fire_inspection_pipeline/邻里中心1_2_3_楼平面剖面图0620_t3_20260825120342`。
- 历史视觉来源：同目录下 `邻里中心1_2_3_楼平面剖面图0620_t3_20260821182111`。
- 先检查源 DXF SHA-256、9 层 CAD 范围、图片尺寸和坐标变换相同。
- 复用历史视觉中的建筑数量和名称，重新执行本地矢量密度/连通性分组。此次不允许错误的粗矩形重新分配矢量组件的建筑归属。
- 轮廓在轴网检测范围内提取并吸附到当前障碍物墙线；处理合并矩形的共线顶点，避免产生斜补线。
- 只在门扇径向门洞线与拟补外墙对齐时，切除新增补墙中的开口。不会删除原始墙体；门证据也不等于已完成导航可通行验证。
- 有两条连续侧墙、内部清空、两端与外墙对齐的连接走廊不按外墙封堵。只有空白区域不构成走廊证据。
- 屋面层矢量证据不足时输出原障碍物审核图并明确跳过，不用视觉粗框闭合。

## 重新生成

在项目根目录的 PowerShell 中执行：

```powershell
& 'D:\Anaconda\envs\torch_new\python.exe' -m fire_inspection_system.stages.outer_wall_topology_repair `
  --run-dir 'D:\untitled5\outputs\fire_inspection_pipeline\邻里中心1_2_3_楼平面剖面图0620_t3_20260825120342' `
  --vision-source-run 'D:\untitled5\outputs\fire_inspection_pipeline\邻里中心1_2_3_楼平面剖面图0620_t3_20260821182111'
```

加 `--no-dxf` 可以仅生成几何数据和图片，用于快速审核。
DXF 每次使用时间戳文件名，避免覆盖正在 CAD 中打开的版本。
最终文件路径以 `obstacles/outer_wall_topology_repair/outer_wall_topology_repair_audit.json` 为准。

## 图例和输出

- PNG 红色：原识别障碍物；蓝色：新增补墙；绿色：按门扇证据保留的开口；橙黄色：保留的连接走廊口。
- DXF 图层：`AI_WALL_TOPOLOGY_REPAIR`（青色补墙）、`AI_PROTECTED_DOOR_PORTAL`（绿色门口）、`AI_PROTECTED_CONNECTOR`（黄色连接走廊口）。保护标记是审核用图层，不是障碍物数据。
- `review/*_building_regions_vector_validated_reused.png`：复用视觉信息后重新校验的建筑分区。
- `review/*_obstacles_topology_repaired.png`：逐层障碍物补全。
- `review/obstacles_topology_repair_9_floors_overview.png`：9 层总览。
- `obstacles/outer_wall_topology_repair/door_aware_outer_wall_repairs.geojson`：独立新增障碍物。
- `protected_exterior_door_portals.geojson`：门和连接走廊的保护区域，仅供审核，不能作为障碍物加载。

审核重点：F1 左翼归属、F2 断裂区域、F3/F4 连接走廊、外墙窗口与真正门口的区分。
`vector_validated` 表示通过当前几何分组规则，不表示所有建筑语义和真实通行性都已人工确认。
原始识别仍可能包含误识别或漏识别；此工具只处理外轮廓补全，不是完整的室内障碍物修复。

## 测试

```powershell
& 'D:\Anaconda\envs\torch_new\python.exe' -m pytest fire_inspection_system/tests -q
```

审核通过后，应把独立补墙 GeoJSON 加入导航障碍物输入，并重新构图及执行最终路线碰撞审核，不能仅替换显示图片。

## 审核通过后的路线重建（2026-08-27）

已支持显式批准补墙、合并原障碍物并从 Stage06 重跑至路线导出：

```powershell
& 'D:\Anaconda\envs\torch_new\python.exe' scripts/rerun_safe_route.py `
  --run-dir 'D:\untitled5\outputs\fire_inspection_pipeline\邻里中心1_2_3_楼平面剖面图0620_t3_20260825120342' `
  --approved-wall-repairs 'D:\untitled5\outputs\fire_inspection_pipeline\邻里中心1_2_3_楼平面剖面图0620_t3_20260825120342\obstacles\outer_wall_topology_repair\door_aware_outer_wall_repairs.geojson' `
  --force-refinement
```

- 只传审核通过的补墙 GeoJSON，不能传绿色门洞/连接走廊保护标记文件。
- 原障碍物和补墙按物理楼层合并，解决 `F1__B01` 补墙不能匹配 `F1` 导航图的问题。
- 统一数据保存在 `obstacles/navigation_hard_constraints/obstacles.geojson`，批准来源和 SHA-256 保存在同目录 `manifest.json`。该运行的 Stage06 会采用这份数据，替代旧全封闭围护补全。
- Stage07 校验导航输入版本，重新认证所有边（包括门洞边、目标接入边和绕行边），不允许门洞或端点容差豁免硬障碍物。后续有效自由空间再次扣除同一份墙体。
- 最终审核直接检查每条实际转发路线的坐标，并与认证图边几何核对，不再只看边编号。包含接口三在内的公共导出入口会在审核通过后才写 DXF。
- 原始 CAD 和识别结果不修改，不调用豆包。旧导航和路线输出先复制到 `replanning_backups/<时间戳>`，审核 DXF 使用新的文件名。
- 约束为路线中心线不得进入障碍物内部；允许贴边，不代表已经满足人体通行宽度或消防疏散净宽。门洞证据和未识别室内墙体仍需审核。
- 默认目标选择策略没有改变。安全图断开的区域不能用穿墙线接通；分段路线、孤立目标和完整覆盖率必须分别查看，不能把“无碰撞”误报为“全部目标连续可达”。

生成路线后可复核实际 DXF 中的路线线段并生成逐层预览：

```powershell
& 'D:\Anaconda\envs\torch_new\python.exe' scripts/review_safe_wall_routes.py `
  --run-dir 'D:\untitled5\outputs\fire_inspection_pipeline\邻里中心1_2_3_楼平面剖面图0620_t3_20260825120342'
```

输出 `review/approved_walls_safe_routes_overview.png`、逐层 PNG 和 `approved_walls_route_review.json`；DXF 文件位置见运行输出及 `pipeline_summary.json`。
