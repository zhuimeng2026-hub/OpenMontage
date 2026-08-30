# scripts/mvp_dev/

OpenMontage 商品视频 MVP §17 Golang/Gin 阶段化开发的 cron 骨架。

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md`

## 文件结构

```
mvp_dev/
├── orchestrator.sh        # cron 主入口(01:30 每日)
├── install_scaffolding.sh # 一键创建 phase_0..5 占位(idempotent)
├── README.md              # 本文件
├── phase_0/               # A 微信身份
│   ├── tasks.yaml         # 范围清单 + gate 描述
│   ├── run.sh             # 实际执行(检查 tasks.yaml status=READY 才动手)
│   ├── gate.sh            # 通过条件检查
│   └── README.md          # 阶段说明
├── phase_1/..phase_5/     # B+H / C / D / E / F+G
└── (logs/mvp_dev/ 在 REPO_ROOT/logs/)
```

## 安装

```bash
# 1. 已跑过:目录 + orchestrator + install_scaffolding 已就位

# 2. 创建每个 phase 的占位(已跑过可跳过)
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/install_scaffolding.sh

# 3. 干跑 orchestrator,确认 6 个 phase 都被识别为 STUB
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh --dry-run

# 4. 填 phase_0/tasks.yaml
#    - status: STUB → status: READY
#    - 填 files_to_create / files_to_modify / sql_migrations / go_tests / gate_endpoints
# 5. 实现 phase_0/run.sh 与 phase_0/gate.sh 的 TODO 段
# 6. 单独验证 phase_0
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh --only 0

# 7. phase_0 gate 绿了再上 cron
crontab -e
# 末尾追加:
# 30 1 * * * /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh >> /opt/OpenMontage_Voicebox/logs/mvp_dev/cron-stdout.log 2>&1
```

## 重要约定

- **绝不在 tasks.yaml status=STUB 时真跑**:orchestrator 看到 STUB 状态会跳过 phase,但 phase 的 run.sh 自身也有 STUB 防御。
- **state/phase_N.json 是真相**:任何 phase 决策都基于此文件(`--resume` 跳过、`--fresh` 重跑、`interrupted=true` 表示上次第 1 阶段失败)。
- **diff 文件永久保留**:`logs/mvp_dev/diff-phase_*.txt`,事后审计 / cherry-pick 用。
- **任何 gate 失败 halt orchestrator**:不会自动重试,等下次 cron 启动再处理。
- **repo 必须干净**:orchestrator 启动时跑 `git status --porcelain`,有改动直接 exit 2。

## 故障排查

| 症状 | 检查 |
|---|---|
| phase 全 skip | `cat logs/mvp_dev/state/phase_*.json` 看 last_run_exit_code;`--fresh` 强制重跑 |
| phase halt 在 gate | `tail -n 200 logs/mvp_dev/summary-*.log` 看 gate 输出 |
| cron 没跑 | `crontab -l` 看 01:30 行;`tail -n 100 logs/mvp_dev/cron-stdout.log` |
| repo 不干净拒绝跑 | `git -C /opt/OpenMontage_Voicebox status`,把未提交的改动 stash 或 commit |
| 想暂停 cron | `crontab -e` 注释掉 01:30 行;state 文件保留,下次恢复时按 `--resume` 行为继续 |

## 配套对照表

| Phase | §17 项 | 文件 |
|---|---|---|
| 0 | A 微信身份 | phase_0/ |
| 1 | B + H 多租户 + 文件权限 | phase_1/ |
| 2 | C Product/Asset | phase_2/ |
| 3 | D Project/Job | phase_3/ |
| 4 | E Quota | phase_4/ |
| 5 | F + G Agent Gateway + 状态聚合 | phase_5/ |

完整范围说明见 `docs/openmontage_product_video_mvp_golang_scope.md` §17、§19、§20。
