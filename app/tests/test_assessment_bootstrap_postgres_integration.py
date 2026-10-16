"""Opt-in lifecycle checks against a real PostgreSQL scratch database.

Set ``FUNDAMENTUS_TEST_POSTGRES_URL`` to a disposable database URL before
running this module.  The test only deletes rows with its unique run-key
prefix; it never drops shared tables or touches provider data.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.assessment.store import ASSESSMENT_BOOTSTRAP_TABLE, AssessmentStore
from app.core.postgres import postgres_connect

DSN = os.environ.get("FUNDAMENTUS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="FUNDAMENTUS_TEST_POSTGRES_URL is not configured",
)


async def _cleanup(prefix: str) -> None:
    connection = await postgres_connect(DSN)
    try:
        await connection.execute(
            f"DELETE FROM {ASSESSMENT_BOOTSTRAP_TABLE} WHERE run_key LIKE %s",
            (f"{prefix}%",),
        )
        await connection.execute(
            "DELETE FROM fundamentus_assessment_snapshots WHERE run_key LIKE %s",
            (f"{prefix}%",),
        )
        await connection.commit()
    finally:
        await connection.close()


async def _store() -> AssessmentStore:
    store = AssessmentStore(database_url=DSN, default_lease_seconds=1)
    await store.startup()
    return store


async def _run_queue_lifecycle() -> None:
    first = await _store()
    second = await _store()
    prefix = f"assessment:test-bootstrap:{uuid.uuid4().hex}:"
    key = f"{prefix}run"
    bootstrap_key = uuid.uuid4().hex + uuid.uuid4().hex
    now = datetime.now(UTC) - timedelta(seconds=2)
    try:
        admitted = await first.enqueue_bootstrap(
            key=key,
            bootstrap_key=bootstrap_key,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="First issuer hint",
            period_at=now,
            now=now,
        )
        repeated = await second.enqueue_bootstrap(
            key=key,
            bootstrap_key=bootstrap_key,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Later issuer hint",
            period_at=now,
            now=now,
        )
        assert admitted.state == "processing"
        assert repeated.state == "processing"
        assert repeated.work.corporate_name == "First issuer hint"

        claim_a, claim_b = await asyncio.gather(
            first.claim_bootstrap_due(limit=1, now=now, lease_seconds=1),
            second.claim_bootstrap_due(limit=1, now=now, lease_seconds=1),
        )
        owners = [claim for claim in (*claim_a, *claim_b)]
        assert len(owners) == 1
        stale = owners[0]
        assert not await second.renew_bootstrap_lease(
            key=key,
            token="wrong-token",
            now=now,
            lease_seconds=1,
        )

        await asyncio.sleep(1.1)
        recovered = await second.claim_bootstrap_due(
            limit=1,
            now=datetime.now(UTC),
            lease_seconds=1,
        )
        assert len(recovered) == 1
        assert recovered[0].token != stale.token
        assert not await first.renew_bootstrap_lease(
            key=key,
            token=stale.token,
            now=datetime.now(UTC),
            lease_seconds=1,
        )
        assert await second.complete_bootstrap(
            key=key,
            token=recovered[0].token,
            success=True,
            now=datetime.now(UTC),
        )
    finally:
        await first.close()
        await second.close()
        await _cleanup(prefix)


def test_bootstrap_queue_is_durable_and_single_flight() -> None:
    asyncio.run(_run_queue_lifecycle())
