# 5G-A 场景加速运营服务

这是一个面向运营商网络优化与产品运营团队的 Python 后端，用于管理高铁、地铁、演唱会和大型场馆中的应用体验采样、质差识别、动态加速、用户权益、策略版本、容量预留与运营审计。服务使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 场景与区段：维护高铁线路、地铁区段、演唱会和场馆的容量、顺序、停留时间及运行状态。
- 应用画像：按游戏、直播、视频通话、视频和办公类别保存时延、丢包、上下行速率与优先级目标。
- 策略版本：校验质差评分权重、严重度阈值、资源倍数和会话时长，支持草稿、发布、生效与退役状态。
- 体验样本：使用业务采样键进行幂等写入，保存脱敏用户标识、终端类型、速度和网络指标。
- 质差事件：将应用目标与实测指标进行确定性比较，记录原因、严重度和处理状态。
- 动态加速：校验用户权益与生效策略，按区段容量预留上下行资源，支持完成、取消和超时释放。
- 维护窗口：按 drain_mode 冻结新会话并排空存量会话，正常模式等待自然结束，强制模式宽限期后取消并释放预留，可迁移会话优先转移到相邻区段；窗口排空后才进入施工状态，结束后恢复候补晋级，并保留每个会话的处理结果与人工覆盖记录。
- 运营分析：提供场景与应用质差率、容量利用率、会话成效和可恢复事件游标。
- 身份与审计：提供管理员初始化、用户、角色、会话、权限、操作审计和后台维护能力。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/network-acceleration.db`。可以复制 `.env.example` 并通过 `NETWORK_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

5G-A 运营接口统一使用 `/api/network` 前缀，场景、应用、策略、权益、样本、事件、会话和分析报告都在该路径下。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

测试覆盖身份初始化、角色权限、审计脱敏、场景与应用登记、策略发布、样本幂等、质差判定、权益校验、容量拒绝、会话完成、固定时钟过期恢复、维护窗口排空协议和分析游标。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 与 CLI 冒烟

```bash
python -m app.cli smoke
python -m app.cli network-demo
```

`smoke` 在进程内检查根路径、健康接口和运营摘要；`network-demo` 会创建高铁场景、应用画像与策略，登记有效权益，写入一条质差样本并启动加速会话。

## 目录结构

```text
app/
  network/         场景、应用、策略、采样、质差、加速、容量和分析
  api/             用户、角色、认证、审计、系统与维护接口
  core/            时钟、安全、异常、隐私和分页能力
  repositories/    通用 SQLite 查询与身份持久化
  schemas/         身份与管理接口输入模型
  services/        认证、审计、用户、后台任务和维护服务
  cli.py           初始化、检查和业务冒烟入口
  database.py      SQLite 连接、基础表结构与权限初始化
tests/             核心、身份、网络运营和分析回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。策略发布、样本与事件创建、容量预留、会话终止和过期恢复使用即时事务。体验样本保存脱敏用户标识，登录令牌只保存摘要，审计和会话事件不会记录明文密码或令牌。
