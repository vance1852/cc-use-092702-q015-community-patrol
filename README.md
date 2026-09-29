# 核算社区共管巡护任务与补偿基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单，并支持公园与周边村组共同承担防火瞭望、垃圾清运和野生动物冲突上报的共管任务、离线回执补传与月末补偿核算。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/comanagement/：村组共管任务版本、服务区域与检查点、人员资格、计价规则、离线设备回执（按设备序号识别重放/分叉/缺口）、跨村组转派双方确认、周期补偿可复算草案、三方确认、争议按任务冻结与追加决定；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m comanagement.acceptance --workspace .
~~~

这些命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及社区共管全流程：登记服务区域/检查点/人员资格/计价规则版本、计划与临时增援任务、按设备序号补传回执（重放吸收、分叉留痕、缺口标记后补传）、封路合理免责、跨村组转派双方确认、月末可复算草案、村组/保护站/财务分别确认、争议只冻结受影响任务和金额、无争议部分进入支付清单，并在重启后恢复未结争议；不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m comanagement.api --database comanagement.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。社区共管服务的关键接口包括：

- `POST /tasks` / `POST /tasks/{id}/revisions`：计划任务与追加版本（调整必须填 `change_reason`，开始后禁止改派）；
- `POST /devices/{serial}/receipts`：离线补传，返回 `replayed_sequences`、`forked_sequences`、`gapped_sequences`、`gap_filled_sequences`；
- `POST /transfers` / `POST /transfers/{id}/decide`：跨村组转派需转出方发起、接收村组组长确认；
- `POST /periods/{id}/compose`：按输入哈希可复算的周期草案（输入不变返回 `replayed: true`）；
- `POST /periods/{id}/confirm`：按 `village_head`、`station`、`finance` 三种 scope 分别确认或提出争议；
- `POST /periods/{id}/payments`：无争议且三方确认齐备的行进入支付清单，争议行继续冻结；
- `GET /lines/{id}/explain`：逐事件解释一笔补偿由哪些有效回执、免责封路与追加决定组成；
- `GET /disputes/open`：恢复全部未结争议（重启后仍可读取）。
