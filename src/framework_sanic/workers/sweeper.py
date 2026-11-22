"""Weekly staggered sweep + change-log polling (normally driven by the scheduler).

`python -m framework_sanic.workers.sweeper` runs one sweep tick, suitable for Cloud Scheduler / cron.
"""

import asyncio

from framework_sanic.bootstrap import build_services, shutdown, startup
from framework_sanic.core.services.subscription_reconciler import Sweeper


async def run_once() -> int:
    services = build_services()
    await startup(services, schedules=False)
    try:
        sweeper = Sweeper(services.repo, services.queue, services.clock, services.settings.SWEEP_TICK_SECONDS)
        return await sweeper.tick()
    finally:
        await shutdown(services)


if __name__ == "__main__":
    print(f"enqueued {asyncio.run(run_once())} reconciles")
