"""Blue-green database password rotation, one step at a time.

    python rotate/rotate_db_login.py STEP [--new-login LOGIN]

Run by .github/workflows/rotate-db-login.yaml, one step per job, each after a
person has approved it. Every step reads api_secrets the way the API does:
the switch `database_user_active` names the login the API uses, and each
login's password is in its own slot, `<login>_password`.

- `set-password` gives the idle login, the one the switch doesn't name, the
  new password a person put in its slot, so the login works before anything
  switches to it. The active login is left alone, so the running API notices
  nothing.
- `check-switch` makes sure a person has flipped the switch to that login, and
  that both logins still work, before anything restarts.
- `check-connections` waits until the restarted API replicas have connected
  as the new login.
- `switch-off` switches off the previous login's password, once a person has
  emptied its slot and nothing is connected as it any more.
- `log` adds the rotation to the log, with who approved it.

Only a hash of a password reaches the database, so a database log that records
every ALTER statement never sees one, and no password is ever printed.

Settings, as environment variables:

    SECRETS_DIR         the secrets store folder (default: secrets)
    ADMIN_DATABASE_URI  how to connect as the database admin
                        (default: the docker-compose database on port 5499)
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
SWITCH = "database_user_active"
SECRETS_DIR = os.environ.get("SECRETS_DIR", "secrets")
ADMIN_DATABASE_URI = os.environ.get(
    "ADMIN_DATABASE_URI", "postgresql://postgres:admin-not-secret@127.0.0.1:5499/demo"
)

# Refuses a placeholder or a hand-typed word; a generated password is longer.
MIN_PASSWORD_LENGTH = 32

# How long check-connections waits for the restarted replicas to connect, how
# long switch-off waits for the previous login's last connection to end, and
# how often each wait looks again. The real system waits 5 and 15 minutes.
CONNECT_WAIT_SECONDS = 2 * 60
SWITCH_OFF_WAIT_SECONDS = 2 * 60
CHECK_EVERY_SECONDS = 5

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


def slot(login: str) -> str:
    """The key in api_secrets that holds `login`'s password."""
    return f"{login}_password"


# --- The secrets store: one JSON file, see secrets_store.py.


def read_api_secrets():
    """The login the switch names, and each login's slot (None when empty).

    Refuses where the API would refuse to start: an unknown login, or an empty
    slot for the login the switch names.
    """
    path = os.path.join(SECRETS_DIR, "api_secrets.json")
    try:
        with open(path) as file:
            secret = json.load(file)
    except FileNotFoundError:
        raise RotationRefused(f"{path} doesn't exist: run secrets_store.py init first")
    except json.JSONDecodeError as error:
        raise RotationRefused(f"{path} isn't valid JSON: {error}")
    active = secret.get(SWITCH)
    if active not in LOGINS:
        raise RotationRefused(f"{SWITCH} is {active!r}, not one of {', '.join(LOGINS)}")
    passwords = {login: secret.get(slot(login)) or None for login in LOGINS}
    if not passwords[active]:
        raise RotationRefused(f"{SWITCH} names {active} but {slot(active)} is empty")
    return active, passwords


# --- The steps. Each takes what it needs as arguments, so it can be tested
# without a database, and returns one line saying what happened.


def idle_login(active: str) -> str:
    return next(login for login in LOGINS if login != active)


def set_password(admin, active: str, passwords: dict, logs_in) -> str:
    """Give the idle login the new password from its slot.

    `admin` is an autocommit connection as the database admin, `active` the
    login the switch names, `passwords` each login's slot (None when empty),
    and `logs_in(login, password)` whether that login connects with that
    password.
    """
    idle = idle_login(active)
    new = passwords[idle]
    if not logs_in(active, passwords[active]):
        raise RotationRefused(f"{slot(active)} doesn't hold {active}'s working password")
    if not new:
        raise RotationRefused(f"{slot(idle)} is empty: put the new password in it first")
    if len(new) < MIN_PASSWORD_LENGTH:
        raise RotationRefused(
            f"the password in {slot(idle)} is shorter than {MIN_PASSWORD_LENGTH} characters"
        )
    if new == passwords[active]:
        raise RotationRefused(f"{idle}'s new password is the same as {active}'s")
    # A password the login already has isn't new: most likely nobody replaced
    # what the last rotation left in the slot.
    if logs_in(idle, new):
        raise RotationRefused(
            f"{idle} already logs in with the password in {slot(idle)}: put a new one in it"
        )
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
        raise RotationRefused(f"{SWITCH} names {active}, not {new_login}: flip it before approving")
    if not logs_in(new_login, passwords[new_login]):
        raise RotationRefused(f"{new_login} can't log in with the password in {slot(new_login)}")
    if not passwords[previous] or not logs_in(previous, passwords[previous]):
        raise RotationRefused(
            f"{previous} can't log in with the password in {slot(previous)}, so replicas "
            "still on it couldn't reconnect while the API restarts"
        )
    return f"{SWITCH} names {new_login}, and both logins work"


def check_connections(admin, new_login: str, sleep=time.sleep) -> str:
    """Wait until something has connected as `new_login`: the restarted replicas."""
    _wait_until(
        lambda: _connections(admin, new_login),
        CONNECT_WAIT_SECONDS,
        lambda: f"nothing has connected as {new_login} after {CONNECT_WAIT_SECONDS} seconds",
        sleep,
    )
    previous = idle_login(new_login)
    return (
        f"{new_login} has {_connections(admin, new_login)} connection(s); "
        f"{previous} still has {_connections(admin, previous)}"
    )


def switch_off(admin, active: str, new_login: str, passwords: dict, logs_in, sleep=time.sleep) -> str:
    """Switch off the previous login's password once nothing uses it.

    A person empties the previous login's slot first. Then the next rotation
    refuses until someone puts a new password in it, so an old password can't
    come back, and a retired password doesn't stay in api_secrets.
    """
    previous = idle_login(new_login)
    if active != new_login:
        raise RotationRefused(
            f"{SWITCH} names {active}, not {new_login}, so no password is switched off"
        )
    if not logs_in(new_login, passwords[new_login]):
        raise RotationRefused(f"{new_login} can't log in, so {previous}'s password is left alone")
    if passwords[previous]:
        raise RotationRefused(f"{slot(previous)} still holds a password: empty it first")
    _wait_until(
        lambda: not _connections(admin, previous),
        SWITCH_OFF_WAIT_SECONDS,
        lambda: (
            f"{previous} is still connected after {SWITCH_OFF_WAIT_SECONDS} seconds, from "
            f"{_who(admin, previous)}: find what uses it, then re-run this job"
        ),
        sleep,
    )
    _switch_off_password(admin, previous)
    return f"{previous}'s password is switched off; only {new_login} logs in now"


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


def _who(admin, login: str) -> str:
    """Where `login`'s connections come from, to help find what still uses it."""
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(host(client_addr), 'a local socket') || "
            "coalesce(' (' || nullif(application_name, '') || ')', '') "
            "FROM pg_stat_activity WHERE usename = %s",
            [login],
        )
        return ", ".join(row[0] for row in cursor.fetchall()) or "nowhere any more"


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


def _wait_until(done, seconds: int, refusal, sleep) -> None:
    """Wait until `done()`, or refuse with `refusal()` after `seconds`."""
    waited = 0
    while not done():
        if waited >= seconds:
            raise RotationRefused(refusal())
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
    active, passwords = read_api_secrets()
    if args.step == "check-switch":
        return check_switch(active, args.new_login, passwords, _logs_in)
    admin = _admin()
    try:
        if args.step == "switch-off":
            return switch_off(admin, active, args.new_login, passwords, _logs_in)
        outcome = set_password(admin, active, passwords, _logs_in)
    finally:
        admin.close()
    _hand_on("new_login", idle_login(active))
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
