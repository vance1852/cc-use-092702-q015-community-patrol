# 核算社区共管巡护任务与补偿基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/comanage_ops/：公园与周边村组共管任务、离线回执、跨村组转派、周期结算、争议冻结与补偿解释；
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
PYTHONPATH=src python3 -m comanage_ops.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及共管任务派单、离线补传重放/分叉识别、跨村组转派、三方结算确认与争议冻结恢复，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m comanage_ops.api --database comanage.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 共管任务与补偿核算

src/comanage_ops/ 面向公园与周边村组共同承担的防火瞭望、垃圾清运和野生动物冲突上报：

- 任务定义版本化（tasks 只追加，task_runtime 保存运行态），任何调整都写入新版本并附原因；
- 服务区域、人员资格有效期、检查点点位、计价规则独立建档，派单与回执均校验资格；
- 离线设备按出厂序号登记，补传携带设备本地序号与事件 UUID：同 UUID/同序号重放幂等跳过，
  同序号不同 UUID 判定为设备日志分叉并拒绝整批；重复签到等无效回执进入 rejected 但保留原始记录；
- 跨村组转派必须转出、转入双方村组各自确认；已经开始的任务禁止转派，旧受托人的后续回执被拒，
  后台无法静默换人；
- 周期结算先生成带 input_sha256 的可复算草案，村组、保护站、财务分别确认自己负责的部分，
  每行独立在三方确认齐备后进入支付清单；争议只冻结对应任务行和金额，无争议行照常支付；
- 争议解决后按当前有效事件现场重算该行，解冻回到草案并要求三方重新确认；
- 所有调整只追加决定（event_decisions、settlement_decisions、dispute_events），
  `GET /settlements/{id}/tasks/{task_id}` 解释每笔补偿由哪些有效事件、按哪条计价规则组成，
  未结争议可在进程重启后经 `GET /disputes/open` 恢复处理。
