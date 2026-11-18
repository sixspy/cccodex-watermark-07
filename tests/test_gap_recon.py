import sqlite3
import threading

import pytest

from app.db import get_db
from app.services.errors import ServiceError
from app.services.gaps import FORM_MARKER, run_gap_report


async def _void(authed, run_code):
    batches = (await (await authed.get("/api/batches")).json())["items"]
    bid = next(b["id"] for b in batches if b["run_code"] == run_code)
    r = await authed.post(f"/api/batches/{bid}/void", json={})
    assert r.status == 200
    return bid


@pytest.mark.asyncio
async def test_voided_batch_kept_physical_but_excluded_from_expected(authed):
    # 种子：截至 11-16 共 4 个实物批次（3 待盖印 + 1 已盖印）
    await _void(authed, "ord-20261114-01")

    r = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    assert r.status == 200
    report = (await r.json())["report"]

    # 作废实物还在，但应盖印只剩 3，缺口 = 3 - 1 = 2
    assert report["voided_count"] == 1
    assert report["expected_count"] == 3
    assert report["stamped_count"] == 1
    assert report["gap_count"] == 2
    assert report["form_marker"] == FORM_MARKER
    assert report["check_round"] == 2
    assert FORM_MARKER in report["summary"]
    assert "作废 1" in report["summary"]

    # 旁路快照里作废批实物留存且明确标记不计应盖印
    sources = report["sources"]
    assert len(sources) == 4
    voided = next(s for s in sources if s["run_code"] == "ord-20261114-01")
    assert voided["is_voided"] == 1
    assert voided["counted_as_expected"] == 0
    assert voided["is_gap"] == 0
    gap_codes = {s["run_code"] for s in sources if s["is_gap"]}
    assert gap_codes == {"pay-20261114-01", "gmv-20261114-01"}


@pytest.mark.asyncio
async def test_two_rescans_keep_numbers_stable_and_immutable(authed):
    await _void(authed, "pay-20261114-01")

    first = (
        await (
            await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
        ).json()
    )["report"]
    second = (
        await (
            await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
        ).json()
    )["report"]

    keys = ("expected_count", "stamped_count", "gap_count", "voided_count")
    assert all(first[k] == second[k] for k in keys)
    assert first["summary"] == second["summary"]
    # 旧对账包不可变，没有被回滚清掉
    assert first["id"] != second["id"]
    listing = (await (await authed.get("/api/gap-reports")).json())["items"]
    assert {r["id"] for r in listing} == {first["id"], second["id"]}

    # 再盖印一个待盖印批次后重扫：缺口随实物减少，但老包数字不动
    batches = (await (await authed.get("/api/batches")).json())["items"]
    bid = next(b["id"] for b in batches if b["status"] == "待盖印")
    await authed.post(
        f"/api/batches/{bid}/stamp", json={"watermark_value": "WM-NEW-001"}
    )
    third = (
        await (
            await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
        ).json()
    )["report"]
    assert third["gap_count"] == first["gap_count"] - 1
    old = (await (await authed.get(f"/api/gap-reports/{first['id']}")).json())[
        "report"
    ]
    assert old["gap_count"] == first["gap_count"]


@pytest.mark.asyncio
async def test_concurrent_rescan_exclusive_lock_no_double_write(authed):
    import os

    import app.services.gaps as gaps_mod

    db_path = os.environ["DATABASE_PATH"]
    box = {}

    # 主测先持排他锁，模拟另一个重扫正在进行；竞争方（含另一线程）必须立刻 409
    gaps_mod._rescan_lock.acquire()
    try:
        with pytest.raises(ServiceError) as info:
            run_gap_report("2026-11-16", db_path)
        assert info.value.status_code == 409
        assert "重扫" in info.value.message

        def worker():
            try:
                box["report"] = run_gap_report("2026-11-16", db_path)
            except ServiceError as exc:
                box["error"] = (exc.status_code, exc.message)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        assert "report" not in box
        assert box["error"][0] == 409

        # 锁被占期间任何重扫都进不来，库里没有任何对账包
        with get_db(db_path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) AS c FROM gap_reports").fetchone()["c"]
                == 0
            )
    finally:
        gaps_mod._rescan_lock.release()

    # 锁释放后重扫成功，且只落一份对账包，不存在双写覆盖
    report = run_gap_report("2026-11-16", db_path)
    assert report["form_marker"] == FORM_MARKER
    with get_db(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM gap_reports").fetchone()["c"]
        assert count == 1
        sources = conn.execute(
            "SELECT COUNT(*) AS c FROM gap_report_sources"
        ).fetchone()["c"]
        assert sources == 4


@pytest.mark.asyncio
async def test_async_lock_serializes_same_event_loop(tmp_path):
    # 同事件循环并发：第二个重扫必须 409，而不是排队各写一份
    import asyncio
    import os

    from app.services.gaps import run_gap_report_async

    db_path = str(tmp_path / "async.db")
    from app.db import init_db

    init_db(db_path)
    os.environ["DATABASE_PATH"] = db_path

    results = await asyncio.gather(
        run_gap_report_async("2026-11-16", db_path),
        run_gap_report_async("2026-11-16", db_path),
        return_exceptions=True,
    )
    reports = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(reports) == 1 and len(errors) == 1
    assert isinstance(errors[0], ServiceError) and errors[0].status_code == 409
    assert "重扫" in errors[0].message

    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_reports").fetchone()["c"] == 1


def test_begin_immediate_blocks_second_writer(tmp_path):
    # 跨进程/跨连接防线：第一个写事务未提交时，第二个 BEGIN IMMEDIATE 必须失败
    from app.db import connect, init_db

    db_path = str(tmp_path / "writers.db")
    init_db(db_path)
    first = connect(db_path)
    first.isolation_level = None
    first.execute("BEGIN IMMEDIATE")
    try:
        second = connect(db_path)
        second.isolation_level = None
        second.execute("PRAGMA busy_timeout = 300")
        with pytest.raises(sqlite3.OperationalError):
            second.execute("BEGIN IMMEDIATE")
        second.close()
    finally:
        first.execute("ROLLBACK")
        first.close()


def test_alignment_guard_rejects_bad_tally_directly():
    from app.services.gaps import _assert_aligned

    snapshot = [
        {"batch_status": "待盖印", "counted_as_expected": 1, "is_gap": 1, "is_voided": 0},
        {"batch_status": "已作废", "counted_as_expected": 0, "is_gap": 0, "is_voided": 1},
    ]
    bad = {"expected_count": 1, "stamped_count": 1, "voided_count": 1, "gap_count": 9}
    with pytest.raises(ServiceError) as info:
        _assert_aligned(bad, snapshot)
    assert "对不齐" in info.value.message


@pytest.mark.asyncio
async def test_misaligned_gap_aborts_and_persists_nothing(tmp_path):
    db_path = str(tmp_path / "align.db")
    from app.db import init_db

    init_db(db_path)
    # 绕过 CHECK 造一个不在三态内的脏实物：对不齐时必须停落库
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute(
        "UPDATE run_batches SET status='在途异常' WHERE run_code='ord-20261114-01'"
    )
    raw.commit()
    raw.close()

    with pytest.raises(ServiceError) as info:
        run_gap_report("2026-11-16", db_path)
    assert "对不齐" in info.value.message

    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_reports").fetchone()["c"] == 0
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM gap_report_sources").fetchone()["c"]
            == 0
        )


@pytest.mark.asyncio
async def test_report_detail_endpoint_sources_and_marker(authed):
    created = (
        await (
            await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
        ).json()
    )["report"]
    r = await authed.get(f"/api/gap-reports/{created['id']}")
    assert r.status == 200
    detail = (await r.json())["report"]
    assert detail["form_marker"] == FORM_MARKER
    assert len(detail["sources"]) == 4
    assert all(s["run_code"] for s in detail["sources"])

    missing = await authed.get("/api/gap-reports/99999")
    assert missing.status == 404
    assert (await missing.json())["error"] == "对账包不存在"


@pytest.mark.asyncio
async def test_void_rules_and_chinese_errors(authed):
    batches = (await (await authed.get("/api/batches")).json())["items"]
    stamped_id = next(b["id"] for b in batches if b["status"] == "已盖印")
    r = await authed.post(f"/api/batches/{stamped_id}/void", json={})
    assert r.status == 409
    assert "已盖印" in (await r.json())["error"]

    pending_code = "gmv-20261114-01"
    bid = await _void(authed, pending_code)
    again = await authed.post(f"/api/batches/{bid}/void", json={})
    assert again.status == 409
    assert "已作废" in (await again.json())["error"]

    # 重扫成功返回中文摘要（失败路径也统一走 ServiceError 中文消息）
    ok_resp = await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )
    assert ok_resp.status == 200
    assert "缺口" in (await ok_resp.json())["report"]["summary"]


@pytest.mark.asyncio
async def test_gap_endpoints_require_login(client):
    from blacksheep.contents import JSONContent

    r = await client.get("/api/gap-reports")
    assert r.status == 401
    r2 = await client.post("/api/gap-reports", content=JSONContent({}))
    assert r2.status == 401
