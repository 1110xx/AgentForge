灾难恢复与PITR门禁
恢复顺序是PostgreSQL事实、对象版本、控制面,再到NATS 通知:NATS和 telemetry不能作为恢复源。
1,在隔离环境将PostgreSQL 恢复到选定时间点,记录backupID、WALboundary 与恢复时间。
2.恢复或挂接对象存储version history,验证所有COMMITTEDCheckpoint/READY Artifact 的key、size、checksum。
3.校验RunEventseq 连续、AuditEvent 完整、EffectLedger不重复、每个执行单元最多一个活跃Attempt/Lease。
4.部署相同digest 的控制面镜像,先禁用 admission:运行migration parity 和只读一致性检查。
5.创建空NATSR3 Stream,从未完成Outbox重放。先启动reconciliation,再逐tenant 恢复worker。
PITR通过条件:RPO/RTo 达标、零audit gap、零committed checksum 错误、零重复Effect、零stale fence success,任何一项失败都保持隔离并继续取证,不切换生产流量。
.D
