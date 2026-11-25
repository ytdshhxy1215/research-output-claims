# 项目文档

这是与源代码一起评审的文档入口。课程立项稿保留历史背景；判断**当前系统行为**时，以运行手册、下面的架构专题和连接到测试的需求为准。

| 想了解 | 文档 |
|---|---|
| 目标、P0/P1 边界、需求和验收 | [立项](design/00-立项评审.md) · [需求规格](design/01-需求规格.md) · [业务评审](design/03-业务评审.md) |
| 系统边界、DFD、容器拓扑 | [运行架构与数据流](architecture/overview.md) |
| 模块内模型与关键约束 | [领域对象与数据关系](architecture/data-model.md) |
| 两数据流、分页、水位和失败续跑 | [同步时序与失败语义](architecture/ingestion.md) |
| 角色、待办、认领、申诉及撤销 | [认领工作流状态与交互](architecture/claims.md) |
| 指标、授权和快照的组成 | [统计数据流与一致性](architecture/reporting.md) |
| 技术选型与早期决策 | [初始架构](design/02-系统架构.md) · [技术评审](design/04-技术评审.md) |
| 实验协议与合成数据限制 | [消歧评估](evaluation/README.md) |
| 启动、演示、更新和发布 | [容器运行](operations/containers.md) · [演示脚本](operations/demo.md) · [CI/CD 与发布](operations/ci.md) |
| 验收证据和协作记录 | [P0 验收](development/p0-acceptance.md) · [首批实现记录](development/implementation-log.md) · [业务迭代清单](development/business-iterations.md) |

## 更新约定

同一 PR 变更业务规则时，同步更新相关专题、链接到需求和测试；调整 PostgreSQL 约束时更新 ER 图和迁移；调整 Compose 或工作流时更新拓扑及运行手册。图用 Markdown 中的 Mermaid 代码块维护，不把图片作为唯一设计源。运行 `python scripts/ci/check_docs.py` 检查文档，或加 `--render <新的输出目录>` 用固定镜像生成可看 SVG；GitHub PR 的 `Docs / links and Mermaid` 检查会实际渲染并保存产物。
