# 容器运行手册

首版使用 Docker Engine（或 Docker Desktop 的 Linux 容器模式）和 Docker Compose v2 或更新版本。不需要 Node、宿主机 Python 或 Redis。以下命令在仓库根目录执行。

## 本地构建与启动

1. 复制 `.env.example` 为 `.env`（若已存在，保留原文件并补齐配置）。为 `DJANGO_SECRET_KEY` 和 `POSTGRES_PASSWORD` 分别设置高强度随机值。不要提交 `.env`。默认仅在 `127.0.0.1:8000` 提供 Web；端口冲突时修改 `WEB_PORT`。
2. 确保仓库根目录有与 `pyproject.toml` 匹配的 `uv.lock`。镜像只安装锁定的生产依赖，构建过程中执行 WhiteNoise `collectstatic`，不连接数据库、不需要部署密钥。

```shell
docker compose -f compose.yaml -f docker/compose.build.yaml build web
docker compose up -d --no-build --wait --wait-timeout 120
docker compose ps --all
```

构建覆盖文件只给 `web` 添加构建定义；构建产物即 `APP_IMAGE`（默认 `research-output-claims:local`），由四个应用服务复用。调试 Compose 配置可运行 `docker compose config --quiet`；不要分享完整的 `docker compose config` 输出，其中包含密钥。

| 服务 | 启动/就绪契约 |
| --- | --- |
| `db` | PostgreSQL 18，容器内 TCP 5432，使用 `pg_isready`；不映射宿主机端口。 |
| `fixture-source` | `python -m fixture_source`，容器网络内 `http://fixture-source:8081/healthz`；不映射宿主机端口。 |
| `migrate` | 等待 `db` 健康，执行 `python manage.py migrate --noinput`，预期退出码为 `0`。 |
| `web` | 数据库健康且迁移成功后启动 Gunicorn。健康检查要求 `/healthz`（存活）和 `/readyz`（数据库连通性）均返回 `200`。 |
| `worker` | 等待数据库、迁移和模拟源就绪，执行 `python manage.py run_sync_worker`。通过日志和任务状态验证业务运行；容器运行状态本身不证明归集成功。 |

四个应用服务均以非 root 用户（UID/GID `10001`）运行。数据库命名卷 `postgres-data` 挂载到 `/var/lib/postgresql`，PostgreSQL 18 自己在其下使用版本化的 `PGDATA`。不要挂到 PG 17 及以前常用的 `/var/lib/postgresql/data`，也不要把旧主版本的数据卷直接交给 PG 18；请先备份并安排版本升级。修改 `.env` 中的 `POSTGRES_*` 不会重置已初始化数据库里的用户或密码。

### 检查与演示

在 Windows 上用 `curl.exe`，其他平台可将其替换为 `curl`。使用自定义 `WEB_PORT` 时改用实际端口。

```shell
curl.exe --fail --max-time 5 http://127.0.0.1:8000/healthz
curl.exe --fail --max-time 5 http://127.0.0.1:8000/readyz
curl.exe --fail --max-time 5 --head http://127.0.0.1:8000/static/admin/css/base.css
docker compose logs --tail 80 db migrate fixture-source web worker
```

若需要演示账号，在 `.env` 中设置至少 12 个字符的 `DEMO_PASSWORD`，堆栈就绪后执行：

```shell
docker compose run --rm --no-deps migrate python manage.py seed_demo
```

该变量只传给 `migrate` 的管理命令容器。发布环境建议保持为空，不运行演示账号初始化。`docker compose down` 仅停止并移除本项目容器和网络，默认保留数据库命名卷；不要为普通重启或升级使用 `down --volumes`。

## 发布：仅拉取应用镜像

发布主机只需要 `compose.yaml` 和部署用 `.env`，不需要源码、Dockerfile 或构建覆盖文件。将 `APP_IMAGE` 设为已发布的完整镜像地址，建议用不可变摘要（例如 `ghcr.io/<owner>/<image>@sha256:<digest>`）。若仓库私有，先在主机登录镜像仓库。备份现有数据库后执行：

```shell
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d --no-build --pull never --wait --wait-timeout 120
docker compose -f compose.yaml ps --all
```

`compose.yaml` 本身没有构建定义。升级后检查迁移退出码、Web 就绪及 Worker 日志；不要假设升级没有停机。发布镜像必须包含同版本的应用代码、`fixture_source`、容器探针和已收集的静态文件。

Web 始终绑定宿主机回环地址。如果在同一宿主机上用 TLS 反向代理提供访问，设置 `DJANGO_DEBUG=0`、`DJANGO_HTTPS=1`，在 `DJANGO_ALLOWED_HOSTS` 中列出正式域名且保留 `127.0.0.1`（容器内探针使用），并设置 `DJANGO_CSRF_TRUSTED_ORIGINS` 为对应的 `https://` 源。代理应传递正确的 `Host`，并覆盖而非信任外部请求中的 `X-Forwarded-Proto`；不要在未配置 TLS 代理时开启此模式。

## PostgreSQL 备份与恢复演练

在 Compose 项目目录执行，以当前环境的数据库账号替换 `research`。把归档复制到受控存储并记录应用镜像摘要。下面恢复到新的 `research_restore_check` 数据库，可在隔离测试项目演练。

```shell
docker compose exec -T db pg_dump -U research -d research -Fc -f /tmp/research.dump
docker compose cp db:/tmp/research.dump ./research.dump
docker compose cp ./research.dump db:/tmp/research-restore.dump
docker compose exec -T db createdb -U research research_restore_check
docker compose exec -T db pg_restore -U research -d research_restore_check --exit-on-error /tmp/research-restore.dump
docker compose exec -T db psql -U research -d research_restore_check -c "SELECT COUNT(*) FROM django_migrations;"
```

恢复前后核对核心表数量、最新同步窗口以及快照记录，再以匹配的应用镜像做就绪和登录检查。备份文件含帐号与审计数据，放在仓库之外或受忽略的本地目录。清理时只移除自己创建的演练库和临时归档。
