"""分配网段原子调整（resize）测试。

缩小保留原网络地址并释放尾部空间；扩展改为唯一包含原网段的目标前缀，
仅当目标在池内且新增范围不与其他分配或不可用段重叠才成功；校验与更新
在同一 BEGIN IMMEDIATE 事务内完成，失败不改变原分配；成功后既有计划
指纹失效，快照差异表现为一次移除加一次新增。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


def allocate(client, prefix_len, name="eng", count=1):
    r = client.post(
        f"/pools/{name}/allocations",
        json={"requests": [{"prefix_len": prefix_len, "count": count}]},
    )
    assert r.status_code == 201, r.text
    return r.json()["allocated"]


def resize(client, cidr, new_prefix_len, name="eng"):
    return client.post(
        f"/pools/{name}/allocations/resize",
        json={"cidr": cidr, "new_prefix_len": new_prefix_len},
    )


def allocated_cidrs(client, name="eng"):
    return [a["cidr"] for a in client.get(f"/pools/{name}").json()["allocations"]]


# ---------------------------------------------------------------------------
# 缩小
# ---------------------------------------------------------------------------

def test_shrink_keeps_network_address_and_releases_tail(client):
    make_pool(client)
    allocate(client, 25)  # 10.0.0.0/25

    r = resize(client, "10.0.0.0/25", 26)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "pool": "eng",
        "old_cidr": "10.0.0.0/25",
        "new_cidr": "10.0.0.0/26",
    }

    st = client.get("/pools/eng").json()
    assert [a["cidr"] for a in st["allocations"]] == ["10.0.0.0/26"]
    # 尾部 10.0.0.64/26 已释放，与后半段合并进剩余空间
    assert st["remaining"] == ["10.0.0.64/26", "10.0.0.128/25"]
    assert st["remaining_addresses"] == 192
    # 释放的尾部可以立即再分配
    assert allocate(client, 26) == ["10.0.0.64/26"]


def test_shrink_off_network_start_keeps_its_own_address(client):
    make_pool(client)
    allocate(client, 25, count=2)  # 10.0.0.0/25, 10.0.0.128/25

    r = resize(client, "10.0.0.128/25", 28)
    assert r.status_code == 200, r.text
    assert r.json()["new_cidr"] == "10.0.0.128/28"
    assert allocated_cidrs(client) == ["10.0.0.0/25", "10.0.0.128/28"]


# ---------------------------------------------------------------------------
# 扩展
# ---------------------------------------------------------------------------

def test_grow_to_unique_containing_prefix(client):
    make_pool(client)
    # 引导分配到 10.0.0.128/26，其唯一包含它的 /25 为 10.0.0.128/25
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"})
    allocate(client, 26)

    r = resize(client, "10.0.0.128/26", 25)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "pool": "eng",
        "old_cidr": "10.0.0.128/26",
        "new_cidr": "10.0.0.128/25",
    }
    assert allocated_cidrs(client) == ["10.0.0.128/25"]


def test_grow_to_pool_boundary(client):
    make_pool(client)
    allocate(client, 25)  # 10.0.0.0/25

    r = resize(client, "10.0.0.0/25", 24)
    assert r.status_code == 200, r.text
    assert r.json()["new_cidr"] == "10.0.0.0/24"
    st = client.get("/pools/eng").json()
    assert st["remaining"] == []
    assert st["remaining_addresses"] == 0


def test_grow_blocked_by_other_allocation(client):
    make_pool(client)
    allocate(client, 25, count=2)  # 10.0.0.0/25, 10.0.0.128/25

    r = resize(client, "10.0.0.0/25", 24)  # 目标 10.0.0.0/24 与后者重叠
    assert r.status_code == 409, r.text
    # 失败不改变原分配
    assert allocated_cidrs(client) == ["10.0.0.0/25", "10.0.0.128/25"]


def test_grow_blocked_by_unavailable(client):
    make_pool(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.128/25"})
    allocate(client, 25)  # 10.0.0.0/25

    r = resize(client, "10.0.0.0/25", 24)  # 新增范围 10.0.0.128/25 不可用
    assert r.status_code == 409, r.text
    assert allocated_cidrs(client) == ["10.0.0.0/25"]


def test_grow_out_of_pool_rejected(client):
    # 池 10.0.0.0/25：唯一包含 10.0.0.0/26 的 /24 越出池范围。
    # new_prefix_len 受池前缀下界约束，越界目标只能以更小的前缀出现，
    # 因此先命中前缀长度校验（400）。
    make_pool(client, cidr="10.0.0.0/25")
    allocate(client, 26)  # 10.0.0.0/26
    assert resize(client, "10.0.0.0/26", 24).status_code == 400
    assert allocated_cidrs(client) == ["10.0.0.0/26"]


# ---------------------------------------------------------------------------
# 校验与幂等
# ---------------------------------------------------------------------------

def test_resize_same_prefix_len_is_noop(client):
    make_pool(client)
    allocate(client, 26)  # 10.0.0.0/26

    r = resize(client, "10.0.0.0/26", 26)
    assert r.status_code == 200, r.text
    assert r.json()["old_cidr"] == r.json()["new_cidr"] == "10.0.0.0/26"
    assert allocated_cidrs(client) == ["10.0.0.0/26"]


def test_resize_unknown_allocation_404(client):
    make_pool(client)
    assert resize(client, "10.0.0.0/26", 27).status_code == 404
    # 已释放的网段同样视为不存在
    allocate(client, 26)
    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/26"})
    assert resize(client, "10.0.0.0/26", 27).status_code == 404


def test_resize_unknown_pool_404(client):
    assert resize(client, "10.0.0.0/26", 27, name="nope").status_code == 404


def test_resize_invalid_cidr_400(client):
    make_pool(client)
    assert resize(client, "not-a-cidr", 26).status_code == 400
    assert resize(client, "10.0.0.1/24", 26).status_code == 400  # 主机位非零


def test_resize_prefix_len_bounds(client):
    make_pool(client)
    allocate(client, 26)
    assert resize(client, "10.0.0.0/26", 23).status_code == 400  # 小于池前缀
    assert resize(client, "10.0.0.0/26", 33).status_code == 400  # 超过 IPv4 上限
    assert resize(client, "10.0.0.0/26", 32).status_code == 200
    assert allocated_cidrs(client) == ["10.0.0.0/32"]


def test_resize_missing_fields_422(client):
    make_pool(client)
    assert client.post("/pools/eng/allocations/resize", json={"cidr": "10.0.0.0/26"}).status_code == 422
    assert client.post(
        "/pools/eng/allocations/resize", json={"new_prefix_len": 26}
    ).status_code == 422


def test_failed_resize_leaves_pool_untouched(client):
    make_pool(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.128/25"})
    allocate(client, 25)  # 10.0.0.0/25
    before = client.get("/pools/eng").json()

    assert resize(client, "10.0.0.0/25", 24).status_code == 409  # 与不可用段重叠
    assert resize(client, "10.0.0.0/25", 33).status_code == 400
    assert resize(client, "10.0.0.0/25", 23).status_code == 400
    assert client.get("/pools/eng").json() == before


# ---------------------------------------------------------------------------
# 与计划 / 快照的联动
# ---------------------------------------------------------------------------

def test_resize_invalidates_pending_plan(client):
    make_pool(client)
    allocate(client, 26)  # 10.0.0.0/26
    plan = client.post("/pools/eng/plans", json={"requests": [{"prefix_len": 26}]}).json()

    r = resize(client, "10.0.0.0/26", 27)
    assert r.status_code == 200, r.text

    # 分配集合已变化，计划指纹自然失效
    r = client.post(f"/pools/eng/plans/{plan['plan_id']}/commit")
    assert r.status_code == 409, r.text


def test_resize_snapshot_diff_shows_one_remove_and_one_add(client):
    make_pool(client)
    allocate(client, 25)  # 10.0.0.0/25
    snap = client.post("/pools/eng/snapshots").json()

    r = resize(client, "10.0.0.0/25", 24)
    assert r.status_code == 200, r.text

    body = client.get(f"/pools/eng/snapshots/{snap['snapshot_id']}/diff").json()
    assert body["changes"]["allocations"]["removed"] == ["10.0.0.0/25"]
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/24"]
    assert body["changes"]["unavailable"] == {"added": [], "removed": []}
    assert body["allocated_addresses"] == {"snapshot": 128, "current": 256, "delta": 128}
    assert body["remaining_addresses"] == {"snapshot": 128, "current": 0, "delta": -128}


def test_shrink_snapshot_diff_deltas(client):
    make_pool(client)
    allocate(client, 25)  # 10.0.0.0/25
    snap = client.post("/pools/eng/snapshots").json()

    assert resize(client, "10.0.0.0/25", 26).status_code == 200

    body = client.get(f"/pools/eng/snapshots/{snap['snapshot_id']}/diff").json()
    assert body["changes"]["allocations"]["removed"] == ["10.0.0.0/25"]
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/26"]
    assert body["allocated_addresses"]["delta"] == -64
    assert body["remaining_addresses"]["delta"] == 64


# ---------------------------------------------------------------------------
# IPv6
# ---------------------------------------------------------------------------

def test_resize_ipv6(client):
    make_pool(client, name="v6", cidr="2001:db8::/32")
    allocate(client, 48, name="v6")  # 2001:db8::/48

    r = resize(client, "2001:db8::/48", 64, name="v6")
    assert r.status_code == 200, r.text
    assert r.json()["new_cidr"] == "2001:db8::/64"

    r = resize(client, "2001:db8::/64", 33, name="v6")
    assert r.status_code == 200, r.text
    assert r.json()["new_cidr"] == "2001:db8::/33"
    assert allocated_cidrs(client, name="v6") == ["2001:db8::/33"]


def test_resize_allocation_scoped_to_pool(client):
    make_pool(client)
    make_pool(client, name="other", cidr="10.1.0.0/24")
    allocate(client, 26)  # eng: 10.0.0.0/26

    # 同 CIDR 在别的池不存在
    assert resize(client, "10.0.0.0/26", 27, name="other").status_code == 404
    assert resize(client, "10.0.0.0/26", 27, name="eng").status_code == 200
