# 本地演示脚本

先按[容器手册](containers.md)空卷启动并初始化演示用户。只用合成账户和本地端口。

| 演示角色 | 账号示例 | 可用入口 |
|---|---|---|
| 教师 | `lin`、`chen` | 认领工作台和本人申请；不能打开校院报表 |
| 院系审核员 | `review-alpha`、`review-beta` | 审核工作台、获授权院系的统计和导出 |
| 校级复核员 | `school-review` | 审核工作台、校级统计及快照 |
| 同步运维 | `source-ops` | 同步运行；不因运维身份拥有评审统计权限 |

如果以教师账号直接打开报表，HTTP 403 是权限保护而非功能缺失；错误页可返回认领工作台，或退出后切换为审核账号。报表默认当前事件年；实时结果为 0 时可使用报告内的「查看 2025 演示年度」，但未核准的成果仍不计入。

1. **归集**：以 `source-ops` 登录，打开「同步运行」。`batch-1` 两条运行都成功；成果库有论文 `10.5555/p1` 和一件尚未授权的专利。
2. **消歧**：以 `lin` 登录，看第一署名推荐和原因。点击「不属于我」后只隐藏候选，再按 DOI 搜索仍可认领；换 `lin-other` 不应看到该署名推荐。
3. **审核与申诉**：`lin` 提交第一署名。以 `review-beta` 驳回，填写理由；`lin` 补充理由后申诉；以非初审人的 `school-review` 改判核准。用 `review-alpha` 不能读取认领详情，初审者不能复核同案。审核台默认「待我处理」，切到「全部授权记录」可查看历史；教师可用「可申诉」直接找到被驳回的申请。
4. **统计**：以 `school-review` 查询 2025，切换口径、下钻检查甲院、导出 CSV。论文由乙院审核，但计入发表时所在的甲院。让 `chen` 的第二署名通过后，概览在两院各计一次、校级仍只计一篇，严格口径仍只计甲院。可在改判前冻结一份同条件快照，改判后对比实时数据和原快照。点击「查看跨年成果趋势」，选择截止 2026、统计 3 年；首批论文核准后，2025 显示 1，2024 和 2026 显示 0，且可分别打开明细。趋势是实时的，单年快照不显示实时趋势。
5. **增量与重放**：在容器内向模拟源发布 `batch-2`，不用更改数据库种子。再打开同步运行，核对两个新窗口。此时应有 2 篇论文、1 件专利、共 4 个署名和 5 个来源版本；一件专利只计一次，授权日期补齐。重放论文窗口不另增成果、署名或来源版本。

```shell
docker compose exec -T worker python -c 'import json,urllib.request as u; r=u.Request("http://fixture-source:8081/__fixture/release",json.dumps(dict(release_id="batch-2")).encode(),{"Content-Type":"application/json"}); print(u.urlopen(r,timeout=5).read().decode())'
docker compose exec -T worker python manage.py run_sync_worker --replay <运行编号>
```

运行编号可从数据库的 `sources_syncrun` 中查询；也可用测试 `tests/test_ingestion.py` 复现限流、回滚检查点和过期 Worker 场景。批次发布状态只保存在模拟源进程，重启模拟源若需继续 `batch-2`，重新发布即可；已经提交的数据库水位与记录独立保留。停止本地服务用 `docker compose down`，保留 PG 数据卷。
