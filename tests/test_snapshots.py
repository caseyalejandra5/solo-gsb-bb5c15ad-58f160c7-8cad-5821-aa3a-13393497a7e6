"""池快照与差异比较测试。

快照在 BEGIN IMMEDIATE 事务内保存按 CIDR 排序的 allocations / unavailable；
diff 在单个读事务中比较当前状态，CIDR 由分配转为不可用时同时计入
removed 与 added；快照归属路径池，删除池级联删除。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import db_path
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIDR_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_pool(client, name="eng", cidr="10.0.0.0/24"):
    r = client.post("/pools", json={"name": name, "cidr": cidr})
    assert r.status_code == 201, r.text
    return r


def make_snapshot(client, name="eng"):
    r = client.post(f"/pools/{name}/snapshots")
    assert r.status_code == 201, r.text
    return r.json()


def diff(client, snapshot, name="eng"):
    return client.get(f"/pools/{name}/snapshots/{snapshot['snapshot_id']}/diff")


# ---------------------------------------------------------------------------
# 创建快照
# ---------------------------------------------------------------------------

def test_snapshot_empty_pool(client):
    make_pool(client)
    snap = make_snapshot(client)
    assert snap["pool"] == "eng"
    assert len(snap["snapshot_id"]) == 32  # uuid4().hex
    assert snap["created_at"]
    assert snap["allocations"] == []
    assert snap["unavailable"] == []

    r = diff(client, snap)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changes"] == {
        "allocations": {"added": [], "removed": []},
        "unavailable": {"added": [], "removed": []},
    }
    assert body["allocated_addresses"] == {"snapshot": 0, "current": 0, "delta": 0}
    assert body["remaining_addresses"] == {"snapshot": 256, "current": 256, "delta": 0}


def test_snapshot_records_current_state_sorted_by_cidr(client):
    make_pool(client)
    # 逆地址序登记不可用段、做分配，快照仍必须按 CIDR（网络地址）升序返回
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.128/25"})
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/26"})
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 30}]})
    # 剩余首个对齐 /30：10.0.0.64/30（0/26 被不可用占用，128/25 也不可用）
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 30}]})

    snap = make_snapshot(client)
    assert snap["unavailable"] == ["10.0.0.0/26", "10.0.0.128/25"]
    # 两次分配：10.0.0.64/30 与 10.0.0.68/30（0/26、128/25 均不可用）
    assert snap["allocations"] == ["10.0.0.64/30", "10.0.0.68/30"]

    # 快照保存的排序确实落库（直接按 position 读取）
    conn = sqlite3.connect(db_path())
    rows = conn.execute(
        "SELECT position, cidr FROM snapshot_unavailable ORDER BY position"
    ).fetchall()
    conn.close()
    assert [r[1] for r in rows] == ["10.0.0.0/26", "10.0.0.128/25"]


def test_snapshot_does_not_change_pool_state(client):
    make_pool(client)
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})
    before = client.get("/pools/eng").json()
    make_snapshot(client)
    make_snapshot(client)
    after = client.get("/pools/eng").json()
    assert after == before


def test_snapshot_is_immutable(client):
    """状态变化不影响已创建的快照内容。"""
    make_pool(client)
    snap = make_snapshot(client)

    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"})
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    saved_alloc = [
        r["cidr"]
        for r in conn.execute(
            "SELECT cidr FROM snapshot_allocations WHERE snapshot_id = ? "
            "ORDER BY position",
            (snap["snapshot_id"],),
        )
    ]
    saved_unavail = [
        r["cidr"]
        for r in conn.execute(
            "SELECT cidr FROM snapshot_unavailable WHERE snapshot_id = ? "
            "ORDER BY position",
            (snap["snapshot_id"],),
        )
    ]
    conn.close()
    assert saved_alloc == []
    assert saved_unavail == []


# ---------------------------------------------------------------------------
# 差异比较
# ---------------------------------------------------------------------------

def test_diff_added_and_removed(client):
    make_pool(client)
    r = client.post(
        "/pools/eng/allocations", json={"requests": [{"prefix_len": 28, "count": 3}]}
    )
    assert r.status_code == 201
    snap = make_snapshot(client)

    # 释放前两个、再新分配一个 /27
    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/28"})
    client.post("/pools/eng/release", json={"cidr": "10.0.0.16/28"})
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 27}]})

    body = diff(client, snap).json()
    assert body["changes"]["allocations"]["removed"] == [
        "10.0.0.0/28",
        "10.0.0.16/28",
    ]
    # 释放后 0-31 空闲，首个对齐 /27 为 10.0.0.0/27
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/27"]
    assert body["changes"]["unavailable"] == {"added": [], "removed": []}
    # 已分配地址：快照 3*16=48；当前 16（32/28）+ 32（0/27）= 48
    assert body["allocated_addresses"] == {"snapshot": 48, "current": 48, "delta": 0}
    # 剩余地址：快照 256-48=208；当前 256-48=208
    assert body["remaining_addresses"] == {
        "snapshot": 208,
        "current": 208,
        "delta": 0,
    }


def test_diff_signed_deltas(client):
    make_pool(client)
    snap = make_snapshot(client)  # 空池
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.128/26"})

    body = diff(client, snap).json()
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/26"]
    assert body["changes"]["unavailable"]["added"] == ["10.0.0.128/26"]
    assert body["allocated_addresses"]["delta"] == 64
    assert body["remaining_addresses"]["delta"] == -128
    assert body["allocated_addresses"] == {"snapshot": 0, "current": 64, "delta": 64}
    assert body["remaining_addresses"] == {
        "snapshot": 256,
        "current": 128,
        "delta": -128,
    }

    # 释放后反向增量
    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/26"})
    body = diff(client, snap).json()
    assert body["allocated_addresses"]["delta"] == 0
    assert body["remaining_addresses"]["delta"] == -64  # 仅剩不可用段占用 64


def test_diff_cidr_moving_from_allocation_to_unavailable(client):
    """同一 CIDR 由分配转为不可用：allocations.removed 与 unavailable.added
    同时记录该 CIDR；已分配地址 -64，剩余地址不变。"""
    make_pool(client)
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    snap = make_snapshot(client)

    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/26"})
    r = client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/26"})
    assert r.status_code == 201

    body = diff(client, snap).json()
    assert body["changes"]["allocations"]["removed"] == ["10.0.0.0/26"]
    assert body["changes"]["allocations"]["added"] == []
    assert body["changes"]["unavailable"]["added"] == ["10.0.0.0/26"]
    assert body["changes"]["unavailable"]["removed"] == []
    assert body["allocated_addresses"]["delta"] == -64
    assert body["remaining_addresses"]["delta"] == 0


def test_diff_cidr_moving_from_unavailable_to_allocation(client):
    """反向：同一 CIDR 由不可用转为分配（释放占用需要直接改库模拟，
    因为 API 没有删除不可用段的入口）。"""
    make_pool(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/26"})
    snap = make_snapshot(client)

    conn = sqlite3.connect(db_path())
    conn.execute("DELETE FROM unavailable WHERE cidr = '10.0.0.0/26'")
    conn.commit()
    conn.close()

    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    assert r.json()["allocated"] == ["10.0.0.0/26"]

    body = diff(client, snap).json()
    assert body["changes"]["unavailable"]["removed"] == ["10.0.0.0/26"]
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/26"]
    assert body["allocated_addresses"]["delta"] == 64
    assert body["remaining_addresses"]["delta"] == 0


def test_diff_ipv6_pool(client):
    make_pool(client, name="v6", cidr="2001:db8::/32")
    snap = make_snapshot(client, name="v6")
    client.post("/pools/v6/allocations", json={"requests": [{"prefix_len": 48}]})

    body = diff(client, snap, name="v6").json()
    assert body["changes"]["allocations"]["added"] == ["2001:db8::/48"]
    size = 2 ** (128 - 48)
    assert body["allocated_addresses"]["delta"] == size
    assert body["remaining_addresses"]["delta"] == -size
    assert body["remaining_addresses"]["snapshot"] == 2 ** (128 - 32)


def test_multiple_snapshots_compare_independently(client):
    make_pool(client)
    snap0 = make_snapshot(client)  # 空池
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})
    snap1 = make_snapshot(client)  # 含 10.0.0.0/28
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})

    body0 = diff(client, snap0).json()
    body1 = diff(client, snap1).json()
    assert body0["changes"]["allocations"]["added"] == [
        "10.0.0.0/28",
        "10.0.0.16/28",
    ]
    assert body1["changes"]["allocations"]["added"] == ["10.0.0.16/28"]
    assert body1["changes"]["allocations"]["removed"] == []


# ---------------------------------------------------------------------------
# 404 与归属 / 级联
# ---------------------------------------------------------------------------

def test_snapshot_unknown_pool_404(client):
    assert client.post("/pools/nope/snapshots").status_code == 404
    assert client.get("/pools/nope/snapshots/abc/diff").status_code == 404


def test_diff_unknown_snapshot_404(client):
    make_pool(client)
    assert client.get("/pools/eng/snapshots/does-not-exist/diff").status_code == 404


def test_snapshot_scoped_to_path_pool(client):
    """快照必须归属路径池：别池的 snapshot_id 在本池 diff 返回 404。"""
    make_pool(client)
    make_pool(client, name="other", cidr="10.1.0.0/24")
    snap = make_snapshot(client, name="other")

    assert diff(client, snap, name="eng").status_code == 404
    assert diff(client, snap, name="other").status_code == 200


def test_snapshot_cascade_deleted_with_pool(client):
    make_pool(client)
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})
    snap = make_snapshot(client)
    assert diff(client, snap).status_code == 200

    assert client.delete("/pools/eng").status_code == 204
    # 池已不存在
    assert client.get("/pools/eng/snapshots/{}/diff".format(snap["snapshot_id"])).status_code == 404

    # 快照行及其明细随池级联删除
    conn = sqlite3.connect(db_path())
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM snapshot_allocations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM snapshot_unavailable").fetchone()[0] == 0
    conn.close()

    # 同名新池不继承旧快照
    make_pool(client)
    assert diff(client, snap).status_code == 404
