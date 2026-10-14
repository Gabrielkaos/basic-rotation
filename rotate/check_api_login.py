"""Check, inside an API replica, which database login the API logs in as.

After the rotation workflow has restarted the replicas, it pipes this file into
each one, the way a real system does with `kubectl exec`:

    docker compose exec -T api_1 python - --new-login LOGIN < rotate/check_api_login.py

The replica's image only has the API's own app.py, so this file uses that: a
fresh process reads the switch and connects the way the API did when the
replica started, without waiting for a request that needs the database. No
password is ever printed.
"""

import argparse
import sys

LOGINS = ("app_blue", "app_green")


class WrongLogin(Exception):
    """The replica doesn't log in as the login the rotation moved to."""


def check(new_login: str, configured: str, logged_in_as: str) -> str:
    """Compare the login the API chose and the one the database saw."""
    if configured != new_login:
        raise WrongLogin(f"this replica's API uses {configured}, not {new_login}")
    if logged_in_as != new_login:
        raise WrongLogin(f"this replica logged in as {logged_in_as}, not {new_login}")
    return f"this replica logs in as {new_login}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check which login this API replica logs in as.")
    parser.add_argument("--new-login", required=True, choices=LOGINS)
    args = parser.parse_args(argv)
    try:
        # Importing the API reads the switch and opens its pool, as at start.
        import app

        logged_in_as, _ = app.ask_database()
    except BaseException as error:  # the API stops with SystemExit when it can't log in
        # Only the kind of error: its message could repeat connection details.
        print(f"db login check: refused: this replica can't log in: {type(error).__name__}")
        return 1
    try:
        outcome = check(args.new_login, app.LOGIN, logged_in_as)
    except WrongLogin as error:
        print(f"db login check: refused: {error}")
        return 1
    print(f"db login check: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
