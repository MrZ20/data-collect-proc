# 数据采集与处理

每个任务单独维护依赖、Dockerfile、部署配置和本地状态。当前只有 `tasks/vllm-ascend-ci`，用于把 GitHub Actions 运行时的失败 job 初筛后交给 agent 分析。

镜像手动构建。本仓库没有 GitHub Actions 工作流。部署文件中的 systemd timer 需要手动安装、启用。

## 仓库分工

- 本仓库：采集程序、测试、镜像和部署文件；目前是本地 Git 仓库。
- [MrZ20/data_repo](https://github.com/MrZ20/data_repo)：私有规则和采集结果。
- [MrZ20/ascend-ci-monitor](https://github.com/MrZ20/ascend-ci-monitor)：后续 agent 公开发布分析；采集器不向它写入。

两个远端仓库重置前的完整文件树已保存在各自的 `archive/2026-11-16` 分支下，目录为 `archive/2026-11-16/`。原历史也保留，未强制重写。公开仓库的新 `main` 留空；私仓的新 `main` 保存配置。

## 规则

运行时规则在私仓 `main:configs/vllm-ascend-ci/workflows.yaml`。本仓库同路径文件是首版种子，后续以私仓配置为准。

- `steps` 仅是实际运行步骤清单，包含初始化、容器和收尾步骤，不是白名单。
- `exclude_jobs` 排除整个 job，优先级最高；E2E 的 `ci-gate` 已排除。
- job 内 `exclude` 排除单次失败步骤。
- `exclude_chains` 是同一次 job 执行中按顺序全部失败的步骤链。中间可有其他步骤，但链中的成功或跳过不算失败。先从原始失败集合匹配，再执行排除；链外失败仍进入候选。
- job 名和普通 step 排除支持 `*` 通配符，也优先匹配完整字面名称；链内 step 使用精确名称。
- 未登记的 job/step 默认纳入。没有失败 step 时不凭 job 结论生成候选；失败 job 缺步骤会补读一次，仍为空则记录警告，不补造步骤。
- 普通排除步骤和链首步骤连续失败达到 `repeat_threshold` 后重新进入候选；链后续步骤不单独触发。成功打断连续失败，跳过不计数，同一个 run 的重跑不增加独立样本数（采用首次观察到的有效结果）。整 job 排除不会被重新纳入。

```yaml
repository: vllm-project/vllm-ascend
repeat_threshold: 10
workflows:
  - name: E2E
    exclude_jobs: [ci-gate]
    jobs:
      - name: example-test-job
        steps: [Set up job, Run tests, Upload logs, Complete job]
        exclude: [Run tests]
        exclude_chains:
          - chain1: [Run tests, Upload logs]
```

链配置是 agent 维护的初筛假设，不代表程序证明了因果。首版没有证据充分的链，因此实际配置中的链暂时为空。

首版采样了 96 次运行（包含补充的 5 次 E2E 运行），形成 57 个 workflow 条目、198 条 job 规则。12 个条目没有观察到 job，一些条件 job 只有跳过记录，所以 `steps` 为空。清单不宣称覆盖全部运行分支。E2E 的 `run-selected-tests*` 规则使用在同一 workflow 的 CPU 测试中观察到的两个用例步骤名进行排除；首次真实采集又补入了 upstream 测试、旧版 mypy 和 CODEOWNERS 审批步骤。其余尚未观察到的 NPU 步骤不编造。新步骤仍默认纳入。动态矩阵名已按保守前缀合并，runner 和矩阵不参与筛选。

## 本地运行

要求 Python 3.12+，所有本地安装和测试使用项目虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e 'tasks/vllm-ascend-ci[test]'
.venv/bin/python -m pytest tasks/vllm-ascend-ci/tests -q
.venv/bin/ascend-ci validate-policy configs/vllm-ascend-ci/workflows.yaml
```

token 文件只包含一行 token，不放进仓库。可以用 `--token-file /path/to/token`，也可以设置 `GITHUB_TOKEN_FILE`。分离读写凭据时使用 `GITHUB_READ_TOKEN_FILE` 和 `GITHUB_WRITE_TOKEN_FILE`：前者读取源仓库，后者读取私仓配置并上传数据。

```bash
# 合并新的运行时步骤，保留已有排除规则；不自动上传配置。
.venv/bin/ascend-ci --token-file /path/to/token discover \
  --output configs/vllm-ascend-ci/workflows.yaml --samples 2

# 从私仓同步规则，采集一轮并上传。
.venv/bin/ascend-ci --token-file /path/to/token collect

# 采集进本地 outbox，暂不上传；之后只重试上传。
.venv/bin/ascend-ci --token-file /path/to/token collect --no-upload
.venv/bin/ascend-ci --token-file /path/to/token publish
```

部署参数使用 `--config /path/to/config.yaml`，示例见任务的 `deploy/config.example.yaml`。本地不传配置时，SQLite 和配置缓存默认保存在 `./state/vllm-ascend-ci/`。容器配置使用 `/state`。

`discover` 从当前默认分支的文件列表过滤已删除的历史 workflow，再读取实际运行记录；不解析源码中的复用关系。重新发现会保留已有通配规则，并补充新观察到的精确 job 名，agent 可继续合并冗余条目。

## 采集与输出

首次默认扫描最近一小时，加十分钟重叠窗口。以后使用本地游标，持续跟踪未完成运行，并每 24 小时回扫最近七天以发现旧 run 的重跑。更早的历史重跑不保证捕获。配置更新作用于之后处理的执行，不自动重新分析已处理 job。

程序只读取状态，不下载日志或 PR diff。候选按日写入私仓 `data/vllm-ascend-ci` 分支：

```text
data/YYYY-MM-DD.jsonl
reports/YYYY-MM-DD.md
```

每条 JSONL 只有 `time`（UTC）、`workflow`、`job`、`failed_steps`、`runner`、`base_repo`、`url`；连续失败重新纳入时加 `consecutive_failures`。`failed_steps` 保留该 job 的全部失败步骤，便于 agent 理解上下文。agent 沿 job 链接自行读取日志等信息。

SQLite 保存采集游标、待完成运行、去重、连续失败记录和待上传记录。上传失败不会丢失 outbox。每批 JSONL 与 Markdown 同一次提交，重试按 job URL 去重，不强制覆盖远端分支。没有新增数据则不提交。使用文件锁防止同一状态目录被同时采集。

采集先按每页 100 条批量获取 run 元数据，再在本地筛选：跳过已处理的相同 attempt、尚未执行的首次 queued run 和 skipped run；成功 run 只有在该 workflow 配置了需要连续失败统计的步骤时才获取 job。随后一次分页 job 列表获取所有 attempt 的 job/step、runner 和链接，不对每个步骤单独查询；只对失败且缺步骤的 job 补读一次。不做连续失败统计的成功 run 中被 `continue-on-error` 吞掉的失败不保证捕获。

HTTP 连接复用。读取请求对连接错误、超时、临时 5xx 和限流最多尝试三次；按退避、`Retry-After` 或剩余额度重置时间等待。需要等待超过 60 秒则退出，保留状态供下一轮重试。写操作不盲目重放：提交响应丢失时先重读远端、按 URL 去重，再有限重试整批上传。

## 手动构建与部署

从仓库根目录构建（在 ARM64 机器上构建即得到 ARM64 镜像）：

```bash
docker build -f tasks/vllm-ascend-ci/Dockerfile \
  -t data-collect-vllm-ascend-ci:0.1.0 .
```

服务器约定目录：

```text
/opt/data-collect-proc/
  source/                         # 本仓库代码
  config.yaml                     # 从 config.example.yaml 复制
  secrets/github_token            # 不进入 source 或镜像
  state/vllm-ascend-ci/            # 持久化 SQLite/outbox
```

镜像以 UID/GID 10001 运行。服务器管理员准备权限：

```bash
install -d -m 755 /opt/data-collect-proc/state/vllm-ascend-ci
install -d -m 700 /opt/data-collect-proc/secrets
chown -R 10001:10001 /opt/data-collect-proc/state/vllm-ascend-ci
chown 10001:10001 /opt/data-collect-proc/secrets/github_token
chmod 400 /opt/data-collect-proc/secrets/github_token
```

手动执行一轮：

```bash
cd /opt/data-collect-proc/source
tasks/vllm-ascend-ci/deploy/run.sh
```

`run.sh` 只依赖 Docker Engine，无需 Compose；服务器 `zsl-arm64` 使用此入口。安装了 Compose 的机器也可使用提供的 `deploy/compose.yaml`。`run.sh publish` 只重试上传，`run.sh collect --no-upload` 只采集。

需要替换服务器根目录或镜像时设置 `DEPLOY_DIR`、`IMAGE`。复制代码时不要复制 `.venv`、本地状态或 token；密钥单独传输并只读挂载。无需 NPU、特权容器或开放入站端口。

源仓库为公开仓库，读取 token 无需源仓库写权限。私仓 token 限定 `MrZ20/data_repo`，需要 `Contents: read/write`。这仍是仓库级权限，不是目录级权限。采集器不需要公开分析仓库的 token。

后续决定开启定时采集时，手动安装 timer；本次验证没有启用它：

```bash
sudo install -m 644 tasks/vllm-ascend-ci/deploy/collector.service \
  /etc/systemd/system/ascend-ci-collector.service
sudo install -m 644 tasks/vllm-ascend-ci/deploy/collector.timer \
  /etc/systemd/system/ascend-ci-collector.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ascend-ci-collector.timer
journalctl -u ascend-ci-collector.service
```

定时器在每小时第七分钟附近运行。每个新增任务使用自己的镜像、服务、配置和状态目录；暂不引入共享任务框架。

## 已完成的验证（2026-11-16）

- 本地项目虚拟环境与 `zsl-arm64` 的 Python 3.12 基础容器均通过 33 项测试，覆盖步骤链、去重、增量采集、重跑统计、网络退避和上传响应丢失。
- 在基础容器内先手动创建虚拟环境、安装包、运行测试与真实采集，再将成功流程写入 Dockerfile。
- 批量筛选后的增量验证：扫描 308 个 run，仅查询 40 个 run 的 job，总共 75 次 API 请求；新增 2 条候选，上传队列归零。
- 最终 Docker 镜像通过 `run.sh` 实测：扫描 219 个 run，仅查询 36 个 run 的 job，总共 98 次 API 请求；新增 3 条候选，上传队列归零。这是不同时间窗口的实测记录，不是固定数据集上的性能对照。
- 镜像为 `data-collect-vllm-ascend-ci:0.1.0`，架构 `arm64`，运行身份 `10001:10001`；镜像 ID 为 `sha256:018ee95be2f8b7c009547aa70873f152aabd5d67a39b85369c90500da6b2e338`。
- 服务器代码、配置、状态和运行时 token 已放置到上述目录；没有安装或启用定时器。token 不在 Git 或镜像中。
