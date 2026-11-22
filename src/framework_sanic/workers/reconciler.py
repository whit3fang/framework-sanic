"""Standalone reconciler worker process: `python -m framework_sanic.workers.reconciler` (topic: control)."""

from framework_sanic.cli import main

if __name__ == "__main__":
    main(["worker", "--topics", "control"])
