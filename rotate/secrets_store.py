"""The secrets store, standing in for a cloud secrets manager.

It is a folder of JSON files that a person edits by hand, and that the API
and the rotation only read. Three secrets:

    db_login_active         {"login": "app_blue"}                    the switch
    db_login_app_blue       {"username": "app_blue", "password": …}  blue's password
    db_login_app_green      {"username": "app_green", "password": …} green's password

A file's modification time is the secret's "last saved" date; the rotation
uses it to refuse a password that was saved before the switch last changed,
because that is the password from the previous rotation.

    python3 rotate/secrets_store.py init                 first time: blue gets a password, the switch names blue
    python3 rotate/secrets_store.py new-password LOGIN   put a fresh password in LOGIN's secret, without showing it
    python3 rotate/secrets_store.py switch LOGIN         flip the switch to LOGIN
    python3 rotate/secrets_store.py show                 what the switch names, and when each secret was saved

The folder is SECRETS_DIR, by default `secrets` in the current directory.
"""

import argparse
import json
import os
import secrets
import string
import sys
import time

LOGINS = ("app_blue", "app_green")
SWITCH_SECRET = "db_login_active"
SECRETS_DIR = os.environ.get("SECRETS_DIR", "secrets")
PASSWORD_LENGTH = 48


def path(name: str) -> str:
    return os.path.join(SECRETS_DIR, f"{name}.json")


def save(name: str, value: dict) -> None:
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(path(name), "w") as file:
        json.dump(value, file)
        file.write("\n")


def new_password() -> str:
    """Letters and digits only, so it needs no escaping anywhere."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def init() -> str:
    if os.path.exists(path(SWITCH_SECRET)):
        return f"{SECRETS_DIR} already exists; nothing changed"
    for login in LOGINS:
        save(f"db_login_{login}", {"username": login, "password": new_password()})
    # The switch is saved last, so both passwords count as older than it: the
    # first rotation then insists on a freshly typed password for the idle login.
    time.sleep(0.01)
    save(SWITCH_SECRET, {"login": LOGINS[0]})
    return (
        f"created {SECRETS_DIR}: the switch names {LOGINS[0]}, each login has a "
        "password in its own secret"
    )


def set_new_password(login: str) -> str:
    save(f"db_login_{login}", {"username": login, "password": new_password()})
    return f"saved a new {PASSWORD_LENGTH}-character password in db_login_{login}"


def switch(login: str) -> str:
    save(SWITCH_SECRET, {"login": login})
    return f"{SWITCH_SECRET} now names {login}"


def show() -> str:
    lines = []
    for name in (SWITCH_SECRET, *(f"db_login_{login}" for login in LOGINS)):
        try:
            with open(path(name)) as file:
                value = json.load(file)
            saved = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path(name))))
        except FileNotFoundError:
            lines.append(f"{name}: missing")
            continue
        if name == SWITCH_SECRET:
            lines.append(f"{name}: names {value.get('login')!r}, saved {saved}")
        else:
            state = "has a password" if value.get("password") else "has NO password"
            lines.append(f"{name}: username {value.get('username')!r}, {state}, saved {saved}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The demo's secrets store.")
    parser.add_argument("command", choices=["init", "new-password", "switch", "show"])
    parser.add_argument("login", nargs="?", choices=LOGINS)
    args = parser.parse_args(argv)
    if args.command in ("new-password", "switch") and not args.login:
        parser.error(f"{args.command} needs a login: one of {', '.join(LOGINS)}")
    if args.command == "init":
        print(init())
    elif args.command == "new-password":
        print(set_new_password(args.login))
    elif args.command == "switch":
        print(switch(args.login))
    else:
        print(show())
    return 0


if __name__ == "__main__":
    sys.exit(main())
