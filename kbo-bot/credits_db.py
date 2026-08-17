"""
KBO 봇 크레딧(사용권) 관리 모듈 v2

기존 credits.db / user_credits 테이블 구조를 유지하면서
SQLite 트랜잭션과 동시 접근 제어를 강화한다.

핵심:
- use_credit(): 잔액 확인 + 1회 차감을 하나의 원자적 UPDATE로 처리
- add_credits(): 충전 + 누적 구매를 하나의 원자적 UPDATE로 처리
- BEGIN IMMEDIATE로 동시 쓰기 충돌 방지
- WAL + busy_timeout으로 동시 접근 안정성 향상
- 음수/0 충전 차단
- 크레딧이 음수로 내려가지 않도록 DB 조건으로 방지
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# credits_db.py가 있는 폴더에 DB 저장
DB_FILE = str(Path(__file__).resolve().with_name("credits.db"))

# 운영상 안전한 1회 충전 상한
MAX_CREDIT_ADD = 1000


def _configure_connection(conn: sqlite3.Connection) -> None:
    """SQLite 운영 옵션 설정."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


@contextmanager
def get_db(write: bool = False) -> Iterator[sqlite3.Connection]:
    """
    SQLite 연결 및 트랜잭션 관리.

    write=True:
        BEGIN IMMEDIATE로 쓰기 잠금을 먼저 확보해
        잔액 차감/충전의 경쟁 상태를 줄인다.
    """
    conn = sqlite3.connect(
        DB_FILE,
        timeout=5.0,
        isolation_level=None,
    )
    _configure_connection(conn)

    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")

        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    """테이블이 없으면 생성."""
    with get_db(write=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_credits (
                discord_id TEXT PRIMARY KEY,
                credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
                total_used INTEGER NOT NULL DEFAULT 0 CHECK (total_used >= 0),
                total_purchased INTEGER NOT NULL DEFAULT 0 CHECK (total_purchased >= 0)
            )
            """
        )


def _validate_user_id(discord_id: str) -> str:
    user_id = str(discord_id).strip()
    if not user_id:
        raise ValueError("discord_id가 비어 있습니다.")
    if len(user_id) > 64:
        raise ValueError("discord_id가 너무 깁니다.")
    return user_id


def _validate_add_amount(amount: int) -> int:
    try:
        value = int(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("충전 수량은 정수여야 합니다.") from exc

    if value <= 0:
        raise ValueError("충전 수량은 1 이상이어야 합니다.")
    if value > MAX_CREDIT_ADD:
        raise ValueError(f"한 번에 최대 {MAX_CREDIT_ADD}회까지 충전할 수 있습니다.")
    return value


def _ensure_user(conn: sqlite3.Connection, discord_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO user_credits
            (discord_id, credits, total_used, total_purchased)
        VALUES (?, 0, 0, 0)
        """,
        (discord_id,),
    )


def get_status(discord_id: str) -> dict:
    """잔여 크레딧, 누적 사용, 누적 구매 반환."""
    user_id = _validate_user_id(discord_id)

    with get_db(write=True) as conn:
        _ensure_user(conn, user_id)
        row = conn.execute(
            """
            SELECT credits, total_used, total_purchased
            FROM user_credits
            WHERE discord_id = ?
            """,
            (user_id,),
        ).fetchone()

        if row is None:
            raise RuntimeError("사용자 크레딧 정보를 불러오지 못했습니다.")

        return {
            "credits": int(row["credits"]),
            "total_used": int(row["total_used"]),
            "total_purchased": int(row["total_purchased"]),
        }


def add_credits(discord_id: str, amount: int) -> int:
    """
    관리자용 크레딧 충전.

    충전과 total_purchased 증가가 같은 트랜잭션에서 처리된다.
    """
    user_id = _validate_user_id(discord_id)
    value = _validate_add_amount(amount)

    with get_db(write=True) as conn:
        _ensure_user(conn, user_id)

        conn.execute(
            """
            UPDATE user_credits
            SET
                credits = credits + ?,
                total_purchased = total_purchased + ?
            WHERE discord_id = ?
            """,
            (value, value, user_id),
        )

        row = conn.execute(
            "SELECT credits FROM user_credits WHERE discord_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise RuntimeError("충전 후 사용자 정보를 불러오지 못했습니다.")

        return int(row["credits"])


def use_credit(discord_id: str) -> bool:
    """
    크레딧 1회 차감.

    핵심:
    SELECT로 잔액을 읽은 뒤 별도로 UPDATE하지 않고,
    아래 UPDATE 한 번으로 '잔액이 1 이상인 경우에만 차감'한다.

    따라서 동시에 여러 요청이 들어와도 음수 잔액이나
    중복 차감 상태가 발생하지 않는다.
    """
    user_id = _validate_user_id(discord_id)

    with get_db(write=True) as conn:
        _ensure_user(conn, user_id)

        cursor = conn.execute(
            """
            UPDATE user_credits
            SET
                credits = credits - 1,
                total_used = total_used + 1
            WHERE discord_id = ?
              AND credits > 0
            """,
            (user_id,),
        )

        return cursor.rowcount == 1


def set_credits_for_admin(discord_id: str, credits: int) -> int:
    """
    관리자용 강제 잔액 설정.
    일반 충전 명령에서는 사용하지 않고,
    데이터 복구/운영자 조정이 필요한 경우에만 사용한다.
    """
    user_id = _validate_user_id(discord_id)

    try:
        value = int(credits)
    except (TypeError, ValueError) as exc:
        raise ValueError("credits는 정수여야 합니다.") from exc

    if value < 0:
        raise ValueError("크레딧은 음수가 될 수 없습니다.")

    with get_db(write=True) as conn:
        _ensure_user(conn, user_id)
        conn.execute(
            """
            UPDATE user_credits
            SET credits = ?
            WHERE discord_id = ?
            """,
            (value, user_id),
        )

        row = conn.execute(
            "SELECT credits FROM user_credits WHERE discord_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise RuntimeError("잔액 설정 후 사용자 정보를 불러오지 못했습니다.")

        return int(row["credits"])


init_db()