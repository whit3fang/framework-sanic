"""Stubs: in-memory fakes of external systems for tests, local development and the docker-compose
harness. Never used in production: selected only by config (``PLATFORM_ADAPTER=fake``,
``CONNECTOR=fake``) through ``bootstrap.py``. Production code (core, features, adapters, http,
connectors, workers) must not import this package (lint-enforced).

* ``dialpad``         FakeDialpadPlatform: OAuth, target hierarchy, webhooks and subscriptions
* ``dialpad_tokens``  dev ``id_token`` minting (matches ``adapters/dialpad/id_token.py``)
* ``connector``       FakeConnector (an in-memory CRM) and its ``fake.dev_harness`` feature
"""
