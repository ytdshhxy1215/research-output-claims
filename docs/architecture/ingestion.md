# 同步时序与失败语义

## UML 时序 · 固定发布批次与续跑

```mermaid
sequenceDiagram
    participant W as Worker
    participant F as HTTP 模拟源
    participant D as PostgreSQL
    W->>F: 请求 manifest 获取 release_id 和上界
    W->>D: 分范围排队固定窗口，领取租约和 fencing token
    loop 每页独立处理
        W->>F: GET 指定 release_id、窗口边界和 cursor
        alt HTTP 成功
            F-->>W: 事件和下一游标
            W->>D: 短事务校验 token，幂等保存版本和检查点
        else 临时失败
            F-->>W: 429 或可重试错误
            W->>D: 保存失败分类、检查点和下次尝试时间
            Note over W,D: 水位仍停在上个完整窗口
        end
    end
    W->>D: 仅在终页提交成功时推进水位并完成运行
```

读取为至少一次：一个 HTTP 请求在数据库事务外；页面重复处理不能复制成果、署名或版本。游标绑定窗口，时间相同用稳定事件键排序。检查点、租约和水位分开：过期 Worker 不能提交；坏事件可阻断当前数据流，另一流继续。

Operator 通过[演示脚本](../operations/demo.md)推进模拟发布及重放；不手工推进水位。测试验证 HTTP 第 2 页 429、数据库重启恢复、重复版本去重与 fencing，参见 [归集测试](../../tests/test_ingestion.py)。历史窗口重放不直接创建申请和归属。
