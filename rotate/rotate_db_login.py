"""Blue-green database password rotation, one step at a time.

    python rotate/rotate_db_login.py STEP [--new-login LOGIN]

Run by .github/workflows/rotate-db-login.yaml, one step per job, each after a
person has approved it:

- `set-password` gives the idle login, the one the switch db_login_active
  doesn't name, the new password a person put in that login's own secret, so
  the login works before anything switches to it. The active login is left
  alone, so the running API notices nothing.
- `check-switch` makes sure a person has flipped the switch to that login, and
  that both logins still work, before anything restarts.
- `check-connections` waits until something has connected as the new login:
  the restarted API replicas.
- `switch-off` switches off the previous login's password once nothing uses it.
- `log` adds the rotation to the log, with who approved it.

Only a hash of a password reaches the database, so a database log that records
every ALTER statement never sees one, and no password is ever printed.

Settings, as environment variables:

    SECRETS_DIR         the secrets store folder (default: secrets)
    ADMIN_DATABASE_URI  how to connect as the database admin
                        (default: the docker-compose database on port 5499)
    QUIET_SECONDS       how long the previous login must have been unused
                        before its password is switched off (default: 30)
"""

import argparse
import datetime
import json
import os
import sys
import time
from urllib.parse import urlsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rotation_log import NAMES, Rotation, RotationLogError, append_rotation  # noqa: E402

LOGINS = ("app_blue", "app_green")
SWITCH_SECRET = "db_login_active"
SECRETS_DIR = os.environ.get("SECRETS_DIR", "secrets")
ADMIN_DATABASE_URI = os.environ.get(
    "ADMIN_DATABASE_URI", "postgresql://postgres:admin-not-secret@127.0.0.1:5499/demo"
)

# Refuses a placeholder or a hand-typed word; a generated password is longer.
MIN_PASSWORD_LENGTH = 32

# How long check-connections waits for the restarted replicas to connect, and
# how often each wait looks again.
CONNECT_WAIT_SECONDS = 2 * 60
CHECK_EVERY_SECONDS = 5

# How long the previous login must have done nothing before its password is
# switched off, and how long switch-off waits for that to happen. The real
# system waits 5 and 15 minutes, because a connection proxy in front of its
# database keeps idle connections open for a long time.
QUIET_SECONDS = int(os.environ.get("QUIET_SECONDS", "30"))
SWITCH_OFF_WAIT_SECONDS = 2 * 60

# Each step, and the options it needs.
STEPS = {
    "set-password": [],
    "check-switch": ["new_login"],
    "check-connections": ["new_login"],
    "switch-off": ["new_login"],
    "log": ["new_login", "approved_by", "file"],
}


class RotationRefused(Exception):
    """Carrying out the step now would be unsafe, so the script fails instead."""


# --- The secrets store: a folder of JSON files, see secrets_store.py.


def _secret_path(name: str) -> str:
    return os.path.join(SECRETS_DIR, f"{name}.json")


def read_secret(name: str):
    try:
        with open(_secret_path(name)) as file:
            return json.load(file)
    except FileNotFoundError:
        raise RotationRefused(f"the secret {name} doesn't exist in {SECRETS_DIR}")
    except json.JSONDecodeError as error:
        raise RotationRefused(f"the secret {name} isn't valid JSON: {error}")


def read_switch() -> str:
    """The login the switch names."""
    switch = read_secret(SWITCH_SECRET)
    if not isinstance(switch, dict) or switch.get("login") not in LOGINS:
        raise RotationRefused(
            f'{SWITCH_SECRET} must be {{"login": ...}} naming one of {", ".join(LOGINS)}'
        )
    return switch["login"]


def read_password(login: str) -> str:
    """`login`'s password, from its own secret."""
    name = f"db_login_{login}"
    secret = read_secret(name)
    if (
        not isinstance(secret, dict)
        or secret.get("username") != login
        or not secret.get("password")
    ):
        raise RotationRefused(f"{name} must hold username {login!r} and its password")
    return secret["password"]


def saved_at(name: str) -> float:
    """When the secret was last saved: its file's modification time."""
    return os.path.getmtime(_secret_path(name))


# --- The steps. Each takes what it needs as arguments, so it can be tested
# without a database, and returns one line saying what happened.


def idle_login(active: str) -> str:
    return next(login for login in LOGINS if login != active)


def set_password(admin, active: str, passwords: dict, typed_after_switch: bool, logs_in) -> str:
    """Give the idle login the new password from its own secret.

    `admin` is an autocommit connection as the database admin, `active` the
    login the switch names, `passwords` each login's password from its own
    secret, `typed_after_switch` whether the idle login's secret was saved
    after the switch last changed, and `logs_in(login, password)` whether that
    login connects with that password.
    """
    idle = idle_login(active)
    new = passwords[idle]
    if not logs_in(active, passwords[active]):
        raise RotationRefused(f"db_login_{active} doesn't hold {active}'s working password")
    if not typed_after_switch:
        raise RotationRefused(
            f"db_login_{idle} hasn't been saved since {SWITCH_SECRET} last changed, so it "
            "still holds an old password: put a new one in it first"
        )
    if len(new) < MIN_PASSWORD_LENGTH:
        raise RotationRefused(
            f"the password in db_login_{idle} is shorter than {MIN_PASSWORD_LENGTH} characters"
        )
    if new == passwords[active]:
        raise RotationRefused(f"{idle}'s new password is the same as {active}'s")
    if logs_in(idle, new):
        return f"{idle} already had its new password"
    if _connections(admin, idle):
        raise RotationRefused(
            f"{idle} has open connections, so it isn't idle; its password is left alone"
        )
    _set_password(admin, idle, new)
    if not logs_in(idle, new):
        raise RotationRefused(f"{idle} still can't log in with its new password")
    return f"{idle} has its new password; {active} is untouched"


def check_switch(active: str, new_login: str, passwords: dict, logs_in) -> str:
    """Make sure the switch names `new_login`, and that both logins work."""
    previous = idle_login(new_login)
    if active != new_login:
        raise RotationRefused(
            f"{SWITCH_SECRET} names {active}, not {new_login}: flip it before approving"
        )
    if not logs_in(new_login, passwords[new_login]):
        raise RotationRefused(
            f"{new_login} can't log in with the password in db_login_{new_login}"
        )
    if not logs_in(previous, passwords[previous]):
        raise RotationRefused(
            f"{previous} can't log in any more, so running copies couldn't reconnect"
        )
    return f"{SWITCH_SECRET} names {new_login}, and both logins work"


def check_connections(admin, new_login: str, sleep=time.sleep) -> str:
    """Wait until something has connected as `new_login`: the restarted replicas."""
    _wait_until(
        lambda: _connections(admin, new_login),
        CONNECT_WAIT_SECONDS,
        f"nothing has connected as {new_login} after {CONNECT_WAIT_SECONDS} seconds",
        sleep,
    )
    previous = idle_login(new_login)
    return (
        f"{new_login} has {_connections(admin, new_login)} connection(s); "
        f"{previous} still has {_connections(admin, previous)}"
    )


def switch_off(admin, active: str, new_login: str, passwords: dict, logs_in, sleep=time.sleep) -> str:
    """Switch off the previous login's password once nothing uses it.

    The previous login counts as unused once none of its connections has done
    anything for QUIET_SECONDS. Leftover idle connections are then closed.
    """
    previous = idle_login(new_login)
    if active != new_login:
        raise RotationRefused(
            f"{SWITCH_SECRET} names {active}, not {new_login}, so no password is switched off"
        )
    if not logs_in(new_login, passwords[new_login]):
        raise RotationRefused(f"{new_login} can't log in, so {previous}'s password is left alone")
    _wait_until(
        lambda: not _in_use(admin, previous),
        SWITCH_OFF_WAIT_SECONDS,
        f"{previous} is still in use after {SWITCH_OFF_WAIT_SECONDS} seconds: find what "
        "uses it, then re-run this job",
        sleep,
    )
    _switch_off_password(admin, previous)
    closed = _close_idle_connections(admin, previous)
    if logs_in(previous, passwords[previous]):
        raise RotationRefused(f"{previous} can still log in with its old password")
    return (
        f"{previous}'s password is switched off and its {closed} leftover connection(s) "
        f"closed; only {new_login} logs in now"
    )


def log_rotation(path: str, new_login: str, approved_by: str) -> str:
    """Add the rotation to `new_login` at the end of the log."""
    names = {login: name for name, login in NAMES.items()}
    rotation = Rotation(
        datetime.date.today().isoformat(),
        names[idle_login(new_login)],
        names[new_login],
        os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "local",
        approved_by,
        _run_url(),
    )
    try:
        append_rotation(path, rotation)
    except RotationLogError as error:
        raise RotationRefused(f"{path}: {error}") from error
    return (
        f"logged the rotation from {rotation.active_user_before_rotation} to "
        f"{rotation.active_user_after_rotation} in {path}"
    )


# --- Database helpers.


def _connections(admin, login: str) -> int:
    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = %s", [login])
        return cursor.fetchone()[0]


def _in_use(admin, login: str) -> int:
    """How many of `login`'s connections did something in the last QUIET_SECONDS."""
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND ("
            "state IS DISTINCT FROM 'idle' OR "
            "state_change > now() - make_interval(secs => %s))",
            [login, QUIET_SECONDS],
        )
        return cursor.fetchone()[0]


def _set_password(admin, login: str, password: str) -> None:
    # The hash is computed here, so the statement the database sees and may
    # log never contains the password itself.
    hashed = encrypt_password(password, login, admin, "scram-sha-256")
    with admin.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(login)), [hashed]
        )


def _switch_off_password(admin, login: str) -> None:
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD NULL").format(sql.Identifier(login)))


def _close_idle_connections(admin, login: str) -> int:
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE usename = %s AND state = 'idle'",
            [login],
        )
        return cursor.fetchone()[0]


def _logs_in(login: str, password: str) -> bool:
    """Whether `login` connects with `password`, to the same database as the admin."""
    uri = urlsplit(ADMIN_DATABASE_URI)
    try:
        psycopg2.connect(
            host=uri.hostname,
            port=uri.port or 5432,
            dbname=uri.path.lstrip("/"),
            user=login,
            password=password,
            connect_timeout=5,
        ).close()
    except psycopg2.OperationalError:
        return False
    return True


def _admin():
    try:
        admin = psycopg2.connect(ADMIN_DATABASE_URI, connect_timeout=5)
    except psycopg2.OperationalError as error:
        raise RotationRefused(f"can't connect as the database admin: {error}".strip())
    admin.autocommit = True
    return admin


def _hand_on(name: str, value: str) -> None:
    """Pass a value on to the workflow's later jobs, as a GitHub Actions output."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as file:
            file.write(f"{name}={value}\n")


def _wait_until(done, seconds: int, refusal: str, sleep) -> None:
    waited = 0
    while not done():
        if waited >= seconds:
            raise RotationRefused(refusal)
        sleep(CHECK_EVERY_SECONDS)
        waited += CHECK_EVERY_SECONDS


def _run_url() -> str:
    """This GitHub Actions run's page, or `local` outside GitHub."""
    if not os.environ.get("GITHUB_RUN_ID"):
        return "local"
    return "{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}".format(
        **os.environ
    )


def _run(args) -> str:
    if args.step == "log":
        return log_rotation(args.file, args.new_login, args.approved_by)
    if args.step == "check-connections":
        admin = _admin()
        try:
            return check_connections(admin, args.new_login)
        finally:
            admin.close()
    active = read_switch()
    passwords = {login: read_password(login) for login in LOGINS}
    if args.step == "check-switch":
        return check_switch(active, args.new_login, passwords, _logs_in)
    if args.step == "switch-off":
        admin = _admin()
        try:
            return switch_off(admin, active, args.new_login, passwords, _logs_in)
        finally:
            admin.close()
    idle = idle_login(active)
    typed_after_switch = saved_at(f"db_login_{idle}") > saved_at(SWITCH_SECRET)
    admin = _admin()
    try:
        outcome = set_password(admin, active, passwords, typed_after_switch, _logs_in)
    finally:
        admin.close()
    _hand_on("new_login", idle)
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Blue-green database password rotation steps.")
    parser.add_argument("step", choices=list(STEPS))
    parser.add_argument(
        "--new-login",
        choices=LOGINS,
        help="for the steps after set-password: the login set-password prepared",
    )
    parser.add_argument("--approved-by", help="who approved the run, GitHub logins separated by ';'")
    parser.add_argument("--file", help="for log: the rotation log")
    args = parser.parse_args(argv)
    for option in STEPS[args.step]:
        if not getattr(args, option):
            parser.error(f"{args.step} needs --{option.replace('_', '-')}")

    try:
        outcome = _run(args)
    except RotationRefused as error:
        print(f"db login rotation: refused: {error}")
        return 1
    print(f"db login rotation: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
