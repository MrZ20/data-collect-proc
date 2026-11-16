# vLLM Ascend 文件变化监测

该任务独立于 CI 失败采集：每 15 分钟更新一次 vLLM Ascend `main` 的本地工作目录，检查配置路径并立即上传结果。它不修改 workflow 清单。

路径由私仓 `main:configs/vllm-ascend-changes/paths.yaml` 维护；本仓库的同名文件是初始配置。默认只匹配 `.github/workflows` 根目录的 `.yaml` 和 `.yml` 文件。`*`、`?`、`[]` 只在单个路径层级内匹配；新增监测路径时会比较上一次成功检查的 commit 与当前 commit。每轮读取配置，网络失败时使用上一次缓存并告警。

结果写入私仓 `main` 的 `data/vllm-ascend-changes/YYYY-MM-DD.jsonl`，以 UTC 日期分文件；每次检查完成立即上传，不等待当天结束。每行包含 `time`（UTC）、`commit`、`changed` 和 `files`。文件新增、修改和删除都纳入；重命名分别表示旧路径和新路径。两次检查之间修改后又恢复原样不会被标记。初始化先建立基线，再执行首次检查。

服务器 `/opt/data-collect-proc/state/vllm-ascend-changes/` 保存浅克隆、配置缓存、SQLite 状态和上传队列。每次使用 `git pull --ff-only`，单独保留上次成功检查的 commit。拉取或比较失败不会写入 false 或更新基线；强制推送使 fast-forward 不成立时会报错，需排查上游变更。上传失败保留本地队列，下轮先重试上传，按时间戳去重。单个状态目录通过文件锁避免并发运行。

手动构建和运行：

```bash
docker build -f tasks/vllm-ascend-changes/Dockerfile -t data-collect-vllm-ascend-changes:0.1.1 .
tasks/vllm-ascend-changes/deploy/run.sh
```

运行脚本将服务器现有 token 只读挂载到 `/run/secrets/github_token`，需要私仓 `Contents: read/write` 权限；源仓库是公开的。服务器安装以下 timer，每 15 分钟运行一次：

```bash
sudo install -m 644 tasks/vllm-ascend-changes/deploy/collector.service /etc/systemd/system/ascend-changes-collector.service
sudo install -m 644 tasks/vllm-ascend-changes/deploy/collector.timer /etc/systemd/system/ascend-changes-collector.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ascend-changes-collector.timer
sudo systemctl start ascend-changes-collector.service
journalctl -u ascend-changes-collector.service
```

本地测试在项目虚拟环境：

```bash
.venv/bin/python -m pip install -e 'tasks/vllm-ascend-changes[test]'
.venv/bin/python -m pytest tasks/vllm-ascend-changes/tests -q
```
