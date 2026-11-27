# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The second MEHO process of the two-process block test.

It builds its own app and its own copy of the block list, on the same
PostgreSQL database as the test process. Every 0.1 seconds it reads
``GET /api/v1/connectors`` with the agent's token and prints one line
``answer <status code> <wall clock>`` each time the status code changes.
Other lines on the output are log lines. It stops
after ``SECOND_PROCESS_SECONDS``.

Input (environment): ``DATABASE_URL`` and the usual settings,
``SECOND_PROCESS_TOKEN`` (the agent's token) and ``SECOND_PROCESS_JWKS``
(the public keys as JSON).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import respx
from httpx import ASGITransport

from tests._oidc_jwt_helpers import mock_discovery_and_jwks


async def _main() -> None:
    from meho_backplane.main import app

    token = os.environ["SECOND_PROCESS_TOKEN"]
    jwks = json.loads(os.environ["SECOND_PROCESS_JWKS"])
    stop_at = time.monotonic() + float(os.environ.get("SECOND_PROCESS_SECONDS", "60"))
    last: int | None = None
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, jwks)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            while time.monotonic() < stop_at:
                answer = await client.get(
                    "/api/v1/connectors", headers={"Authorization": f"Bearer {token}"}
                )
                if answer.status_code != last:
                    last = answer.status_code
                    print(f"answer {last} {time.time():.3f}", flush=True)
                await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(_main())
