"""Probe a container-local HTTP endpoint without proxies or redirects."""

import os
import sys
from http.client import HTTPConnection, HTTPException


def main():
    port, *paths = sys.argv[1:]
    headers = {}
    if os.environ.get("DJANGO_HTTPS") == "1":
        headers["X-Forwarded-Proto"] = "https"

    for path in paths:
        conn = HTTPConnection("127.0.0.1", int(port), timeout=2)
        try:
            conn.request("GET", path, headers=headers)
            status = conn.getresponse().status
        except (OSError, HTTPException) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        if status != 200:
            print(f"{path}: HTTP {status}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
