# 运行架构与数据流

基线：单校、多院系，Django 模块化单体；独立合成 HTTP 源；PostgreSQL 是业务数据、作业和会话的权威存储。没有 Redis、外部 LLM 或正式考核数据。

## DFD 0 · 系统上下文

```mermaid
flowchart LR
    T[教师]
    D[院系审核员]
    S[校级复核员]
    O[同步运维]
    F[实验用论文和专利 HTTP 源]
    P((科研成果归集与认领系统))
    T -->|标识检索、认领、申诉| P
    P -->|推荐理由、本人进度| T
    D -->|初审决定| P
    P -->|授权待办、院系统计| D
    S -->|复核、纠错、冻结快照| P
    P -->|校级统计、审计| S
    P -->|作业与水位| O
    P -->|请求固定窗口和页| F
    F -->|可重放合成元数据| P
```

## DFD 1 · 处理与存储

```mermaid
flowchart LR
    Feed[合成 HTTP 源] --> P1((1 增量归集))
    P1 --> D1[(来源版本和同步检查点)]
    P1 --> D2[(成果和署名位)]
    D2 --> P2((2 匹配与解释))
    D3[(教师身份与历史任职)] --> P2
    P2 -->|候选建议| Teacher[教师]
    Teacher -->|申请与证据| P3((3 审核与申诉))
    Reviewer[审核与复核人员] -->|决定| P3
    D2 --> P3
    P3 --> D4[(申请、有效归属和审计事件)]
    D2 --> P4((4 按口径统计))
    D3 --> P4
    D4 -->|有效归属与撤销痕迹| P4
    P4 -->|同一权限投影| Out[看板、趋势、明细与 CSV]
    P4 -->|校级冻结| D5[(单年度冻结快照)]
    D5 -->|读取时重新检查授权| Out
```

## 容器部署视图

```mermaid
flowchart TB
    U[本机浏览器]
    subgraph Host[单主机 Docker Compose]
        W[web: Gunicorn Django]
        M[migrate: 一次性迁移]
        J[worker: run_sync_worker]
        F[fixture-source: 合成 HTTP 服务]
        DB[(db: PostgreSQL 18)]
        Vol[(命名持久卷)]
        M -->|数据库健康后执行| DB
        M -.->|成功后启动| W
        M -.->|成功后启动| J
        J -->|HTTP 定时轮询| F
        J --> DB
        W --> DB
        DB --- Vol
    end
    U -->|回环端口| W
```

边界：不发布 PostgreSQL 或模拟源的宿主端口；生产式部署需要主机、HTTPS 和运维配置。代码路径分别为 `apps/sources`、`apps/catalog`、`apps/matching`、`apps/claims`、`apps/reports` 和 `apps/people`。图描述的是部署结构和数据流，不是独立微服务。
