# 关注的 PR

每 15 分钟检查一次。运行时从私仓 main 读取 [watch.yaml](https://github.com/MrZ20/data_repo/blob/main/configs/github-work-watch/watch.yaml)，验证后原子替换本地副本。配置读取失败会中止本轮。编辑 `users` 可增删 GitHub 登录名，编辑 `paths` 和 `exclude_paths` 可增删仓库相对文件名和目录。排除项优先于关注路径，目录包含所有子目录。清单都允许为空。

`paths` 作用于 vllm-project/vllm-ascend。首次按最近 14 天有活动的开放 PR 初始化。之后用 Git 批量读取 PR head 引用，在本地按 SHA 找出变化。需要检查路径时，批量获取各 PR 的目标分支和候选 head 的 commit 与 tree，以两者的共同祖先为基线比较路径，包括删除和移入移出。连续两次不同 head 命中后，后续推送先更新时间，最多每四小时复核路径一次。修改路径清单会在下轮重新匹配已发现的开放 PR。

`users` 匹配令牌可见仓库中被直接 @ 或被直接请求 Review 的人。匹配任一人员条件即可收录。令牌所属账号自己创建的 PR 不收录。人员条件消失时重新判断路径。对已关注 PR 批量检查状态，即使代码未变，也能发现草稿状态变化、合并和关闭。

结果覆盖私仓 [prs.json](https://github.com/MrZ20/data_repo/blob/main/data/github-work-watch/prs.json)，按 PR 编号降序。每项只有 `pr`、`code_updated_at`（UTC）和 `status`。首次收录使用 head 的提交时间，后续 SHA 变化使用发现时间。合并或关闭后保留最终记录，结束跟踪。内容有变化才提交。持久化目录保存引用基线、匹配结果、Git 对象和上次发布摘要，上传失败在后续轮次重试。

从代码仓库根目录部署。沿用服务器已有的只读 token 挂载，令牌需能读取源 PR 并写入私仓。服务在每小时第 04、19、34、49 分钟运行。

````sh
docker build -f tasks/github-work-watch/Dockerfile -t data-collect-github-work-watch:0.1.0 .
install -d -o 10001 -g 10001 -m 750 /opt/data-collect-proc/state/github-work-watch
install -m 644 tasks/github-work-watch/deploy/collector.service /etc/systemd/system/github-work-watch.service
install -m 644 tasks/github-work-watch/deploy/collector.timer /etc/systemd/system/github-work-watch.timer
systemctl daemon-reload
systemctl enable --now github-work-watch.timer
systemctl start github-work-watch.service
````
