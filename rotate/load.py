"""Keeps asking the API who it is, through the load balancer, and prints one
line a second: which login each replica answered as, and how many requests
have failed since the start. Leave it running in a terminal during a
rotation: the login flips from app_blue to app_green, and the error count
stays at 0.

    python3 rotate/load.py [URL]        default URL: http://127.0.0.1:8080/whoami
"""

import json
import sys
import time
import urllib.error
import urllib.request


def ask(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/whoami"
    ok = errors = 0
    last_error = ""
    seen = {}
    next_line = time.monotonic() + 1
    print(f"asking {url} five times a second; Ctrl-C stops", flush=True)
    while True:
        try:
            answer = ask(url)
            ok += 1
            seen[answer["replica"]] = answer["login"]
        except Exception as error:  # a failed request is the thing being counted
            errors += 1
            last_error = str(error)
        if time.monotonic() >= next_line:
            replicas = "  ".join(f"{replica}={seen[replica]}" for replica in sorted(seen))
            line = f"{time.strftime('%H:%M:%S')}  {replicas or 'no answer'}  ok={ok}  errors={errors}"
            if errors and last_error:
                line += f"  last error: {last_error}"
            print(line, flush=True)
            seen = {}
            next_line += 1
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
