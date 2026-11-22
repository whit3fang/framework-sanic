"""``framework-sanic dev``: run the framework locally with zero setup.

API server (SQLite + Redis, embedded workers, fake Dialpad), the UI, and demo data:

* Database: SQLite at ``.local/dev.db`` (``--reset`` deletes it).
* Cache: Redis. Reuses one on ``--redis-port``, else starts ``redis-server``, else a
  ``redis:7-alpine`` container; ``--cache memory`` opts out, ``CACHE_URL`` points elsewhere.
* UI: the prebuilt UI shipped in the package is served at ``/app/``; in a framework checkout
  without a build, ``--ui vite`` (the default when pnpm is available) runs the Vite dev server.
* Seeding (fake CRM only): admin connect, CRM connect, a demo contact and one logged call.

Every setting can still be overridden with environment variables (or ``.env``); flags win.
Logs go to ``.local/logs/``. Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from framework_sanic.tools import fake_dialpad

PACKAGE_UI = Path(__file__).resolve().parents[1] / "ui_dist"


def say(msg: str) -> None:
    print(f"\033[1;34m[dev]\033[0m {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\033[1;31m[dev]\033[0m {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def redis_ping(url: str, *, flush: bool = False) -> bool:
    import redis

    try:
        client = redis.Redis.from_url(url, socket_timeout=1)
        client.ping()
        if flush:
            client.flushdb()
        client.close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False


def framework_checkout() -> Path | None:
    """The repository root when running from a framework checkout (for the Vite dev server)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "ui" / "packages" / "ui-shell").is_dir() and (parent / "pyproject.toml").exists():
            return parent
    return None


class Dev:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path.cwd()
        self.local = self.root / ".local"
        self.logs = self.local / "logs"
        self.procs: list[tuple[str, subprocess.Popen[bytes]]] = []
        self.container: str | None = None

    # --- processes --------------------------------------------------------------------------
    def start(self, name: str, log: str, cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        fh = open(self.logs / log, "wb")  # noqa: SIM115 - owned by the child for its lifetime
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, **kw)
        self.procs.append((name, proc))
        say(f"{name} started (pid {proc.pid}, log: .local/logs/{log})")
        return proc

    def stop(self) -> None:
        # Ctrl-C reaches both `uv run` and us; a second interrupt must not cut the cleanup short.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        say("stopping...")
        for _, proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        for _, proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.container:
            subprocess.run(["docker", "stop", self.container], capture_output=True, check=False)

    def wait_http(self, url: str, name: str, log: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=1)  # noqa: S310 - local URL
                return
            except OSError:
                pass
            for pname, proc in self.procs:
                if proc.poll() is not None:
                    self.tail(log)
                    die(f"{pname} exited early")
            time.sleep(0.5)
        self.tail(log)
        die(f"{name} did not become ready at {url}")

    def tail(self, log: str, lines: int = 20) -> None:
        path = self.logs / log
        if path.exists():
            print("\n".join(path.read_text(errors="replace").splitlines()[-lines:]), file=sys.stderr)

    # --- steps ------------------------------------------------------------------------------
    def configure(self) -> dict[str, str]:
        a = self.args
        port = a.port
        env: dict[str, str] = {}

        def default(key: str, value: str) -> None:
            env[key] = os.environ.get(key, value)

        connector = a.connector or os.environ.get("CONNECTOR", "fake")
        env["CONNECTOR"] = connector
        default_features = (
            "fake.calls,fake.sms,fake.notes,fake.dev_harness"
            if connector == "fake"
            else ",".join(f"{connector}.{kind}" for kind in ("calls", "sms", "notes"))
        )
        env["FEATURES"] = a.features or os.environ.get("FEATURES", default_features)
        default("PLATFORM_ADAPTER", "fake")
        default("STORAGE_ADAPTER", "sqlalchemy")
        default("DATABASE_URL", f"sqlite+aiosqlite:///{self.local / 'dev.db'}")
        default("QUEUE_ADAPTER", "memory")
        default("EMBEDDED_WORKERS", "true")
        default("PUBLIC_BASE_URL", f"http://localhost:{port}")
        default("UI_ORIGINS", f"http://localhost:{a.ui_port},http://127.0.0.1:{a.ui_port}")
        default("LOG_LEVEL", "INFO")
        return env

    def preflight(self, env: dict[str, str]) -> None:
        """Check the backends' client libraries before starting anything."""
        from framework_sanic.bootstrap import require_extra
        from framework_sanic.core.domain.errors import ConfigurationError

        needs = []
        if env["STORAGE_ADAPTER"] == "sqlalchemy":
            needs.append(("sql", "STORAGE_ADAPTER=sqlalchemy"))
        if self.args.cache != "memory" and not os.environ.get("CACHE_URL", "").startswith("memory://"):
            needs.append(("redis", "the Redis cache"))
        for extra, what in needs:
            try:
                require_extra(extra, what)
            except ConfigurationError as exc:
                die(
                    f"{exc}\n       (`pip install 'framework-sanic[dev-stack]'` covers `framework-sanic dev`)"
                )

    def ensure_cache(self, env: dict[str, str]) -> None:
        a = self.args
        if a.cache == "memory":
            env["CACHE_URL"] = "memory://"
            say("cache: in-process (memory://)")
            return
        if os.environ.get("CACHE_URL"):
            env["CACHE_URL"] = os.environ["CACHE_URL"]
            say(f"cache: using CACHE_URL={env['CACHE_URL']}")
            return
        url = f"redis://127.0.0.1:{a.redis_port}/0"
        env["CACHE_URL"] = url
        if redis_ping(url):
            say(f"cache: reusing the Redis already on port {a.redis_port}")
            if a.reset and redis_ping(url, flush=True):
                say("cache: flushed (--reset)")
            return
        if shutil.which("redis-server"):
            self.start(
                "Redis",
                "redis.log",
                [
                    "redis-server",
                    "--port",
                    str(a.redis_port),
                    "--bind",
                    "127.0.0.1",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ],
            )
        elif (
            shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
        ):
            say(f"cache: starting a redis:7-alpine container on port {a.redis_port}")
            out = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    f"framework-sanic-dev-redis-{a.redis_port}",
                    "-p",
                    f"127.0.0.1:{a.redis_port}:6379",
                    "redis:7-alpine",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if out.returncode != 0:
                die(f"could not start Redis in Docker: {out.stderr.strip()}")
            self.container = out.stdout.strip()
        else:
            die(
                "Redis is required for local development. Install it (e.g. `sudo apt install redis-server`, "
                "`brew install redis`), enable Docker, point CACHE_URL at a Redis, or use --cache memory"
            )
        for _ in range(60):
            if redis_ping(url):
                return
            time.sleep(0.5)
        die(f"Redis did not answer at {url}")

    def ui_mode(self) -> str:
        mode: str = self.args.ui
        has_build = (PACKAGE_UI / "index.html").exists()
        checkout = framework_checkout()
        pnpm = shutil.which("pnpm") or shutil.which("corepack")
        if mode == "auto":
            mode = "built" if has_build else ("vite" if checkout and pnpm else "none")
            if mode == "none":
                say(
                    "UI: no prebuilt UI in the package and no pnpm: running the API only "
                    "(build it with `pnpm -C ui install && pnpm -C ui build`)"
                )
        if mode == "built" and not has_build:
            die(f"no prebuilt UI at {PACKAGE_UI}: run `pnpm -C ui build` in the framework checkout")
        if mode == "vite" and not (checkout and pnpm):
            die("--ui vite needs a framework checkout (ui/) and pnpm")
        return mode

    def start_vite(self, env: dict[str, str]) -> None:
        checkout = framework_checkout()
        assert checkout is not None
        pnpm = ["pnpm"] if shutil.which("pnpm") else ["corepack", "pnpm"]
        ui = checkout / "ui"
        if not (ui / "node_modules").is_dir():
            say("installing UI dependencies")
            subprocess.run([*pnpm, "install"], cwd=ui, check=True)
        say("generating API client types")
        from framework_sanic.tools.schemas import export

        export(str(ui / "packages" / "api-client" / "src"))
        vite_env = {
            **os.environ,
            **env,
            "API_PROXY_TARGET": f"http://127.0.0.1:{self.args.port}",
            "UI_PORT": str(self.args.ui_port),
        }
        self.start(
            "UI dev server", "ui.log", [*pnpm, "exec", "vite"], cwd=ui / "packages" / "ui-shell", env=vite_env
        )
        self.wait_http(f"http://127.0.0.1:{self.args.ui_port}/app/", "UI dev server", "ui.log")

    def run(self) -> None:
        a = self.args
        self.logs.mkdir(parents=True, exist_ok=True)
        ui = self.ui_mode()
        ports = [a.port] + ([a.ui_port] if ui == "vite" else [])
        for port in ports:
            if port_in_use(port):
                die(f"port {port} is already in use")
        if a.reset:
            db = self.local / "dev.db"
            say(f"resetting {db.relative_to(self.root)}")
            db.unlink(missing_ok=True)
        env = self.configure()
        self.preflight(env)
        self.ensure_cache(env)

        base = f"http://localhost:{a.port}"
        server_env = {**os.environ, **env}
        self.start(
            "API server",
            "server.log",
            [
                sys.executable,
                "-m",
                "framework_sanic.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(a.port),
            ],
            env=server_env,
        )
        self.wait_http(f"http://127.0.0.1:{a.port}/healthz", "API server", "server.log")
        say(
            f"API ready on {base}  (connector: {env['CONNECTOR']}, features: {env['FEATURES']}, "
            f"cache: {env['CACHE_URL']})"
        )

        call_id = str(int(time.time()))
        phone = "+14155550100"
        seeded = False
        if not a.no_seed and env["CONNECTOR"] == "fake" and "fake.dev_harness" in env["FEATURES"]:
            say("seeding: admin connect, CRM connect, demo contact, one logged call")
            os.environ.update({k: v for k, v in env.items() if k.startswith(("WEBHOOK_", "DIALPAD_"))})
            try:
                fake_dialpad.main(["--base", base, "seed", "--call-id", call_id, "--phone", phone])
                seeded = True
            except SystemExit as exc:
                if exc.code not in (0, None):
                    die(f"seeding failed ({exc}); see .local/logs/server.log")
        elif not a.no_seed:
            say(
                f"seeding skipped: it needs the fake CRM (CONNECTOR={env['CONNECTOR']}); "
                "connect your sandbox account from the UI"
            )

        if ui == "vite":
            self.start_vite(env)
        token = fake_dialpad.id_token(fake_dialpad.COMPANY, fake_dialpad.ADMIN, True)
        print()
        say("ready")
        print(f"  API      {base}   (healthz, readyz" + (", /dev/fake-connector/records)" if seeded else ")"))
        if ui != "none":
            ui_base = f"http://localhost:{a.ui_port}/app/" if ui == "vite" else f"{base}/app/"
            extra = f"&dev_call_id={call_id}&dev_phone=%2B{phone[1:]}" if seeded else ""
            print(f"  UI       {ui_base}?id_token={token}{extra}")
            print(
                "           (the id_token is stripped from the URL after load: reopen this link for a new session)"
            )
        print(f"  Replay   framework-sanic replay --base {base} replay-call --shuffle --duplicates 3")
        print("  Logs     .local/logs/")
        print("  Ctrl-C to stop", flush=True)
        while all(p.poll() is None for _, p in self.procs):
            time.sleep(0.5)
        exited = next(name for name, p in self.procs if p.poll() is not None)
        say(f"{exited} exited; see .local/logs/")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=int(os.environ.get("API_PORT", 8000)))
    p.add_argument("--ui", choices=["auto", "built", "vite", "none"], default="auto")
    p.add_argument("--ui-port", type=int, default=int(os.environ.get("UI_PORT", 5173)))
    p.add_argument("--connector", help="connector entry-point name (default: CONNECTOR or fake)")
    p.add_argument("--features", help="comma-separated features (default: FEATURES or the logging features)")
    p.add_argument("--cache", choices=["redis", "memory"], default="redis")
    p.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", 6379)))
    p.add_argument("--reset", action="store_true", help="delete the local SQLite database first")
    p.add_argument("--no-seed", action="store_true", help="start empty")


def main(args: argparse.Namespace) -> None:
    dev = Dev(args)

    def interrupted(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        dev.run()
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()
