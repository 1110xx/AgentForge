#PostgreSOL failover 处置
PostgreSQL是业务与审计事实源。连接失败期间应停止需要新事实的控制面操作,不能从 NATS或 telemetry 推断成功。
1.将API 写路径置为unavailable,暂停scheduler/outbox publisher;保留只读状态页时要标注可能陈旧。
2.由数据库平台按既定 HA 流程完成failover,确认新 primary 的 timeline、同步复制状态和连接endpoint。
3.执行Alembicrevision 检查:禁止故障时自动 downgrade。
4.对比 RunEvent seq、Checkpoint state、EffectLedger、AuditEvent 和 Outbox 连续性。
5.先恢复reconciliation/outbox,再恢复admission:消费者Inbox 会抑制重复消息。
恢复门禁:无committedrow丢失、无audit gap、无多个活跃Attempt、数据库时间单调可用,并完成一次只读恢复演练查询。
