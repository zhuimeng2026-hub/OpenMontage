# Phase 3 — §17.D — Project / Job

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1(§17.D)

## Gate 最小验证

POST /api/video-projects 创建;POST /api/video-projects/:id/storyboard 启动后 GET .../status 状态机单调推进

## 开工步骤

1. 编辑 `tasks.yaml`:
   - 把 `status: STUB` 改成 `status: READY`
   - 填 `files_to_create` / `files_to_modify` / `sql_migrations` / `go_tests` / `gate_endpoints`
2. 编辑 `run.sh` — 把 TODO 段替换成实际 schema / handler 改动
3. 编辑 `gate.sh` — 把 TODO 段替换成实际端到端验证
4. 干跑:`bash scripts/mvp_dev/phase_3/run.sh --fresh && bash scripts/mvp_dev/phase_3/gate.sh`
5. 通过后:`bash scripts/mvp_dev/orchestrator.sh --only 3`
