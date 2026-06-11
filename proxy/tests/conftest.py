import sys
import os
import asyncio
import pytest

# Add proxy/ to path so tests can import proxy modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use a temp DB and desensitize mode for all tests
os.environ.setdefault("DB_PATH", ":memory:")
os.environ["SANITY_MODE"] = "desensitize"


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset all module-level state between tests to avoid cross-test contamination."""
    import storage

    # Close any open DB connection
    if storage._db is not None:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(storage._db.close())
        loop.close()
        storage._db = None

    # Clear log buffers and rule caches
    storage._log_buffer.clear()
    storage._log_subscribers.clear()
    storage._snapshot_buffer.clear()

    # 脱敏注册表已改为每请求隔离（_new_registry），无需在此清理全局状态。

    yield

    # Teardown: close DB again if opened during the test
    if storage._db is not None:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(storage._db.close())
        loop.close()
        storage._db = None
