"""CIDR 地址池分配 API（FastAPI 入口）。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException

from . import logic
from .db import connect, init_db
from .schemas import (
    AllocateBody,
    AllocRequest,
    PoolCreate,
    ReleaseBody,
    ResizeBody,
    UnavailableCreate,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="CIDR Pool Allocator",
    description="本地 IPv4 / IPv6 CIDR 地址池分配服务",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """写事务。

    ``BEGIN IMMEDIATE`` 立即取得写锁，把并发写事务在 SQLite 层面串行化，
    因此并发申请不会拿到重叠网段；任何一步失败都会整体回滚，
    批量申请要么全部落库，要么不产生任何分配。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """读事务。

    显式开启一个事务（deferred），保证事务内的所有读取看到同一时刻的数据库
    快照；快照差异比较必须在单个读事务中完成，否则并发写入可能让"当前状态"
    在两次查询之间发生变化。任何异常都会回滚（只读，回滚仅释放事务）。
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_or_400(cidr: str) -> logic.Network:
    try:
        return logic.parse_network(cidr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"无效 CIDR {cidr!r}: {exc}")


def get_pool_or_404(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM pools WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"地址池 {name!r} 不存在")
    return row


def load_occupied(conn: sqlite3.Connection, pool_id: int) -> list[logic.Network]:
    rows = conn.execute(
        "SELECT cidr FROM unavailable WHERE pool_id = ? "
        "UNION ALL SELECT cidr FROM allocations WHERE pool_id = ?",
        (pool_id, pool_id),
    ).fetchall()
    return [logic.parse_network(r["cidr"]) for r in rows]


def pool_fingerprint(conn: sqlite3.Connection, pool_id: int) -> str:
    """池状态指纹：已分配项与不可用段的有序摘要。

    计划在创建时记录指纹，提交时重新计算并比对；只要计划创建后该池的
    分配项或不可用段发生过任何变化，指纹即不一致，计划随之失效。
    """
    alloc = sorted(
        r["cidr"] for r in conn.execute(
            "SELECT cidr FROM allocations WHERE pool_id = ?", (pool_id,)
        )
    )
    unavail = sorted(
        r["cidr"] for r in conn.execute(
            "SELECT cidr FROM unavailable WHERE pool_id = ?", (pool_id,)
        )
    )
    payload = json.dumps({"allocations": alloc, "unavailable": unavail})
    return hashlib.sha256(payload.encode()).hexdigest()


def check_prefix_bounds(
    pool_net: logic.Network, version: int, requests: list[AllocRequest]
) -> None:
    max_len = logic.max_prefixlen(version)
    for req in requests:
        if not pool_net.prefixlen <= req.prefix_len <= max_len:
            raise HTTPException(
                status_code=400,
                detail=f"prefix_len {req.prefix_len} 非法："
                       f"必须在 [{pool_net.prefixlen}, {max_len}] 之间",
            )


def compute_allocations(
    pool_net: logic.Network,
    version: int,
    occupied: list[logic.Network],
    requests: list[AllocRequest],
    pool_name: str,
) -> list[logic.Network]:
    """按"网络地址最小的对齐子网"规则为批量申请选路（就地消耗 ``occupied``）。

    任一申请无法满足时抛 409，调用方所在事务整体回滚。
    """
    allocated: list[logic.Network] = []
    for req in requests:
        for _ in range(req.count):
            free = logic.free_intervals(pool_net, occupied)
            net = logic.smallest_aligned_network(free, req.prefix_len, version)
            if net is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"地址池 {pool_name!r} 剩余空间不足，无法分配 /{req.prefix_len}；"
                           f"本次批量申请已全部回滚",
                )
            occupied.append(net)
            allocated.append(net)
    return allocated


# ---------------------------------------------------------------------------
# 计划工具
# ---------------------------------------------------------------------------

DEFAULT_PLAN_TTL_SECONDS = 600  # 计划有效期：10 分钟


def plan_ttl_seconds() -> int:
    """计划有效期（秒），可用环境变量 ``CIDR_PLAN_TTL_SECONDS`` 覆盖。"""
    return int(os.environ.get("CIDR_PLAN_TTL_SECONDS", DEFAULT_PLAN_TTL_SECONDS))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pool_summary(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "cidr": row["cidr"],
        "version": row["version"],
        "prefix_len": row["prefix_len"],
        "total_addresses": 1 << (logic.max_prefixlen(row["version"]) - row["prefix_len"]),
        "created_at": row["created_at"],
    }


def pool_status_payload(conn: sqlite3.Connection, pool: sqlite3.Row) -> dict:
    pool_net = logic.parse_network(pool["cidr"])
    by_addr = lambda n: int(n.network_address)  # noqa: E731

    unavailable = sorted(
        (logic.parse_network(r["cidr"]) for r in conn.execute(
            "SELECT cidr FROM unavailable WHERE pool_id = ?", (pool["id"],)
        )),
        key=by_addr,
    )
    alloc_rows = conn.execute(
        "SELECT cidr, created_at FROM allocations WHERE pool_id = ?", (pool["id"],)
    ).fetchall()
    allocations = sorted(
        ((logic.parse_network(r["cidr"]), r["created_at"]) for r in alloc_rows),
        key=lambda t: by_addr(t[0]),
    )

    occupied = unavailable + [net for net, _ in allocations]
    free = logic.free_intervals(pool_net, occupied)

    return {
        **pool_summary(pool),
        "unavailable": [str(n) for n in unavailable],
        "allocations": [
            {"cidr": str(net), "created_at": created} for net, created in allocations
        ],
        "allocated_addresses": sum(net.num_addresses for net, _ in allocations),
        "remaining": logic.summarize_free(free, pool["version"]),
        "remaining_addresses": sum(b - a + 1 for a, b in free),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/pools", status_code=201)
def create_pool(body: PoolCreate) -> dict:
    net = parse_or_400(body.cidr)
    with write_tx() as conn:
        if conn.execute("SELECT 1 FROM pools WHERE name = ?", (body.name,)).fetchone():
            raise HTTPException(status_code=409, detail=f"地址池名称 {body.name!r} 已存在")
        rows = conn.execute(
            "SELECT name, cidr FROM pools WHERE version = ?", (net.version,)
        ).fetchall()
        for row in rows:
            if net.overlaps(logic.parse_network(row["cidr"])):
                raise HTTPException(
                    status_code=409,
                    detail=f"与已有地址池 {row['name']!r} ({row['cidr']}) 重叠",
                )
        conn.execute(
            "INSERT INTO pools (name, cidr, version, prefix_len) VALUES (?, ?, ?, ?)",
            (body.name, str(net), net.version, net.prefixlen),
        )
        row = conn.execute("SELECT * FROM pools WHERE name = ?", (body.name,)).fetchone()
        return pool_summary(row)


@app.get("/pools")
def list_pools() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM pools ORDER BY name").fetchall()
        pools = []
        for row in rows:
            status = pool_status_payload(conn, row)
            pools.append({
                **pool_summary(row),
                "allocated_count": len(status["allocations"]),
                "unavailable_count": len(status["unavailable"]),
                "remaining_addresses": status["remaining_addresses"],
            })
        return {"pools": pools}
    finally:
        conn.close()


@app.get("/pools/{name}")
def pool_status(name: str) -> dict:
    """地址池状态：已分配项、不可用段、按最小 CIDR 集合归并的剩余空间。"""
    conn = connect()
    try:
        pool = get_pool_or_404(conn, name)
        return pool_status_payload(conn, pool)
    finally:
        conn.close()


@app.delete("/pools/{name}", status_code=204)
def delete_pool(name: str) -> None:
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        conn.execute("DELETE FROM pools WHERE id = ?", (pool["id"],))


@app.post("/pools/{name}/unavailable", status_code=201)
def add_unavailable(name: str, body: UnavailableCreate) -> dict:
    net = parse_or_400(body.cidr)
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        pool_net = logic.parse_network(pool["cidr"])
        if net.version != pool_net.version:
            raise HTTPException(
                status_code=400,
                detail=f"地址族不符：地址池为 IPv{pool_net.version}，子网为 IPv{net.version}",
            )
        if not net.subnet_of(pool_net):
            raise HTTPException(
                status_code=400, detail=f"{net} 超出地址池 {pool_net} 范围"
            )
        for occ in load_occupied(conn, pool["id"]):
            if net.overlaps(occ):
                raise HTTPException(
                    status_code=409, detail=f"{net} 与已登记网段 {occ} 重叠"
                )
        conn.execute(
            "INSERT INTO unavailable (pool_id, cidr) VALUES (?, ?)",
            (pool["id"], str(net)),
        )
        return {"pool": name, "unavailable": str(net)}


@app.post("/pools/{name}/allocations", status_code=201)
def allocate(name: str, body: AllocateBody) -> dict:
    """按前缀长度申请子网；批量申请在同一事务中全部成功，否则整体回滚。"""
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        pool_net = logic.parse_network(pool["cidr"])
        check_prefix_bounds(pool_net, pool["version"], body.requests)
        occupied = load_occupied(conn, pool["id"])
        allocated = compute_allocations(
            pool_net, pool["version"], occupied, body.requests, name
        )
        for net in allocated:
            conn.execute(
                "INSERT INTO allocations (pool_id, cidr, prefix_len) VALUES (?, ?, ?)",
                (pool["id"], str(net), net.prefixlen),
            )
        return {"pool": name, "allocated": [str(n) for n in allocated]}


# ---------------------------------------------------------------------------
# 两阶段分配计划
# ---------------------------------------------------------------------------

@app.post("/pools/{name}/plans", status_code=201)
def create_plan(name: str, body: AllocateBody) -> dict:
    """创建分配计划：按与直接分配相同的规则预选子网并持久化，但不占用地址。

    计划有效期 10 分钟（``CIDR_PLAN_TTL_SECONDS`` 可调）；仅当计划未过期、
    未提交且池状态（分配项与不可用段）自创建后未变化时，才能提交生效。
    """
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        pool_net = logic.parse_network(pool["cidr"])
        check_prefix_bounds(pool_net, pool["version"], body.requests)
        occupied = load_occupied(conn, pool["id"])
        planned = compute_allocations(
            pool_net, pool["version"], occupied, body.requests, name
        )

        plan_id = str(uuid.uuid4())
        expires_at = utcnow() + timedelta(seconds=plan_ttl_seconds())
        conn.execute(
            "INSERT INTO plans (id, pool_id, cidrs, fingerprint, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan_id,
                pool["id"],
                json.dumps([str(n) for n in planned]),
                pool_fingerprint(conn, pool["id"]),
                iso_z(expires_at),
            ),
        )
        return {
            "pool": name,
            "plan_id": plan_id,
            "cidrs": [str(n) for n in planned],
            "expires_at": iso_z(expires_at),
        }


@app.post("/pools/{name}/plans/{plan_id}/commit", status_code=201)
def commit_plan(name: str, plan_id: str) -> dict:
    """提交计划：在同一事务中校验并原子落库；任一条件不满足返回 409，不分配。"""
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        plan = conn.execute(
            "SELECT * FROM plans WHERE id = ? AND pool_id = ?",
            (plan_id, pool["id"]),
        ).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail=f"计划 {plan_id!r} 不存在")
        if plan["status"] != "pending":
            raise HTTPException(
                status_code=409, detail=f"计划 {plan_id!r} 已提交，不能重复提交"
            )
        if utcnow() >= parse_iso_z(plan["expires_at"]):
            raise HTTPException(status_code=409, detail=f"计划 {plan_id!r} 已过期")
        if pool_fingerprint(conn, pool["id"]) != plan["fingerprint"]:
            raise HTTPException(
                status_code=409,
                detail=f"计划创建后地址池 {name!r} 的分配项或不可用段已变化，计划失效",
            )

        cidrs: list[str] = json.loads(plan["cidrs"])
        for cidr in cidrs:
            net = logic.parse_network(cidr)
            conn.execute(
                "INSERT INTO allocations (pool_id, cidr, prefix_len) VALUES (?, ?, ?)",
                (pool["id"], str(net), net.prefixlen),
            )
        conn.execute(
            "UPDATE plans SET status = 'committed', committed_at = ? WHERE id = ?",
            (iso_z(utcnow()), plan_id),
        )
        return {"pool": name, "plan_id": plan_id, "allocated": cidrs}


@app.post("/pools/{name}/release")
def release(name: str, body: ReleaseBody) -> dict:
    """释放已分配网段；空闲空间动态计算，相邻空闲段自动重新合并。"""
    net = parse_or_400(body.cidr)
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        cur = conn.execute(
            "DELETE FROM allocations WHERE pool_id = ? AND cidr = ?",
            (pool["id"], str(net)),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404, detail=f"地址池 {name!r} 中不存在已分配网段 {net}"
            )
        return {"pool": name, "released": str(net)}


@app.post("/pools/{name}/allocations/resize")
def resize_allocation(name: str, body: ResizeBody) -> dict:
    """原子调整已分配网段的前缀长度，返回 ``old_cidr`` 与 ``new_cidr``。

    缩小（``new_prefix_len`` 更大）时保留原网络地址，尾部空间立即释放；
    扩展（``new_prefix_len`` 更小）时改为唯一包含原网段的目标前缀，仅当
    目标仍在池内且新增范围不与其他分配或不可用段重叠才成功；前后缀长度
    相等时为幂等空操作。校验与更新在同一 ``BEGIN IMMEDIATE`` 事务内完成，
    任一校验失败整体回滚，原分配不变；成功后分配集合变化，既有计划的
    状态指纹自然失效，快照差异表现为一次移除加一次新增。
    """
    old_net = parse_or_400(body.cidr)
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        pool_net = logic.parse_network(pool["cidr"])
        max_len = logic.max_prefixlen(pool["version"])
        if not pool_net.prefixlen <= body.new_prefix_len <= max_len:
            raise HTTPException(
                status_code=400,
                detail=f"new_prefix_len {body.new_prefix_len} 非法："
                       f"必须在 [{pool_net.prefixlen}, {max_len}] 之间",
            )
        row = conn.execute(
            "SELECT id, prefix_len FROM allocations WHERE pool_id = ? AND cidr = ?",
            (pool["id"], str(old_net)),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"地址池 {name!r} 中不存在已分配网段 {old_net}",
            )

        if body.new_prefix_len > row["prefix_len"]:
            # 缩小：保留原网络地址，尾部空间随更新落库即释放。
            new_net = logic.shrink_to(old_net, body.new_prefix_len)
        elif body.new_prefix_len < row["prefix_len"]:
            # 扩展：唯一包含原网段的 new_prefix_len 前缀。
            new_net = logic.grow_to(old_net, body.new_prefix_len)
            if not new_net.subnet_of(pool_net):
                raise HTTPException(
                    status_code=400,
                    detail=f"扩展后的目标网段 {new_net} 超出地址池 {pool_net} 范围",
                )
            for occ in load_occupied(conn, pool["id"]):
                if occ == old_net:
                    continue  # 跳过被调整的原分配自身
                if new_net.overlaps(occ):
                    raise HTTPException(
                        status_code=409,
                        detail=f"扩展后的目标网段 {new_net} 与已登记网段 {occ} 重叠",
                    )
        else:
            new_net = old_net  # 前缀长度不变：幂等空操作

        conn.execute(
            "UPDATE allocations SET cidr = ?, prefix_len = ? WHERE id = ?",
            (str(new_net), new_net.prefixlen, row["id"]),
        )
        return {"pool": name, "old_cidr": str(old_net), "new_cidr": str(new_net)}


# ---------------------------------------------------------------------------
# 池快照与差异比较
# ---------------------------------------------------------------------------

def load_current_networks(
    conn: sqlite3.Connection, pool_id: int
) -> tuple[list[logic.Network], list[logic.Network]]:
    """读取池当前的分配项与不可用段，各自按 CIDR（网络地址升序）排序。"""
    by_addr = lambda n: int(n.network_address)  # noqa: E731
    allocations = sorted(
        (logic.parse_network(r["cidr"]) for r in conn.execute(
            "SELECT cidr FROM allocations WHERE pool_id = ?", (pool_id,)
        )),
        key=by_addr,
    )
    unavailable = sorted(
        (logic.parse_network(r["cidr"]) for r in conn.execute(
            "SELECT cidr FROM unavailable WHERE pool_id = ?", (pool_id,)
        )),
        key=by_addr,
    )
    return allocations, unavailable


def load_snapshot_networks(
    conn: sqlite3.Connection, snapshot_id: str
) -> tuple[list[logic.Network], list[logic.Network]]:
    """按创建时保存的 CIDR 顺序读取快照中的分配项与不可用段。"""
    allocations = [
        logic.parse_network(r["cidr"])
        for r in conn.execute(
            "SELECT cidr FROM snapshot_allocations WHERE snapshot_id = ? "
            "ORDER BY position",
            (snapshot_id,),
        )
    ]
    unavailable = [
        logic.parse_network(r["cidr"])
        for r in conn.execute(
            "SELECT cidr FROM snapshot_unavailable WHERE snapshot_id = ? "
            "ORDER BY position",
            (snapshot_id,),
        )
    ]
    return allocations, unavailable


def remaining_addresses(
    pool_net: logic.Network,
    allocations: list[logic.Network],
    unavailable: list[logic.Network],
) -> int:
    """给定占用集合（分配项 + 不可用段）下池内的剩余地址数。"""
    free = logic.free_intervals(pool_net, unavailable + allocations)
    return sum(b - a + 1 for a, b in free)


@app.post("/pools/{name}/snapshots", status_code=201)
def create_snapshot(name: str) -> dict:
    """为地址池创建不可变快照。

    在单个 ``BEGIN IMMEDIATE`` 事务内分别读取并按 CIDR 排序保存当前的
    allocations 与 unavailable，快照归属路径中的地址池；快照写入后不提供
    任何修改或删除入口，删除地址池时由外键级联删除。
    """
    with write_tx() as conn:
        pool = get_pool_or_404(conn, name)
        allocations, unavailable = load_current_networks(conn, pool["id"])

        snapshot_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO snapshots (id, pool_id) VALUES (?, ?)",
            (snapshot_id, pool["id"]),
        )
        conn.executemany(
            "INSERT INTO snapshot_allocations (snapshot_id, position, cidr, prefix_len) "
            "VALUES (?, ?, ?, ?)",
            [
                (snapshot_id, i, str(net), net.prefixlen)
                for i, net in enumerate(allocations)
            ],
        )
        conn.executemany(
            "INSERT INTO snapshot_unavailable (snapshot_id, position, cidr) "
            "VALUES (?, ?, ?)",
            [(snapshot_id, i, str(net)) for i, net in enumerate(unavailable)],
        )
        row = conn.execute(
            "SELECT created_at FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return {
            "pool": name,
            "snapshot_id": snapshot_id,
            "created_at": row["created_at"],
            "allocations": [str(n) for n in allocations],
            "unavailable": [str(n) for n in unavailable],
        }


@app.get("/pools/{name}/snapshots/{snapshot_id}/diff")
def snapshot_diff(name: str, snapshot_id: str) -> dict:
    """比较快照与地址池当前状态的差异。

    全部读取在单个读事务内完成，保证比较基于同一时刻的当前状态。
    ``added`` 为相对快照新增、``removed`` 为相对快照消失的 CIDR；
    同一 CIDR 由分配转为不可用（或反向）时会同时出现在两侧。
    地址数增量为有符号数：``当前 − 快照``。
    """
    with read_tx() as conn:
        pool = get_pool_or_404(conn, name)
        snapshot = conn.execute(
            "SELECT id, created_at FROM snapshots WHERE id = ? AND pool_id = ?",
            (snapshot_id, pool["id"]),
        ).fetchone()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail=f"地址池 {name!r} 中不存在快照 {snapshot_id!r}",
            )

        pool_net = logic.parse_network(pool["cidr"])
        snap_alloc, snap_unavail = load_snapshot_networks(conn, snapshot_id)
        cur_alloc, cur_unavail = load_current_networks(conn, pool["id"])

        snap_alloc_set, cur_alloc_set = set(snap_alloc), set(cur_alloc)
        snap_unavail_set, cur_unavail_set = set(snap_unavail), set(cur_unavail)

        by_addr = lambda n: int(n.network_address)  # noqa: E731

        snap_allocated = sum(net.num_addresses for net in snap_alloc)
        cur_allocated = sum(net.num_addresses for net in cur_alloc)
        snap_remaining = remaining_addresses(pool_net, snap_alloc, snap_unavail)
        cur_remaining = remaining_addresses(pool_net, cur_alloc, cur_unavail)

        def fmt(networks: set[logic.Network]) -> list[str]:
            return [str(n) for n in sorted(networks, key=by_addr)]

        return {
            "pool": name,
            "snapshot_id": snapshot_id,
            "snapshot_created_at": snapshot["created_at"],
            "changes": {
                "allocations": {
                    "added": fmt(cur_alloc_set - snap_alloc_set),
                    "removed": fmt(snap_alloc_set - cur_alloc_set),
                },
                "unavailable": {
                    "added": fmt(cur_unavail_set - snap_unavail_set),
                    "removed": fmt(snap_unavail_set - cur_unavail_set),
                },
            },
            "allocated_addresses": {
                "snapshot": snap_allocated,
                "current": cur_allocated,
                "delta": cur_allocated - snap_allocated,
            },
            "remaining_addresses": {
                "snapshot": snap_remaining,
                "current": cur_remaining,
                "delta": cur_remaining - snap_remaining,
            },
        }
