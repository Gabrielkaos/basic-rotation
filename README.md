# Blue-green database password rotation, with approval buttons

A GitHub Actions workflow, started by hand with **Run workflow**, rotates a database password in three steps. Before each step a named person has to press **Approve** on the run page, and after each step the run page says what to do by hand before the next one. Meanwhile a small API behind a load balancer keeps answering requests, and never fails one.

Everything runs on your own machine with docker compose. Only the buttons are GitHub's.

## The idea

The database has two logins with the same rights, `app_blue` and `app_green`. A switch, the secret `db_login_active`, names the one the API uses; the other one is idle. A rotation:

1. **Give the idle login a new password.** A person saves a new password in that login's secret; the workflow sets it in the database. The active login is untouched, so the running API notices nothing.
2. **Move the API over.** A person flips the switch; the workflow restarts the API replicas one at a time. Each restarted replica reads the switch and connects as the new login, while the other replica still answers.
3. **Switch off the previous login's password**, once nothing uses it any more. The rotation is then logged in a pull request, and the run page says "Rotation complete".

Both logins work until step 3, so there is never a moment where the API can't log in. Next time the roles swap: the idle login is the one that was active before.

## What's in the repo

| Path | What it is |
|---|---|
| `.github/workflows/rotate-db-login.yaml` | The workflow: three approval gates, three jobs |
| `.github/workflows/check-rotation-log.yaml` | Checks the log on the pull request the workflow opens |
| `rotate/rotate_db_login.py` | The rotation steps, one per job, each refusing when it would be unsafe |
| `rotate/secrets_store.py` | The secrets store: three JSON files a person edits by hand |
| `rotate/rotation_log.py` | The log's rules, and the append-only check |
| `rotate/load.py` | Asks the API who it is five times a second and counts failures |
| `rotations/demo.csv` | The log: one row per rotation, oldest first |
| `docker-compose.yml`, `db/`, `app/`, `Caddyfile` | The database with its two logins, two API replicas, the load balancer |

## How it maps to a real system

| Here | In a real system |
|---|---|
| `secrets/`, three JSON files edited by hand | A cloud secrets manager; a person edits the secret in the console |
| A file's modification time | The secret's version date |
| `postgres:16` container | A managed Postgres |
| `api_1` and `api_2` behind Caddy | API pods behind a Kubernetes service |
| `docker compose up -d --force-recreate --wait api_1`, then `api_2` | `kubectl rollout restart` |
| Environments `demo-gate` and `demo` | `staging-gate`/`staging`, `production-gate`/`production` |
| The built-in `GITHUB_TOKEN` opens the log's pull request | A GitHub App token, so that the log's check runs on the pull request |
| 30 seconds of quiet before a password is switched off | Minutes, when a connection proxy keeps idle connections open |

Left out on purpose: anything else that logs in with the password, such as serverless functions or BI tools, and a chat message at the end.

## Run it on your machine

You need docker with compose, python3, git and the GitHub CLI `gh`. Your user must be allowed to use docker: `docker ps` has to work without `sudo`.

```bash
cd basic-rotation
python3 -m venv .venv && .venv/bin/pip install -r rotate/requirements.txt   # psycopg2, for the rotation script
python3 rotate/secrets_store.py init        # blue gets a password, the switch names blue
docker compose up -d --build --wait         # database, two replicas, load balancer
curl -s http://127.0.0.1:8080/whoami        # {"replica": "api_1", "login": "app_blue", ...}
python3 rotate/load.py                      # leave it running in its own terminal
```

### A rotation by hand, without GitHub

The same steps the workflow runs, so you can watch each one refuse or succeed:

```bash
PY=.venv/bin/python
$PY rotate/rotate_db_login.py set-password                          # refused: green's password predates the switch
python3 rotate/secrets_store.py new-password app_green              # the person's job
$PY rotate/rotate_db_login.py set-password                          # green has its new password; blue is untouched
$PY rotate/rotate_db_login.py check-switch --new-login app_green    # refused: the switch still names blue
python3 rotate/secrets_store.py switch app_green                    # the person's job
$PY rotate/rotate_db_login.py check-switch --new-login app_green    # both logins work
docker compose up -d --force-recreate --no-deps --wait api_1        # load.py: api_1 now answers as app_green
docker compose up -d --force-recreate --no-deps --wait api_2
$PY rotate/rotate_db_login.py check-connections --new-login app_green
$PY rotate/rotate_db_login.py switch-off --new-login app_green      # once nothing uses blue: PASSWORD NULL
$PY rotate/rotate_db_login.py log --new-login app_green --approved-by you --file rotations/demo.csv
```

Run it again with `app_blue` as the new login, and the roles rotate back.

## Put it on GitHub

Approval gates live in GitHub *environments*. On a personal account, a **private** repository only has them with GitHub Pro; on the Free plan only public repositories have them. Check at https://github.com/settings/billing/summary.

```bash
# 1. The repository, private, from this folder.
cd basic-rotation
git init -b main && git add -A && git commit -m "Blue-green database password rotation demo"
gh repo create basic-rotation --private --source=. --remote=origin --push

# 2. The environments: demo-gate with you as a required reviewer, demo without rules.
#    An error about your plan here means the account is on Free: see above.
OWNER=$(gh api user --jq .login)
ME=$(gh api user --jq .id)
gh api -X PUT "repos/$OWNER/basic-rotation/environments/demo-gate" --input - <<EOF
{"reviewers": [{"type": "User", "id": $ME}], "prevent_self_review": false}
EOF
gh api -X PUT "repos/$OWNER/basic-rotation/environments/demo"

# 3. Where this folder is on the machine that will run the self-hosted runner.
gh variable set DEMO_DIR --body "$PWD" -R "$OWNER/basic-rotation"

# 4. Let the workflow open the log's pull request.
gh api -X PUT "repos/$OWNER/basic-rotation/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true

# 5. The person who will press the buttons: invite them, and once they have
#    accepted, make them a reviewer too. With prevent_self_review on, whoever
#    starts a run can't approve it, so it has to be them.
gh api -X PUT "repos/$OWNER/basic-rotation/collaborators/THEIR-LOGIN" -f permission=push
THEM=$(gh api users/THEIR-LOGIN --jq .id)
gh api -X PUT "repos/$OWNER/basic-rotation/environments/demo-gate" --input - <<EOF
{"reviewers": [{"type": "User", "id": $ME}, {"type": "User", "id": $THEM}], "prevent_self_review": true}
EOF
```

### On the Free plan: make the repository public

Step 2 then fails with "ensure the billing plan supports the required reviewers protection rule". Two ways out: GitHub Pro for a month keeps the repository private, or the repository becomes public. Nothing in it is secret, so public is fine, but GitHub's own advice is to almost never put a self-hosted runner on a public repository, because a pull request from a fork can carry code. So, if you go public, also:

```bash
gh repo edit "$OWNER/basic-rotation" --visibility public --accept-visibility-change-consequences
# now repeat step 2, which succeeds
# workflow runs from forks need your approval before they run
gh api -X PUT "repos/$OWNER/basic-rotation/actions/permissions/fork-pr-contributor-approval" -f approval_policy=all_external_contributors
```

Keep the runner registered only while you rehearse or demo, remove it right after, and never approve a workflow run from a fork. The check workflow already runs on GitHub's machines, never on yours.

### The runner

The jobs that touch the database and docker run on your machine, as a self-hosted runner. GitHub shows the exact commands under **Settings → Actions → Runners → New self-hosted runner → Linux**: a download, then a configure step with a token from that page. Press Enter at every question, then start it:

```bash
./run.sh      # leave it running while you demo
```

Don't install it as a service. `./run.sh` in a terminal is enough, and `./config.sh remove` unregisters it afterwards. On a public repository, register it only for the time you need it, see above.

## The rotation, with the buttons

Three terminals: the runner's `./run.sh`, `python3 rotate/load.py`, and one for the commands below. A browser on the repository's **Actions** tab.

1. `python3 rotate/secrets_store.py show` says which login the switch names; the other one is idle. Put a new password in the idle one, the person's first job: `python3 rotate/secrets_store.py new-password app_green` the first time. The steps below say `app_green`; read `app_blue` when blue is the idle one.
2. **Actions → Rotate database login → Run workflow**, environment `demo`.
3. **Approve "Approve the rotation".** The runner gives green its new password. The run page then says green is ready and asks for the switch.
4. `python3 rotate/secrets_store.py switch app_green`, then **approve "db_login_active now names the new login"**. The runner checks the switch, restarts `api_1` and then `api_2`, and waits until something connects as green. Watch `load.py`: the login flips, the error count stays at 0.
5. **Approve "switch off the previous login's password"**. The runner makes sure nothing has used blue for 30 seconds, which is at once if the old replicas are gone, switches its password off, closes any leftover connections, appends the rotation to `rotations/demo.csv` in a new pull request, and says "Rotation complete" on the run page.
6. Merge the pull request. The log's check runs on main after the merge, and on the pull request itself if `LOG_PR_TOKEN` is set, see below.

To show a refusal: skip step 1. The first job fails with `db login rotation: refused: … put a new one in it first`. Do step 1, then **Re-run failed jobs**.

To show something still using the old login: before step 5, open `psql` as `app_blue` in a fourth terminal and run `select pg_sleep(60)`. The last job waits until it is done, then carries on.

Optional, so that the log's check also runs on the pull request the workflow opens: create a fine-grained personal access token for this repository only, with *Contents* and *Pull requests* set to read and write, and save it with `gh secret set LOG_PR_TOKEN -R OWNER/basic-rotation`. GitHub doesn't start workflows on pull requests opened with the built-in token.

## When a step refuses

It prints `db login rotation: refused:` and the reason, changes nothing unsafe, and the job fails. Fix the reason, then use **Re-run failed jobs** on the run page, never **Re-run all jobs**: that one starts the rotation over, works out the idle login from the switch again, and if you have already flipped the switch it tries a rotation in the other direction and refuses, saying the other login's password predates the switch. If that has happened: flip the switch back to the login the API is still using, give the idle login a fresh password, and start a new run. If the log's pull request couldn't be opened, the rotation itself is done; add the row by hand in a pull request.

## Clean up

```bash
docker compose down -v        # also deletes the database volume
rm -rf secrets .venv
./config.sh remove            # in the runner's folder; it asks for a token from Settings → Actions → Runners
```
