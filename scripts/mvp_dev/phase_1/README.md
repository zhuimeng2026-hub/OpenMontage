# Phase 1 — §17.B + §17.H — 多租户 + 文件权限

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1(§17.B + §17.H)

## Gate 最小验证

跨 tenant 调用任何资源接口返回 403;无 tenant header 返回 401;signed URL 过期拒绝

## 开工步骤

1. 编辑 `tasks.yaml`:
   - 把 `status: STUB` 改成 `status: READY`
   - 填 `files_to_create` / `files_to_modify` / `sql_migrations` / `go_tests` / `gate_endpoints`
2. 编辑 `run.sh` — 把 TODO 段替换成实际 schema / handler 改动
3. 编辑 `gate.sh` — 把 TODO 段替换成实际端到端验证
4. 干跑:`bash scripts/mvp_dev/phase_1/run.sh --fresh && bash scripts/mvp_dev/phase_1/gate.sh`
5. 通过后:`bash scripts/mvp_dev/orchestrator.sh --only 1`
