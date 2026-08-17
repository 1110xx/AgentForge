|文档|说明|
|---1---1
README.md |Backend定位、公开组合边界、本地演示、生产factory、合同与验证入口。
目录内容
名称|类型|说明
10 1112 131415 16
    ——-----
src/|目录|enterprise_agent_platformPython package 源码。
tests/|目录|Contracts、领域、持久化、安全、执行、UI、平台与环境门禁测试。
alembic.ini丨文件丨Backendmigration 命令与脚本路径配置。
1pyproject.toml|文件|Python package、runtime/dev dependencies、build、pytest 和 Ruff 配置
uv.lock丨文件丨可复现Backend依赖锁文件。
##文档索引
无
##目录内容
名称|类型|说明|
    initpy丨文件平台I/0适配层导出。1
entrypoint.pyI文件从显式module:callablefactory启动API/worker,不提供权限回退。
message_bus.py|文件NATsJetStream通知Inbox去重和ACK/NAK交付边界
outbox.py丨文件从PostgresQLOutbox可靠发布通知
telemetry.py|文件有限span、低基数metrics和零容忍正确性信号。
