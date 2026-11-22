"""Standalone events worker: `python -m framework_sanic.workers.stage1` (topic: events, i.e. the
the listeners' event handlers)."""

from framework_sanic.cli import main

if __name__ == "__main__":
    main(["worker", "--topics", "events"])
