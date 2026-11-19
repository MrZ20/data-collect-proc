# 数据采集与处理

每个任务单独维护依赖、Dockerfile、部署配置和本地状态。`tasks/vllm-ascend-ci` 初筛 GitHub Actions 失败 job；`tasks/vllm-ascend-changes` 每 15 分钟检查配置的文件路径并立即上传结果，详见[任务说明](tasks/vllm-ascend-changes/README.md)。

镜像手动构建。本仓库没有 GitHub Actions 工作流。`zsl-arm64` 已启用 systemd timer，每小时启动一次容器，采集并上传数据。

## 仓库分工

- [MrZ20/data-collect-proc](https://github.com/MrZ20/data-collect-proc)：公开的采集程序、测试、镜像和部署文件。
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
- conclusion 为 cancelled 的 job 不纳入候选，也不参与连续失败统计；run 取消不影响其中 conclusion 为 failure 的 job。
- 已登记 workflow 内未登记的 job/step 默认纳入。没有失败 step 时不凭 job 结论生成候选；失败 job 缺步骤会补读一次，仍为空则进入待补查队列，不补造步骤。
- 普通排除步骤和链首步骤连续失败达到 `repeat_threshold` 后重新进入候选；链后续步骤不单独触发。`repeat_exclude` 可以为具体 job 配置不参加连续失败统计的步骤白名单；要同时排除单次失败，仍须将该步骤写入 `exclude`。成功打断连续失败，跳过不计数，同一个 run 的重跑不增加独立样本数（采用首次观察到的有效结果）。整 job 排除不会被重新纳入。

````yaml
repository: vllm-project/vllm-ascend
repeat_threshold: 10
workflows:
  - name: E2E
    file: .github/workflows/pr_test.yaml
    exclude_jobs: [ci-gate]
    jobs:
      - name: example-test-job
        steps: [Set up job, Run tests, Upload logs, Complete job]
        exclude: [Run tests]
        exclude_chains:
          - chain1: [Run tests, Upload logs]
````

链配置是 agent 维护的初筛假设，不代表程序证明了因果。首版没有证据充分的链，因此实际配置中的链暂时为空。

清单由本次人工/agent 分析直接初始化，之后由 agent 在私仓维护；项目不提供生成或回填 workflow/step 清单的初始化功能。当前登记 44 个 workflow、191 条 job 规则，均已核对为 `main` 上仍存在、Actions 中启用且可独立触发的 workflow。13 个仅含 `workflow_call` 的复用定义不单列，它们的运行时 job 归在实际调用的 workflow 下。审查对应上游 main 提交 `c3d1d1fc1dbaee5c38c0601d23be99f14129fa46`。`file` 是 workflow 文件路径，用它匹配 run，避免 `(PR) 21990` 等动态显示名导致漏采，也无需额外请求注册表；候选的 `workflow` 统一使用清单名。这里按 `main` 的定义确定采集范围，仍采集这些 workflow 的 PR、分支、定时和手动运行。

此次从真实运行记录补入 15 条缺项 job 规则，新增 273 个步骤关联，移除 6 条重复 job 占位规则，保留所有现有排除项。目前还有 54 条 job 规则未取得实际步骤：51 条在采样历史中只匹配到 skipped job，3 条匹配到已完成但步骤为空的记录；不编造步骤。清单是初筛依据，已登记 workflow 内的新 job/step 默认纳入候选，未登记 workflow 不获取 job 或生成候选。数据目录、SQLite 建表、首次扫描起点、游标和上传队列等必要的数据初始化仍由程序正常完成。

## 本地运行

要求 Python 3.12+，所有本地安装和测试使用项目虚拟环境：

````bash
python3 -m venv .venv
.venv/bin/python -m pip install -e 'tasks/vllm-ascend-ci[test]'
.venv/bin/python -m pytest tasks/vllm-ascend-ci/tests -q
.venv/bin/ascend-ci validate-policy configs/vllm-ascend-ci/workflows.yaml
````

token 文件只包含一行 token，不放进仓库。可以用 `--token-file /path/to/token`，也可以设置 `GITHUB_TOKEN_FILE`。分离读写凭据时使用 `GITHUB_READ_TOKEN_FILE` 和 `GITHUB_WRITE_TOKEN_FILE`：前者读取源仓库，后者读取私仓配置并上传数据。

````bash
# 从私仓同步规则，采集一轮并上传。
.venv/bin/ascend-ci --token-file /path/to/token collect

# 采集进本地 outbox，暂不上传；之后只重试上传。
.venv/bin/ascend-ci --token-file /path/to/token collect --no-upload
.venv/bin/ascend-ci --token-file /path/to/token publish
````

部署参数使用 `--config /path/to/config.yaml`，示例见任务的 `deploy/config.example.yaml`。本地不传配置时，SQLite 和配置缓存默认保存在 `./state/vllm-ascend-ci/`。容器配置使用 `/state`。

## 采集与输出

首次默认扫描最近一小时，加十分钟重叠窗口，将实际起点持久化为 `collection_started_at`。以后按游标补采，并重叠十分钟；每轮同时回查最近两天创建的 run，及时发现 attempt 增加的重跑。所有批量扫描都不会早于首次起点。两天是原始 run 的创建时间范围，不是重跑开始时间；更早且已完成的 run 不追查重跑。已登记但未完成的 run 持续跟踪到结束，即使已超出两天。停机超过两天时仍补齐停机期间的新 run。配置更新作用于之后处理的执行，不自动重新分析已处理 job。

旧版状态没有保存首次起点，不能从移动后的游标准确还原；通用升级会保守使用旧游标作为下界并记录警告。此次服务器升级根据首次真实采集日志恢复了准确下界 `2026-11-16T07:28:40Z`。

程序只读取状态，不下载日志或 PR diff。候选按日写入私仓 `main` 分支：

````text
data/vllm-ascend-ci/YYYY-MM-DD.jsonl
reports/vllm-ascend-ci/YYYY-MM-DD.md
````

每条 JSONL 是一个 `run_id`、`run_attempt` 分组，包含 `workflow`、`base_repo`、`head_sha`、`status`、`conclusion` 和 `jobs`；Job 保留 UTC 完成时间 `time`、名称、结论、失败步骤、runner 和 URL，连续失败时额外记录 `consecutive_failures`。同一运行跨 UTC 日时分别保存在对应日期，分析时合并；后续 Job 增量合并，同 attempt 完成后更新已有分组状态。旧数据中无法确认的运行状态为 null。`failed_steps` 保留全部失败步骤。

SQLite 保存固定采集起点、游标、待完成运行、去重、连续失败记录、压缩的 HTTP 缓存和待上传记录。上传失败不会丢失 outbox。每批 JSONL 与 Markdown 同一次提交，重试按 job URL 去重，不强制覆盖远端分支。没有新增数据则不提交。使用文件锁防止同一状态目录被同时采集。

采集将创建时间范围切成固定整点窗口，每页批量获取 100 条 run 元数据；达到 GitHub 的 1000 条搜索上限时继续拆分窗口。按 run ID 去重，检查分页总数与实际条数；发现翻页变化就重读该窗口一次，仍不一致则本轮失败且不推进游标。固定窗口结合 ETag 条件请求，未变化的页面使用 304 和本地缓存；仍会发 HTTP 请求，但[认证的 304 不消耗主要 API 限额](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate)。缓存也会重新验证，不能用旧缓存掩盖网络失败。随后在本地筛选：跳过未登记 workflow、已处理的相同 attempt、尚未执行的首次 queued run 和 skipped run；成功 run 只有在该 workflow 配置了需要连续失败统计的步骤时才获取 job。随后对新 run、未完成 run 和 attempt 改变的 run，分页获取所有 attempt 的 job/step、runner 和链接，不对每个步骤单独查询；对失败且缺步骤的 job 先单独补读一次。Job 列表跨页核对总数和唯一 ID，发现不一致则补查对应 run。不做连续失败统计的成功 run 中被 `continue-on-error` 吞掉的失败不保证捕获。

HTTP 连接复用。读取请求对连接错误、超时、临时 5xx 和限流最多尝试三次；按退避、`Retry-After` 或剩余额度重置时间等待。需要等待超过 60 秒则退出，保留状态供下一轮重试。写操作不盲目重放：提交响应丢失时先重读远端、按 URL 去重，再有限重试整批上传。

Run 列表扫描失败时保留游标，并尝试发布已有 outbox。单个 Run 的 Job 请求、详情或旧 attempt 状态查询出错时保存待补查记录，同轮其他完整数据仍可入库和发布。暂缺 Jobs/Steps 或已结束 Run 仍有未完成 Job 时先在下一轮补查，两小时后降到每六小时，24 小时仍不完整则记录告警并结束补查。暂时的 API 请求错误会继续留在队列。Run 分组保留 GitHub 给出的状态，补查状态单独存储。发现重跑时，仅补查已知但尚无最终状态的旧分组。

日志分别记录配置加载、Run 列表、Run/Job 详情、写入 SQLite 和上传的耗时与请求数。配置日志包括来源和摘要，成功输出包括补查数量、最老补查等待秒数和详情错误数。详情阶段达到 20 分钟预算时保留已读结果并发布，扫描游标留待下一轮续跑。

## 手动构建与部署

从仓库根目录构建（在 ARM64 机器上构建即得到 ARM64 镜像）：

````bash
docker build -f tasks/vllm-ascend-ci/Dockerfile \
  -t data-collect-vllm-ascend-ci:0.1.5 .
````

服务器约定目录：

````text
/opt/data-collect-proc/
  source/                         # 本仓库代码
  config.yaml                     # 从 config.example.yaml 复制
  secrets/github_token            # 不进入 source 或镜像
  state/vllm-ascend-ci/            # 持久化 SQLite/outbox
````

镜像以 UID/GID 10001 运行。服务器管理员准备权限：

````bash
install -d -m 755 /opt/data-collect-proc/state/vllm-ascend-ci
install -d -m 700 /opt/data-collect-proc/secrets
chown -R 10001:10001 /opt/data-collect-proc/state/vllm-ascend-ci
chown 10001:10001 /opt/data-collect-proc/secrets/github_token
chmod 400 /opt/data-collect-proc/secrets/github_token
````

手动执行一轮：

````bash
cd /opt/data-collect-proc/source
tasks/vllm-ascend-ci/deploy/run.sh
````

`run.sh` 只依赖 Docker Engine，无需 Compose；服务器 `zsl-arm64` 使用此入口。密钥已配置在服务器 `secrets/github_token`，每次启动时只读挂载到容器的 `/run/secrets/github_token`，采集器通过 `GITHUB_TOKEN_FILE` 自动读取。安装了 Compose 的机器也可使用提供的 `deploy/compose.yaml`。`run.sh publish` 只重试上传，`run.sh collect --no-upload` 只采集。

需要替换服务器根目录或镜像时设置 `DEPLOY_DIR`、`IMAGE`。复制代码时不要复制 `.venv`、本地状态或 token；密钥单独传输并只读挂载。无需 NPU、特权容器或开放入站端口。

源仓库为公开仓库，读取 token 无需源仓库写权限。私仓 token 限定 `MrZ20/data_repo`，需要 `Contents: read/write`。这仍是仓库级权限，不是目录级权限。采集器不需要公开分析仓库的 token。

服务器已安装并启用以下 service/timer，重启后会自动恢复。部署到其他服务器时执行：

````bash
sudo install -m 644 tasks/vllm-ascend-ci/deploy/collector.service \
  /etc/systemd/system/ascend-ci-collector.service
sudo install -m 644 tasks/vllm-ascend-ci/deploy/collector.timer \
  /etc/systemd/system/ascend-ci-collector.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ascend-ci-collector.timer
sudo systemctl start ascend-ci-collector.service
systemctl list-timers ascend-ci-collector.timer
journalctl -u ascend-ci-collector.service
````

定时器在每小时第七分钟附近运行（附加最多 60 秒随机延迟），完成后退出容器，SQLite 和上传队列保存在持久化目录。采集或上传失败由现有网络重试机制处理，未上传数据保留到下轮。每个新增任务使用自己的镜像、服务、配置和状态目录；暂不引入共享任务框架。

## 已完成的验证（2026-11-16）

- 本地项目虚拟环境和 `zsl-arm64` 的 0.1.1 镜像均通过 43 项测试，覆盖首次边界、两天内重跑、窗口外待完成运行、翻页变化、workflow 文件范围及动态显示名筛选、ETag 缓存、网络退避及上传去重。
- 首次部署曾在 Python 3.12 基础容器内手动创建虚拟环境、安装包、运行测试与真实采集，再将成功流程写入 Dockerfile；此次按 Dockerfile 重新构建验证。
- 最终镜像真实采集扫描 2181 个 run，仅查询 28 个 run 的 job：共 59 次 API 请求，其中 12 次元数据请求返回 304；处理 130 个新 job，新增并上传 5 条候选，上传队列归零，固定起点保持 `2026-11-16T07:28:40Z`。
- 服务器旧 SQLite 已备份为 `state-before-0.1.1.sqlite3`，恢复固定首次边界并将 `recheck_days` 改为 2。
- 上述历史验证使用镜像 `data-collect-vllm-ascend-ci:0.1.1`；当前部署版本为 `0.1.3`，继续使用 `arm64` 和 `10001:10001`。
- 服务器代码、配置、状态和运行时 token 已放置到上述目录；`ascend-ci-collector.timer` 已启用每小时采集上传。token 不在 Git 或镜像中。
