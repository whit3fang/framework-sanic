"""Standalone sync worker: `python -m framework_sanic.workers.stage2` (topic: sync, i.e. features'
follow-up jobs queued by listeners)."""

from framework_sanic.cli import main

if __name__ == "__main__":
    main(["worker", "--topics", "sync"])
