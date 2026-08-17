#NATS JetStream重建
NATS 只运输通知,不保存Run、Attempt、审批或 Effect 真相。重建 Stream 不允许修改 PostgreSQL 业务事实。
1.停止 publishers/consumers,记录 stream、consumer durable 和 last delivered/ack floor,仅用于诊断。
2.确认 PostgreSQL Outbox/Inbox健康并备份:不要从 NATSpayload 回写事实表。
3.按生产配置创建R3 Stream、subjects、retention、max age 与 explicit-ack consumers。
4.从未完成的 PostgreSQL Outbox 重新发布:稳定 message ID 保持不变。
5.消费者在同一事务中提交业务变更与Inboxprocessed marker:重复投递返回 ACK 而不重复执行。
恢复门禁:抽样证明NATS rebuild 前后 Run facts 相同,Outbox 无遗漏,Inbox 去重生效,consumer lag 回落。
