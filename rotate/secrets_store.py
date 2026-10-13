"""The secrets store, standing in for a cloud secrets manager.

One secret, `api_secrets`, a JSON file that a person edits by hand and that
the API and the rotation only read. It holds the switch and a password slot
for each login:

    {
      "database_user_active": "app_blue",     the switch: the login the API uses
      "app_blue_password": "…",               blue's slot
      "app_green_password": ""                green's slot, empty until a rotation needs it
    }

    python3 rotate/secrets_store.py init                 first time: the switch names blue, only blue's slot is filled
    python3 rotate/secrets_store.py new-password LOGIN   put a fresh password in LOGIN's slot, without showing it
    python3 rotate/secrets_store.py switch LOGIN         flip the switch to LOGIN
    python3 rotate/secrets_store.py empty LOGIN          empty LOGIN's slot, once the rotation no longer needs it
    python3 rotate/secrets_store.py show                 what the switch names, and which slots are filled

The folder is SECRETS_DIR, by default `secrets` in the current directory.
"""

import argparse
import json
import os
import secrets
import string
import sys

LOGINS = ("app_blue", "app_green")
SWITCH = "database_user_active"
SECRETS_DIR = os.environ.get("SECRETS_DIR", "secrets")
SECRET = os.path.join(SECRETS_DIR, "api_secrets.json")
PASSWORD_LENGTH = 48


def slot(login: str) -> str:
    """The key in api_secrets that holds `login`'s password."""
    return f"{login}_password"


def load() -> dict:
    with open(SECRET) as file:
        return json.load(file)


def save(value: dict) -> None:
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(SECRET, "w") as file:
        json.dump(value, file)
        file.write("\n")


def new_password() -> str:
    """Letters and digits only, so it needs no escaping anywhere."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def init() -> str:
    if os.path.exists(SECRET):
        return f"{SECRET} already exists; nothing changed"
    save({SWITCH: LOGINS[0], slot(LOGINS[0]): new_password(), slot(LOGINS[1]): ""})
    return f"created {SECRET}: the switch names {LOGINS[0]}, and only its slot holds a password"


def set_new_password(login: str) -> str:
    save({**load(), slot(login): new_password()})
    return f"saved a new {PASSWORD_LENGTH}-character password in {slot(login)}"


def switch(login: str) -> str:
    save({**load(), SWITCH: login})
    return f"{SWITCH} now names {login}"


def empty(login: str) -> str:
    value = load()
    if value.get(SWITCH) == login:
        return f"{SWITCH} names {login}, so the API needs its password; nothing changed"
    save({**value, slot(login): ""})
    return f"emptied {slot(login)}"


def show() -> str:
    try:
        value = load()
    except FileNotFoundError:
        return f"{SECRET} is missing: run init first"
    active = value.get(SWITCH)
    lines = [f"{SWITCH}: {active!r}"]
    for login in LOGINS:
        state = "holds a password" if value.get(slot(login)) else "is empty"
        role = "active" if login == active else "idle"
        lines.append(f"{slot(login)} ({role}): {state}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The demo's secrets store.")
    parser.add_argument("command", choices=["init", "new-password", "switch", "empty", "show"])
    parser.add_argument("login", nargs="?", choices=LOGINS)
    args = parser.parse_args(argv)
    if args.command in ("new-password", "switch", "empty") and not args.login:
        parser.error(f"{args.command} needs a login: one of {', '.join(LOGINS)}")
    if args.command == "init":
        print(init())
    elif args.command == "new-password":
        print(set_new_password(args.login))
    elif args.command == "switch":
        print(switch(args.login))
    elif args.command == "empty":
        print(empty(args.login))
    else:
        print(show())
    return 0


if __name__ == "__main__":
    sys.exit(main())
