[English](README.md) | **日本語** | [简体中文](README.zh-CN.md) | [Français](README.fr.md)

# Loop X Engineering

![CI](https://github.com/encoreshao/loop-engineering/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/encoreshao/loop-engineering)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Shell](https://img.shields.io/badge/shell-bash-4EAA25)

Loop X Engineering の使命は、Issue のトリアージに奪われる時間をあなたに取り戻すことです。
平日は毎日、無人で常駐するチームメイトとして GitLab のキューを処理し、
あなたにアサインされた Issue が放置されないようにします。修正を出荷し、
質問に回答し、本当にあなたの判断が必要なものだけをフラグ付けするので、
あなたは本当に重要なことだけに集中できます。ローカルの Web ダッシュボードでは、
動作の様子を見守り、これまでの作業をすべてレビューし、すべての設定を
手動で行えます。JSON を編集する必要はありません。

無人で動かし続けても安全なように設計されています。自分のマージリクエストを
マージすることはなく、自分に新しい Issue をアサインすることもなく、
明示的に指定したプロジェクト以外には一切触れません。

## 目次

- [仕組み](#仕組み)
- [ループ](#ループ)
- [必要要件](#必要要件)
- [クイックスタート](#クイックスタート)
- [ディレクトリ構成](#ディレクトリ構成)
- [設定](#設定)
- [実行方法](#実行方法)
- [ダッシュボード](#ダッシュボード)
- [コネクタ](#コネクタ)
- [スクリプトリファレンス](#スクリプトリファレンス)
- [安全上の境界](#安全上の境界)
- [テスト](#テスト)
- [プロジェクトドキュメント](#プロジェクトドキュメント)
- [ライセンス](#ライセンス)



## 仕組み

スケジュールされた各実行（`run-loop-now.sh gitlab-loop`）では次のことを行います。

1. 設定内のすべてのプロジェクトエイリアスを対象に、設定したユーザー名にアサインされているオープンな GitLab Issue をすべて一覧化します。
2. [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md) の段階的な判断手順に従い、それらを**一度に 1 件ずつ、決して並列にせず**処理します。
3. 各 Issue について、次のいずれか 1 つだけを行います。
  - **修正する** — 隔離された git worktree の `loop/issue-<iid>` ブランチ上で作業し、プロジェクト自身の lint/test コマンドが通った場合にのみマージリクエストを作成します。
  - **回答する** — コード変更が不要な依頼（質問、状況確認）には GitLab コメントを投稿します。
  - **エスカレーションする** — 依頼が曖昧な場合や検証が失敗した場合は、確認を求める GitLab コメントを投稿します。
4. Issue ごとの Slack メッセージと、実行終了時のダイジェストを 1 件送信します（アサインが何もない朝も含め、毎回の実行で）。
5. [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md) と `outputs/daily-review.md` を更新し、次回の実行とあなたが何が起きたかを把握できるようにします。

実行をまたいで再利用できる教訓（修正パターン、落とし穴）は、`bin/memory_store.py` によって Issue ごとに markdown のタスクメモリファイルとして記録されます（この形式以前に記録されたエントリは引き続き `bin/project_memory.py` 経由で読み込まれます）。これにより、後の実行は前回より賢い状態から始まります。

2 つ目の独立したループ（`run-loop-now.sh topic-loop`）は、GitLab の代わりに Web 全般の任意のトピックを監視します。詳しくは [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) を参照してください。

3 つ目のループ（`run-loop-now.sh inbox-triage-loop`）は Gmail と Outlook の受信トレイをトリアージします。新しい未読メッセージをそれぞれ `Loop/*` ラベルに分類し、緊急のものにはスレッド形式の返信を下書き（送信はしません）し、Slack ダイジェストとダッシュボードの **Loops → Inbox Triage** ページで報告します。詳しくは [`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md) を参照してください。

## ループ

`config/loops.json.template` には 7 つのループが同梱されています。それぞれダッシュボードの **Loops** に専用ページがあり、`docs/tasks/` に個別の仕様があります。スケジュールはそのページで編集でき、下記の既定値はテンプレートのものです。

| ループ | 内容 | 必要なもの | 既定のスケジュール | Loop X の外への書き込み |
| --- | --- | --- | --- | --- |
| GitLab issues | 割り当てられた Issue を処理：修正・回答・エスカレーション | GitLab 設定（`~/.gitlab/config.json`） | 平日 10:00 | ブランチとマージリクエスト（マージはしない）、Issue コメント、Slack |
| Topic monitor | 指定トピックをウェブで調査し、毎日ブリーフィングを送信 | トピック（`topics.json`） | 毎日 10:00 | Slack ダイジェスト |
| Inbox triage | 新着メールにラベルを付け、緊急メールへの返信を下書き（既定では無効） | メールボックス（`mail` ケイパビリティ） | 平日 09:00 | メールのラベルと下書き（送信はしない） |
| Daily Digest | 朝の1通のまとめ：ToDo、担当 Issue、レビュー待ちの MR、今日の会議、昨日の Loop X の作業（既定では無効） | `issues` コネクタ（カレンダーは任意） | 平日 09:30 | 通知のみ |
| MR Review | あなたがレビュアーのマージリクエストを事前レビュー（既定では無効） | `merge_requests` コネクタ（GitLab） | 2 時間ごと | GitLab のドラフトノートのみ。公開・承認・通常ノートの投稿は一切しない |
| Pipeline Doctor | 追跡中のプロジェクトと自分の MR の失敗した CI パイプラインを診断し、繰り返す失敗を指摘（既定では無効） | `pipelines` コネクタ（GitLab） | 毎時 | 通知のみ |
| RSS Watch | フィードの新着エントリを関心に合わせてランク付けし、短いダイジェストを送信（既定では無効） | `feed` コネクタ（RSS） | 毎日 08:00 | 通知のみ |

後ろの 4 つは LoopKit プラグイン（`bin/loopkit.py`、`bin/loop_plugins/`）です。モデルはツールも MCP サーバーも持たない密閉状態で実行され、項目ごとに分離されるため 1 件の失敗で実行全体が止まらず、通知はループの **Notify via** コネクタ経由で送られます。詳しくは [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md#loopkit) を参照してください。

## 必要要件

- macOS（スケジュールとダッシュボードはどちらも `launchd` エージェントとして動作します）
- Python 3.12+ — このリポジトリ自身のコードは **stdlib のみ**で、実行に `pip install` は不要です
- `git` 2.42+（worktree、push-options）
- GitLab アカウントと、追跡したいプロジェクト用のパーソナルアクセストークン
- （任意）実行通知用の Slack incoming webhook
- `[encore-skills](https://github.com/encoreshao/encore-skills)` の `[gitlab-config](https://github.com/encoreshao/encore-skills/tree/main/skills/gitlab-config)` スキル — このループ唯一の外部依存で、`setup.sh` によって `~/.encore-skills` にデプロイされます。実際にインストールされているかは、ダッシュボードの **Settings → Skills** ページでいつでも確認できます。
- `pytest` — 開発専用。このリポジトリ自身のテストスイートの実行に使います



## クイックスタート

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
```

このリポジトリを `~/.loop-engineering` にクローンし（別の場所にする場合は `--dir <path>` を指定）、`bin/scripts/setup.sh` を実行します。これにより `gitlab-config` スキルがインストールされ、テンプレートから `projects.json`/`topics.json` の雛形が作成されます。続いてローカルの nginx リバースプロキシを設定し、ダッシュボードを常時稼働の `launchd` エージェントとして起動するため、このコマンド 1 つでダッシュボードが実際にアクセス可能な状態で動作します。どちらかを無効にするには `--skip-nginx` や `--skip-launchd-daemons` を指定してください。（スケジュールされた GitLab ループとトピックモニターは自動起動*されません*。`projects.json`/`topics.json` を記入する前に動作してしまうためです。設定が済んだら、ダッシュボードの **Settings → Daemons** ページから自分で起動してください。）後で同じコマンドを再実行すると、再クローンせずに最新の `main` を pull するだけです。

すでにインストール済みで更新だけしたい場合は `--upgrade` を付けます。

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash -s -- --upgrade
```

手順は上と同じですが、`--dir` にまだ何もインストールされていない場合は黙って新規クローンせずに即座に失敗し、ダッシュボードだけでなく、現在ロードされているこのプロジェクトのすべての launchd エージェントを更新します。ダッシュボード（常時稼働のサーバー）は実際に再起動されます（`launchctl kickstart -k`）。単なる `launchctl load` は、すでに動作中のエージェントに対しては何もしないためです。`com.hermes.loop-engineering`（`loops.json` に登録されたすべてのループを実行する単一のスケジューラー。**Settings → Daemons** ページで有効化している場合）は、登録の再読み込み（`unload` + `load -w`）のみが行われます。kickstart は決して行いません。そうするとスケジューラー自身の次回ポーリングを待たずに、本番の GitLab/Slack に対してスケジュール外の実行が今すぐ走ってしまうためです。`--upgrade` は、統合スケジューラー導入前から残っているレンダリング済み plist（削除済みの `run-loop.sh` をまだ指しているもの）の移行も行い、その移行以前からインストールされたままの、もはや不要な `com.hermes.loop-engineering-topic-monitor` デーモンが残っていれば削除します。

クローンの様子を先に自分で確認したい場合は次のとおりです。

```bash
git clone https://github.com/encoreshao/loop-engineering.git
cd loop-engineering
bin/scripts/setup.sh
```

スキルはすでにインストール済みで、設定の雛形だけが欲しい場合は次のとおりです。

```bash
bin/scripts/setup.sh --skip-skills-install
```

完了したら、ダッシュボードの **Settings → Skills** ページを開き、必要なものがすべて実際にインストールされているか確認してください。ライブでチェックするので推測は不要です。

**すでに Claude Code で作業中ですか？** コマンドを自分で実行する代わりに、次を貼り付けてください。

> Clone and set up [https://github.com/encoreshao/loop-engineering](https://github.com/encoreshao/loop-engineering) for me: run its online installer
> (`curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash`),
> then help me fill in `~/.loop-engineering/projects.json` with my own GitLab project(s), and `~/.gitlab/config.json` with my GitLab token.



### アンインストール

```bash
bin/scripts/uninstall.sh                 # or: curl -fsSL .../uninstall.sh | bash
```

このリポジトリの `launchd` エージェントをアンロードして削除し、`setup-nginx.sh` を実行していればその変更を元に戻し、`~/.loop-engineering` フォルダ全体（コード、設定、実行履歴をまとめて）を削除します。代わりにすべてをそのまま残したい場合（再インストール直前など）は `--keep-config` を指定してください。再実行しても安全です。

## ディレクトリ構成

デフォルトのインストールパスを使うと、すべてが 1 つのフォルダ配下に置かれます。

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

`projects.json`、`topics.json`、`loops.json`、`instructions.md`、`ai_cli.json` は、コードをどこにクローンしたかに関係なく、常に `~/.loop-engineering/…` に解決されます。上記のリポジトリフォルダ*内*に置かれるのは、`install.sh` のデフォルトのクローン先がたまたま同じパスだからにすぎません。手動で別の場所にクローンした場合でも、これら 5 つのファイルはコードとは別に `~/.loop-engineering/` に置かれます。`projects.json` の雛形の `worktree_root` も、同じ理由でデフォルトは `~/.loop-engineering/worktrees` です。

さらに 2 つの設定ファイル `~/.gitlab/config.json` と `~/.slack/config.json` は、このツリーの完全に外側にあり、手動ではなくダッシュボードの **Loops → GitLab Issues → Projects** ページと **Notifications** ページから編集できます。

## 設定


| ファイル                              | 内容                                                                                                                                                                                                                    | 管理方法                                                                                                                                                                      |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `~/.loop-engineering/projects.json`   | 追跡するプロジェクト、そのローカルチェックアウトのパス、ターゲットブランチ、install/lint/test コマンド、GitLab ユーザー名、worktree の作業ディレクトリ（`worktree_root`、デフォルトは `~/.loop-engineering/worktrees`） | ダッシュボードの **Loops → GitLab Issues → Projects** ページの「Tracked Projects」セクション、または [`config/projects.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/projects.json.template) を手動でコピー、または `bin/scripts/setup.sh` に任せる |
| ↳ プロジェクトごとの `instance`（任意） | 1 つのプロジェクトについてトップレベルの `gitlab_instance` を上書きします。プロジェクトが複数の GitLab インスタンスにまたがる場合に設定します。省略時は `gitlab_instance` が使われます。                               | 同じファイルの各プロジェクトエントリ — テンプレートの `harbor` の例を参照                                                                                                     |
| `~/.loop-engineering/topics.json`     | 監視するトピックと、それぞれで注目に値するものの基準（トピックモニターループのみ）                                                                                                                                      | [`config/topics.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/topics.json.template) を手動でコピー、または `bin/scripts/setup.sh` に任せる                                                                |
| `~/.loop-engineering/inboxes.json`    | トリアージするメールボックス（プロバイダー、アカウント、カテゴリ、VIP/除外する送信者、Slack バンドル）と、共有のデフォルトカテゴリセット（Inbox Triage ループのみ）                                                    | ダッシュボードの **Loops → Inbox Triage → Setup** ページ（`/inbox/setup`）、または `bin/scripts/setup.sh` に [`config/inboxes.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/inboxes.json.template) から雛形を作成させる               |
| `~/.loop-engineering/mail_oauth.json` | Gmail/Outlook OAuth アプリ自体のクライアント ID（Google の場合はクライアントシークレットも）— メールボックスごとの認証情報ではなく、一度きりのアプリ登録手順                                                            | ダッシュボードの **Loops → Inbox Triage → Setup** ページ                                                                                                                                              |
| `~/.loop-engineering/loops.json`      | スケジュールされたループのレジストリ：各エントリの名前、スケジュール（曜日/時/分）、エントリポイントモジュール、タイムアウト、ループごとの設定項目 — `bin/loops_config.py` が読み込み、`bin/loop_scheduler.py` がポーリング | [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template) を手動でコピー、または `bin/scripts/setup.sh` に任せる                                                                  |
| `~/.loop-engineering/loop_scheduler_state.json` | ループごとの最終試行日。スケジューラーが同じループを 1 日に 2 回実行しないようにするためのもので、手動で編集するものではありません                                                                               | `bin/loop_scheduler.py` が自動で書き込みます。`bin/scripts/setup.sh` が登録済みの全ループについて今日の日付で初期化するため、スケジューラーを有効にしても即座に実行が走ることはありません |
| `~/.loop-engineering/instructions.md` | 自由記述の独自指示。毎回の実行開始時にループが読み込みます                                                                                                                                                              | ダッシュボードの **Settings** ページの Instructions タブ                                                                                                                     |
| `~/.loop-engineering/ai_cli.json`     | `run-loop-now.sh` が登録済みの全ループで呼び出す AI CLI（Claude Code または Codex CLI）。デフォルトは `claude`                                                                                                           | ダッシュボードの **Settings** ページの AI CLI タブ、または `bin/scripts/setup.sh` に任せる                                                                                                    |
| `~/.gitlab/config.json`               | GitLab インスタンスの URL、トークン、プロジェクトエイリアス → プロジェクト ID の対応（`gitlab-config` スキルが読み込みます）                                                                                            | ダッシュボードの **Loops → GitLab Issues → Projects** ページ                                                                                                                                            |
| `~/.slack/config.json`                | Slack incoming webhook の URL（およびバンドルごとの上書き）                                                                                                                                                              | ダッシュボードの **Settings** ページの Notifications タブ（デフォルトの webhook）/ **Loops → GitLab Issues → Projects** ページの Access bundles セクション（バンドルごとの上書き）                              |


`projects.json` を読み込むコードは `bin/loop_config.py` だけです。ターミナルから設定の妥当性を確認するのに使えます。

```bash
python3 bin/loop_config.py aliases                # every configured project alias
python3 bin/loop_config.py project <alias>         # that alias's full config, incl. resolved GitLab instance
python3 bin/loop_config.py assignee                # the GitLab username being tracked
python3 bin/loop_config.py worktree-root           # where per-issue worktrees get created
```

`~/.loop-engineering/projects.json` がまだ存在しない場合、それを必要とするすべてのスクリプトは、`bin/scripts/setup.sh` を実行するよう促すメッセージを出して即座に失敗します。パスを黙って推測することはありません。

**Access bundles** — プロジェクトごとのトークン/webhook の上書き

ほとんどのプロジェクトは、GitLab インスタンスのデフォルトトークンをそのまま使います。**access bundle** は名前付きの上書き設定で、独自の `{instance, token}` の組と任意の Slack webhook を持ちます。インスタンスのデフォルトトークンでは必要なアクセス権がない、まれなプロジェクト向けです。

バンドルはダッシュボードの **Loops → GitLab Issues → Projects** ページにある専用の「Access bundles」セクションで管理します。

- **バンドルを追加する**：名前を付け、認証先の GitLab インスタンスを選び、トークンを貼り付け、必要に応じて Slack webhook の URL も入力します。
- **プロジェクトにバンドルを割り当てる**：プロジェクトエイリアスの行を編集し、**Bundle** ドロップダウンからバンドルを選びます。デフォルトは「(use instance default)」です。
- いずれかのプロジェクトエイリアスがまだ参照している間は、バンドルを削除することも、そのインスタンスを変更することもできません。
- バンドルを削除すると、Slack webhook の上書きが設定されていた場合はそれも消去されます。

バンドルは `~/.gitlab/config.json` の `bundles` キーに保存され、webhook の上書きが設定されている場合は `~/.slack/config.json` の `bundle_webhooks` キーにも保存されます。両者はバンドル名だけで結び付けられます。

## 実行方法

スケジュールに任せる前に動作を確認するため、まず**手動で**一度実行します。

```bash
bash run-loop-now.sh gitlab-loop   # the daily GitLab issue loop
bash run-loop-now.sh topic-loop    # the topic monitor loop
```

どちらも `outputs/history/` にログを出力し、さらに `claude` CLI の各呼び出しの出力を `logs/loop-engineering.log` に追記します（ダッシュボードの **Runs → Logs** ページで閲覧できます）。また、ターミナルを使わずにダッシュボードの **Run now** ボタン（Dashboard → Overview）から GitLab ループを起動することもできます。

**スケジュール実行**は `launchd` 経由で行います。[`launchd/`](https://github.com/encoreshao/loop-engineering/tree/main/launchd) 配下の 2 つのエージェントをインストールしてください。最も簡単なのは、ダッシュボードの **Settings → Daemons** ページからそれぞれワンクリックでインストールする方法です（各エージェントが現在ロードされているかと、その PID も表示されます）。手動で行う場合は次のとおりです。

```bash
cp launchd/com.hermes.loop-engineering*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
```


| エージェント                            | 実行内容                                                                                                                                         |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------|
| `com.hermes.loop-engineering`           | 単一のスケジューラーのポーリングループ（`bin/loop_scheduler.py`）を 15 分ごとに実行（`StartInterval`）— `~/.loop-engineering/loops.json` に登録されたループのうち期限が来たものを `run-loop-now.sh` 経由で実行します |
| `com.hermes.loop-engineering-dashboard` | Web ダッシュボード。常時稼働（`RunAtLoad` + `KeepAlive`）                                                                                          |


どのループをどのスケジュールで実行するかはコードではなく設定です。ループを追加したり実行タイミングを変えたりするには `~/.loop-engineering/loops.json` を編集してください（[`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template) を参照）。3 つ目のループを追加するのに必要なのは新しい `loops.json` エントリであり、新しい plist ではありません。**Settings → Daemons** ページのエージェントごとのスケジュールエディターは plist 自身の `StartCalendarInterval` にしか適用されませんが、`com.hermes.loop-engineering` にはもうそれがありません（固定の `StartInterval` で 15 分ごとにポーリングし、どのループの期限が来ているかは `loops.json` に委ねます）。そのため、ループ自体のスケジュール変更は、現時点では `loops.json` の手動編集で行います。

## ダッシュボード

localhost 専用で依存関係のない（stdlib の Python のみ、JS フレームワークなし）Web UI で、`bin/web/dashboard_server.py` が配信します。ローカル開発用に直接（引数なしで）実行すると、独自のデフォルトポート `8420` を使います。`bin/scripts/install.sh` は、常時稼働の `launchd` エージェントを初めてインストールする際に `48420`-`48620` の範囲からランダムなポートを選びます（`--port` で上書き可能で、後の `--upgrade` で選び直されることはありません）。既存のインストールが実際に使っているポートは `launchd/com.hermes.loop-engineering-dashboard.plist` で確認してください。


| サイドバー項目 | 表示内容 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard** (`/`) | ビュー：Overview — 現在/前回の実行状況、ライブの進捗インジケーター、Run now ボタン; Activity — ループとのメッセージスレッドと、専用のライブ進捗インジケーター。ここに GitLab Issue のリンクを貼り付けると、誰にアサインされているかに関係なく、その Issue 1 件をループに即座に処理させられます。 |
| **Loops** (`/loops`) | アクティブなループと利用可能なループを分けて表示するカタログ。表示中の各ループは、サイドバーの **Loops** 配下にも子リンクとして並びます（Inbox Triage は無効かつ一度も実行されていない間はそこに表示されません） |
| — **GitLab Issues** (`/loops/gitlab-loop`) | ビュー：Live — 現在アサインされている Issue とオープンな MR をライブで取得して表示; Projects — `~/.gitlab/config.json`（インスタンス、プロジェクトエイリアス、access bundles）と `~/.loop-engineering/projects.json`（追跡するプロジェクト、ループの設定）を JSON を手動編集せずに管理 |
| — **Topic Monitor** (`/loops/topic-loop`) | ビュー：Live — 設定済みの各トピックの状況と保存済みブリーフィング; Topics — 監視するトピックの追加・編集・削除 — 設定によって Live 状況表示が煩雑にならないよう、同じページ内の別ビューに分離 |
| — **Inbox Triage** (`/loops/inbox-triage-loop`) | ビュー：Inbox Triage 自体のライブ状況（**Live**）と、メールボックスの接続・カテゴリ（**Setup**：Gmail/Outlook の接続、カテゴリ、VIP/除外する送信者、Slack バンドル） |
| **Runs** (`/runs`) | ビュー：Loop Runs — `outputs/loop-runs/` 配下に記録されたすべての実行（処理した Issue またはトピックごとに 1 件）を新しい順に表示 — 読み取り専用。概要ストリップには総実行数、成功/エスカレーション率、平均コスト、実験的な Loop Efficiency Score を表示; History — 過去の各実行のレビューレポートを新しい順に表示; Logs — `logs/loop-engineering.log` の末尾 — GitLab ループ、トピックモニターループ、およびこのダッシュボード自身のチャットアシスタントにわたる、`claude` CLI の各呼び出しの出力 |
| **Insights** (`/insights`) | ビュー：Analytics — 選択した日数の範囲におけるループのパフォーマンス：Loop Health スコア、成果、品質、リスクと分類、失敗の内訳、学習の傾向; Cost — AI の利用コスト — GitLab Issue ループ自体の期間別コストと、`outputs/loop-runs/` 配下の全実行の合計コスト; Budget — 記録された各実行の最新の予算ステータスと、ループ定義別および日/週/月別の集計; Memory — プロジェクトごとに記録された実行をまたぐ教訓（GitLab Issue ごとに 1 つの markdown ファイル）と、この形式以前に記録されたもの（「Legacy learnings」の下に表示） |
| **Harness** (`/harness`) | ビュー：Audit — 各ループ定義のスコアと合否チェック |
| **Connectors** (`/connectors`) | ビュー：Accounts — 種類ごとにまとめた全コネクタアカウント。機能チップ、最後の結果を表示する Test ボタン（通知先は **Send test message**）、外部アカウントには所有ページへのリンク付き「Managed on …」バッジを表示します。Add — 種類を選んでフォームに入力します。シークレットはパスワード欄に入力し、保存後は二度と表示されません（編集時に空欄のままにすると保存済みの値が維持されます） |
| **Settings** (`/settings`) | ビュー：General — Notifications（`~/.slack/config.json` のデフォルト webhook を管理）、AI CLI（Claude Code または Codex CLI を選択し、それぞれのインストール有無をライブで確認）、Appearance（カラーモード、アクセントテーマ、自動更新間隔 — このブラウザーの `localStorage` に保存）、Instructions（毎回の実行開始時にループが読み込む自由記述の独自指示）— 1 ページ内のタブとしてまとめています (**General** ビューは Notifications / AI CLI / Appearance / Instructions のタブ（`?tab=`）に分かれています); Daemons — すべての `launchd` エージェントのロード状態、編集可能なスケジュール、有効化/無効化に加え、統合スケジューラーが実行する全ループの Registered Loops 内訳（各ループのスケジュールと前回の実行状況。`loops.json` から読み込み）; Skills — このループが依存するすべての外部スキルと、それらが実際にインストールされているか |
| **README** (`/readme`) | トップバーのヘルプアイコン（`/readme`）に移動：このファイルを、セクションへジャンプできるクイックナビ付きでアプリ内に表示 |

従来のすべての URL（`/activity`、`/gitlab`、`/topic-monitor`、`/inbox`、`/loop-runs`、`/history`、`/logs`、`/analytics`、`/cost`、`/budget`、`/memory`、`/audit`、`/settings/general`、`/daemons`、`/skills` など）は、クエリ文字列を保ったまま新しい場所へ恒久リダイレクト（301）されるため、ブックマークはそのまま使えます。


**任意：nginx によるわかりやすいホスト名**

デフォルトでは、ダッシュボードには `http://127.0.0.1:<port>` でしかアクセスできません（`<port>` の決まり方は上記を参照）。`bin/scripts/setup-nginx.sh` はローカルの nginx リバースプロキシを設定し、代わりに `http://loop.x/`（ポート 80）でアクセスできるようにします。必要に応じて Homebrew で nginx をインストールし、プロキシ設定を書き込み、`loop.x` を `/etc/hosts` に追加し、nginx をシステムサービスとして起動します。`install.sh` はインストールしたポートを自動的に渡します。冪等なので、単体で再実行しても安全です。

```bash
bin/scripts/setup-nginx.sh
# or, with no clone at all:
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
```

`/etc/hosts` への書き込みと nginx サービスの起動にはどちらも `sudo` が必要で、macOS はこの 2 つのステップでパスワードを求めます。`loop.x`/`8420` 以外を使うには `--domain`/`--port` を指定してください。

## コネクタ

コネクタは、ループが接続できるアカウントです。GitLab や GitHub のインスタンス、Slack・Telegram・チャットの Webhook、Notion ワークスペース、RSS フィード一覧、Jira や Linear のワークスペース、メールボックス、Google カレンダーなどが該当します。ダッシュボードの **System → Connectors** ページ（`/connectors`）で管理します。各タイプは*機能*（`issues`、`merge_requests`、`pipelines`、`notify`、`feed`、`mail`、`docs`、`calendar`）を宣言しており、ループは特定の製品ではなく機能を要求できます。

| タイプ | 機能 | 入力項目 | シークレット |
| --- | --- | --- | --- |
| GitLab | `issues`, `merge_requests`, `pipelines` | URL | パーソナルアクセストークン |
| GitHub | `issues`, `merge_requests`, `pipelines` | API URL（デフォルト `https://api.github.com`）、ユーザー名 | トークン |
| Slack webhook | `notify` | — | Webhook URL |
| Chat webhook | `notify` | プリセットを選択：Feishu、DingTalk、WeCom 企業微信（グループボット。個人の WeChat にはボット API がありません）、Microsoft Teams、Discord、Google Chat、Generic webhook | Webhook URL |
| Telegram ボット | `notify` | チャット ID | ボットトークン |
| RSS / Atom feeds | `feed` | フィード URL（1 行に 1 件） | — |
| Notion | `docs`（Documents と表示） | — | インテグレーショントークン |
| Jira Cloud | `issues` | サイト URL、メールアドレス | API トークン |
| Linear | `issues` | — | API キー |
| Mailbox | `mail` | 外部管理 — Inbox Triage のセットアップで管理 | — |
| Google Calendar | `calendar`（Calendar と表示） | カレンダー ID（既定は `primary`） | Google サインイン（読み取り専用、`calendar.readonly`）。リフレッシュトークンはキーチェーンに保存 |

**ギャラリーとフォーム。**

- **Add** を開くとコネクタの種類のギャラリーが表示されます。Code hosting、Chat & notifications、Work tracking、Knowledge、Feeds、Mail に分類され（Outlook は Mail にあります）、先頭の Google セクションに Gmail、Google Calendar、Google Chat が並びます。検索ボックスで絞り込めます。カードは **Settings → Appearance** で選んだアクセントカラーと各サービスのブランドカラーに合わせて表示され、チャット Webhook の各プリセットには個別の説明があります。
- 各タイルとアカウント行にはサービスのブランドロゴが付きます（インラインの Simple Icons マーク。マークのないサービス（Feishu、DingTalk、汎用 Webhook）はレターマーク）。
- チャット Webhook のタイルは上記のプリセットに展開され、それぞれに一行の説明（例：Teams の Workflows Webhook は Adaptive Cards が必要な場合があります）と、そのサービス自身のドキュメントへの **Where do I get this?** リンクが付きます。
- 追加・編集フォームは **Account** セクション（**Label** と **Connector id**。id は編集するまでラベルから自動提案されます）、**Connection** セクション（種類ごとの設定）、**Credentials** セクション（シークレット）で構成されます。
- **Google Calendar** には貼り付けるシークレットがありません。**Connect with Google**（接続後は **Reconnect**）をクリックしてサインインします。Inbox Triage のセットアップページ（Gmail タブ）で Gmail 用に設定済みの Google OAuth クライアントを再利用します（未設定の場合はフォームにそのページへのリンクが表示されます）。そのため Google Cloud には同じリダイレクト URI、`http://127.0.0.1:<port>/oauth/google/callback` が必要です。アカウント行には **Connected** / **Not connected** の表示があり、**Test** はカレンダーを読み取ります。要求するスコープは読み取り専用の `calendar.readonly` のみです。
- 必須項目には `*` が付き、それ以外は「(optional)」と表示され、入力例のプレースホルダーもあります。シークレット欄には **Show**／**Hide** の切り替えがあります。
- ボタンは **Save**、**Save and test**（保存してからプローブを実行）、**Cancel** です。保存に失敗した場合は、シークレット以外の入力値を保持したままフォームが再表示されます。

**データの保存先。** ページ上で追加したアカウントは*ネイティブ*で、シークレット以外の設定は `~/.loop-engineering/connectors.json` に、シークレットは macOS キーチェーンのサービス `loop-engineering.connectors` に保存されます（`LOOP_ENGINEERING_HOME` が設定されている場合は `.sandbox-<hash>` が付くため、サンドボックス実行が本物のシークレットに触れることはありません）。シークレットが `connectors.json` に書き込まれることはなく、保存後に再表示されることもありません。

**外部アカウント**は、すでにそれを管理しているファイルから読み取り専用で取り込まれ、移行は行われません。GitLab インスタンスは `~/.gitlab/config.json`（id はインスタンスのエイリアス）、Slack の Webhook は `~/.slack/config.json`（`slack-default` と、バンドルごとの Webhook に対する `slack-<bundle>`）、メールボックスは `inboxes.json`（id は受信箱の名前）から取得します。編集ページへのリンク付き「Managed on …」バッジが表示され、ここから Test を実行することもできます。

**Test ボタン。** すべてのアカウントに **Test** ボタンがあります（Slack・Telegram・チャット Webhook は **Send test message**）。最後の結果は `outputs/connectors/test-results.json` に保存されます。

**ループと通知。** **Loops** では、機能を必要とするループに「Needs: …」チップが表示され、その機能を持つコネクタが存在するまで（UI でもサーバー側でも）有効化できません。`loops.json` のエントリで `"routes_notifications": true` を宣言しているループ（ランナーが `bin/notify.py` 経由で送信するもの）には **Notify via** の選択もあり、`notify: [コネクタ id]` として保存されます。`bin/notify.py` はそうしたループの通知をそれらのコネクタへ振り分け、`notify` が未設定の場合は従来どおりデフォルトの Slack Webhook に投稿します。組み込みの GitLab・Topic・Inbox ループはまだこれを宣言しておらず、引き続き Slack Webhook に直接投稿します。それらに既に `notify` リストがある場合、Loops では読み取り専用で表示され、**Clear** ボタンで解除できます。CLI からは `python3 bin/notify.py <loop> "<text>"` で試せます。ダッシュボードの AI パネルからもコネクタを一覧できます（チャットツール `connector-list`）。

## スクリプトリファレンス

展開すると全一覧を表示します


| スクリプト                          | 用途                                                                                                                                                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run-loop-now.sh`                   | 登録済みループ 1 つを実行する汎用エントリポイント（`bin/loops_config.py` 経由で `~/.loop-engineering/loops.json` から検索）— `outputs/history/` にログを出力し、失敗時は Slack に通知します。`bin/loop_scheduler.py`（スケジュール時）またはダッシュボード（オンデマンド時）から呼び出されます |
| `bin/loop_scheduler.py`             | launchd でスケジュールされる単一のポーリングループ：`~/.loop-engineering/loops.json` を読み込み、期限が来た登録済みループを `run-loop-now.sh` 経由で実行します                                                                       |
| `bin/loops_config.py`               | `~/.loop-engineering/loops.json`（スケジュールされたループのレジストリ：名前、スケジュール、エントリポイント）を読み込みます。現在は書き込み機能がないため、変更するにはファイルを手動編集（またはテンプレートをコピー）してください |
| `bin/gitlab_loop_runner.py`         | `gitlab-loop` 実行時に `run-loop-now.sh` が処理を委譲する Issue ごとのオーケストレーター：アサインされた Issue を検出し、各 Issue を専用の `LoopRuntime` で処理し（`outputs/loop-runs/` 配下に Issue ごとに `LoopResult` を 1 件）、`claude -p`/`codex exec` の呼び出しと、その `--allowedTools`/`--disallowedTools` による安全境界を担い、最後にバッチ全体に対して無条件の終了処理を 1 回実行します |
| `bin/scripts/build_run_prompt.sh`   | `bin/gitlab_loop_runner.py` が AI CLI に渡すプロンプト文字列を構築します — `<alias> <issue_iid>` で単一 Issue 用プロンプト（ダッシュボードの Dashboard → Activity チャットからの対象限定実行）、`--batch-issue <alias> <issue_iid>` でスケジュールされたバッチ内の 1 Issue 用（終了処理なし）、`--batch-end-of-run` でバッチ全体のダイジェスト/daily-review の終了処理用 |
| `bin/web/dashboard_server.py`       | Web ダッシュボード。小さな CLI（`write-status`、`write-skills-install-status`、`read-messages`、`add-message`、`chat-tool`）も兼ねており、`run-loop-now.sh`、`bin/loop_scheduler.py`、ダッシュボード自身のアクション、Dashboard → Activity ビューに埋め込まれたチャットアシスタントから使われます |
| `bin/loop_config.py`                | `~/.loop-engineering/projects.json` を読み込みます                                                                                                                                                                                   |
| `bin/list_assigned_issues.py`       | 設定済みのプロジェクト全体で、設定したユーザーにアサインされたオープンな GitLab Issue を一覧表示します                                                                                                                               |
| `bin/track_new_comments.py`         | キャッシュされた Issue のノートのうち、ループが前回確認して以降に追加されたものを検出します                                                                                                                                          |
| `bin/project_memory.py`             | GitLab キャッシュ内にインラインで保存された、プロジェクトごとの永続的な教訓（レガシー）を読み込みます                                                                                                                                |
| `bin/memory_store.py`               | Issue ごとの永続的なタスクメモリを markdown ファイル（Issue ごとに 1 つ、加えてプロジェクトごとの MEMORY.md インデックス）として読み込み/記録します                                                                                  |
| `bin/ai_cli_config.py`              | `~/.loop-engineering/ai_cli.json`（`run-loop-now.sh` が登録済みの全ループで呼び出す AI CLI、`claude` または `codex`）を読み書きします                                                                                                |
| `bin/topic_monitor_runner.py`       | `topic-loop` 実行時に `run-loop-now.sh` が処理を委譲するトピックごとのオーケストレーター：設定済みの各トピックを専用の `LoopRuntime` で処理し（`outputs/loop-runs/` 配下にトピックごとに `LoopResult` を 1 件）、`claude -p`/`codex exec` の呼び出しとその安全境界を担います — `bin/gitlab_loop_runner.py` が GitLab ループで果たすのと同じ役割をトピックモニターループで果たします |
| `bin/scripts/build_topic_prompt.sh` | 設定済みのトピック 1 つ分のプロンプト文字列を構築します。上記の `build_run_prompt.sh` と同じ役割です。`topic_monitor_runner.py` からはもう呼ばれていませんが、文書化された手動の逃げ道として残しています                            |
| `bin/topic_config.py`               | `~/.loop-engineering/topics.json` を読み込みます                                                                                                                                                                                     |
| `bin/topic_seen.py`                 | トピックごとの 7 日間のローリング重複排除ウィンドウ。ブリーフィングが 2 日続けて同じ話題を繰り返さないようにします                                                                                                                   |
| `bin/slack_notify.py`               | 設定済みの Slack incoming webhook にメッセージを投稿します                                                                                                                                                                           |
| `bin/scripts/new_worktree.sh`       | `loop/issue-<iid>` ブランチ上に隔離された git worktree を作成（または再利用）します                                                                                                                                                  |
| `bin/scripts/open_merge_request.sh` | Issue ブランチを push して MR を作成します — `loop/issue-*` という名前でないものは拒否します                                                                                                                                         |
| `bin/scripts/install.sh`            | オンラインインストーラー — このリポジトリをクローン（または更新）し、`setup.sh` を実行します（`--config-path`/`--topics-config-path`/`--ai-cli-config-path`/`--loops-config-path`/`--state-path` をそのまま転送）。既存のインストールには `--upgrade` を使い、現在ロードされているすべての launchd エージェントを更新して新しいコードを反映させます（ダッシュボードは再起動、スケジューラーデーモンは再登録のみ）。また、統合スケジューラー導入前の古い `com.hermes.loop-engineering.plist` をその場で移行し、統合スケジューラー以前からインストールされたままの、もはや不要な `com.hermes.loop-engineering-topic-monitor` デーモンが残っていれば削除します。`curl` からパイプしても安全です |
| `bin/scripts/setup.sh`              | ワンコマンドのインストール：`gitlab-config` スキルと `projects.json`/`topics.json`/`ai_cli.json`/`loops.json` の雛形、さらに登録済みの全ループについて今日の日付で初期化した `loop_scheduler_state.json` を用意します。これによりインストール直後にスケジューラーを有効にしても即座に実行が走ることはありません |
| `bin/scripts/setup-nginx.sh`        | 任意のローカル nginx リバースプロキシ（`http://loop.x/` → ダッシュボード）                                                                                                                                                       |
| `bin/scripts/uninstall.sh`          | `setup.sh`/`setup-nginx.sh`/`install.sh` の変更を元に戻します。`curl` からパイプしても安全です                                                                                                                                       |




## 安全上の境界

固定されており、時間の経過や成功の積み重ねによって緩むことはありません（[`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md) を参照）。

- **マージリクエストを決してマージしません。** ループの仕事は「MR を作成し、検証が通っている」状態で終わります。マージは常に人間が手動で行うステップです。
- すべてのコード変更は、専用の git worktree の `loop/issue-<iid>` ブランチ上で行われ、ターゲットブランチ上で直接行われることはありません。
- MR が作成されるのは、プロジェクト自身に設定された `test_cmd`/`lint_cmd` が通り、かつ diff が Issue に関連するファイルだけに触れている場合に限られます。
- 任意のシェル実行、依存関係のアップグレード、`.env`/認証情報/SSH キーの読み取りは行いません — 使えるのは `LOOPX_INSTRUCTIONS.md` の許可コマンドリストだけです。
- Issue は一度に 1 件ずつ順番に処理され、決して並列には処理されません。
- 同じ Issue で検証が失敗した場合、その実行内で再試行されることはなく、代わりに GitLab コメントでエスカレーションされます。

Inbox Triage ループには独自の固定された安全境界があります（[`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md) を参照）。

- **メールを決して送信しません。** どちらのメールプロバイダーモジュールにも送信関数はなく、取得する Outlook トークンも `Mail.Send` を含まないスコープになっています。送信はコードレベルだけでなく、トークンレベルで不可能です。
- **アーカイブ、削除、移動、既読状態の変更を決して行いません。** メールボックスへの書き込みは、`Loop/*` ラベル/カテゴリの作成とその適用、そしてメールボックス自身の下書きフォルダに残す返信下書きの作成だけです。
- **適用されるのは `Loop/*` ラベルだけです。** カテゴリラベルは、デフォルトでもカスタムでも、すべて `Loop/` で始まる必要があります。そうでなければ `inboxes.json` は読み込み時に拒否されるため、手動編集された `TRASH` や `UNREAD` のようなシステムラベルが実際のメールに適用されることはありません。
- **メッセージ本文は決して永続化されません。** 本文はメモリ上と、受信トレイごとの `claude -p` 呼び出し（加えて最大 1 回の再試行）のプロンプト内にのみ存在し、セッションのトランスクリプトは残りません。`outputs/`、ログ、ステータス、Slack ダイジェストに書き込まれることはなく、これらが受け取るのは送信者、件名、カテゴリ、AI による短い理由、下書きへのリンクだけです。AI が書いた返信の下書きは、メールボックス自身の下書きフォルダにのみ保存されます。
- **Inbox Triage には Claude CLI が必要です。** Codex は常にモデルにシェルを与え、プロンプトを `~/.codex/sessions/` 配下に記録するため、Codex を選択していると、**Settings** で AI CLI を Claude に戻すまで、メールを読む前の段階ですべての受信トレイが失敗します。
- **リフレッシュトークンは macOS キーチェーンにのみ保存されます。** トークンを stdin で渡す `security -i` で書き込まれ、平文でディスクに置かれることも、プロセスの argv に現れることもありません。



## テスト

```bash
python3 -m pytest tests/
```

`bin/` 配下のすべてのスクリプト（Python でもシェルでも、どのフォルダにあっても）には対応する `tests/test_*.py` があり、可能な限りモックではなく実際のサブプロセスや一時ディレクトリを使ってテストされています（実際のローカル git リポジトリを使う例として `tests/test_new_worktree.py` を参照）。

## プロジェクトドキュメント


| ドキュメント                                                           | 用途                                                                                                   |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md)                         | V2 ランタイムのアーキテクチャ：`LoopDefinition`/`LoopState`/`LoopRuntime`、検証/予算/ポリシー、可観測性、CLI — 個々のループの仕様ではなく全体の地図 |
| [`TASK.md`](https://github.com/encoreshao/loop-engineering/blob/main/TASK.md)                                                   | このリポジトリが実行するすべてのスケジュールタスクの索引。それぞれ `docs/tasks/` 配下の仕様を指しています |
| [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)   | GitLab Issue ループの人間向け仕様：目的、範囲、安全上の境界                                            |
| [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) | トピックモニターループの人間向け仕様：目的、範囲、安全上の境界                                         |
| [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md)                         | GitLab Issue ループ自身が毎回の実行で従う段階的な手順                                                  |
| [`TOPIC_MONITOR_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/TOPIC_MONITOR_INSTRUCTIONS.md)       | トピックモニターループ自身が毎回の実行で従う段階的な手順                                               |
| [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md)                                           | ループが毎回の実行で読み込み・更新するライブ状態 — 前回の実行の概要、未解決のエスカレーション、下された判断 |
| [`docs/troubleshooting/crash-looping-launchd-agent.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/troubleshooting/crash-looping-launchd-agent.md) | クラッシュループに陥ってログを溢れさせている `com.hermes.loop-engineering*` launchd エージェントの診断と修正 |




## ライセンス

[MIT](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) — 詳しくは [`LICENSE`](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) ファイルを参照してください。
