# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据、恢复许可、群组隔离单和离线巡检记录。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

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
- `GET /api/audit`：读取审计记录。
- `GET /api/recovery-board`：群组隔离单、停梯范围、每台设备的恢复阻碍与恢复快照。
- `GET /api/rescue-queue`：未完成救援任务队列（严重在前、已到场在前，再按到场/ETA时间排序）。
- `POST /api/offline-records`：提交`{"records":[...]}`，按`(source_id, record_id)`幂等合并，记录带整数`version`。
- `GET /api/offline-records/versions?source_id=&record_id=`：查询离线记录当前版本（旧版重放不会覆盖新版）。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

### 群组隔离与恢复

- 设备可用`group_id`标记同一群组（同一机房/同一组并联电梯）。报警可带`severity`：`normal`或`severe`（缺省`normal`）。
- **严重报警划定停梯范围**：创建严重报警后自动生成`isolation`隔离单，群组内在用/已挂起的设备全部置为`suspended`并写入`isolated_by`；其他群组不受影响，维保照常。
- **许可、维保和复检一起变化**：范围内已发放或待审许可自动`revoked`；进行中维保持`paused`、计划中维保持`on_hold`，隔离期间禁止在停梯设备上新建或开工维保；每台停梯设备自动生成一张关联报警的复检（`inspection.kind=reinspection`）。
- **逐台恢复**：每台设备单独核对①未关闭报警（含触发隔离的群报警）②未关闭整改（含挂在报警上的整改）③隔离复检通过；隔离中的设备只接受`return_to_service`许可。许可发放后执行设备`return_to_service`，设备上保留`recovery_snapshot`（报警/整改/检验/救援/许可/维保快照），该台挂起的维保自动恢复。范围全部恢复后隔离单变为`released`。
- 阻碍清单通过`GET /api/recovery-board`按台展示，页面同步显示停梯范围与每台阻碍。

### 救援排队与轮次

- 救援任务创建时继承报警严重级别并记录`round`；队列按“严重优先、已到场优先、再按`arrived_at/eta`时间”排序，严重任务可插队。
- 每次报警`dispatch`开启新救援轮次；报警关闭只检查**当前轮次**的任务，已撤回（`aborted`）或已完成的旧轮次任务不会关闭新一轮报警，新报警必须由当前轮次任务处理完才能关闭。

### 离线巡检合并

- 记录按`(source_id, record_id)`确定身份，显式整数`version`合并：高版本覆盖、相同版本重放幂等（`duplicate`）、低版本迟到保留可查但不应用（`superseded`）。
- 合并在单条SQLite事务内完成，并对写入设备的每个读数记录来源标记；服务重启或扩容后重放同一请求不会重复修改设备。`readings`合并到设备的`offline_fields`。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验、未关闭整改和未关闭报警共同限制；隔离设备还必须通过隔离复检且许可用途为`return_to_service`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
