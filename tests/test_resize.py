"""分配网段原子调整（resize）测试。

缩小保留原网络地址并释放尾部空间；扩展取唯一包含原网段的父网，仅当父网仍在
池内且新增范围不与其他分配 / 不可用段重叠时成功。校验与更新在同一
BEGIN IMMEDIATE 事务内完成，失败不改变原分配。
"""
from __future__ import annotations

import ipaddress

import pytest
from fastapi.testclient import TestClient

from app import logic
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


def alloc(client, prefix_len, name="eng"):
    r = client.post(
        f"/pools/{name}/allocations", json={"requests": [{"prefix_len": prefix_len}]}
    )
    assert r.status_code == 201, r.text
    return r.json()["allocated"][0]


def resize(client, cidr, new_prefix_len, name="eng"):
    return client.post(
        f"/pools/{name}/allocations/resize",
        json={"cidr": cidr, "new_prefix_len": new_prefix_len},
    )


def allocation_cidrs(client, name="eng"):
    st = client.get(f"/pools/{name}").json()
    return [a["cidr"] for a in st["allocations"]]


# ---------------------------------------------------------------------------
# 目标网段计算（logic.resize_network）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cidr,new_len,expected",
    [
        # 缩小：网络地址保持不变
        ("10.0.0.0/26", 28, "10.0.0.0/28"),
        ("10.0.0.128/25", 26, "10.0.0.128/26"),
        ("2001:db8::/48", 50, "2001:db8::/50"),
        # 扩展：唯一包含原网段的父网（网络地址可能改变）
        ("10.0.0.64/26", 25, "10.0.0.0/25"),
        ("10.0.0.192/26", 24, "10.0.0.0/24"),
        ("2001:db8:1::/48", 47, "2001:db8::/47"),
    ],
)
def test_resize_network_target(cidr, new_len, expected):
    net = logic.parse_network(cidr)
    got = logic.resize_network(net, new_len, net.version)
    assert str(got) == expected
    if new_len >= net.prefixlen:
        assert got.network_address == net.network_address


# ---------------------------------------------------------------------------
# 缩小
# ---------------------------------------------------------------------------

def test_shrink_keeps_network_address_and_releases_tail(client):
    make_pool(client)
    cidr = alloc(client, 26)  # 10.0.0.0/26
    r = resize(client, cidr, 28)
    assert r.status_code == 200, r.text
    assert r.json() == {"pool": "eng", "old_cidr": "10.0.0.0/26", "new_cidr": "10.0.0.0/28"}

    st = client.get("/pools/eng").json()
    assert allocation_cidrs(client) == ["10.0.0.0/28"]
    assert st["allocated_addresses"] == 16
    # 尾部 10.0.0.16/28 与 10.0.0.64/26 等空间被释放并重新可分配
    assert "10.0.0.16/28" in st["remaining"]
    # 释放出的尾部空间可以立即重新分配
    assert alloc(client, 28) == "10.0.0.16/28"


def test_shrink_to_host_route(client):
    make_pool(client)
    cidr = alloc(client, 30)
    r = resize(client, cidr, 32)
    assert r.status_code == 200
    assert r.json()["new_cidr"] == "10.0.0.0/32"


# ---------------------------------------------------------------------------
# 扩展
# ---------------------------------------------------------------------------

def test_expand_uses_unique_containing_supernet(client):
    make_pool(client)
    cidr = alloc(client, 26)  # 10.0.0.0/26
    # 0/26 扩展为 /25：唯一包含它的父网是 10.0.0.0/25
    r = resize(client, cidr, 25)
    assert r.status_code == 200, r.text
    assert r.json() == {"pool": "eng", "old_cidr": "10.0.0.0/26", "new_cidr": "10.0.0.0/25"}
    assert allocation_cidrs(client) == ["10.0.0.0/25"]
    assert client.get("/pools/eng").json()["allocated_addresses"] == 128


def test_expand_blocked_by_other_allocation_leaves_original(client):
    make_pool(client)
    cidr = alloc(client, 26)   # 10.0.0.0/26
    other = alloc(client, 26)  # 10.0.0.64/26
    # 0/26 的唯一 /25 父网是 0/25，与 64/26 重叠
    r = resize(client, cidr, 25)
    assert r.status_code == 409
    assert allocation_cidrs(client) == ["10.0.0.0/26", "10.0.0.64/26"]
    # 另一个分配完好无损
    assert other == "10.0.0.64/26"


def test_expand_blocked_by_unavailable_segment(client):
    make_pool(client)
    cidr = alloc(client, 26)  # 10.0.0.0/26
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.64/26"})
    r = resize(client, cidr, 25)
    assert r.status_code == 409
    assert allocation_cidrs(client) == ["10.0.0.0/26"]


def test_expand_prefix_below_pool_prefix_rejected(client):
    # 池本身是 10.0.0.128/25：new_prefix_len 小于池前缀 25 时直接 400
    make_pool(client, cidr="10.0.0.128/25")
    cidr = alloc(client, 26)  # 10.0.0.128/26
    r = resize(client, cidr, 24)
    assert r.status_code == 400
    assert allocation_cidrs(client) == ["10.0.0.128/26"]


def test_expand_to_pool_prefix_succeeds(client):
    make_pool(client)
    cidr = alloc(client, 26)  # 10.0.0.0/26
    r = resize(client, cidr, 24)  # 扩展到整个池
    assert r.status_code == 200
    assert r.json()["new_cidr"] == "10.0.0.0/24"
    assert client.get("/pools/eng").json()["remaining_addresses"] == 0


def test_expand_does_not_check_range_already_owned(client):
    """扩展时新增范围不能与他人重叠，但原子网自身不算冲突。"""
    make_pool(client)
    cidr = alloc(client, 28)  # 10.0.0.0/28
    r = resize(client, cidr, 26)  # 0/26 内除 0/28 外无其他占用
    assert r.status_code == 200
    assert r.json()["new_cidr"] == "10.0.0.0/26"


# ---------------------------------------------------------------------------
# 往返与失败原子性
# ---------------------------------------------------------------------------

def test_shrink_then_expand_roundtrip(client):
    make_pool(client)
    cidr = alloc(client, 26)
    assert resize(client, cidr, 28).json()["new_cidr"] == "10.0.0.0/28"
    r = resize(client, "10.0.0.0/28", 26)
    assert r.status_code == 200
    assert r.json()["old_cidr"] == "10.0.0.0/28"
    assert r.json()["new_cidr"] == "10.0.0.0/26"
    assert allocation_cidrs(client) == ["10.0.0.0/26"]


def test_failed_resize_keeps_created_at_and_row(client):
    """失败只回滚 UPDATE，原行（含 created_at）不变。"""
    make_pool(client)
    cidr = alloc(client, 26)
    before = client.get("/pools/eng").json()["allocations"][0]
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.64/26"})
    assert resize(client, cidr, 25).status_code == 409
    after = client.get("/pools/eng").json()["allocations"][0]
    assert after == before


# ---------------------------------------------------------------------------
# 校验错误
# ---------------------------------------------------------------------------

def test_resize_unknown_pool_and_allocation(client):
    make_pool(client)
    assert resize(client, "10.0.0.0/26", 28, name="nope").status_code == 404
    assert resize(client, "10.0.0.0/26", 28).status_code == 404


def test_resize_invalid_cidr(client):
    make_pool(client)
    r = client.post(
        "/pools/eng/allocations/resize",
        json={"cidr": "10.0.0.1/26", "new_prefix_len": 28},
    )
    assert r.status_code == 400
    r = client.post(
        "/pools/eng/allocations/resize",
        json={"cidr": "not-a-cidr", "new_prefix_len": 28},
    )
    assert r.status_code == 400


def test_resize_prefix_len_bounds(client):
    make_pool(client)
    cidr = alloc(client, 26)
    assert resize(client, cidr, 23).status_code == 400  # 小于池前缀
    assert resize(client, cidr, 33).status_code == 400
    # 池不存在时优先 404
    assert resize(client, cidr, 23, name="nope").status_code == 404


def test_resize_same_prefix_len(client):
    make_pool(client)
    cidr = alloc(client, 26)
    assert resize(client, cidr, 26).status_code == 400
    assert allocation_cidrs(client) == ["10.0.0.0/26"]


def test_resize_version_mismatch(client):
    make_pool(client)
    # IPv6 网段发到 IPv4 池
    r = resize(client, "2001:db8::/48", 50)
    assert r.status_code == 400
    assert allocation_cidrs(client) == []


# ---------------------------------------------------------------------------
# IPv6
# ---------------------------------------------------------------------------

def test_resize_ipv6(client):
    make_pool(client, name="v6", cidr="2001:db8::/32")
    cidr = alloc(client, 48, name="v6")  # 2001:db8::/48
    r = resize(client, cidr, 50, name="v6")
    assert r.status_code == 200
    assert r.json() == {"pool": "v6", "old_cidr": "2001:db8::/48",
                        "new_cidr": "2001:db8::/50"}

    r = resize(client, "2001:db8::/50", 47, name="v6")
    assert r.status_code == 200
    assert r.json()["new_cidr"] == "2001:db8::/47"
    st = client.get("/pools/v6").json()
    assert [a["cidr"] for a in st["allocations"]] == ["2001:db8::/47"]
    assert st["allocated_addresses"] == 2 ** (128 - 47)


# ---------------------------------------------------------------------------
# 计划指纹失效
# ---------------------------------------------------------------------------

def test_resize_invalidates_existing_plan(client):
    make_pool(client)
    plan_cidr = alloc(client, 28)  # 10.0.0.0/28
    # 计划预选 10.0.0.16/28
    r = client.post("/pools/eng/plans", json={"requests": [{"prefix_len": 28}]})
    plan = r.json()
    assert plan["cidrs"] == ["10.0.0.16/28"]

    # 调整既有分配：池状态指纹变化
    assert resize(client, plan_cidr, 29).status_code == 200
    commit = client.post(f"/pools/eng/plans/{plan['plan_id']}/commit")
    assert commit.status_code == 409
    # 计划未被提交
    assert allocation_cidrs(client) == ["10.0.0.0/29"]


# ---------------------------------------------------------------------------
# 快照差异：一次移除 + 一次新增
# ---------------------------------------------------------------------------

def test_snapshot_diff_shows_one_remove_and_one_add(client):
    make_pool(client)
    cidr = alloc(client, 26)
    snap = client.post("/pools/eng/snapshots").json()

    r = resize(client, cidr, 25)
    assert r.status_code == 200

    body = client.get(
        f"/pools/eng/snapshots/{snap['snapshot_id']}/diff"
    ).json()
    assert body["changes"]["allocations"]["removed"] == ["10.0.0.0/26"]
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/25"]
    assert body["changes"]["unavailable"] == {"added": [], "removed": []}
    # 扩展多占 64 个地址
    assert body["allocated_addresses"] == {"snapshot": 64, "current": 128, "delta": 64}
    assert body["remaining_addresses"] == {"snapshot": 192, "current": 128, "delta": -64}


def test_snapshot_diff_after_shrink(client):
    make_pool(client)
    cidr = alloc(client, 25)  # 10.0.0.0/25
    snap = client.post("/pools/eng/snapshots").json()

    assert resize(client, cidr, 26).status_code == 200

    body = client.get(
        f"/pools/eng/snapshots/{snap['snapshot_id']}/diff"
    ).json()
    assert body["changes"]["allocations"]["removed"] == ["10.0.0.0/25"]
    assert body["changes"]["allocations"]["added"] == ["10.0.0.0/26"]
    assert body["allocated_addresses"]["delta"] == -64
    assert body["remaining_addresses"]["delta"] == 64


# ---------------------------------------------------------------------------
# 调整后地址仍不与任何网段重叠
# ---------------------------------------------------------------------------

def test_new_allocation_after_expand_never_overlaps(client):
    make_pool(client)
    cidr = alloc(client, 28)  # 10.0.0.0/28
    assert resize(client, cidr, 26).status_code == 200  # 10.0.0.0/26
    # 池里已有 0/26，下一个 /26 必须是 64/26
    assert alloc(client, 26) == "10.0.0.64/26"
    nets = [ipaddress.ip_network(c) for c in allocation_cidrs(client)]
    assert not nets[0].overlaps(nets[1])
