# 学术成果智能归集与认领系统

面向高校科研管理的课程设计项目。首个可运行版本把**合成 HTTP 数据源、增量归集、可解释认领推荐、审核申诉和多口径统计**串成一条链路。数据和统计规则均为演示样例，不代表真实学校或真实环境的消歧准确率。

## 启动本地演示

需要 Docker Engine / Docker Desktop（Linux 容器）和 Docker Compose。将 `.env.example` 复制为 `.env`，为 `DJANGO_SECRET_KEY`、`POSTGRES_PASSWORD` 设置不同的本地随机值，并设置至少 12 个字符的 `DEMO_PASSWORD`。不要使用真实账户密码或提交 `.env`。

```shell
docker compose -f compose.yaml -f docker/compose.build.yaml build web
docker compose up -d --no-build --wait --wait-timeout 120
docker compose run --rm --no-deps migrate python manage.py seed_demo
```

打开 `http://127.0.0.1:8000/`（或 `.env` 指定的 `WEB_PORT`）。演示用户共用你设置的 `DEMO_PASSWORD`：教师 `lin`、`chen`、`lin-other`，院系审核 `review-alpha`、`review-beta`，校级复核 `school-review`，同步监控 `source-ops`。页面按角色显示入口：教师处理认领，院系审核员与校级复核员查看获授权的统计，运维查看同步。同步由 Worker 经 HTTP 执行，账户初始化不会写入成果记录。

先以 `lin` 查看或查找 `10.5555/p1`，对第一署名提交认领；由 `review-beta` 初审驳回，`lin` 申诉后用 `school-review` 改判。以校级角色在成果统计选择 **2025 年 / 学院考核示例**：论文计入林岚发表时所在的甲院，不计入处理认领的乙院。也可以打开「跨年成果趋势」，按同一口径看近 1–10 年的计数，再进入对应年度明细。更多步骤、批次推进和停止方式见[演示脚本](docs/operations/demo.md)与[容器手册](docs/operations/containers.md)。

## 验证与维护

- `python scripts/ci/compose_smoke.py`：在隔离 Compose 项目构建、启动、迁移、登录和验证认领、申诉及统计，再清理此次运行的资源。
- `python -m apps.matching.evaluate`：复跑冻结合成样本上的姓名基线和多证据方法，详见[评测协议及结果](docs/evaluation/README.md)。
- Python 依赖由 `uv.lock` 锁定；集成测试用 PostgreSQL 18。GitHub Actions 定义了 PR 检查和 tag 触发的镜像/部署包发布，不自动部署到未知主机。见[CI 与发布手册](docs/operations/ci.md)。

## 设计与过程

从 [项目文档索引](docs/INDEX.md) 查看 UML 类图、状态图、时序图、分层数据流、数据库关系与交付拓扑。

- [立项评审](docs/design/00-立项评审.md)、[需求规格](docs/design/01-需求规格.md)、[业务评审](docs/design/03-业务评审.md)
- [系统架构](docs/design/02-系统架构.md)、[技术评审](docs/design/04-技术评审.md)、[首批执行记录](docs/development/implementation-log.md)、[P0 验收清单](docs/development/p0-acceptance.md)

当前使用合成数据、单机部署和服务端页面。不收录课程站内容、真实人员信息或凭据。仓库公开可见，但项目许可证尚未选定。
