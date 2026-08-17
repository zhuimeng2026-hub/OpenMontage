# FrameFlow 生产更新脚本

服务器更新使用仓库中的 `scripts/update_frameflow_server.sh`。脚本会拒绝脏工作区，默认执行 fast-forward 拉取、Python 上传测试、BFF Go 测试和临时目录构建；随后备份旧二进制、重启 BFF/MCP，并检查 HTTP 状态和 systemd MainPID 与端口监听 PID 是否一致。

首次取得脚本或代码时执行：

```bash
cd /opt/OpenMontage
git pull --ff-only origin main
bash scripts/update_frameflow_server.sh --no-pull
```

之后每次更新可直接执行（脚本会自行 fast-forward 拉取）：

```bash
cd /opt/OpenMontage
bash scripts/update_frameflow_server.sh
```

常用选项：

```bash
bash scripts/update_frameflow_server.sh --help
bash scripts/update_frameflow_server.sh --no-pull
bash scripts/update_frameflow_server.sh --branch main --repo /opt/OpenMontage
bash scripts/update_frameflow_server.sh --skip-tests
```

Python 解释器可用 `PYTHON_BIN=/path/to/python3` 覆盖；默认优先使用 `/root/.pyenv/versions/3.11.8/bin/python3`，否则使用 `python3`。脚本会在失败时恢复 `/var/backups/frameflow/` 下带时间戳的旧 BFF 二进制并重新启动 BFF，同时输出脱敏后的相关 journal。临时构建目录由退出 trap 清理；可用 `BACKUP_DIR=/path` 覆盖备份目录。

脚本固定分别重启 `frameflow-bff.service` 与 `openmontage-mcp.service`，不检测或管理遗留 `mcp-server.service`，两者按独立服务验收：8080 `/api/me` 返回 200，8900 `/mcp` 在无 token 时返回 401。脚本不会打印环境变量或服务密钥。

限制：脚本要求服务器当前已检出目标分支；若工作区有未提交修改会停止。`/api/me` 必须在当前生产配置下返回 200，否则会触发回滚；请先确认测试账号或健康检查配置。旧二进制备份不会自动删除，需要按服务器保留策略手动清理。
