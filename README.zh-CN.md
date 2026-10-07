[English](README.md) | [日本語](README.ja.md) | **简体中文** | [Français](README.fr.md)

# Loop X Engineering

![CI](https://github.com/encoreshao/loop-engineering/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/encoreshao/loop-engineering)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Shell](https://img.shields.io/badge/shell-bash-4EAA25)

Loop X Engineering 的使命是把被 issue 分诊占用的时间还给你：它是一位常驻、
无需值守的队友，每个工作日处理你的 GitLab 队列，确保分配给你的事项不会无人问津——
提交修复、回答问题，或标记出真正需要你判断的事项——让你的注意力只花在真正重要的地方。
本地 Web 仪表盘让你可以观察它的工作、回顾它做过的一切，并通过界面手动完成所有配置，
无需编辑 JSON。

它的设计目标是可以放心地无人值守运行：它从不合并自己的 merge request，
从不给自己分配新 issue，并且只会触碰你明确告诉过它的项目。

## 目录

- [工作原理](#工作原理)
- [循环](#循环)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [配置](#配置)
- [运行](#运行)
- [仪表盘](#仪表盘)
- [连接器](#连接器)
- [脚本参考](#脚本参考)
- [安全边界](#安全边界)
- [测试](#测试)
- [项目文档](#项目文档)
- [许可证](#许可证)



## 工作原理

每次定时运行（`run-loop-now.sh gitlab-loop`）：

1. 列出配置中每个项目别名下、分配给你所配置用户名的所有未关闭 GitLab issue。
2. **逐个处理，绝不并行**，遵循 [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md) 中的分步决策流程。
3. 对每个 issue 只执行以下其中一项：
  - **修复**——在隔离的 git worktree 中、在 `loop/issue-<iid>` 分支上进行，只有在项目自身的 lint/test 命令通过后才会创建 merge request。
  - **回答**——当请求无需改代码时（提问、状态查询），发布一条 GitLab 评论。
  - **升级**——当请求含义不明确或验证失败时，发布一条 GitLab 评论请求澄清。
4. 每个 issue 发送一条 Slack 消息，并在运行结束时发送一份汇总（每次运行都会发送，即使当天早上没有任何分配给你的 issue）。
5. 更新 [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md) 和 `outputs/daily-review.md`，让下一次运行——以及你——了解发生了什么。

可复用的跨运行经验（修复模式、坑点）会通过 `bin/memory_store.py` 按 issue 记录为 markdown 任务记忆文件（在此格式出现之前记录的条目仍通过 `bin/project_memory.py` 读取），因此后续运行会比上一次更聪明。

第二个独立的循环（`run-loop-now.sh topic-loop`）不监控 GitLab，而是监控更广泛网络上的任意主题——参见 [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md)。

第三个循环（`run-loop-now.sh inbox-triage-loop`）对 Gmail 和 Outlook 收件箱进行分诊：它将每封新的未读邮件归类到一个 `Loop/*` 标签，为紧急邮件起草（绝不发送）一封串联回复，并通过 Slack 汇总和仪表盘的 **Loops → Inbox Triage** 页面进行报告——参见 [`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md)。

## 循环

`config/loops.json.template` 内置 7 个循环；每个在仪表盘的 **Loops** 下都有独立页面，并在 `docs/tasks/` 下有各自的规格说明。调度可在该页面修改，下表为模板中的默认值。

| 循环 | 作用 | 所需条件 | 默认调度 | 对 Loop X 之外的写入 |
| --- | --- | --- | --- | --- |
| GitLab issues | 处理分配给你的 issue：修复、答复或上报 | GitLab 配置（`~/.gitlab/config.json`） | 工作日 10:00 | 分支与合并请求（绝不合并）、issue 评论、Slack |
| Topic monitor | 在网上调研你的主题并发送每日简报 | 主题（`topics.json`） | 每天 10:00 | Slack 摘要 |
| Inbox triage | 为新邮件打标签，并为紧急邮件起草回复（默认禁用） | 邮箱（`mail` 能力） | 工作日 09:00 | 邮件标签与草稿（绝不发送） |
| Daily Digest | 一份晨间简报：待办、分配给你的 issue、等你评审的 MR、今天的会议、Loop X 昨天做了什么（默认禁用） | `issues` 连接器（日历可选） | 工作日 09:30 | 仅通知 |
| MR Review | 预先评审你担任评审人的合并请求（默认禁用） | `merge_requests` 连接器（GitLab） | 每 2 小时 | 仅 GitLab 草稿备注；从不发布、批准或发表普通备注 |
| Pipeline Doctor | 诊断所跟踪项目和你名下 MR 中失败的 CI 流水线，并标出反复出现的失败（默认禁用） | `pipelines` 连接器（GitLab） | 每小时 | 仅通知 |
| RSS Watch | 按你的兴趣为订阅源新条目排序并发送简短摘要（默认禁用） | `feed` 连接器（RSS） | 每天 08:00 | 仅通知 |
| Calendar Prep | 每次会议前发送准备摘要：议程、关联的 GitLab 工作、与参会者的近期邮件、上次的跟进事项（默认禁用） | `calendar` 连接器（Google 日历），可选 GitLab 和邮箱 | 每 15 分钟 | 仅通知 |
| Release Notes | 跟踪的项目有新标签时，根据上一个标签以来合并的 MR 编写发布说明（默认禁用） | `merge_requests` 连接器（GitLab） | 每小时 | 本地 Markdown 文件 + 通知 |
| Stale Work Sweeper | 每周一列出停滞的 GitLab 议题和 MR 以及等待你审查的 MR；不调用模型（默认禁用） | `issues` 连接器（GitLab） | 每周一 09:00 | 仅通知 |

后四个是 LoopKit 插件（`bin/loopkit.py`、`bin/loop_plugins/`）：模型在密封环境中运行（无工具、无 MCP 服务器），每个条目相互隔离，单个失败不会中断整次运行，并通过该循环的 **Notify via** 连接器发送通知。详见 [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md#loopkit)。

## 环境要求

- macOS（定时任务和仪表盘都以 `launchd` 代理方式运行）
- Python 3.12+——本仓库自身的代码**仅依赖标准库**，运行无需 `pip install`
- `git` 2.42+（worktree、push-options）
- 一个 GitLab 账号，以及对需要跟踪的项目有权限的个人访问令牌
- （可选）一个 Slack incoming webhook，用于运行通知
- `[encore-skills](https://github.com/encoreshao/encore-skills)` 中的 `[gitlab-config](https://github.com/encoreshao/encore-skills/tree/main/skills/gitlab-config)` skill——本循环唯一的外部依赖，由 `setup.sh` 部署到 `~/.encore-skills`。随时可在仪表盘的 **Settings → Skills** 页面确认它是否确实已安装。
- `pytest`——仅开发时需要，用于运行本仓库自己的测试套件



## 快速开始

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
```

将本仓库克隆到 `~/.loop-engineering`（传入 `--dir <path>` 可指定其他位置），并运行 `bin/scripts/setup.sh`，它会安装 `gitlab-config` skill，并从模板生成 `projects.json`/`topics.json` 骨架。随后它会配置本地 nginx 反向代理，并将仪表盘作为常驻 `launchd` 代理启动，因此这一条命令结束时仪表盘就已可访问并在运行——传入 `--skip-nginx` 和/或 `--skip-launchd-daemons` 可跳过其中任一步。（定时 GitLab 循环和主题监控*不会*自动启动，因为它们会在你填写 `projects.json`/`topics.json` 之前就开始执行——配置完成后，请在仪表盘的 **Settings → Daemons** 页面自行启动。）之后再次运行同一命令只会拉取最新的 `main`，而不会重新克隆。

已经安装过，只想更新？加上 `--upgrade`：

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash -s -- --upgrade
```

步骤与上面相同，但如果 `--dir` 下尚未安装，会立即失败而不是悄悄重新克隆，并且会刷新本项目当前已加载的每一个 launchd 代理——而不仅仅是仪表盘。仪表盘（常驻服务器）会真正重启（`launchctl kickstart -k`），不同于单纯的 `launchctl load`——后者对已在运行的代理不起作用。`com.hermes.loop-engineering`——如果你已在 **Settings → Daemons** 页面启用，它是运行 `loops.json` 中所有已注册循环的唯一调度器——只会重新加载其注册（`unload` + `load -w`），绝不会被 kickstart，因为那会立刻针对真实的 GitLab/Slack 触发一次计划外的运行，而不是等待调度器自己的下一次轮询。`--upgrade` 还会迁移统一调度器出现之前遗留的已渲染 plist（仍指向已删除的 `run-loop.sh` 的那种），并在旧的 `com.hermes.loop-engineering-topic-monitor` 守护进程仍从迁移前安装着时将其移除。

想先亲眼看着克隆过程？

```bash
git clone https://github.com/encoreshao/loop-engineering.git
cd loop-engineering
bin/scripts/setup.sh
```

已经安装了 skill，只需要配置骨架？

```bash
bin/scripts/setup.sh --skip-skills-install
```

完成后，打开仪表盘的 **Settings → Skills** 页面确认所需内容是否都已安装——它会实时检查，无需猜测。

**已经在使用 Claude Code？** 粘贴以下内容，而不必自己运行命令：

> 帮我克隆并设置 [https://github.com/encoreshao/loop-engineering](https://github.com/encoreshao/loop-engineering)：运行它的在线安装程序
> （`curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash`），
> 然后帮我在 `~/.loop-engineering/projects.json` 中填写我自己的 GitLab 项目，并在 `~/.gitlab/config.json` 中填写我的 GitLab 令牌。



### 卸载

```bash
bin/scripts/uninstall.sh                 # or: curl -fsSL .../uninstall.sh | bash
```

卸载并移除本仓库的 `launchd` 代理，如果你运行过 `setup-nginx.sh` 则撤销其更改，并删除整个 `~/.loop-engineering` 文件夹——代码、配置和运行历史一并删除——传入 `--keep-config` 可保留它们（例如你准备重新安装）。可安全地重复运行。

## 目录结构

使用默认安装路径时，所有内容都位于同一个文件夹下：

```
~/.loop-engineering/            # install.sh's clone target
├── bin/, docs/, tests/, ...    # this repo's own code (tracked in git)
├── projects.json                # your config: GitLab projects to track  ┐
├── topics.json                  # your config: topics to monitor         │
├── loops.json                   # your config: scheduled-loop registry   ├─ gitignored, yours
├── instructions.md              # your free-text instructions            │
├── connectors.json              # your config: connector accounts        │
├── ai_cli.json                  # your config: Claude Code vs Codex CLI   ┘
├── loop_scheduler_state.json    # managed automatically, not hand-edited
├── PROGRESS.md                  # live run state, updated every run
├── outputs/                     # ← generated docs & run history live here (gitignored)
│   ├── daily-review.md          #   latest GitLab-issue-loop report
│   ├── connectors/test-results.json  #   last Test result per connector account
│   ├── messages.json             #   Dashboard → Activity message thread
│   ├── status.json               #   GitLab loop's current/last run status
│   ├── status/<loop_name>.json   #   every other registered loop's current/last run status
│   └── history/<date>.{md,log}   #   every past run's report + log
└── worktrees/                    # ← per-issue git worktrees for tracked projects (gitignored)
    └── <project>-issue-<iid>/    #   that project's own checkout, on branch loop/issue-<iid>
```

无论你把代码克隆到哪里，`projects.json`、`topics.json`、`loops.json`、`instructions.md` 和 `ai_cli.json` 始终解析到 `~/.loop-engineering/…`——它们之所以出现在上面的仓库文件夹*内部*，只是因为 `install.sh` 的默认克隆目标恰好是同一路径。如果你手动克隆到别处，这五个文件仍位于 `~/.loop-engineering/`，与代码分离。出于同样的原因，`projects.json` 骨架中的 `worktree_root` 默认也是 `~/.loop-engineering/worktrees`。

另有两个配置文件完全位于此目录树之外，可在仪表盘的 **Loops → GitLab Issues → Projects** 和 **Settings → Notifications** 页面编辑，而无需手动修改：`~/.gitlab/config.json` 和 `~/.slack/config.json`。

## 配置


| 文件                                  | 内容                                                                                                                                                                                                                   | 管理方式                                                                                                                                                                   |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `~/.loop-engineering/projects.json`   | 要跟踪哪些项目、它们的本地检出路径、目标分支、install/lint/test 命令、你的 GitLab 用户名，以及 worktree 临时目录（`worktree_root`，默认为 `~/.loop-engineering/worktrees`） | 仪表盘 **Loops → GitLab Issues → Projects** 页面的 “Tracked Projects” 区域，或手动复制 [`config/projects.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/projects.json.template)，或交给 `bin/scripts/setup.sh` 完成 |
| ↳ 每个项目的 `instance`（可选）   | 为单个项目覆盖顶层的 `gitlab_instance`——当你的项目分布在多个 GitLab 实例上时设置。省略时回退到 `gitlab_instance`。                                               | 同一文件中的各项目条目——参见模板中的 `harbor` 示例                                                                                                            |
| `~/.loop-engineering/topics.json`     | 要监控哪些主题，以及每个主题中什么算值得关注（仅主题监控循环）                                                                                                                               | 手动复制 [`config/topics.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/topics.json.template)，或交给 `bin/scripts/setup.sh` 完成                                                                |
| `~/.loop-engineering/inboxes.json`    | 要分诊哪些邮箱（服务商、账号、分类、VIP/排除的发件人、Slack bundle）以及共享的默认分类集（仅 Inbox Triage 循环）                                                              | 仪表盘的 **Loops → Inbox Triage → Setup** 页面（`/inbox/setup`），或由 `bin/scripts/setup.sh` 从 [`config/inboxes.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/inboxes.json.template) 生成骨架               |
| `~/.loop-engineering/mail_oauth.json` | Gmail/Outlook OAuth 应用自身的 client ID（Google 还包括 client secret）——这是一次性的应用注册步骤，而不是按邮箱的凭据                                                                           | 仪表盘的 **Loops → Inbox Triage → Setup** 页面                                                                                                                                                     |
| `~/.loop-engineering/loops.json`      | 定时循环的注册表：每个条目的名称、计划（工作日/小时/分钟）、入口模块、超时以及各循环的参数——由 `bin/loops_config.py` 读取，由 `bin/loop_scheduler.py` 轮询                 | 手动复制 [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template)，或交给 `bin/scripts/setup.sh` 完成                                                                  |
| `~/.loop-engineering/loop_scheduler_state.json` | 每个循环上次尝试运行的日期，确保调度器不会在同一天重复运行同一循环——不需要手动编辑                                                                                            | 由 `bin/loop_scheduler.py` 自动写入；`bin/scripts/setup.sh` 会为每个已注册循环预填当天日期，因此启用调度器时不会立即触发一次运行 |
| `~/.loop-engineering/instructions.md` | 你自己的自由文本指令，循环在每次运行开始时读取                                                                                                                                             | 仪表盘 **Settings** 页面的 Instructions 标签页                                                                                                                               |
| `~/.loop-engineering/ai_cli.json`     | `run-loop-now.sh` 为每个已注册循环调用哪个 AI CLI（Claude Code 或 Codex CLI）；默认为 `claude`                                                                                                        | 仪表盘 **Settings** 页面的 AI CLI 标签页，或交给 `bin/scripts/setup.sh` 完成                                                                                                                |
| `~/.gitlab/config.json`               | GitLab 实例 URL、令牌，以及项目别名 → 项目 ID 的映射（由 `gitlab-config` skill 读取）                                                                                                               | 仪表盘 **Loops → GitLab Issues → Projects** 页面                                                                                                                                                     |
| `~/.slack/config.json`                | 你的 Slack incoming webhook URL（以及各 bundle 的覆盖设置）                                                                                                                                                          | 仪表盘 **Settings** 页面的 Notifications 标签页（默认 webhook）/ **Loops → GitLab Issues → Projects** 页面的 Access bundles 区域（各 bundle 覆盖设置）                                                              |


`bin/loop_config.py` 是唯一读取 `projects.json` 的代码——可以在终端用它来检查你的配置是否正确：

```bash
python3 bin/loop_config.py aliases                # every configured project alias
python3 bin/loop_config.py project <alias>         # that alias's full config, incl. resolved GitLab instance
python3 bin/loop_config.py assignee                # the GitLab username being tracked
python3 bin/loop_config.py worktree-root           # where per-issue worktrees get created
```

如果 `~/.loop-engineering/projects.json` 尚不存在，每个需要它的脚本都会立即失败，并提示你运行 `bin/scripts/setup.sh`——不会悄悄猜测路径。

**Access bundles（访问包）**——按项目覆盖令牌/webhook

大多数项目直接使用其 GitLab 实例的默认令牌。**access bundle** 是一个具名的覆盖设置——拥有自己的 `{instance, token}` 组合，外加一个可选的 Slack webhook——用于那些实例默认令牌权限不足的少数项目。

在仪表盘 **Loops → GitLab Issues → Projects** 页面的独立 “Access bundles” 区域中管理 bundle：

- **添加 bundle**：为其命名，选择它要认证的 GitLab 实例，粘贴其令牌，并可选填写 Slack webhook URL。
- **将 bundle 分配给项目**：编辑项目别名所在行，从 **Bundle** 下拉框中选择 bundle——默认为 “(use instance default)”。
- 只要仍有项目别名指向某个 bundle，该 bundle 就不能删除，其实例也不能更改。
- 删除 bundle 时也会一并清除其 Slack webhook 覆盖设置（如果有）。

Bundle 保存在 `~/.gitlab/config.json` 的 `bundles` 键中；如果设置了 webhook 覆盖，则还保存在 `~/.slack/config.json` 的 `bundle_webhooks` 键中——两者仅通过 bundle 名称关联。

## 运行

**手动**运行一次，在把它交给定时任务之前先看看它如何工作：

```bash
bash run-loop-now.sh gitlab-loop   # the daily GitLab issue loop
bash run-loop-now.sh topic-loop    # the topic monitor loop
```

两者都会把日志写入 `outputs/history/`，并且都会把每次 `claude` CLI 调用的输出追加到 `logs/loop-engineering.log`（可在仪表盘的 **Runs → Logs** 页面查看）；你也可以通过仪表盘的 **Run now** 按钮（Dashboard → Overview）触发 GitLab 循环，无需终端。

**定时运行**，通过 `launchd`——安装 [`launchd/`](https://github.com/encoreshao/loop-engineering/tree/main/launchd) 下的两个代理，最简单的方式是在仪表盘的 **Settings → Daemons** 页面各点一下（该页面还会显示每个代理当前是否已加载及其 PID），也可以手动安装：

```bash
cp launchd/com.hermes.loop-engineering*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
```


| 代理                                   | 运行内容                                                                                                                                             |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------|
| `com.hermes.loop-engineering`           | 唯一的调度器轮询循环（`bin/loop_scheduler.py`），每 15 分钟一次（`StartInterval`）——通过 `run-loop-now.sh` 运行 `~/.loop-engineering/loops.json` 中已到期的已注册循环 |
| `com.hermes.loop-engineering-dashboard` | Web 仪表盘，常驻运行（`RunAtLoad` + `KeepAlive`）                                                                                          |


运行哪些循环、按什么计划运行属于配置而非代码——编辑 `~/.loop-engineering/loops.json`（参见 [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template)）即可添加循环或更改其到期时间；添加第三个循环只需新增一个 `loops.json` 条目，而不需要新的 plist。**Settings → Daemons** 页面中每个代理的计划编辑器只作用于 plist 自身的 `StartCalendarInterval`，而 `com.hermes.loop-engineering` 已不再有该字段（它以固定的 `StartInterval` 每 15 分钟轮询一次，并由 `loops.json` 决定哪个循环真正到期）——目前修改循环自身的计划需要手动编辑 `loops.json`。

## 仪表盘

一个仅限 localhost、无第三方依赖（标准库 Python，无 JS 框架）的 Web UI，由 `bin/web/dashboard_server.py` 提供服务。本地开发时直接运行它（不带参数）会使用其默认端口 `8420`。`bin/scripts/install.sh` 首次安装常驻 `launchd` 代理时会在 `48420`-`48620` 中随机选择一个端口（可用 `--port` 覆盖，之后的 `--upgrade` 不会重新选择）——请查看 `launchd/com.hermes.loop-engineering-dashboard.plist` 以确认现有安装实际运行的端口。


| 侧边栏条目 | 显示内容 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard** (`/`) | 视图：Overview — 当前/上次运行状态、实时进度指示器，以及 Run now 按钮; Activity — 与循环的消息会话，并带有独立的实时进度指示器。在此粘贴 GitLab issue 链接即可让循环立即处理该 issue，无论它分配给了谁。 |
| **Loops** (`/loops`) | 目录页，把已启用的循环与可用的循环分开列出；每个可见的循环也会作为子链接出现在侧边栏 **Loops** 之下（Inbox Triage 在被禁用且从未运行过时不会显示在那里） |
| — **GitLab Issues** (`/loops/gitlab-loop`) | 视图：Live — 实时获取的当前分配给你的 issue 和未关闭的 MR; Projects — 管理 `~/.gitlab/config.json`（实例、项目别名、access bundles）和 `~/.loop-engineering/projects.json`（跟踪的项目、循环设置），无需手动编辑 JSON |
| — **Topic Monitor** (`/loops/topic-loop`) | 视图：Live — 每个已配置主题的状态和已保存的简报; Topics — 添加、编辑和删除监控主题——同一页面上的独立视图，避免配置项干扰 Live 状态视图 |
| — **Inbox Triage** (`/loops/inbox-triage-loop`) | 视图：Inbox Triage 自身的实时状态（**Live**），以及邮箱连接与分类（**Setup**：连接 Gmail/Outlook、分类、VIP/排除的发件人、Slack bundle） |
| **Runs** (`/runs`) | 视图：Loop Runs — `outputs/loop-runs/` 下记录的每次运行（每个处理过的 issue 或主题一条），按时间倒序——只读；概览栏显示总运行次数、成功/升级比例、平均成本，以及实验性的 Loop Efficiency Score; History — 每次历史运行的回顾报告，按时间倒序; Logs — `logs/loop-engineering.log` 的末尾部分——包括 GitLab 循环、主题监控循环以及本仪表盘自身聊天助手的每次 `claude` CLI 调用输出 |
| **Insights** (`/insights`) | 视图：Analytics — 在可选的天数窗口内循环的表现：Loop Health 评分、结果、质量、风险与分类、失败细分以及学习趋势; Cost — AI 使用成本——GitLab issue 循环自身在窗口期内的成本，以及 `outputs/loop-runs/` 下所有运行的总成本; Budget — 每次已记录运行的最新预算状态，以及按循环定义和按日/周/月的汇总; Memory — 按项目记录的跨运行经验，每个 GitLab issue 一个 markdown 文件，以及此格式出现之前记录的内容（显示在 “Legacy learnings” 下） |
| **Harness** (`/harness`) | 视图：Audit — 每个循环定义的评分及通过/未通过检查项 |
| **Connectors** (`/connectors`) | 视图：Accounts — 按类型分组的所有连接器账号，带能力标签、显示上次结果的 Test 按钮（通知目标为 **Send test message**），外部账号还有指向其所属页面的「Managed on …」徽标；Add — 选择类型并填写表单。密钥填入密码框，保存后不会再次显示（编辑时留空即保留已存的值） |
| **Settings** (`/settings`) | 视图：General — Notifications（管理 `~/.slack/config.json` 的默认 webhook）、AI CLI（选择 Claude Code 或 Codex CLI，并实时检查各自是否已安装）、Appearance（颜色模式、强调色主题、自动刷新间隔——保存在当前浏览器的 `localStorage` 中）以及 Instructions（你自己的自由文本指令，循环在每次运行开始时读取）——以标签页形式集中在同一页面 (**General** 视图拆分为 Notifications / AI CLI / Appearance / Instructions 标签页（`?tab=`）); Daemons — 每个 `launchd` 代理的加载状态、可编辑的计划及启用/禁用操作，外加 Registered Loops 分栏，列出统一调度器运行的每个循环（其自身计划和上次运行状态，读取自 `loops.json`）; Skills — 本循环依赖的每个外部 skill，以及它是否确实已安装 |
| **README** (`/readme`) | 已移至顶栏的帮助图标（`/readme`）：本文件，在应用内渲染，并带有跳转到章节的快速导航 |

所有旧 URL（`/activity`、`/gitlab`、`/topic-monitor`、`/inbox`、`/loop-runs`、`/history`、`/logs`、`/analytics`、`/cost`、`/budget`、`/memory`、`/audit`、`/settings/general`、`/daemons`、`/skills` 等）都会保留查询字符串并永久重定向（301）到新位置，因此书签依然有效。


**可选：通过 nginx 使用友好的主机名**

默认情况下，仪表盘只能通过 `http://127.0.0.1:<port>` 访问（`<port>` 的选取方式见上文）。`bin/scripts/setup-nginx.sh` 会配置一个本地 nginx 反向代理，使其可以通过 `http://loop.x/`（80 端口）访问——必要时通过 Homebrew 安装 nginx、写入代理配置、将 `loop.x` 添加到 `/etc/hosts`，并以系统服务方式启动 nginx。`install.sh` 已自动向它传入已安装的端口；该脚本是幂等的，也可以单独安全地重复运行：

```bash
bin/scripts/setup-nginx.sh
# or, with no clone at all:
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
```

写入 `/etc/hosts` 和启动 nginx 服务都需要 `sudo`——macOS 会在这两个步骤提示你输入密码。传入 `--domain`/`--port` 可以使用 `loop.x`/`8420` 以外的值。

## 连接器

连接器是循环可以接入的账号：GitLab 或 GitHub 实例、Slack、Telegram 或聊天 Webhook、Notion 工作区、RSS 订阅源列表、Jira 或 Linear 工作区、邮箱、Google 日历。在仪表盘的 **System → Connectors** 页面（`/connectors`）中管理。每种类型都声明了*能力*（`issues`、`merge_requests`、`pipelines`、`notify`、`feed`、`mail`、`docs`、`calendar`），循环可以要求某种能力，而不是某个具体产品。

| 类型 | 能力 | 需要填写 | 密钥 |
| --- | --- | --- | --- |
| GitLab | `issues`, `merge_requests`, `pipelines` | URL | 个人访问令牌 |
| GitHub | `issues`, `merge_requests`, `pipelines` | API URL（默认 `https://api.github.com`）、用户名 | 令牌 |
| Slack webhook | `notify` | — | Webhook URL |
| Chat webhook | `notify` | 选择预设：Feishu、DingTalk、WeCom 企业微信（群机器人；个人微信没有机器人 API）、Microsoft Teams、Discord、Google Chat 或 Generic webhook | Webhook URL |
| Telegram 机器人 | `notify` | 聊天 ID | 机器人令牌 |
| RSS / Atom feeds | `feed` | 订阅源 URL，每行一个 | — |
| Notion | `docs`（显示为 Documents） | — | 集成令牌 |
| Jira Cloud | `issues` | 站点 URL、邮箱 | API 令牌 |
| Linear | `issues` | — | API 密钥 |
| Mailbox | `mail` | 外部账号——在 Inbox Triage 设置中管理 | — |
| Google Calendar | `calendar`（显示为 Calendar） | 日历 ID（默认 `primary`） | Google 登录（只读，`calendar.readonly`）；刷新令牌保存在钥匙串中 |

**图库与表单。**

- **Add** 会打开连接器类型图库，按 Code hosting、Chat & notifications、Work tracking、Knowledge、Feeds、Mail 分组（Outlook 在 Mail 中），最前面是 Google 分组，依次为 Gmail、Google Calendar 和 Google Chat，并带搜索框用于筛选。卡片会跟随 **Settings → Appearance** 中选择的强调色以及各服务自身的品牌色，聊天 Webhook 的每个预设都有各自的说明。
- 每个卡片和每个账号行都带有对应服务的品牌标志（内联的 Simple Icons 图标；没有图标的服务——Feishu、DingTalk、通用 Webhook——使用字母标记）。
- 聊天 Webhook 卡片会展开为上述预设，每个预设带一行提示（例如 Teams 的 Workflows Webhook 可能需要 Adaptive Cards）以及指向该服务官方文档的 **Where do I get this?** 链接。
- 添加/编辑表单包含 **Account** 部分（**Label** 和 **Connector id**；id 会根据 label 自动建议，直到你手动修改）、**Connection** 部分（该类型的设置）和 **Credentials** 部分（密钥）。
- **Google Calendar** 没有需要粘贴的密钥：点击 **Connect with Google**（已连接时为 **Reconnect**）登录即可。它复用你已在 Inbox Triage 设置页（Gmail 标签页）为 Gmail 配置的 Google OAuth 客户端（缺少时表单会提供指向该页面的链接），因此 Google Cloud 中需要相同的重定向 URI：`http://127.0.0.1:<port>/oauth/google/callback`。账号行会显示 **Connected** / **Not connected** 标记，**Test** 会读取该日历。只请求只读的 `calendar.readonly` 权限范围。
- 必填项标有 `*`，其余标注「(optional)」，并带有示例占位文字。密钥框带有 **Show**/**Hide** 切换。
- 按钮有 **Save**、**Save and test**（先保存再探测）和 **Cancel**。保存失败时，表单会重新显示，并保留已填写的非机密值。

**数据存放位置。** 在页面上添加的账号是*原生*账号：非机密设置保存在 `~/.loop-engineering/connectors.json`，密钥保存在 macOS 钥匙串中，服务名为 `loop-engineering.connectors`（设置了 `LOOP_ENGINEERING_HOME` 时会加上 `.sandbox-<hash>` 后缀，因此沙盒运行永远不会碰到真实密钥）。密钥不会写入 `connectors.json`，保存后也不会再次显示。

**外部账号**直接从原本管理它们的文件中读取，不做任何迁移：GitLab 实例来自 `~/.gitlab/config.json`（id 为实例别名），Slack Webhook 来自 `~/.slack/config.json`（`slack-default`，以及每个 bundle Webhook 对应的 `slack-<bundle>`），邮箱来自 `inboxes.json`（id 为收件箱名称）。它们会显示带链接的「Managed on …」徽标，指向可编辑它们的页面；你仍可在这里对其执行 Test。

**Test 按钮。** 每个账号都有 **Test** 按钮（Slack、Telegram 和聊天 Webhook 为 **Send test message**）；上次结果保存在 `outputs/connectors/test-results.json`。

**循环与通知。** 在 **Loops** 页面，需要某种能力的循环会显示「Needs: …」标签，在存在具备该能力的连接器之前无法启用（UI 和服务端都会拦截）。在 `loops.json` 条目中声明了 `"routes_notifications": true` 的循环（其运行器通过 `bin/notify.py` 发送）还会有 **Notify via** 选项，以 `notify: [连接器 id]` 的形式保存。`bin/notify.py` 会把这类循环的通知路由到这些连接器；未设置 `notify` 时，仍和以前一样发送到默认的 Slack Webhook。内置的 GitLab、Topic 和 Inbox 循环尚未声明该字段，仍直接发送到 Slack Webhook；如果它们已有 `notify` 列表，Loops 页面会以只读方式显示，并提供 **Clear** 按钮清除。可以通过命令行试用：`python3 bin/notify.py <loop> "<text>"`。仪表盘的 AI 面板也能列出连接器（聊天工具 `connector-list`）。

## 脚本参考

展开查看完整列表


| 脚本                              | 用途                                                                                                                                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run-loop-now.sh`                   | 单个已注册循环运行的通用入口（通过 `bin/loops_config.py` 从 `~/.loop-engineering/loops.json` 中查找）——日志写入 `outputs/history/`，失败时通知 Slack。由 `bin/loop_scheduler.py`（定时）或仪表盘（按需）调用 |
| `bin/loop_scheduler.py`             | 唯一由 launchd 调度的轮询循环：读取 `~/.loop-engineering/loops.json`，并通过 `run-loop-now.sh` 运行已到期的已注册循环                                                                                  |
| `bin/loops_config.py`               | 读取 `~/.loop-engineering/loops.json`——定时循环的注册表（名称、计划、入口）；目前没有写入路径，修改需手动编辑该文件（或复制模板）                                                 |
| `bin/gitlab_loop_runner.py`         | 运行 `gitlab-loop` 时 `run-loop-now.sh` 委托的逐 issue 编排器：发现分配的 issue，让每个 issue 运行在独立的 `LoopRuntime` 中（每个 issue 在 `outputs/loop-runs/` 下对应一个 `LoopResult`），负责 `claude -p`/`codex exec` 调用及其 `--allowedTools`/`--disallowedTools` 安全边界，然后为整批任务执行一次无条件的运行结束收尾 |
| `bin/scripts/build_run_prompt.sh`   | 构建 `bin/gitlab_loop_runner.py` 交给 AI CLI 的提示词——`<alias> <issue_iid>` 为单 issue 提示词（仪表盘 Dashboard → Activity 聊天中的限定范围运行），`--batch-issue <alias> <issue_iid>` 用于定时批次中的单个 issue（不含运行结束收尾），`--batch-end-of-run` 用于批次唯一的汇总/daily-review 收尾 |
| `bin/web/dashboard_server.py`       | Web 仪表盘；同时也是一个小型 CLI（`write-status`、`write-skills-install-status`、`read-messages`、`add-message`、`chat-tool`），供 `run-loop-now.sh`、`bin/loop_scheduler.py`、仪表盘自身的操作以及 Dashboard → Activity 视图内嵌的聊天助手使用 |
| `bin/loop_config.py`                | 读取 `~/.loop-engineering/projects.json`                                                                                                                                                                                            |
| `bin/list_assigned_issues.py`       | 列出已配置项目中分配给所配置用户的未关闭 GitLab issue                                                                                                                                                  |
| `bin/track_new_comments.py`         | 检测缓存 issue 上自循环上次查看以来有哪些新 note                                                                                                                                                             |
| `bin/project_memory.py`             | 读取（旧版）按项目存储的持久经验，内联保存在 GitLab 缓存中                                                                                                                                                |
| `bin/memory_store.py`               | 以 markdown 文件形式读取/记录按 issue 的持久任务记忆（每个 issue 一个文件，外加每个项目一个 MEMORY.md 索引）                                                                                                                    |
| `bin/ai_cli_config.py`              | 读写 `~/.loop-engineering/ai_cli.json`——`run-loop-now.sh` 为每个已注册循环调用哪个 AI CLI（`claude` 或 `codex`）                                                                                              |
| `bin/topic_monitor_runner.py`       | 运行 `topic-loop` 时 `run-loop-now.sh` 委托的逐主题编排器：让每个已配置主题运行在独立的 `LoopRuntime` 中（每个主题在 `outputs/loop-runs/` 下对应一个 `LoopResult`），负责 `claude -p`/`codex exec` 调用及其安全边界——它在主题监控循环中的角色与 `bin/gitlab_loop_runner.py` 在 GitLab 循环中的角色相同 |
| `bin/scripts/build_topic_prompt.sh` | 为单个已配置主题构建提示词，角色与上面的 `build_run_prompt.sh` 相同；尽管 `topic_monitor_runner.py` 已不再调用它，仍作为有文档说明的手动后备方案保留                                       |
| `bin/topic_config.py`               | 读取 `~/.loop-engineering/topics.json`                                                                                                                                                                                              |
| `bin/topic_seen.py`                 | 每个主题滚动 7 天的去重窗口，避免简报连续两天重复同一条新闻                                                                                                                                      |
| `bin/slack_notify.py`               | 向配置的 Slack incoming webhook 发送消息                                                                                                                                                                             |
| `bin/scripts/new_worktree.sh`       | 在 `loop/issue-<iid>` 分支上创建（或复用）一个隔离的 git worktree                                                                                                                                                          |
| `bin/scripts/open_merge_request.sh` | 推送 issue 分支并创建其 MR——拒绝任何不以 `loop/issue-*` 命名的分支                                                                                                                                                  |
| `bin/scripts/install.sh`            | 在线安装程序——克隆（或更新）本仓库，然后运行 `setup.sh`（将 `--config-path`/`--topics-config-path`/`--ai-cli-config-path`/`--loops-config-path`/`--state-path` 透传给它）；对已有安装使用 `--upgrade`，刷新当前已加载的每个 launchd 代理（仪表盘重启，调度器守护进程仅重新注册），使其加载新代码；还会就地迁移统一调度器之前的过时 `com.hermes.loop-engineering.plist`，并在旧的、已成孤儿的 `com.hermes.loop-engineering-topic-monitor` 守护进程仍从统一调度器之前安装着时将其移除；可安全地通过 `curl` 管道运行                                          |
| `bin/scripts/setup.sh`              | 一条命令完成安装：`gitlab-config` skill + `projects.json`/`topics.json`/`ai_cli.json`/`loops.json` 骨架，外加一个为每个已注册循环预填当天日期的 `loop_scheduler_state.json`，确保安装后立即启用调度器时不会立刻触发一次运行 |
| `bin/scripts/setup-nginx.sh`        | 可选的本地 nginx 反向代理（`http://loop.x/` → 仪表盘）                                                                                                                                                            |
| `bin/scripts/uninstall.sh`          | 撤销 `setup.sh`/`setup-nginx.sh`/`install.sh` 的操作；可安全地通过 `curl` 管道运行                                                                                                                                                          |




## 安全边界

这些边界是固定的，不会随时间或多次成功而放宽（参见 [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)）：

- **从不合并 merge request。** 循环的工作止于“MR 已创建、验证通过”——合并始终是人工手动步骤。
- 每次代码变更都在独立的 git worktree 中、在 `loop/issue-<iid>` 分支上进行，绝不直接在目标分支上修改。
- 只有当项目自身配置的 `test_cmd`/`lint_cmd` 通过，且 diff 只涉及与该 issue 相关的文件时，才会创建 MR。
- 不允许任意 shell 命令、不升级依赖、不读取 `.env`/凭据/SSH 密钥——只允许 `LOOPX_INSTRUCTIONS.md` 中的命令白名单。
- issue 逐个按顺序处理，绝不并行。
- 同一 issue 的验证失败在一次运行内绝不重试——而是通过 GitLab 评论升级处理。（启用 `verification.mode: gate` 后，循环会自行重新运行项目的检查，并允许带着失败输出作为反馈进行一次有限重试；测试/lint 失败的 MR 绝不会被创建，而是以 `loop:needs-human` 标签升级处理。）

Inbox Triage 循环有其自己的固定安全边界（参见 [`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md)）：

- **从不发送邮件。** 两个邮件服务商模块都不包含发送函数，且获取的 Outlook 令牌权限范围不含 `Mail.Send`——在令牌层面就无法发送，而不仅仅是代码层面。
- **从不归档、删除、移动邮件或更改已读状态。** 对邮箱的写操作仅限于创建 `Loop/*` 标签/分类、应用这些标签，以及在邮箱自身的草稿箱中创建回复草稿。
- **只应用 `Loop/*` 标签。** 每个分类标签（无论默认还是自定义）都必须以 `Loop/` 开头——否则 `inboxes.json` 在加载时会被拒绝，因此手动编辑的系统标签（如 `TRASH` 或 `UNREAD`）永远不会被应用到真实邮件上。
- **邮件正文从不持久化。** 它们只存在于内存中，以及每个收件箱的 `claude -p` 调用提示词中（外加最多一次重试），该调用不保留会话记录——绝不写入 `outputs/`、日志、状态或 Slack 汇总，这些地方只会包含发件人、主题、分类、AI 的简短理由和草稿链接。AI 撰写的回复草稿只保存在邮箱自身的草稿箱中。
- **Inbox Triage 需要 Claude CLI。** Codex 总是会给模型一个 shell，并将提示词记录到 `~/.codex/sessions/` 下，因此选择 Codex 时，每个收件箱都会在读取任何邮件之前直接失败——直到在 **Settings** 中将 AI CLI 切回 Claude。
- **刷新令牌只存储在 macOS 钥匙串中**，通过 `security -i` 并经 stdin 传入令牌写入，绝不以明文形式存储在磁盘上或出现在进程的 argv 中。



## 测试

```bash
python3 -m pytest tests/
```

`bin/` 下的每个脚本（无论是 Python 还是 shell，无论位于哪个文件夹）都有对应的 `tests/test_*.py`，并尽可能针对真实子进程/临时目录而非 mock 进行测试（示例参见 `tests/test_new_worktree.py`，它使用了一个真实的本地 git 仓库）。

`loop eval` 运行脚本化评测用例（免费），并写入 `outputs/evals/last.json`。`loop eval --golden [--budget-usd N] [--case NAME]` 在合成的夹具仓库上运行真实智能体（付费：预算默认为 10 美元，花完后不再启动新用例），并写入 `outputs/evals/golden-last.json`。两者的结果都显示在 Harness → Evals。`loop ledger backfill` 会根据旧的 `result.json` 重建账本记录。

## 项目文档


| 文档                                                                    | 用途                                                                                          |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md)                         | V2 运行时架构：`LoopDefinition`/`LoopState`/`LoopRuntime`、验证/预算/策略、可观测性以及 CLI——这是全局地图，而非任一循环自身的规格说明 |
| [`TASK.md`](https://github.com/encoreshao/loop-engineering/blob/main/TASK.md)                                                   | 本仓库运行的所有定时任务的索引，每项都指向 `docs/tasks/` 下各自的规格说明        |
| [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)   | GitLab issue 循环面向人的规格说明：目标、范围、安全边界                              |
| [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) | 主题监控循环面向人的规格说明：目标、范围、安全边界                             |
| [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md)                         | GitLab issue 循环每次运行时自身遵循的分步流程                               |
| [`TOPIC_MONITOR_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/TOPIC_MONITOR_INSTRUCTIONS.md)       | 主题监控循环每次运行时自身遵循的分步流程                              |
| [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md)                                           | 循环每次运行都会读取和更新的实时状态——上次运行摘要、未解决的升级事项、已做出的决策 |
| [`docs/troubleshooting/crash-looping-launchd-agent.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/troubleshooting/crash-looping-launchd-agent.md) | 诊断并修复陷入崩溃循环、日志被刷屏的 `com.hermes.loop-engineering*` launchd 代理 |




## 许可证

[MIT](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE)——详见 [`LICENSE`](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) 文件。
