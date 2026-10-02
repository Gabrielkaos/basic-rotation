"""A small API, standing in for the real one.

When it starts, it reads which database login to use, opens a pool of
connections as that login, and keeps them until it is restarted. Changing the
switch does nothing to a running copy: only a restart makes it read the switch
again. That is why the rotation workflow restarts the replicas one at a time,
and that restart is the moment the switch happens.

    GET /whoami   which replica answered, and the login it is connected as
    GET /health   200 once a connection from the pool works

Settings, as environment variables: REPLICA (a name for this copy),
SECRETS_DIR (the secrets store folder), DATABASE_HOST, DATABASE_PORT,
DATABASE_NAME and PORT (which port to listen on, 8000 by default).
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

REPLICA = os.environ.get("REPLICA", "api")
SECRETS_DIR = os.environ.get("SECRETS_DIR", "secrets")
DATABASE_HOST = os.environ.get("DATABASE_HOST", "127.0.0.1")
DATABASE_PORT = int(os.environ.get("DATABASE_PORT", "5432"))
DATABASE_NAME = os.environ.get("DATABASE_NAME", "demo")
PORT = int(os.environ.get("PORT", "8000"))


def read_secret(name: str) -> dict:
    with open(os.path.join(SECRETS_DIR, f"{name}.json")) as file:
        return json.load(file)


def credentials():
    """The active login and its password.

    Read the way the real app reads its secrets store: the switch names the
    login, and that login's own secret holds the password.
    """
    login = read_secret("db_login_active")["login"]
    secret = read_secret(f"db_login_{login}")
    if secret.get("username") != login or not secret.get("password"):
        raise SystemExit(f"{REPLICA}: db_login_{login} must hold username {login!r} and its password")
    return login, secret["password"]


def open_pool():
    """A pool of connections as the active login, once the database is up."""
    login, password = credentials()
    for attempt in range(30):
        try:
            pool = ThreadedConnectionPool(
                1,
                4,
                host=DATABASE_HOST,
                port=DATABASE_PORT,
                dbname=DATABASE_NAME,
                user=login,
                password=password,
                connect_timeout=3,
            )
        except psycopg2.OperationalError as error:
            last_error = error
            time.sleep(1)
        else:
            print(f"{REPLICA}: connected as {login}", flush=True)
            return login, pool
    raise SystemExit(f"{REPLICA}: can't connect as {login}: {last_error}")


LOGIN, POOL = open_pool()
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def ask_database():
    """Who the database says we are, and the greeting, from a pooled connection."""
    connection = POOL.getconn()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, (SELECT text FROM greeting LIMIT 1)")
            user, greeting = cursor.fetchone()
        connection.rollback()  # end the transaction, so the connection shows as idle
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        POOL.putconn(connection, close=True)  # a broken connection is replaced next time
        raise
    POOL.putconn(connection)
    return user, greeting


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            try:
                ask_database()
            except psycopg2.Error as error:
                return self._send(503, {"status": "database unreachable", "error": str(error).strip()})
            return self._send(200, {"status": "ok"})
        if self.path == "/whoami":
            try:
                user, greeting = ask_database()
            except psycopg2.Error as error:
                return self._send(503, {"replica": REPLICA, "error": str(error).strip()})
            return self._send(
                200,
                {"replica": REPLICA, "login": user, "greeting": greeting, "started_at": STARTED_AT},
            )
        return self._send(404, {"error": "try /whoami or /health"})

    def _send(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # keep the container log quiet
        pass


if __name__ == "__main__":
    print(f"{REPLICA}: listening on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
