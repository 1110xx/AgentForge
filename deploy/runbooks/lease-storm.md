#Lease storm 处置
症状包括lease获取/过期速率突增、同一执行单元频繁生成新Attempt、SandboxPod快速抖动。
1.暂停新的admission,不删除Run、Checkpoint或Lease历史。
2.检查数据库时间、worker heartbeat、调度leader、网络延迟和Podeviction;不要以单机时间判断过期。
3.查询每个executionunit 的活跃Attempt/Lease 唯一约束。若出现多个活跃Attempt,按零容忍事件page 并停止全部相关写入。
4.终止失去fence的Pod:只从最后一个CoMMITTEDCheckpoint创建新Attempt 并领取新Lease/generation。
5.逐tenant限速恢复,观察expiry/acquireratio与队列滞后。
恢复门禁:数据库时钟健康、唯一约束无违规、旧generation写入被拒绝、连续两个leaseTTL窗口不再抖动。
