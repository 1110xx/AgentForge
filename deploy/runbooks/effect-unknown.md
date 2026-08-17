#EffectUNKNOWN处置
告警表示外部系统调用已发出,但平台不能证明其结果。此时禁止自动重试WRITE。
1.冻结对应effect_id的调度与人工审批,保留当前Attempt、generation和capability digest。
2.以PostgreSQLEffectLedger、Outbox、AuditEvent为事实:NATS、应用日志和trace只用于定位。
3.使用connector的idempotencykey在目标系统执行只读reconciliation,区分"已提交"明确未提交"仍未知"。
4,已提交:计算绑定effect_id、effect_key"、查询结果和证据引|用的 digest;使用effect-reconcilerservice identity 与专用reconciliation capability 调内部reconcileroute,把原 Effect标为SuccEEDED:不得创建第二个业务动作。
5.明确未提交:关闭原Attempt,经过新审批后创建新Attempt/Effect;禁止修改原Effect历史。
6,仍未知:继续冻结并升级给业务owner,不通过"再试一次"消除不确定性。
    恢复门禁:双人复核reconciliationevidence digest、可信actor、审计事实与generation fence;确认duplicate_effect指标末增加后才能恢复队列。禁止直接改数据库状态。
