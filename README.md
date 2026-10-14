# Blue-green database password rotation, with approval buttons

A GitHub Actions workflow, started by hand with **Run workflow**, rotates a database password in three steps. Every rotation starts there: the run page first says what to do by hand, then a named person has to press **Approve** before each step, and after each step the run page says what to do by hand before the next one. Meanwhile a small API behind a load balancer keeps answering requests, and never fails one.

Everything runs on your own machine with docker compose. Only the buttons are GitHub's.

## The idea

The database has two logins with the same rights, `app_blue` and `app_green`. One secret, `api_secrets`, holds a switch, `database_user_active`, naming the login the API uses, and a password slot for each login, `app_blue_password` and `app_green_password`. The login the switch doesn't name is idle. A rotation starts with **Run workflow**: the first job reads the switch and says on the run page which login is idle and whose slot gets the new password. Then:

1. **Give the idle login a new password.** A person puts a new password in the idle login's slot; the workflow sets it in the database. The active login is untouched, so the running API notices nothing.
2. **Move the API over.** A person flips the switch; the workflow restarts the API replicas one at a time. Each restarted replica reads the switch and connects as the new login, while the other replica still answers. Then the workflow checks inside each replica which login it logs in as.
3. **Switch off the previous login's password.** A person empties that login's slot; the workflow waits until nothing is connected as it, then switches its password off. The rotation is logged in a pull request, and the run page says "Rotation complete".

Both logins work until step 3, so there is never a moment where the API can't log in, and until then you can still go back by flipping the switch back and restarting. Next time the roles swap: the idle login is the one that was active before.

Emptying the slot in step 3 is what keeps an old password from coming back: the next rotation refuses an empty slot, and refuses a password the login already has, so someone has to put a fresh one in.

## What's in the repo

| Path | What it is |
|---|---|
| `.github/workflows/rotate-db-login.yaml` | The workflow: a job that names the idle login, then three approval gates, each followed by a job |
| `.github/workflows/check-rotation-log.yaml` | Checks the log on the pull request the workflow opens |
| `rotate/rotate_db_login.py` | The rotation steps, one per job, each refusing when it would be unsafe |
| `rotate/check_api_login.py` | Piped into each restarted replica: which login does the API log in as |
| `rotate/secrets_store.py` | The secrets store: one JSON file, `secrets/api_secrets.json`, a person edits by hand |
| `rotate/rotation_log.py` | The log's rules, and the append-only check |
| `rotate/load.py` | Asks the API who it is five times a second and counts failures |
| `rotations/demo.csv` | The log: one row per rotation, oldest first |
| `docker-compose.yml`, `db/`, `app/`, `Caddyfile` | The database with its two logins, two API replicas, the load balancer |

## How it maps to a real system

| Here | In a real system |
|---|---|
| `secrets/api_secrets.json`, edited by hand | One secret in a cloud secrets manager, holding the switch and a slot per login among the app's other settings; a person edits it in the console |
| `postgres:16` container | A managed Postgres |
| `api_1` and `api_2` behind Caddy | API pods behind a Kubernetes service |
| `docker compose up -d --force-recreate --wait api_1`, then `api_2` | `kubectl rollout restart` |
| `docker compose exec -T api_1 python - < rotate/check_api_login.py` | `kubectl exec -i POD -- python - < check.py` |
| Environments `demo-gate` and `demo` | `staging-gate`/`staging`, `production-gate`/`production` |
| The built-in `GITHUB_TOKEN` opens the log's pull request | A GitHub App token, so that the log's check runs on the pull request |
| Waits 2 minutes for the previous login's connections to end | 15 minutes |
| "Rotation complete" on the run page | Also a chat message |

Left out on purpose: anything else that logs in with the password, such as scheduled jobs (they read the switch afresh on every run) or BI tools.

## Run it on your machine

You need docker with compose, python3, git and the GitHub CLI `gh`. Your user must be allowed to use docker: `docker ps` has to work without `sudo`.

```bash
cd basic-rotation
python3 -m venv .venv && .venv/bin/pip install -r rotate/requirements.txt   # psycopg2, for the rotation script
python3 rotate/secrets_store.py init        # the switch names blue; only blue's slot holds a password
docker compose up -d --build --wait         # database, two replicas, load balancer
curl -s http://127.0.0.1:8080/whoami        # {"replica": "api_1", "login": "app_blue", ...}
python3 rotate/load.py                      # leave it running in its own terminal
```

`python3 rotate/secrets_store.py show` says at any time which login is active, which is idle, and which slots hold a password, without showing any password.

### A rotation by hand, without GitHub

The same steps the workflow runs, so you can watch each one refuse or succeed:

```bash
PY=.venv/bin/python
$PY rotate/rotate_db_login.py find-idle                             # the switch names blue, so green is idle
$PY rotate/rotate_db_login.py set-password --new-login app_green    # refused: app_green_password is empty
python3 rotate/secrets_store.py new-password app_green              # the person's job
$PY rotate/rotate_db_login.py set-password --new-login app_green    # green has its new password; blue is untouched
$PY rotate/rotate_db_login.py check-switch --new-login app_green    # refused: the switch still names blue
python3 rotate/secrets_store.py switch app_green                    # the person's job
$PY rotate/rotate_db_login.py check-switch --new-login app_green    # both logins work
docker compose up -d --force-recreate --no-deps --wait api_1        # load.py: api_1 now answers as app_green
docker compose up -d --force-recreate --no-deps --wait api_2
for R in api_1 api_2; do docker compose exec -T $R python - --new-login app_green < rotate/check_api_login.py; done
$PY rotate/rotate_db_login.py switch-off --new-login app_green      # refused: app_blue_password still holds a password
python3 rotate/secrets_store.py empty app_blue                      # the person's job
$PY rotate/rotate_db_login.py switch-off --new-login app_green      # once nothing is connected as blue: PASSWORD NULL
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

The jobs run docker, so start it from a terminal where `docker ps` works. If you were added to the docker group in this login session, that terminal doesn't have the group yet: either log out and in, or start the runner with `sg docker -c ./run.sh`. Otherwise the restart step fails with "permission denied while trying to connect to the docker API".

Don't install it as a service. `./run.sh` in a terminal is enough, and `./config.sh remove` unregisters it afterwards. On a public repository, register it only for the time you need it, see above.

## The rotation, with the buttons

Three terminals: the runner's `./run.sh`, `python3 rotate/load.py`, and one for the commands below. A browser on the repository's **Actions** tab. The steps below say green is the idle login; read blue for green and the other way round when `show` says blue is idle.

1. **Actions → Rotate database login → Run workflow**, environment `demo`. The first job runs on your machine, and the run page says which login is idle, green the first time, and the command that puts a new password in its slot.
2. Run that command, `python3 rotate/secrets_store.py new-password app_green`, then **approve "the idle login's slot holds its new password"**. The runner gives green its new password, and the run page asks for the switch.
3. `python3 rotate/secrets_store.py switch app_green`, then **approve "database_user_active now names the new login"**. The runner checks the switch, restarts `api_1` and then `api_2`, and checks inside each one that it logs in as green. Watch `load.py`: the login flips, the error count stays at 0. The run page asks you to empty blue's slot.
4. `python3 rotate/secrets_store.py empty app_blue`, then **approve "switch off the previous login's password"**. The runner waits until nothing is connected as blue, which is at once if the old replicas are gone, switches its password off, appends the rotation to `rotations/demo.csv` in a new pull request, and says "Rotation complete" on the run page.
5. Merge the pull request. The log's check runs on main after the merge, and on the pull request itself if `LOG_PR_TOKEN` is set, see below.

To show a refusal: approve the first gate without running the command. The password job fails with `db login rotation: refused: app_green_password is empty: put the new password in it first`. Put a new password in the slot, then **Re-run failed jobs**.

To show something still using the old login: before step 4, run `docker compose exec database psql -U app_blue -d demo -c 'select pg_sleep(60)'` in a fourth terminal. The last job waits until it is done, then carries on. If it took longer than 2 minutes, the job would refuse and say where the connection comes from.

Optional, so that the log's check also runs on the pull request the workflow opens: create a fine-grained personal access token for this repository only, with *Contents* and *Pull requests* set to read and write, and save it with `gh secret set LOG_PR_TOKEN -R OWNER/basic-rotation`. GitHub doesn't start workflows on pull requests opened with the built-in token.

## When a step refuses

It prints `db login rotation: refused:` and the reason, changes nothing unsafe, and the job fails. Fix the reason, then use **Re-run failed jobs** on the run page, never **Re-run all jobs**: that one starts the rotation over, and the first job works out the idle login from the switch again. If you have already flipped the switch, the idle login is now the one the API used before, and the password job refuses, because that login already logs in with the password in its slot. If that has happened, either flip the switch back and start a new run, or carry on by hand with the steps from "A rotation by hand". If the log's pull request couldn't be opened, the rotation itself is done; add the row by hand in a pull request.

## Clean up

```bash
docker compose down -v        # also deletes the database volume
rm -rf secrets .venv
./config.sh remove            # in the runner's folder; it asks for a token from Settings → Actions → Runners
```
