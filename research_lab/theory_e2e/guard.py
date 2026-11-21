"""Refusals that keep the planted run on this laptop (lane GV2-7c, Done (1)): it starts its own backend and worker on scratch
databases, so it must never be pointed at production or run with the systemd research-worker's settings.

Plain standard library: the probe interpreter has no fastapi, and run.py checks this before it imports anything heavy."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

LOCAL_HOSTS = ("localhost", "127.0.0.1")
# research_lab/worker/ops/research-worker.service: EnvironmentFile=%h/.research-worker.env
SYSTEMD_WORKER_ENV_FILE = Path.home() / ".research-worker.env"


class RefusedToStart(Exception):
    """The run would reach production or load the systemd worker's settings; the message names the value."""


def check_local_only(environ: dict, env_file: "str | None" = None) -> None:
    """Raise RefusedToStart when RESEARCH_SERVER_URL in `environ` has a host other than localhost or 127.0.0.1, or when the
    environment file the run would load is the systemd worker's ~/.research-worker.env."""
    url = environ.get("RESEARCH_SERVER_URL")
    if url is not None:
        host = urlsplit(url).hostname
        if host not in LOCAL_HOSTS:
            raise RefusedToStart(
                f"RESEARCH_SERVER_URL={url!r} has host {host!r}; the planted run only talks to its own backend on "
                f"{' or '.join(LOCAL_HOSTS)}")
    if env_file is not None:
        resolved = Path(env_file).expanduser().resolve()
        if resolved == SYSTEMD_WORKER_ENV_FILE.resolve():
            raise RefusedToStart(
                f"environment file {str(resolved)!r} is the systemd research-worker's EnvironmentFile; "
                "the planted run builds its own scratch settings and never loads it")
