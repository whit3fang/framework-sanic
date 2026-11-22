"""Command line entry point.

Building an integration:
  framework-sanic new NAME [--feature F]...      generate an integration package (integrations/NAME)
  framework-sanic dev [--connector C] [...]      run everything locally (SQLite, Redis, fake Dialpad, UI)
  framework-sanic replay CMD [...]               play Dialpad against a running service (e2e, sms, ...)
  framework-sanic check [PATH]...                the gate: SDK boundary, plugins, wiring, contract, tests

Running the service:
  framework-sanic serve [--host H] [--port P]    HTTP (+ embedded workers if EMBEDDED_WORKERS)
  framework-sanic worker [--topics events,sync] [--shards 0,1]   standalone workers
  framework-sanic export-schemas [--out DIR]     JSON schemas + TS types for the UI api-client
  framework-sanic export-indexes [--out FILE]    Datastore index.yaml (+ TTL commands) for FEATURES
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from framework_sanic.core.config import Settings


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _run_workers(topics: list[str] | None, shards: list[int] | None) -> None:
    from framework_sanic.bootstrap import build_services, build_workers, shutdown, startup

    services = build_services()
    await startup(services, schedules=True)
    runtime = build_workers(services, shards=shards)
    if topics:
        runtime.pools = [p for p in runtime.pools if p.topic in topics]
    await runtime.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runtime.stop()
    await shutdown(services)


def main(argv: list[str] | None = None) -> None:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["replay"]:  # its own argument parser (shared with the standalone driver)
        from framework_sanic.tools.fake_dialpad import main as replay

        replay(argv[1:])
        return
    parser = argparse.ArgumentParser(
        prog="framework-sanic", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="generate an integration package")
    p_new.add_argument("name", help="connector name, e.g. zendesk")
    p_new.add_argument("--feature", action="append", default=[], help="add a feature skeleton (repeatable)")
    p_new.add_argument("--path", help="target directory (default: integrations/NAME in the framework repo)")
    p_new.add_argument("--package", help="Python package name (default: framework_sanic_NAME)")
    p_new.add_argument("--no-install", action="store_true", help="do not add it to the uv workspace")
    p_dev = sub.add_parser("dev", help="run the framework locally with demo data")
    from framework_sanic.tools.dev import add_arguments as dev_arguments

    dev_arguments(p_dev)
    sub.add_parser("replay", help="play Dialpad against a running service (see `replay --help`)")
    p_check = sub.add_parser("check", help="check integrations (SDK boundary, plugins, contract, tests)")
    p_check.add_argument("paths", nargs="*", help="integration directories (default: integrations/*)")
    p_check.add_argument("--no-tests", action="store_true", help="skip running the test suites")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_worker = sub.add_parser("worker")
    p_worker.add_argument("--topics", default="")
    p_worker.add_argument("--shards", default="")
    p_schema = sub.add_parser("export-schemas")
    p_schema.add_argument("--out", default="ui/packages/api-client/src")
    p_idx = sub.add_parser("export-indexes", help="Datastore index.yaml for the enabled FEATURES")
    p_idx.add_argument("--out", default="index.yaml")
    args = parser.parse_args(argv)
    if args.cmd == "new":
        from framework_sanic.tools.scaffold import main as new

        new(args.name, args.path, args.feature, args.package, install=not args.no_install)
        return
    if args.cmd == "dev":
        from framework_sanic.tools.dev import main as dev

        dev(args)
        return
    if args.cmd == "check":
        from framework_sanic.tools.check import main as check

        raise SystemExit(check(args.paths, run_tests=not args.no_tests))

    settings = Settings()
    _setup_logging(settings)
    if args.cmd == "serve":
        from framework_sanic.http.adapters.sanic import serve

        serve(args.host, args.port, settings)
    elif args.cmd == "worker":
        topics = [t for t in args.topics.split(",") if t] or None
        shards = [int(s) for s in args.shards.split(",") if s] or None
        asyncio.run(_run_workers(topics, shards))
    elif args.cmd == "export-schemas":
        from framework_sanic.tools.schemas import export

        export(args.out)
    elif args.cmd == "export-indexes":
        _export_indexes(settings, args.out)


def _export_indexes(settings: Settings, out: str) -> None:
    """Write the composite indexes the enabled features declared, and print the TTL commands.
    Uses the in-memory adapter only to load the features (nothing touches Datastore)."""
    from pathlib import Path

    from framework_sanic.adapters.storage.datastore.repository import index_yaml, ttl_commands
    from framework_sanic.bootstrap import build_services, storage_schema

    services = build_services(settings.model_copy(update={"STORAGE_ADAPTER": "memory"}))
    schema = storage_schema(services)
    Path(out).write_text(index_yaml(schema))
    print(f"wrote {out}: deploy with `gcloud datastore indexes create {out}`")
    print("native TTL (optional; purge_expired also deletes expired records):")
    for cmd in ttl_commands(schema):
        print(f"  {cmd}")


if __name__ == "__main__":
    main()
