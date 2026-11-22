"""MySQL access. Uses bancho.py's database: accounts come from bancho's `users`
table, and lazer data lives in its own `lazer_*` tables next to it."""
from __future__ import annotations

import os

import aiomysql

SCHEMA = [
    """create table if not exists lazer_tokens (
        access_token char(64) not null primary key,
        refresh_token char(64) not null unique,
        user_id int not null,
        expires_at int not null,
        created_at int not null,
        index (user_id)
    )""",
    """create table if not exists lazer_score_tokens (
        id bigint auto_increment primary key,
        user_id int not null,
        beatmap_id int not null,
        beatmap_md5 char(32) not null,
        ruleset_id tinyint not null,
        created_at int not null,
        score_id bigint null
    )""",
    """create table if not exists lazer_scores (
        id bigint auto_increment primary key,
        user_id int not null,
        beatmap_id int not null,
        beatmap_md5 char(32) not null,
        ruleset_id tinyint not null,
        board varchar(8) not null,
        total_score bigint not null,
        accuracy double not null,
        max_combo int not null,
        grade varchar(4) not null,
        pp double not null default 0,
        mods json not null,
        statistics json not null,
        maximum_statistics json not null,
        passed tinyint(1) not null,
        ranked tinyint(1) not null default 0,
        best tinyint(1) not null default 0,
        started_at datetime null,
        ended_at datetime not null,
        index (beatmap_id, ruleset_id, board, best),
        index (user_id, ruleset_id, board, best)
    )""",
    """create table if not exists lazer_stats (
        user_id int not null,
        ruleset_id tinyint not null,
        board varchar(8) not null,
        pp double not null default 0,
        accuracy double not null default 0,
        play_count int not null default 0,
        play_time int not null default 0,
        ranked_score bigint not null default 0,
        total_score bigint not null default 0,
        total_hits bigint not null default 0,
        max_combo int not null default 0,
        grade_ss int not null default 0,
        grade_ssh int not null default 0,
        grade_s int not null default 0,
        grade_sh int not null default 0,
        grade_a int not null default 0,
        primary key (user_id, ruleset_id, board)
    )""",
]


class DB:
    pool: aiomysql.Pool

    async def connect(self) -> None:
        self.pool = await aiomysql.create_pool(
            host=os.environ.get("DB_HOST", "mysql"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASS"],
            db=os.environ["DB_NAME"],
            autocommit=True,
            charset="utf8mb4",
            minsize=1,
            maxsize=10,
        )
        for stmt in SCHEMA:
            await self.execute(stmt)

    async def one(self, sql: str, args: tuple = ()) -> dict | None:
        async with self.pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return await cur.fetchone()

    async def all(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return list(await cur.fetchall())

    async def execute(self, sql: str, args: tuple = ()) -> int:
        """Run a statement; returns lastrowid (or rowcount when there is none)."""
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, args)
            return cur.lastrowid or cur.rowcount
