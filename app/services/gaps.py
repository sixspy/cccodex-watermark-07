import asyncio
import threading

from app.db import connect
from app.services.errors import ServiceError

# 落库形态：全系统只保留「旁路快照」一种。来源行落 gap_report_sources，
# 对账包 gap_reports 一经写入不可变，复算只听快照、不回滚清旧摘要。
FORM_MARKER = "旁路快照"
CHECK_ROUNDS = 2
DEFAULT_AS_OF = "2026-11-16"

# 同进程两道互斥：
# - asyncio.Lock：Web 路由跑在同一事件循环里，同步重扫在事件循环视角下
#   不会并行，threading.Lock 拦不住"前后脚"的两个重扫请求，必须由协程锁拦；
# - threading.Lock：同进程内别的线程直接调同步服务函数时兜底。
# 跨进程（多 worker）由 SQLite BEGIN IMMEDIATE 库级写锁兜底。
_rescan_lock = threading.Lock()
_async_rescan_lock = asyncio.Lock()


def list_reports(db_path: str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id: int, db_path: str | None = None) -> dict | None:
    """取对账包及其旁路来源快照（详情页形态标记与来源批号同查）。"""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            return None
        sources = conn.execute(
            "SELECT * FROM gap_report_sources WHERE report_id=? ORDER BY id",
            (report_id,),
        ).fetchall()
        result = dict(row)
        result["sources"] = [dict(s) for s in sources]
        return result


def _snapshot(conn, day: str) -> list[dict]:
    """把截至日的来源批次整行拉成旁路快照，作废批实物保留但标记不计应盖印。"""
    rows = conn.execute(
        "SELECT id, run_code, table_name, biz_date, status FROM run_batches "
        "WHERE biz_date<=? ORDER BY id",
        (day,),
    ).fetchall()
    snapshot = []
    for r in rows:
        is_voided = r["status"] == "已作废"
        counted = not is_voided
        is_gap = counted and r["status"] != "已盖印"
        snapshot.append(
            {
                "batch_id": r["id"],
                "run_code": r["run_code"],
                "table_name": r["table_name"],
                "biz_date": r["biz_date"],
                "batch_status": r["status"],
                "counted_as_expected": 1 if counted else 0,
                "is_gap": 1 if is_gap else 0,
                "is_voided": 1 if is_voided else 0,
            }
        )
    return snapshot


def _tally(snapshot: list[dict]) -> dict:
    expected = sum(s["counted_as_expected"] for s in snapshot)
    stamped = sum(
        1 for s in snapshot if s["counted_as_expected"] and s["batch_status"] == "已盖印"
    )
    voided = sum(s["is_voided"] for s in snapshot)
    gap = sum(s["is_gap"] for s in snapshot)
    return {
        "expected_count": expected,
        "stamped_count": stamped,
        "voided_count": voided,
        "gap_count": gap,
    }


def _assert_aligned(tally: dict, snapshot: list[dict]) -> None:
    """缺口数与作废批实物必须对齐，偏了就停，不落库。"""
    known = {"待盖印", "已盖印", "已作废"}
    unknown = sorted({s["batch_status"] for s in snapshot if s["batch_status"] not in known})
    if unknown:
        raise ServiceError(
            f"对账中止：来源实物存在无法识别的状态 {','.join(unknown)}，缺口数对不齐",
            500,
        )
    physical = len(snapshot)
    if tally["expected_count"] + tally["voided_count"] != physical:
        raise ServiceError(
            f"对账中止：应盖印 {tally['expected_count']} 加作废实物 "
            f"{tally['voided_count']} 与来源实物 {physical} 对不齐",
            500,
        )
    if tally["expected_count"] - tally["stamped_count"] != tally["gap_count"]:
        raise ServiceError(
            "对账中止：应盖印减已盖印与缺口数对不齐",
            500,
        )
    if tally["gap_count"] < 0:
        raise ServiceError("对账中止：缺口数为负，数据异常", 500)


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    """点重扫：引擎一次吐数。

    排他锁 → 单事务两轮复验 → 实物对齐校验 → 写不可变对账包 + 旁路来源快照。
    任何一步失败都回滚并返回中文错误，绝不留下半套摘要。
    """
    day = (as_of_date or "").strip() or DEFAULT_AS_OF
    if not _rescan_lock.acquire(blocking=False):
        raise ServiceError("已有重扫在执行，请稍后再试", 409)

    conn = connect(db_path)
    # 自动提交关闭，显式 BEGIN IMMEDIATE 立刻拿库级写锁，
    # 跨进程并发重扫也只能有一个写入方。
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout = 500")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            raise ServiceError("已有重扫在执行，请稍后再试", 409) from exc

        try:
            # 第一轮：来源行挂旁路快照并点数
            snapshot = _snapshot(conn, day)
            tally = _tally(snapshot)
            _assert_aligned(tally, snapshot)

            # 第二轮：同一连接内重拉快照复验，数字必须稳住
            snapshot_2 = _snapshot(conn, day)
            tally_2 = _tally(snapshot_2)
            if snapshot != snapshot_2 or tally != tally_2:
                raise ServiceError("对账中止：两轮复验数字不一致，请重新重扫", 500)
            _assert_aligned(tally_2, snapshot_2)

            summary = (
                f"截至 {day}：应盖印 {tally['expected_count']}，"
                f"已盖印 {tally['stamped_count']}，缺口 {tally['gap_count']}，"
                f"作废 {tally['voided_count']}（{FORM_MARKER}）"
            )
            cur = conn.execute(
                "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, "
                "gap_count, voided_count, form_marker, check_round, summary) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    day,
                    tally["expected_count"],
                    tally["stamped_count"],
                    tally["gap_count"],
                    tally["voided_count"],
                    FORM_MARKER,
                    CHECK_ROUNDS,
                    summary,
                ),
            )
            report_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO gap_report_sources(report_id, batch_id, run_code, "
                "table_name, biz_date, batch_status, counted_as_expected, is_gap, is_voided) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        report_id,
                        s["batch_id"],
                        s["run_code"],
                        s["table_name"],
                        s["biz_date"],
                        s["batch_status"],
                        s["counted_as_expected"],
                        s["is_gap"],
                        s["is_voided"],
                    )
                    for s in snapshot
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        report = get_report(report_id, db_path)
        if report is None:
            raise ServiceError("对账失败：对账包写入后读不到", 500)
        return report
    finally:
        conn.close()
        _rescan_lock.release()


async def run_gap_report_async(as_of_date: str, db_path: str | None = None) -> dict:
    """Web 入口：先在事件循环层做协程互斥，再把同步重扫丢到工作线程。

    同事件循环里同步路由不会真正并行，仅靠 threading.Lock 拦不住前后脚的
    重扫请求；这里非阻塞抢 asyncio.Lock，抢不到直接 409，不排队双写。
    """
    if _async_rescan_lock.locked():
        raise ServiceError("已有重扫在执行，请稍后再试", 409)
    # 同事件循环内 locked() 到此处之间没有 await，状态不会被其他协程改掉
    await _async_rescan_lock.acquire()
    try:
        return await asyncio.to_thread(run_gap_report, as_of_date, db_path)
    finally:
        _async_rescan_lock.release()
