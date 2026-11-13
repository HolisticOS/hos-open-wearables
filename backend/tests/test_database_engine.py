"""The engine must notice a silently dropped connection in seconds, not minutes.

2026-11-12: idle pooled connections were dropped on the path to Postgres. The
next use of one (the pool's pre-ping included) waited out the kernel's ~15.5
min TCP retransmit timeout, and enough of them froze the whole API and worker.
"""

import pytest
from sqlalchemy import create_engine, text

from app.config import settings
from app.database import db_connect_args, engine


def test_idle_connections_are_recycled_well_inside_an_hour() -> None:
    assert engine.pool._recycle == settings.db_pool_recycle_seconds
    assert 0 < settings.db_pool_recycle_seconds <= 600


def test_dead_connections_fail_fast() -> None:
    args = db_connect_args()
    assert args["keepalives"] == 1
    detect_s = args["keepalives_idle"] + args["keepalives_interval"] * args["keepalives_count"]
    assert detect_s <= 120
    assert 0 < args["tcp_user_timeout"] <= 60_000
    assert args["connect_timeout"] > 0


def test_options_are_accepted_by_libpq(request: pytest.FixtureRequest) -> None:
    probe = create_engine(request.getfixturevalue("_postgres_url"), connect_args=db_connect_args())
    try:
        with probe.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        probe.dispose()
