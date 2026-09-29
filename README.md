# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/isolation`：当前停梯范围，逐台列出阻碍恢复的原因。
- `GET /api/rescue-queue`：救援队列（严重任务在前，其余按到场/预计到场时间）。
- `POST /api/offline-records`：合并离线巡检记录；`X-Request-Id`（或请求体`request_id`）保证整批请求幂等。
- `GET /api/offline-records/<record_id>/versions`：查看某条离线记录的全部版本。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

### 群组隔离

设备可用`group_id`声明所属群组。普通报警只停报警设备本身；严重报警（`"severity":"severe"`）自动把停梯范围扩大到同组全部设备（也可用`scope_equipment_ids`显式指定）。隔离在一个事务内完成以下级联：

- 范围内设备置为`suspended`并打上隔离标记；范围外的同类设备不受影响，仍可维保。
- 待审核许可（`pending_review`）被退回`blocked`；隔离期间许可无法签发，维保不能新建或推进。
- 为每台新隔离设备自动安排一次复检（`reinspection_for_alarm`）。
- 救援任务进入队列：严重任务排在最前，其余按实际到场时间（未到场按`eta`）排序；撤回或完成的任务自动离队。

报警关闭要求至少有一个实际到场并完成的救援任务；仅被撤回（aborted）的任务不算数，旧报警遗留的任务不能用于关闭新报警。

### 群组恢复

对已关闭报警执行`recover`动作时，逐台核对范围内设备：不得存在其他活动报警、不得有未关闭整改、必须有隔离发生之后合格的复检。任一设备不满足则整体拒绝（HTTP 409 `RecoveryBlocked`）并返回每台设备的阻碍清单；全部满足后设备恢复运行，并把逐台报警、整改、检验核对结果作为快照写入报警（`last_recovery` / `recovery_snapshots`）。

### 离线巡检合并

离线记录按`(source_id, record_id)`去重、按整数`version`合并：高版本覆盖当前版本，低版本保留为`ignored_old`，被覆盖的版本标记为`superseded`，所有版本都可通过版本接口查询。带`X-Request-Id`的合并请求把结果持久化在服务端，服务重启或扩容到另一实例后重放会返回原结果（`replayed:true`）且不会第二次修改设备。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验、未关闭整改和活动报警共同限制；隔离设备无法逐台自行恢复，必须走群组恢复。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
