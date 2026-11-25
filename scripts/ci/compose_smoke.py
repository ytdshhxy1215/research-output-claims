"""Build or pull, exercise, and clean up one isolated demo Compose project."""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from http_smoke import exercise
from package_demo import IMAGE

SERVICES = {"db", "fixture-source", "migrate", "web", "worker"}
DEMO_PASSWORD = "ci-only-synthetic-demo-password"


def smoke(directory, image, log):
    project = f"roc-ci-{uuid4().hex[:12]}"
    base_file = directory / "compose.yaml"
    build_file = directory / "docker" / "compose.build.yaml"
    if not base_file.is_file() or (not image and not build_file.is_file()):
        raise ValueError("expected compose.yaml and, for a local build, docker/compose.build.yaml")
    # Empty env-file and explicit values keep developer .env and COMPOSE_* out of CI.
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    env.update({
        "APP_IMAGE": image or f"{project}:smoke",
        "WEB_PORT": "0",  # Ask Docker for a free loopback port; see `compose port` below.
        "POSTGRES_DB": "research",
        "POSTGRES_USER": "research",
        "POSTGRES_PASSWORD": "ci-only-db-password",
        "DJANGO_DEBUG": "0",
        "DJANGO_SECRET_KEY": "ci-only-not-for-deployment",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "",
        "DJANGO_HTTPS": "0",
        "FIXTURE_SOURCE_URL": "http://fixture-source:8081",
        "DEMO_PASSWORD": "",  # Never put demo credentials in a long-running service.
    })
    print(f"Smoke project: {project}; compose directory: {directory}", flush=True)
    print(f"Smoke project: {project}", file=log, flush=True)
    with TemporaryDirectory(prefix=project) as tmp:
        env_file = Path(tmp) / "empty.env"
        env_file.touch()
        compose = ("docker", "compose", "--project-name", project, "--env-file", str(env_file),
                   "--file", str(base_file))

        def run(*args, capture=False, stream=None, extra_env=None, timeout=120, check=True):
            print("+", subprocess.list2cmdline(args), flush=True)
            return subprocess.run(
                args, cwd=directory, env=env | (extra_env or {}), check=check, timeout=timeout,
                stdout=subprocess.PIPE if capture else stream,
                stderr=subprocess.STDOUT if stream else None,
                text=True, encoding="utf-8", errors="replace",
            )

        def service_state(service):
            container = run(*compose, "ps", "--all", "--quiet", service, capture=True).stdout.strip()
            if not container or "\n" in container:
                raise RuntimeError(f"expected exactly one container for {service}, got {container!r}")
            return run("docker", "inspect", "--type", "container", "--format",
                       "{{.State.Status}} {{.State.ExitCode}}", container, capture=True).stdout.strip()

        try:
            run(*compose, "config", "--quiet")
            names = run(*compose, "config", "--services", capture=True).stdout.split()
            if set(names) != SERVICES:
                raise RuntimeError(f"unexpected Compose services: {names}")
            if image:
                run(*compose, "pull", "--policy", "missing", timeout=300)
            else:
                build = (*compose, "--file", str(build_file))
                run(*build, "config", "--quiet")
                run(*build, "build", "web", timeout=600)
                run(*compose, "pull", "--policy", "missing", "db", timeout=300)
            run(*compose, "up", "--detach", "--no-build", "--pull", "never", "--wait",
                "--wait-timeout", "120", timeout=150)
            if service_state("migrate") != "exited 0":
                raise RuntimeError("migrate did not exit successfully")
            run(*compose, "run", "--rm", "--no-deps", "-T", "--env", "DEMO_PASSWORD", "web",
                "python", "manage.py", "seed_demo", extra_env={"DEMO_PASSWORD": DEMO_PASSWORD})
            address = run(*compose, "port", "web", "8000", capture=True).stdout.strip()
            base_url = f"http://{address}"
            if urlsplit(base_url).hostname != "127.0.0.1" or not urlsplit(base_url).port:
                raise RuntimeError(f"expected an isolated IPv4 loopback port, got {address!r}")
            exercise(base_url, DEMO_PASSWORD)
            if not service_state("worker").startswith("running "):
                raise RuntimeError("worker is no longer running")
        finally:
            try:
                for args in (("ps", "--all"), ("logs", "--no-color", "--timestamps", "--tail", "150")):
                    print(f"\n--- docker compose {' '.join(args)} ---", file=log, flush=True)
                    try:
                        result = run(*compose, *args, stream=log, timeout=25, check=False)
                        if result.returncode:
                            print(f"Diagnostic command exited {result.returncode}", file=log, flush=True)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        print(f"Could not collect diagnostics: {exc}", file=log, flush=True)
            finally:
                # Never prune or use the repository's default Compose project.
                run(*compose, "down", "--volumes", "--remove-orphans", "--timeout", "10", timeout=60)
                if not image:
                    # Tag unique to this run; there may be no image after a failed build.
                    result = run("docker", "image", "inspect", env["APP_IMAGE"],
                                 capture=True, check=False, timeout=20, stream=log)
                    if result.returncode == 0:
                        run("docker", "image", "rm", env["APP_IMAGE"], timeout=30)
    print(f"Compose smoke passed and project {project} was removed.", flush=True)


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"received signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.cwd(), help="source or unpacked ZIP")
    parser.add_argument("--image", help="pull a GHCR digest instead of building from source")
    parser.add_argument("--log", type=Path, help="write diagnostics here instead of stdout")
    args = parser.parse_args()
    if args.image and not IMAGE.fullmatch(args.image):
        parser.error("--image requires a complete ghcr.io/owner/repo@sha256:<64 hex digits> reference")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with args.log.open("w", encoding="utf-8") if args.log else contextlib.nullcontext(sys.stdout) as log:
            smoke(args.directory.resolve(), args.image, log)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Compose smoke failed: {exc}\n")
    except KeyboardInterrupt as exc:
        parser.exit(130, f"Compose smoke interrupted: {exc}\n")


if __name__ == "__main__":
    main()
