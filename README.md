# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态（`operating` / `shutdown` / `frozen`）；`change`：变更申请；`action_item`：风险控制行动项。

## 影响范围管控

变更单不再只有一句影响描述：

- 建单时通过 `affected_unit_ids` 选择一台或多台受影响装置（旧字段 `unit_id` 仍兼容），系统把每台装置**当时的运行状态**快照写入 `data.affected_units`（装置名、状态、时间）；不选装置不允许建单。
- 评审通过后（`approved` / `implemented`），任一受影响装置被 **shutdown/freeze**，变更自动退回 `assessed`（待评审），`data.blocks` 与 `data.void_reason` 写明是哪台装置、停机还是冻结；装置恢复开车/解冻后清除该台的当前拦截记录，但变更仍需重新评审。
- 停机/冻结期间重新提交评审会被拒绝，`implement` 与 `commission` 同样实时校验装置状态——即使控制措施全部 `verified`，装置不可用就投不了产。
- `revise` 动作用于中途修订：增删受影响装置或把 `risk_level` 调高，原评审立即作废并退回待评审（自动重算所需评审人数）；仅改描述、仅降级风险或装置清单不变，不影响已通过的评审。
- 所有自动退回（审计动作 `auto_return`）和拦截清除（`block_cleared`）都写入审计时间线；演示页面上红色拦截条直接展示被拦下的原因，并对比装置快照状态与当前状态。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
