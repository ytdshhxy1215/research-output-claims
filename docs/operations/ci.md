# CI 与版本发布

## 自动检查

`.github/workflows/ci.yml` 对拉取请求（含 fork）和 `main` 推送运行；发布流程也会在对应标签提交上调用同一套检查。CI 不读取仓库私密配置，使用只读源码权限、合成账号和临时数据库，不访问校园系统或第三方数据源。

| 作业 | 检查 |
| --- | --- |
| Django | 用 `uv 0.13.10` 和 Python `3.14` 执行 `uv sync --locked`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run --noinput`、`collectstatic --noinput` 和 `pytest`。测试连接真实的 PostgreSQL 18 服务容器，不回退到 SQLite。 |
| Compose | 校验基础文件和构建覆盖文件，构建供 `web`、`migrate`、`worker` 和 `fixture-source` 共用的镜像，启动五服务堆栈并核对迁移退出码。一次性命令给空数据库创建合成账号，不直接写入成果。 |

端到端检查使用 Python 标准库，覆盖 `/healthz`、`/readyz`、静态资源、匿名访问 `/claims/` 时的登录跳转，以及缺少 CSRF token 时拒绝写入。`lin` 在工作台搜索由独立模拟源和 Worker 归集的 `10.5555/p1`，对第一作者提交认领；甲院看不到申请，乙院 `review-beta` 初审驳回；`lin` 申诉，`school-review` 独立复核并改判核准。检查最终状态和审计记录，并以校级账号核对 2025 年严格口径总数为 1、按成果年份归属甲院（不是初审管辖的乙院）。烟测不直接写入数据库或绕过表单。

每次烟测生成独立的 `roc-ci-<随机值>` 项目名、回环地址随机端口和新数据库卷。正常成功、错误和可处理的中断都会尝试先采集 `ps --all` 和各服务日志，再以相同项目名执行 `down --volumes --remove-orphans`；不会清理默认项目或运行全局 prune。Actions 将服务日志保存为 `compose-smoke-logs` 或 `release-smoke-logs`；镜像构建和命令输出在步骤日志中。

## 本地复现

在仓库根目录运行。使用本地 Linux Docker Engine 或 Docker Desktop 的 Linux 容器模式，Docker Compose v2（支持 `up --wait`）或更新版本；烟测脚本仅依赖 Python 3.10+ 的标准库。在 Windows PowerShell 将下列 `python3` 换成 `python` 即可。

```sh
python3 scripts/ci/compose_smoke.py --log /tmp/roc-compose-smoke.log
```

也可省略 `--log`，直接在控制台查看诊断。仅调试 HTTP 时，可在已启动且没有历史认领的合成堆栈上运行 `DEMO_PASSWORD='替换为演示密码' python3 scripts/ci/http_smoke.py --base-url http://127.0.0.1:8000`；这个脚本会实际创建并通过一条申请，不要针对日常数据运行。

Django 作业要求另一个临时 PostgreSQL 18 数据库（用户必须能创建测试库）。例如先在独立终端运行以下命令，也可使用自己隔离的 PostgreSQL 18 实例：

```sh
docker run --rm --name roc-local-pg18 -p 127.0.0.1:55432:5432 \
  --env POSTGRES_DB=research --env POSTGRES_USER=research \
  --env POSTGRES_PASSWORD=local-ci-db-password postgres:18-trixie
```

待 PostgreSQL 就绪后执行；结束时停止刚创建的 `roc-local-pg18`。

```sh
export DJANGO_SECRET_KEY=local-ci-not-for-deployment
export POSTGRES_DB=research POSTGRES_USER=research POSTGRES_PASSWORD=local-ci-db-password
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432
uv python install 3.14
uv sync --locked
uv run --locked ruff check .
uv run --locked python manage.py check
uv run --locked python manage.py makemigrations --check --dry-run --noinput
uv run --locked python manage.py collectstatic --noinput
uv run --locked pytest
```

## 标签、镜像与资产

- `.github/workflows/release.yml` 只处理形如 `v1.2.3` 的稳定标签。应将发布标签限制在已审查的提交上；PR 或 `main` 的普通推送不发布。
- 检查通过后，发布作业使用内置 `GITHUB_TOKEN` 将 `linux/amd64` 应用镜像推送至小写的 `ghcr.io/<owner>/<repo>:<tag>`；不推送 `latest`。推送后从 Buildx 元数据取得 `ghcr.io/<owner>/<repo>@sha256:...`，解压并使用该不可变引用对实际发布包再跑一次烟测。
- GHCR 作业仅有 `contents: read` 与 `packages: write`；附加 GitHub Release 资产的作业仅有 `contents: write`。已有同名 GHCR 包须授予该仓库 Actions 写权限。无需另配仓库 secret。
- 版本名适合浏览，摘要用于运行；ZIP 内的 `.env.example` 已固定 `APP_IMAGE`。同一 GitHub Release 存在时流程不会重发；若发布前失败，GHCR 中可能已经有该版本标签，参考失败作业日志后再运行。不要删除已发布摘要或用另一个摘要重发同一版本。
- GitHub Release 附上 `research-output-claims-demo-vX.Y.Z.zip` 和对应 `.zip.sha256`。ZIP 内含 pull-only `compose.yaml`、无密钥 `.env.example`、镜像引用、运行手册及内部 `SHA256SUMS`。流程不部署到任何宿主机。
- 远程 Action 使用上游发行标签对应的完整提交 SHA，注释保留版本。更新时重新核对对应 Action 的上游 Release 及其提交，不要改回可移动标签。

## 运行发布演示包

从同一 GitHub Release 下载 ZIP 和 `.zip.sha256`。例如在 Linux 用 `sha256sum --check research-output-claims-demo-v1.2.3.zip.sha256`；在 PowerShell 执行 `Get-FileHash -Algorithm SHA256 ./research-output-claims-demo-v1.2.3.zip`，将结果与 `.zip.sha256` 的第一列比对。验证 ZIP 后再解压。解压目录内可另用 `sha256sum --check SHA256SUMS` 检查全部内容。

1. 在解压目录复制 `.env.example` 为 `.env`，给 `DJANGO_SECRET_KEY` 和 `POSTGRES_PASSWORD` 分别设置本地随机值，保留 `APP_IMAGE` 的摘要，不要提交 `.env`。默认仅绑定 `127.0.0.1:8000`；端口冲突时调整 `WEB_PORT`。
2. GHCR 包若不是公开的，先用具有读取权限的账号登录 GHCR。以未使用的项目名运行以下命令；无需下载项目源码或在本机构建镜像。

   ```sh
   docker compose -p roc-demo-v123 pull
   docker compose -p roc-demo-v123 up --detach --no-build --pull never --wait --wait-timeout 120
   docker compose -p roc-demo-v123 ps --all
   ```

3. 在当前 shell 设定 `DEMO_PASSWORD`（至少 12 个字符，仅限合成演示账号；PowerShell 用 `$env:DEMO_PASSWORD = '...'`，Bash 用 `export DEMO_PASSWORD='...'`）。仅把它交给一次性 `web` 容器：

   ```sh
   docker compose -p roc-demo-v123 run --rm --no-deps --env DEMO_PASSWORD web python manage.py seed_demo
   ```

4. 打开 `http://127.0.0.1:8000/claims/`（换成实际端口），以 `lin` 登录，搜索 `10.5555/p1`。Worker 正在处理时可稍后再搜索。演示结束运行 `docker compose -p roc-demo-v123 down`；确认不再需要该演示数据库时才添加 `--volumes`。只清理本次演示项目。

进一步的运行排障见 [容器运行手册](containers.md)。
