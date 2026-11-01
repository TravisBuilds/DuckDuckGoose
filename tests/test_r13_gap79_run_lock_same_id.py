"""GAP-79 (ep05v2 r13): a TERMINATED run of an episode never released its run lock, and the next run of
the same workflow id could not take it: describe(id) resolves to the newest run (the caller, RUNNING),
so the stale-holder check always saw "running". A lock held under the caller's own workflow id is an
earlier, closed run (one execution per id) and is reclaimed; another id still blocks."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import ActivityEnvironment

from hfvg.activities.run_lock import acquire_run_lock
from hfvg.credit_ledger import CreditLedger


def _patch_client(monkeypatch, status):
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=MagicMock(status=status))
    client = MagicMock(); client.get_workflow_handle = MagicMock(return_value=handle)
    import temporalio.activity as ta
    real = ta.info

    def fake_info():
        i = real()
        return MagicMock(wraps=i, workflow_client=MagicMock(return_value=client))
    monkeypatch.setattr(ta, "info", fake_info)
    return handle


@pytest.mark.asyncio
async def test_lock_of_an_earlier_run_of_the_same_id_is_reclaimed(tmp_path, monkeypatch):
    db = str(tmp_path / "l.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    led = CreditLedger(db); await led.init_db()
    assert (await led.acquire_run_lock("ep05v2", "ep05v2", "default"))["acquired"]   # terminated run's lock
    _patch_client(monkeypatch, WorkflowExecutionStatus.RUNNING)   # describe(id) = the caller
    out = await ActivityEnvironment().run(acquire_run_lock, "ep05v2", "ep05v2", "default")
    assert out["acquired"] is True


@pytest.mark.asyncio
async def test_lock_of_another_running_workflow_still_blocks(tmp_path, monkeypatch):
    db = str(tmp_path / "l.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    led = CreditLedger(db); await led.init_db()
    assert (await led.acquire_run_lock("ep04", "ep04", "default"))["acquired"]
    _patch_client(monkeypatch, WorkflowExecutionStatus.RUNNING)
    with pytest.raises(ValueError, match="already has episode ep04"):
        await ActivityEnvironment().run(acquire_run_lock, "ep05v2", "ep05v2", "default")
