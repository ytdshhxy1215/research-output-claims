# 领域对象与数据关系

## UML 类图 · 认领相关领域

```mermaid
classDiagram
    class Work {
        kind
        identifier
        published_on
        granted_on
    }
    class ContributionSlot {
        source_slot_id
        position
        name
        affiliation
    }
    class Researcher {
        user_id
        name
    }
    class ClaimCase {
        status
        review_department_id
        initial_reviewer_id
    }
    class ClaimEvent {
        kind
        occurred_at
        reason
    }
    class Attribution {
        valid_from
        valid_until
    }
    Work "1" *-- "0..*" ContributionSlot
    ContributionSlot "1" -- "0..*" ClaimCase
    Researcher "1" -- "0..*" ClaimCase
    ClaimCase "1" *-- "1..*" ClaimEvent
    ContributionSlot "1" -- "0..*" Attribution
    Researcher "1" -- "0..*" Attribution
```

一个申请对应一个具体署名位，`(slot, researcher)` 唯一；同一署名可有多条已撤销历史归属，`valid_until IS NULL` 的归属至多一条，由 PostgreSQL 条件唯一约束保证。审核事件保存操作者和原因；数据库触发器阻止普通行级更新和删除。归属不通过申请 FK 关联，统计只使用 `Attribution` 的当前有效条目，不从推荐或申请状态推断。

## ER 图 · 来源、作业、组织与快照

```mermaid
erDiagram
    SOURCE_SCOPE ||--o{ SYNC_RUN : schedules
    SYNC_RUN ||--o{ SYNC_RUN_ATTEMPT : records
    SOURCE_SCOPE ||--o{ SOURCE_RECORD : identifies
    SOURCE_RECORD ||--o{ SOURCE_REVISION : retains
    SYNC_RUN ||--o{ SOURCE_REVISION : fetched
    WORK ||--o{ SOURCE_RECORD : groups
    WORK ||--o{ CONTRIBUTION_SLOT : has
    RESEARCHER ||--o{ AFFILIATION_PERIOD : holds
    DEPARTMENT ||--o{ AFFILIATION_PERIOD : assigns
    DEPARTMENT ||--o{ DEPARTMENT_REVIEWER : grants
    REPORT_SNAPSHOT {
        uuid id PK
        string policy
        datetime captured_at
        json filters
        json payload
    }
```

`(kind, identifier)` 唯一标识成果；专利公开与授权合并到同一申请。来源版本和当前规范成果是两层数据。`ReportSnapshot` 保存冻结条件与事实 JSON，不以可变业务对象的 FK 代替快照数据。已冻结事实按当前授权重新投影，旧内容不因后续审批而重算。历史任职按 `[starts_on, ends_on)` 判定，不与当前审核管辖或授权混用。
