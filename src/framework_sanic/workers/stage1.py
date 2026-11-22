"""Standalone events worker: `python -m framework_sanic.workers.stage1` (topic: events, i.e. the
features' event handlers; with call_logging that is its stage 1)."""

from framework_sanic.cli import main

if __name__ == "__main__":
    main(["worker", "--topics", "events"])
