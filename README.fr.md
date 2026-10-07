[English](README.md) | [日本語](README.ja.md) | [简体中文](README.zh-CN.md) | **Français**

# Loop X Engineering

![CI](https://github.com/encoreshao/loop-engineering/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/encoreshao/loop-engineering)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Shell](https://img.shields.io/badge/shell-bash-4EAA25)

La mission de Loop X Engineering est de vous rendre le temps que dévore le
tri des tickets : un coéquipier permanent et autonome qui traite votre file
GitLab chaque jour ouvré, pour qu'aucun ticket qui vous est assigné ne reste
en souffrance — en livrant des correctifs, en répondant aux questions ou en
signalant ce qui requiert réellement votre jugement — et que votre attention
n'aille qu'à ce qui compte vraiment. Un tableau de bord web local vous permet
de le regarder travailler, de passer en revue tout ce qu'il a fait et de tout
configurer à la main — sans éditer de JSON.

Il est conçu pour tourner sans surveillance en toute sécurité : il ne fusionne
jamais ses propres merge requests, ne s'assigne jamais de nouveaux tickets et
ne touche qu'aux projets que vous lui avez explicitement indiqués.

## Table des matières

- [Comment ça marche](#comment-ça-marche)
- [Boucles](#boucles)
- [Prérequis](#prérequis)
- [Démarrage rapide](#démarrage-rapide)
- [Arborescence des répertoires](#arborescence-des-répertoires)
- [Configuration](#configuration)
- [Exécution](#exécution)
- [Le tableau de bord](#le-tableau-de-bord)
- [Connecteurs](#connecteurs)
- [Référence des scripts](#référence-des-scripts)
- [Garde-fous de sécurité](#garde-fous-de-sécurité)
- [Tests](#tests)
- [Documentation du projet](#documentation-du-projet)
- [Licence](#licence)



## Comment ça marche

Chaque exécution planifiée (`run-loop-now.sh gitlab-loop`) :

1. Liste tous les tickets GitLab ouverts assignés au nom d'utilisateur configuré, sur chaque alias de projet de votre configuration.
2. Les traite **un par un, jamais en parallèle**, en suivant la procédure de décision pas à pas de [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md).
3. Pour chaque ticket, fait exactement l'une des actions suivantes :
  - **Le corriger** — dans un git worktree isolé, sur une branche `loop/issue-<iid>`, en n'ouvrant une merge request qu'une fois les commandes de lint/test du projet passées.
  - **Y répondre** — publier un commentaire GitLab quand la demande ne nécessite aucune modification de code (une question, un point d'étape).
  - **L'escalader** — publier un commentaire GitLab demandant des précisions quand la demande est ambiguë ou quand la vérification échoue.
4. Envoie un message Slack par ticket plus un récapitulatif de fin d'exécution (à chaque exécution, même les matins sans rien d'assigné).
5. Met à jour [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md) et `outputs/daily-review.md` pour que la prochaine exécution — et vous — sachiez ce qui s'est passé.

Les enseignements réutilisables d'une exécution à l'autre (schémas de correction, pièges) sont enregistrés par ticket sous forme de fichiers markdown de mémoire de tâche via `bin/memory_store.py` (les entrées antérieures à ce format restent lues via `bin/project_memory.py`), de sorte que chaque exécution démarre plus avisée que la précédente.

Une deuxième boucle, indépendante (`run-loop-now.sh topic-loop`), surveille des sujets arbitraires sur le web plutôt que GitLab — voir [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md).

Une troisième boucle (`run-loop-now.sh inbox-triage-loop`) trie les boîtes de réception Gmail et Outlook : elle classe chaque nouveau message non lu sous un libellé `Loop/*`, rédige (sans jamais l'envoyer) un brouillon de réponse dans le fil pour tout ce qui est urgent, et rend compte via un récapitulatif Slack et la page **Loops → Inbox Triage** du tableau de bord — voir [`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md).

## Boucles

Sept boucles sont livrées dans `config/loops.json.template` ; chacune a sa page sous **Loops** dans le tableau de bord et sa propre spécification sous `docs/tasks/`. Le planning se modifie sur cette page ; les valeurs par défaut ci-dessous viennent du modèle.

| Boucle | Ce qu'elle fait | Prérequis | Planning par défaut | Ce qu'elle écrit hors de Loop X |
| --- | --- | --- | --- | --- |
| GitLab issues | Traite vos tickets assignés : corrige, répond ou escalade | Config GitLab (`~/.gitlab/config.json`) | Jours ouvrés 10:00 | Branches et merge requests (jamais fusionnées), commentaires de tickets, Slack |
| Topic monitor | Recherche vos sujets sur le web et envoie un briefing quotidien | Sujets (`topics.json`) | Tous les jours 10:00 | Digest Slack |
| Inbox triage | Étiquette les nouveaux e-mails et rédige des brouillons de réponse aux messages urgents (désactivée par défaut) | Une boîte mail (capacité `mail`) | Jours ouvrés 09:00 | Étiquettes et brouillons (n'envoie jamais) |
| Daily Digest | Un récapitulatif matinal : tâches, tickets assignés, MR à relire, réunions du jour, ce que Loop X a fait hier (désactivée par défaut) | Connecteur `issues` (agenda facultatif) | Jours ouvrés 09:30 | Notifications uniquement |
| MR Review | Pré-relit les merge requests dont vous êtes relecteur (désactivée par défaut) | Connecteur `merge_requests` (GitLab) | Toutes les 2 heures | Notes brouillon GitLab uniquement ; ne publie, n'approuve ni ne poste jamais de note normale |
| Pipeline Doctor | Diagnostique les pipelines CI en échec sur les projets suivis et vos MR, et signale les échecs récurrents (désactivée par défaut) | Connecteur `pipelines` (GitLab) | Toutes les heures | Notifications uniquement |
| RSS Watch | Classe les nouvelles entrées de flux selon vos centres d'intérêt et envoie un court digest (désactivée par défaut) | Connecteur `feed` (RSS) | Tous les jours 08:00 | Notifications uniquement |
| Calendar Prep | Une note de préparation avant chaque réunion : ordre du jour, travail GitLab lié, e-mails récents avec les participants, suivis de la dernière fois (désactivée par défaut) | Connecteur `calendar` (Google Agenda) ; GitLab et boîte mail en option | Toutes les 15 minutes | Notifications uniquement |

Les quatre dernières sont des plugins LoopKit (`bin/loopkit.py`, `bin/loop_plugins/`) : le modèle s'exécute en mode scellé (sans outils ni serveurs MCP), chaque élément est isolé pour qu'un échec n'arrête pas l'exécution, et les notifications passent par les connecteurs **Notify via** de la boucle. Voir [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md#loopkit).

## Prérequis

- macOS (la planification et le tableau de bord tournent tous deux comme agents `launchd`)
- Python 3.12+ — le code de ce dépôt utilise **uniquement la bibliothèque standard**, aucun `pip install` n'est nécessaire pour l'exécuter
- `git` 2.42+ (worktrees, push-options)
- Un compte GitLab + un jeton d'accès personnel pour les projets à suivre
- (facultatif) Un webhook entrant Slack, pour les notifications d'exécution
- La skill `[gitlab-config](https://github.com/encoreshao/encore-skills/tree/main/skills/gitlab-config)` de `[encore-skills](https://github.com/encoreshao/encore-skills)` — l'unique dépendance externe de cette boucle, déployée dans `~/.encore-skills` par `setup.sh`. Vérifiez à tout moment qu'elle est bien présente depuis la page **Settings → Skills** du tableau de bord.
- `pytest` — uniquement pour le développement, pour exécuter la suite de tests de ce dépôt



## Démarrage rapide

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
```

Clone ce dépôt dans `~/.loop-engineering` (passez `--dir <path>` pour un autre emplacement) et exécute `bin/scripts/setup.sh`, qui installe la skill `gitlab-config` et génère `projects.json`/`topics.json` à partir de leurs modèles. Il configure ensuite le reverse proxy nginx local et démarre le tableau de bord comme agent `launchd` permanent, si bien que cette seule commande se termine avec un tableau de bord réellement joignable et en marche — passez `--skip-nginx` et/ou `--skip-launchd-daemons` pour désactiver l'un ou l'autre. (La boucle GitLab planifiée et le moniteur de sujets ne sont *pas* démarrés automatiquement, car ils agiraient sur `projects.json`/`topics.json` avant que vous ne les ayez remplis — démarrez-les vous-même, une fois configurés, depuis la page **Settings → Daemons** du tableau de bord.) Relancer la même commande plus tard récupère simplement la dernière version de `main` au lieu de recloner.

Déjà installé et vous voulez simplement mettre à jour ? Ajoutez `--upgrade` :

```bash
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash -s -- --upgrade
```

Mêmes étapes que ci-dessus, mais échoue immédiatement si rien n'est encore installé dans `--dir` au lieu de cloner silencieusement, et rafraîchit chacun des agents launchd de ce projet actuellement chargés — pas seulement le tableau de bord. Le tableau de bord (un serveur permanent) est réellement redémarré (`launchctl kickstart -k`), contrairement à un simple `launchctl load`, sans effet sur un agent déjà en marche. `com.hermes.loop-engineering` — l'ordonnanceur unique qui exécute chaque boucle enregistrée dans `loops.json`, si vous l'avez activé depuis la page **Settings → Daemons** — voit seulement son enregistrement rechargé (`unload` + `load -w`) — jamais de kickstart, car cela déclencherait immédiatement une vraie exécution hors planning contre GitLab/Slack en production au lieu d'attendre le prochain sondage de l'ordonnanceur. `--upgrade` migre aussi un plist généré antérieur à l'ordonnanceur unifié (qui pointe encore vers `run-loop.sh`, désormais supprimé) et retire le daemon orphelin `com.hermes.loop-engineering-topic-monitor` s'il est encore installé d'avant cette migration.

Vous préférez voir le clonage se faire vous-même d'abord ?

```bash
git clone https://github.com/encoreshao/loop-engineering.git
cd loop-engineering
bin/scripts/setup.sh
```

La skill est déjà installée et vous voulez seulement les fichiers de configuration de base ?

```bash
bin/scripts/setup.sh --skip-skills-install
```

Une fois terminé, ouvrez la page **Settings → Skills** du tableau de bord pour confirmer que tout le nécessaire est bien installé — elle vérifie en direct, sans approximation.

**Vous travaillez déjà dans Claude Code ?** Collez ceci au lieu d'exécuter les commandes vous-même :

> Clone et configure [https://github.com/encoreshao/loop-engineering](https://github.com/encoreshao/loop-engineering) pour moi : lance son installateur en ligne
> (`curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash`),
> puis aide-moi à remplir `~/.loop-engineering/projects.json` avec mon ou mes propres projets GitLab, et `~/.gitlab/config.json` avec mon jeton GitLab.



### Désinstallation

```bash
bin/scripts/uninstall.sh                 # or: curl -fsSL .../uninstall.sh | bash
```

Décharge et supprime les agents `launchd` de ce dépôt, annule `setup-nginx.sh` si vous l'avez exécuté, et supprime tout le dossier `~/.loop-engineering` — code, configuration et historique d'exécution ensemble — passez `--keep-config` pour tout laisser en place (par exemple si vous vous apprêtez à réinstaller). Peut être relancé sans risque.

## Arborescence des répertoires

Avec le chemin d'installation par défaut, tout se retrouve dans un seul dossier :

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

`projects.json`, `topics.json`, `loops.json`, `instructions.md` et `ai_cli.json` se résolvent toujours vers `~/.loop-engineering/…`, quel que soit l'endroit où vous clonez le code — ils ne se retrouvent *dans* le dossier du dépôt ci-dessus que parce que la cible de clonage par défaut de `install.sh` est justement ce même chemin. Si vous clonez ailleurs à la main, ces cinq fichiers restent dans `~/.loop-engineering/`, séparés du code. Le `worktree_root` généré dans `projects.json` vaut aussi `~/.loop-engineering/worktrees` par défaut, pour la même raison.

Deux autres fichiers de configuration se trouvent entièrement hors de cette arborescence, modifiables depuis les pages **Loops → GitLab Issues → Projects** et **Settings → Notifications** du tableau de bord plutôt qu'à la main : `~/.gitlab/config.json` et `~/.slack/config.json`.

## Configuration


| Fichier                               | Contenu                                                                                                                                                                                                                 | Géré via                                                                                                                                                                      |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `~/.loop-engineering/projects.json`   | Les projets à suivre, leurs chemins de checkout locaux, la branche cible, les commandes d'installation/lint/test, votre nom d'utilisateur GitLab et le répertoire de travail des worktrees (`worktree_root`, par défaut `~/.loop-engineering/worktrees`) | Section « Tracked Projects » de la page **Loops → GitLab Issues → Projects** du tableau de bord, ou copiez [`config/projects.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/projects.json.template) à la main, ou laissez `bin/scripts/setup.sh` s'en charger |
| ↳ `instance` par projet (facultatif)  | Remplace le `gitlab_instance` de premier niveau pour un projet — à définir quand vos projets s'étendent sur plusieurs instances GitLab. Retombe sur `gitlab_instance` s'il est omis.                                       | Même fichier, par entrée de projet — voir l'exemple `harbor` du modèle                                                                                                        |
| `~/.loop-engineering/topics.json`     | Les sujets à surveiller et ce qui est jugé notable pour chacun (boucle de surveillance de sujets uniquement)                                                                                                             | Copiez [`config/topics.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/topics.json.template) à la main, ou laissez `bin/scripts/setup.sh` s'en charger                                                                |
| `~/.loop-engineering/inboxes.json`    | Les boîtes mail à trier (fournisseur, compte, catégories, expéditeurs VIP/exclus, bundle Slack) et l'ensemble de catégories par défaut partagé (boucle Inbox Triage uniquement)                                          | Page **Loops → Inbox Triage → Setup** du tableau de bord (`/inbox/setup`), ou laissez `bin/scripts/setup.sh` le générer à partir de [`config/inboxes.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/inboxes.json.template)               |
| `~/.loop-engineering/mail_oauth.json` | Le client ID propre à l'application OAuth Gmail/Outlook (et, pour Google, le client secret) — une étape unique d'enregistrement d'application, pas un identifiant par boîte mail                                         | Page **Loops → Inbox Triage → Setup** du tableau de bord                                                                                                                                                     |
| `~/.loop-engineering/loops.json`      | Le registre des boucles planifiées : nom de chaque entrée, planning (jours ouvrés/heure/minute), module de point d'entrée, délai d'expiration et réglages par boucle — lu par `bin/loops_config.py`, sondé par `bin/loop_scheduler.py` | Copiez [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template) à la main, ou laissez `bin/scripts/setup.sh` s'en charger                                                                  |
| `~/.loop-engineering/loop_scheduler_state.json` | La date de dernière tentative de chaque boucle, pour que l'ordonnanceur n'exécute jamais la même boucle deux fois dans la journée — à ne pas éditer à la main                                              | Écrit automatiquement par `bin/loop_scheduler.py` ; initialisé avec la date du jour pour chaque boucle enregistrée par `bin/scripts/setup.sh`, afin qu'activer l'ordonnanceur ne déclenche pas une exécution immédiate |
| `~/.loop-engineering/instructions.md` | Vos propres instructions en texte libre, lues par la boucle au début de chaque exécution                                                                                                                                | Onglet Instructions de la page **Settings** du tableau de bord                                                                                                                               |
| `~/.loop-engineering/ai_cli.json`     | La CLI d'IA (Claude Code ou Codex CLI) que `run-loop-now.sh` invoque pour chaque boucle enregistrée ; `claude` par défaut                                                                                                 | Onglet AI CLI de la page **Settings** du tableau de bord, ou laissez `bin/scripts/setup.sh` s'en charger                                                                                                                |
| `~/.gitlab/config.json`               | URL des instances GitLab, jetons et correspondances alias de projet → ID de projet (lus par la skill `gitlab-config`)                                                                                                    | Page **Loops → GitLab Issues → Projects** du tableau de bord                                                                                                                                                     |
| `~/.slack/config.json`                | L'URL de votre webhook entrant Slack (et d'éventuelles surcharges par bundle)                                                                                                                                          | Onglet Notifications de la page **Settings** du tableau de bord (webhook par défaut) / section Access bundles de la page **Loops → GitLab Issues → Projects** (surcharges par bundle)                                                              |


`bin/loop_config.py` est le seul code qui lit `projects.json` — utilisez-le pour vérifier votre configuration depuis un terminal :

```bash
python3 bin/loop_config.py aliases                # every configured project alias
python3 bin/loop_config.py project <alias>         # that alias's full config, incl. resolved GitLab instance
python3 bin/loop_config.py assignee                # the GitLab username being tracked
python3 bin/loop_config.py worktree-root           # where per-issue worktrees get created
```

Si `~/.loop-engineering/projects.json` n'existe pas encore, chaque script qui en a besoin échoue immédiatement avec un message vous invitant à exécuter `bin/scripts/setup.sh` — rien ne devine silencieusement les chemins.

**Access bundles** — surcharges de jeton/webhook par projet

La plupart des projets utilisent simplement le jeton par défaut de leur instance GitLab. Un **access bundle** est une surcharge nommée — sa propre paire `{instance, token}`, plus un webhook Slack facultatif — pour le rare projet dont le jeton par défaut de l'instance n'a pas les accès nécessaires.

Gérez les bundles depuis la page **GitLab** du tableau de bord, dans leur propre section « Access bundles » :

- **Ajouter un bundle** : nommez-le, choisissez l'instance GitLab auprès de laquelle il s'authentifie, collez son jeton et, éventuellement, une URL de webhook Slack.
- **Assigner un bundle à un projet** : modifiez la ligne de l'alias de projet et choisissez le bundle dans la liste déroulante **Bundle** — par défaut « (use instance default) ».
- Un bundle ne peut pas être supprimé, ni son instance modifiée, tant qu'un alias de projet pointe encore vers lui.
- Supprimer un bundle efface aussi sa surcharge de webhook Slack, s'il en avait une.

Les bundles se trouvent dans la clé `bundles` de `~/.gitlab/config.json` et, si une surcharge de webhook est définie, dans la clé `bundle_webhooks` de `~/.slack/config.json` — reliés uniquement par le nom du bundle.

## Exécution

**Manuellement**, une fois, pour le voir fonctionner avant de lui confier un planning :

```bash
bash run-loop-now.sh gitlab-loop   # the daily GitLab issue loop
bash run-loop-now.sh topic-loop    # the topic monitor loop
```

Les deux journalisent dans `outputs/history/`, et ajoutent aussi la sortie de chaque invocation de la CLI `claude` à `logs/loop-engineering.log` (consultable sur la page **Runs → Logs** du tableau de bord) ; vous pouvez aussi déclencher la boucle GitLab depuis le bouton **Run now** du tableau de bord (Dashboard → Overview), sans terminal.

**Selon un planning**, via `launchd` — installez les deux agents de [`launchd/`](https://github.com/encoreshao/loop-engineering/tree/main/launchd), le plus simplement d'un clic chacun depuis la page **Settings → Daemons** du tableau de bord (qui indique aussi si chacun est actuellement chargé et son PID), ou à la main :

```bash
cp launchd/com.hermes.loop-engineering*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering.plist
launchctl load -w ~/Library/LaunchAgents/com.hermes.loop-engineering-dashboard.plist
```


| Agent                                   | Exécute                                                                                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------|
| `com.hermes.loop-engineering`           | La boucle de sondage unique de l'ordonnanceur (`bin/loop_scheduler.py`), toutes les 15 minutes (`StartInterval`) — exécute, via `run-loop-now.sh`, la ou les boucles enregistrées dans `~/.loop-engineering/loops.json` qui sont dues |
| `com.hermes.loop-engineering-dashboard` | Le tableau de bord web, en permanence (`RunAtLoad` + `KeepAlive`)                                                                                 |


Les boucles exécutées et leur planning relèvent de la configuration, pas du code — modifiez `~/.loop-engineering/loops.json` (voir [`config/loops.json.template`](https://github.com/encoreshao/loop-engineering/blob/main/config/loops.json.template)) pour ajouter une boucle ou changer son échéance ; ajouter une troisième boucle demande une nouvelle entrée dans `loops.json`, pas un nouveau plist. L'éditeur de planning par agent de la page **Settings → Daemons** ne s'applique qu'au `StartCalendarInterval` propre d'un plist, que `com.hermes.loop-engineering` n'a plus (il sonde toutes les 15 minutes sur un `StartInterval` fixe et s'en remet à `loops.json` pour savoir quelle boucle est due) — modifier le planning d'une boucle passe pour l'instant par une édition manuelle de `loops.json`.

## Le tableau de bord

Une interface web accessible uniquement en localhost et sans dépendance (Python stdlib, aucun framework JS), servie par `bin/web/dashboard_server.py`. Lancé directement pour le développement local (sans argument), il utilise son propre port par défaut, `8420`. `bin/scripts/install.sh` choisit un port aléatoire entre `48420` et `48620` lors de la première installation de l'agent `launchd` permanent (modifiable avec `--port`, et jamais re-tiré lors d'un `--upgrade` ultérieur) — consultez `launchd/com.hermes.loop-engineering-dashboard.plist` pour connaître le port réellement utilisé par une installation existante.


| Entrée de la barre latérale | Affiche |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard** (`/`) | Vues : Overview — L'état de l'exécution en cours/dernière, un indicateur de progression en direct et le bouton Run now; Activity — Un fil de messages avec la boucle, avec son propre indicateur de progression en direct. Collez-y le lien d'un ticket GitLab pour que la boucle le traite immédiatement, quelle que soit la personne à qui il est assigné. |
| **Loops** (`/loops`) | Un catalogue qui sépare les boucles actives de celles disponibles ; chaque boucle visible apparaît aussi comme lien enfant sous **Loops** dans la barre latérale (Inbox Triage y reste masquée tant qu'elle est désactivée et n'a jamais été exécutée) |
| — **GitLab Issues** (`/loops/gitlab-loop`) | Vues : Live — Vos tickets assignés et MR ouvertes du moment, récupérés en direct; Projects — Gère `~/.gitlab/config.json` (instances, alias de projet, access bundles) et `~/.loop-engineering/projects.json` (projets suivis, réglages de la boucle) sans éditer de JSON à la main |
| — **Topic Monitor** (`/loops/topic-loop`) | Vues : Live — L'état et les synthèses enregistrées de chaque sujet configuré; Topics — Ajoute, modifie et supprime les sujets surveillés — une vue distincte sur la même page, pour que la configuration n'encombre pas la vue d'état Live |
| — **Inbox Triage** (`/loops/inbox-triage-loop`) | Vues : L'état en direct propre à Inbox Triage (**Live**), plus la connexion des boîtes mail et les catégories (**Setup** : connecter Gmail/Outlook, catégories, expéditeurs VIP/exclus, bundle Slack) |
| **Runs** (`/runs`) | Vues : Loop Runs — Chaque exécution enregistrée sous `outputs/loop-runs/` (une par ticket ou sujet traité), la plus récente en premier — en lecture seule ; un bandeau de synthèse indique le nombre total d'exécutions, le taux de réussite/escalade, le coût moyen et le Loop Efficiency Score expérimental; History — Le rapport de revue de chaque exécution passée, du plus récent au plus ancien; Logs — La fin de `logs/loop-engineering.log` - la sortie de chaque invocation de la CLI `claude`, pour la boucle GitLab, la boucle de surveillance de sujets et l'assistant de chat du tableau de bord lui-même |
| **Insights** (`/insights`) | Vues : Analytics — Les performances de la boucle sur une fenêtre de jours au choix : un score Loop Health, les résultats, la qualité, le risque et la classification, la répartition des échecs et les tendances d'apprentissage; Cost — Le coût d'utilisation de l'IA — le coût fenêtré propre à la boucle de tickets GitLab, et le coût total de toutes les exécutions sous `outputs/loop-runs/`; Budget — Le dernier état de budget connu de chaque exécution enregistrée, plus des agrégats par définition de boucle et par jour/semaine/mois; Memory — Les enseignements inter-exécutions enregistrés par projet, un fichier markdown par ticket GitLab, plus tout ce qui a été enregistré avant ce format (affiché sous « Legacy learnings ») |
| **Harness** (`/harness`) | Vues : Audit — Un score et des contrôles réussi/échoué pour chaque définition de boucle |
| **Connectors** (`/connectors`) | Vues : Accounts — Tous les comptes de connecteurs regroupés par type, avec des pastilles de capacités, un bouton Test (**Send test message** pour les cibles de notification) qui affiche son dernier résultat, et un badge « Managed on … » renvoyant à la page propriétaire pour les comptes externes ; Add — Choisissez un type et remplissez son formulaire. Les secrets se saisissent dans un champ mot de passe et ne sont plus jamais affichés (laissez vide pour conserver la valeur stockée lors d'une modification) |
| **Settings** (`/settings`) | Vues : General — Notifications (gère le webhook par défaut de `~/.slack/config.json`), AI CLI (choix entre Claude Code et Codex CLI, avec une vérification en direct installé/introuvable pour chacun), Appearance (mode de couleur, thème d'accent, intervalle d'actualisation automatique — enregistrés dans le `localStorage` de ce navigateur) et Instructions (vos propres instructions en texte libre, lues par la boucle au début de chaque exécution) — regroupés en onglets sur une seule page (la vue **General** est répartie en onglets Notifications / AI CLI / Appearance / Instructions (`?tab=`)); Daemons — L'état de chargement, un planning modifiable et l'activation/désactivation de chaque agent `launchd`, plus une vue Registered Loops de chaque boucle exécutée par l'ordonnanceur unifié (son propre planning et l'état de sa dernière exécution, lus depuis `loops.json`); Skills — Chaque skill externe dont dépend cette boucle, et si elle est réellement installée |
| **README** (`/readme`) | Déplacé vers l'icône d'aide de la barre supérieure (`/readme`) : ce fichier, rendu dans l'application avec une navigation rapide vers chaque section |

Toutes les anciennes URL (`/activity`, `/gitlab`, `/topic-monitor`, `/inbox`, `/loop-runs`, `/history`, `/logs`, `/analytics`, `/cost`, `/budget`, `/memory`, `/audit`, `/settings/general`, `/daemons`, `/skills`, etc.) sont redirigées définitivement (301) vers leur nouvel emplacement, query string conservée, de sorte que les favoris continuent de fonctionner.


**Facultatif : un nom d'hôte convivial via nginx**

Par défaut, le tableau de bord n'est joignable qu'à `http://127.0.0.1:<port>` (voir ci-dessus comment `<port>` est choisi). `bin/scripts/setup-nginx.sh` configure un reverse proxy nginx local pour le rendre joignable à `http://loop.x/` (port 80) à la place — installe nginx via Homebrew si nécessaire, écrit la configuration du proxy, ajoute `loop.x` à `/etc/hosts` et démarre nginx comme service système. `install.sh` lui transmet déjà automatiquement le port installé ; idempotent, il peut aussi être relancé seul sans risque :

```bash
bin/scripts/setup-nginx.sh
# or, with no clone at all:
curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
```

L'écriture de `/etc/hosts` et le démarrage du service nginx nécessitent tous deux `sudo` — macOS vous demandera votre mot de passe à ces deux étapes. Passez `--domain`/`--port` pour utiliser autre chose que `loop.x`/`8420`.

## Connecteurs

Un connecteur est un compte auquel les boucles peuvent se connecter : une instance GitLab ou GitHub, un webhook Slack, Telegram ou de messagerie, un espace Notion, une liste de flux RSS, un espace Jira ou Linear, une boîte mail, un Google Agenda. On les gère depuis la page **System → Connectors** du tableau de bord (`/connectors`). Chaque type déclare des *capacités* (`issues`, `merge_requests`, `pipelines`, `notify`, `feed`, `mail`, `docs`, `calendar`), et une boucle peut exiger une capacité plutôt qu'un produit précis.

| Type | Capacités | Ce que vous saisissez | Secret |
| --- | --- | --- | --- |
| GitLab | `issues`, `merge_requests`, `pipelines` | URL | jeton d'accès personnel |
| GitHub | `issues`, `merge_requests`, `pipelines` | URL de l'API (par défaut `https://api.github.com`), nom d'utilisateur | jeton |
| Slack webhook | `notify` | — | URL du webhook |
| Chat webhook | `notify` | choisir un préréglage : Feishu, DingTalk, WeCom 企业微信 (bot de groupe ; WeChat personnel n'a pas d'API de bot), Microsoft Teams, Discord, Google Chat ou Generic webhook | URL du webhook |
| Bot Telegram | `notify` | ID du chat | jeton du bot |
| RSS / Atom feeds | `feed` | URL des flux, une par ligne | — |
| Notion | `docs` (affichée comme Documents) | — | jeton d'intégration |
| Jira Cloud | `issues` | URL du site, e-mail | jeton d'API |
| Linear | `issues` | — | clé d'API |
| Mailbox | `mail` | externe — gérée dans la configuration d'Inbox Triage | — |
| Google Calendar | `calendar` (affichée comme Calendar) | ID du calendrier (par défaut `primary`) | connexion Google (lecture seule, `calendar.readonly`) ; le jeton d'actualisation est stocké dans le Trousseau |

**Galerie et formulaire.**

- **Add** ouvre une galerie des types de connecteurs, regroupés en Code hosting, Chat & notifications, Work tracking, Knowledge, Feeds et Mail (Outlook s'y trouve), la section Google venant en premier avec Gmail, Google Calendar et Google Chat, avec un champ de recherche pour filtrer. Les cartes suivent l'accent choisi dans **Settings → Appearance** ainsi que la couleur de marque de chaque service, et chaque préréglage de webhook de chat a sa propre description.
- Chaque tuile et chaque ligne de compte affiche le logo du service (logos Simple Icons intégrés ; les services sans logo — Feishu, DingTalk, le webhook générique — reçoivent une lettre-monogramme).
- La tuile du webhook de messagerie se déploie en préréglages (voir ci-dessus), chacun avec une courte indication (par exemple, les webhooks Workflows de Teams peuvent exiger des Adaptive Cards) et un lien **Where do I get this?** vers la documentation du service.
- Le formulaire d'ajout/modification comporte une section **Account** (**Label** et **Connector id** ; l'id est suggéré à partir du label tant que vous ne le modifiez pas), une section **Connection** (les réglages du type) et une section **Credentials** (le secret).
- **Google Calendar** n'a aucun secret à coller : cliquez sur **Connect with Google** (**Reconnect** une fois connecté) pour vous connecter. Il réutilise le client OAuth Google déjà configuré pour Gmail dans la page de configuration d'Inbox Triage (onglet Gmail ; s'il manque, le formulaire renvoie vers cette page), donc Google Cloud doit avoir la même URI de redirection, `http://127.0.0.1:<port>/oauth/google/callback`. La ligne du compte affiche un marqueur **Connected** / **Not connected**, et **Test** lit le calendrier. Seule la portée en lecture seule `calendar.readonly` est demandée.
- Les champs obligatoires sont marqués `*`, les autres indiquent « (optional) », et les champs ont des exemples en filigrane. Le champ secret a une bascule **Show**/**Hide**.
- Boutons : **Save**, **Save and test** (enregistre puis lance la sonde) et **Cancel**. Si l'enregistrement échoue, le formulaire est réaffiché avec vos valeurs non secrètes conservées.

**Où sont stockées les données.** Les comptes ajoutés depuis la page sont *natifs* : leurs réglages non secrets vont dans `~/.loop-engineering/connectors.json`, et leurs secrets dans le Trousseau macOS, sous le service `loop-engineering.connectors` (suffixé `.sandbox-<hash>` dès que `LOOP_ENGINEERING_HOME` est défini, de sorte qu'une exécution en bac à sable ne touche jamais les vrais secrets). Les secrets ne sont jamais écrits dans `connectors.json` ni réaffichés après l'enregistrement.

**Les comptes externes** sont lus directement dans les fichiers qui en sont déjà propriétaires, sans aucune migration : les instances GitLab depuis `~/.gitlab/config.json` (id = alias de l'instance), les webhooks Slack depuis `~/.slack/config.json` (`slack-default`, plus `slack-<bundle>` pour chaque webhook de bundle), et les boîtes mail depuis `inboxes.json` (id = nom de la boîte). Ils affichent un badge « Managed on … » qui renvoie à la page où on les modifie ; vous pouvez tout de même les tester ici.

**Boutons de test.** Chaque compte a un bouton **Test** (**Send test message** pour les webhooks Slack, Telegram et de messagerie) ; le dernier résultat est conservé dans `outputs/connectors/test-results.json`.

**Boucles et notifications.** Dans **Loops**, une boucle qui exige une capacité affiche des pastilles « Needs: … » et ne peut pas être activée (ni dans l'interface, ni côté serveur) tant qu'aucun connecteur offrant cette capacité n'existe. Une boucle dont l'entrée `loops.json` déclare `"routes_notifications": true` (son exécuteur envoie via `bin/notify.py`) dispose aussi d'une sélection **Notify via**, enregistrée sous la forme `notify: [ids de connecteurs]`. `bin/notify.py` achemine la notification d'une telle boucle vers ces connecteurs ; sans `notify`, elle part vers le webhook Slack par défaut, exactement comme avant. Les boucles intégrées GitLab, Topic et Inbox ne le déclarent pas encore et publient toujours directement sur le webhook Slack ; si l'une d'elles a déjà une liste `notify`, Loops l'affiche en lecture seule avec un bouton **Clear**. Essayez-le en CLI avec `python3 bin/notify.py <loop> "<text>"`. Le panneau IA du tableau de bord peut aussi lister vos connecteurs (outil de chat `connector-list`).

## Référence des scripts

Dépliez pour la liste complète


| Script                              | Rôle                                                                                                                                                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run-loop-now.sh`                   | Point d'entrée générique pour l'exécution d'une boucle enregistrée (recherchée dans `~/.loop-engineering/loops.json` via `bin/loops_config.py`) — journalise dans `outputs/history/`, notifie Slack en cas d'échec. Invoqué par `bin/loop_scheduler.py` (selon le planning) ou par le tableau de bord (à la demande) |
| `bin/loop_scheduler.py`             | La boucle de sondage unique planifiée par launchd : lit `~/.loop-engineering/loops.json` et exécute, via `run-loop-now.sh`, la ou les boucles enregistrées qui sont dues                                                            |
| `bin/loops_config.py`               | Lit `~/.loop-engineering/loops.json` — le registre des boucles planifiées (nom, planning, point d'entrée) ; aucun chemin d'écriture pour l'instant, éditez le fichier à la main (ou copiez le modèle) pour le modifier                |
| `bin/gitlab_loop_runner.py`         | L'orchestrateur par ticket auquel `run-loop-now.sh` délègue lors de l'exécution de `gitlab-loop` : découvre les tickets assignés, fait passer chacun par son propre `LoopRuntime` (un `LoopResult` par ticket sous `outputs/loop-runs/`), gère l'invocation `claude -p`/`codex exec` et son garde-fou `--allowedTools`/`--disallowedTools`, puis exécute une clôture de fin d'exécution inconditionnelle pour tout le lot |
| `bin/scripts/build_run_prompt.sh`   | Construit le prompt que `bin/gitlab_loop_runner.py` transmet à la CLI d'IA — un prompt mono-ticket pour `<alias> <issue_iid>` (l'exécution ciblée du chat Dashboard → Activity du tableau de bord), `--batch-issue <alias> <issue_iid>` pour un ticket au sein d'un lot planifié (sans fin d'exécution), et `--batch-end-of-run` pour l'unique clôture récapitulatif/daily-review du lot |
| `bin/web/dashboard_server.py`       | Le tableau de bord web ; aussi une petite CLI (`write-status`, `write-skills-install-status`, `read-messages`, `add-message`, `chat-tool`) utilisée par `run-loop-now.sh`, `bin/loop_scheduler.py`, les actions propres du tableau de bord et l'assistant de chat intégré à la vue Dashboard → Activity |
| `bin/loop_config.py`                | Lit `~/.loop-engineering/projects.json`                                                                                                                                                                                              |
| `bin/list_assigned_issues.py`       | Liste les tickets GitLab ouverts assignés à l'utilisateur configuré sur les projets configurés                                                                                                                                      |
| `bin/track_new_comments.py`         | Détecte quelles notes d'un ticket en cache sont nouvelles depuis le dernier passage de la boucle                                                                                                                                     |
| `bin/project_memory.py`             | Lit les enseignements durables par projet (legacy), stockés en ligne dans le cache GitLab                                                                                                                                           |
| `bin/memory_store.py`               | Lit/enregistre la mémoire de tâche durable par ticket sous forme de fichiers markdown (un par ticket, plus un index MEMORY.md par projet)                                                                                            |
| `bin/ai_cli_config.py`              | Lit/écrit `~/.loop-engineering/ai_cli.json` — la CLI d'IA (`claude` ou `codex`) que `run-loop-now.sh` invoque pour chaque boucle enregistrée                                                                                        |
| `bin/topic_monitor_runner.py`       | L'orchestrateur par sujet auquel `run-loop-now.sh` délègue lors de l'exécution de `topic-loop` : fait passer chaque sujet configuré par son propre `LoopRuntime` (un `LoopResult` par sujet sous `outputs/loop-runs/`), gère l'invocation `claude -p`/`codex exec` et son garde-fou — même rôle pour la boucle de surveillance de sujets que `bin/gitlab_loop_runner.py` pour la boucle GitLab |
| `bin/scripts/build_topic_prompt.sh` | Construit le prompt d'un sujet configuré, même rôle que `build_run_prompt.sh` ci-dessus ; conservé comme échappatoire manuelle documentée bien que `topic_monitor_runner.py` ne l'appelle plus                                     |
| `bin/topic_config.py`               | Lit `~/.loop-engineering/topics.json`                                                                                                                                                                                                |
| `bin/topic_seen.py`                 | Fenêtre glissante de dédoublonnage de 7 jours par sujet, pour que les synthèses ne répètent pas la même actualité deux jours de suite                                                                                               |
| `bin/slack_notify.py`               | Publie un message sur le webhook entrant Slack configuré                                                                                                                                                                             |
| `bin/scripts/new_worktree.sh`       | Crée (ou réutilise) un git worktree isolé sur une branche `loop/issue-<iid>`                                                                                                                                                         |
| `bin/scripts/open_merge_request.sh` | Pousse une branche de ticket et ouvre sa MR — refuse tout ce qui n'est pas nommé `loop/issue-*`                                                                                                                                     |
| `bin/scripts/install.sh`            | Installateur en ligne — clone (ou met à jour) ce dépôt, puis exécute `setup.sh` (en lui transmettant `--config-path`/`--topics-config-path`/`--ai-cli-config-path`/`--loops-config-path`/`--state-path`) ; `--upgrade` pour une installation existante, rafraîchit chaque agent launchd actuellement chargé (tableau de bord redémarré, daemon de l'ordonnanceur simplement ré-enregistré) pour qu'ils prennent en compte le nouveau code ; migre aussi sur place un `com.hermes.loop-engineering.plist` obsolète antérieur à l'ordonnanceur unifié et retire l'ancien daemon orphelin `com.hermes.loop-engineering-topic-monitor` s'il est encore installé d'avant l'ordonnanceur unifié ; peut être exécuté via un pipe depuis `curl` sans risque |
| `bin/scripts/setup.sh`              | Installation en une commande : la skill `gitlab-config` + les fichiers de base `projects.json`/`topics.json`/`ai_cli.json`/`loops.json`, plus un `loop_scheduler_state.json` initialisé avec la date du jour pour chaque boucle enregistrée, afin qu'activer l'ordonnanceur juste après l'installation ne déclenche pas d'exécution immédiate |
| `bin/scripts/setup-nginx.sh`        | Reverse proxy nginx local facultatif (`http://loop.x/` → le tableau de bord)                                                                                                                                                     |
| `bin/scripts/uninstall.sh`          | Annule `setup.sh`/`setup-nginx.sh`/`install.sh` ; peut être exécuté via un pipe depuis `curl` sans risque                                                                                                                           |




## Garde-fous de sécurité

Fixes, et ne se relâchent pas avec le temps ni avec les succès répétés (voir [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)) :

- **Ne fusionne jamais une merge request.** Le travail de la boucle s'arrête à « MR ouverte, vérification réussie » — la fusion reste toujours une étape humaine manuelle.
- Chaque modification de code se fait dans son propre git worktree, sur une branche `loop/issue-<iid>`, jamais directement sur la branche cible.
- Une MR n'est ouverte que si les `test_cmd`/`lint_cmd` configurés du projet passent, et le diff ne touche que des fichiers pertinents pour le ticket.
- Pas de shell arbitraire, pas de mise à jour de dépendances, pas de lecture de `.env`/identifiants/clés SSH — uniquement la liste de commandes autorisées de `LOOPX_INSTRUCTIONS.md`.
- Les tickets sont traités un par un, séquentiellement, jamais en parallèle.
- Un échec de vérification sur un même ticket n'est jamais retenté au cours d'une exécution — il est escaladé via un commentaire GitLab. (Avec `verification.mode: gate`, la boucle relance elle-même les vérifications du projet et autorise une nouvelle tentative bornée avec la sortie en échec comme retour ; elle n'ouvre jamais une MR dont les tests/lint échouent et escalade avec le label `loop:needs-human`.)

La boucle Inbox Triage a ses propres garde-fous fixes (voir [`docs/tasks/inbox-triage-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/inbox-triage-loop.md)) :

- **N'envoie jamais de mail.** Aucun module de fournisseur mail ne contient de fonction d'envoi, et le jeton Outlook obtenu est limité sans `Mail.Send` — l'envoi est impossible au niveau du jeton, pas seulement du code.
- **N'archive, ne supprime, ne déplace jamais et ne modifie jamais l'état lu/non lu.** Les seules écritures dans la boîte mail sont la création de libellés/catégories `Loop/*`, leur application et la création de brouillons de réponse laissés dans le dossier Brouillons de la boîte.
- **Seuls des libellés `Loop/*` sont appliqués.** Chaque libellé de catégorie, par défaut ou personnalisé, doit commencer par `Loop/` — `inboxes.json` est sinon rejeté au chargement, si bien qu'un libellé système ajouté à la main comme `TRASH` ou `UNREAD` ne peut jamais être appliqué à de vrais mails.
- **Les corps de message ne sont jamais conservés.** Ils n'existent qu'en mémoire et dans le prompt de l'appel `claude -p` par boîte (plus au plus une nouvelle tentative), qui ne conserve aucune transcription de session — jamais écrits dans `outputs/`, les logs, l'état ou le récapitulatif Slack, qui ne reçoivent que l'expéditeur, l'objet, la catégorie, la brève justification de l'IA et un lien vers le brouillon. Le brouillon de réponse rédigé par l'IA n'est enregistré que dans le dossier Brouillons de la boîte.
- **Inbox Triage requiert la CLI Claude.** Codex donne toujours un shell au modèle et enregistre le prompt sous `~/.codex/sessions/`, donc avec Codex sélectionné chaque boîte échoue d'emblée — avant toute lecture de mail — jusqu'à ce que la CLI d'IA soit repassée sur Claude dans **Settings**.
- **Les refresh tokens ne résident que dans le trousseau macOS**, écrits via `security -i` avec le jeton sur stdin, jamais sur disque en clair ni dans l'argv d'un processus.



## Tests

```bash
python3 -m pytest tests/
```

Chaque script sous `bin/` (Python ou shell, quel que soit son dossier) a un `tests/test_*.py` correspondant, exécuté contre de vrais sous-processus/répertoires temporaires plutôt que des mocks partout où c'est possible (voir `tests/test_new_worktree.py` pour un exemple utilisant un vrai dépôt git local).

`loop eval` exécute les cas d'évaluation scriptés (gratuit) et écrit `outputs/evals/last.json`. `loop eval --golden [--budget-usd N] [--case NAME]` exécute le véritable agent sur des dépôts de test synthétiques (payant : le budget par défaut est de 10 USD et aucun nouveau cas ne démarre une fois dépensé) et écrit `outputs/evals/golden-last.json`. Les deux résultats s'affichent dans Harness → Evals. `loop ledger backfill` reconstruit les enregistrements du registre à partir d'anciens fichiers `result.json`.

## Documentation du projet


| Document                                                               | Utilité                                                                                                |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| [`docs/architecture.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/architecture.md)                         | L'architecture d'exécution V2 : `LoopDefinition`/`LoopState`/`LoopRuntime`, vérification/budget/politique, observabilité et la CLI — la carte d'ensemble, pas la spécification propre à l'une ou l'autre boucle |
| [`TASK.md`](https://github.com/encoreshao/loop-engineering/blob/main/TASK.md)                                                   | Index de chaque tâche planifiée exécutée par ce dépôt, chacune pointant vers sa propre spécification sous `docs/tasks/` |
| [`docs/tasks/gitlab-issue-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/gitlab-issue-loop.md)   | La spécification destinée aux humains de la boucle de tickets GitLab : objectif, périmètre, garde-fous de sécurité |
| [`docs/tasks/topic-monitor-loop.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/tasks/topic-monitor-loop.md) | La spécification destinée aux humains de la boucle de surveillance de sujets : objectif, périmètre, garde-fous de sécurité |
| [`LOOPX_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/LOOPX_INSTRUCTIONS.md)                         | La procédure pas à pas que suit la boucle de tickets GitLab à chaque exécution                         |
| [`TOPIC_MONITOR_INSTRUCTIONS.md`](https://github.com/encoreshao/loop-engineering/blob/main/TOPIC_MONITOR_INSTRUCTIONS.md)       | La procédure pas à pas que suit la boucle de surveillance de sujets à chaque exécution                 |
| [`PROGRESS.md`](https://github.com/encoreshao/loop-engineering/blob/main/PROGRESS.md)                                           | L'état en direct que la boucle lit et met à jour à chaque exécution — résumé de la dernière exécution, escalades ouvertes, décisions prises |
| [`docs/troubleshooting/crash-looping-launchd-agent.md`](https://github.com/encoreshao/loop-engineering/blob/main/docs/troubleshooting/crash-looping-launchd-agent.md) | Diagnostiquer et corriger un agent launchd `com.hermes.loop-engineering*` bloqué dans une boucle de plantages qui inonde son log |




## Licence

[MIT](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE) — voir le fichier [`LICENSE`](https://github.com/encoreshao/loop-engineering/blob/main/LICENSE).
