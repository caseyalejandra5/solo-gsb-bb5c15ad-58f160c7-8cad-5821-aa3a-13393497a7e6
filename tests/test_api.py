"""API 功能测试：每个用例使用独立的临时 SQLite 数据库。"""
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


# ---------------------------------------------------------------------------
# 地址池
# ---------------------------------------------------------------------------

def test_create_pool_and_status(client):
    r = make_pool(client)
    body = r.json()
    assert body["version"] == 4
    assert body["total_addresses"] == 256

    st = client.get("/pools/eng").json()
    assert st["allocations"] == []
    assert st["unavailable"] == []
    assert st["remaining"] == ["10.0.0.0/24"]
    assert st["remaining_addresses"] == 256


def test_create_pool_rejects_invalid_cidr(client):
    assert client.post("/pools", json={"name": "x", "cidr": "10.0.0.1/24"}).status_code == 400
    assert client.post("/pools", json={"name": "x", "cidr": "not-a-cidr"}).status_code == 400


def test_pool_name_unique_and_no_overlap(client):
    make_pool(client)
    assert client.post("/pools", json={"name": "eng", "cidr": "10.1.0.0/24"}).status_code == 409
    assert client.post("/pools", json={"name": "b", "cidr": "10.0.0.0/25"}).status_code == 409
    assert client.post("/pools", json={"name": "c", "cidr": "10.0.0.128/25"}).status_code == 409
    # 不同地址族互不影响
    assert client.post("/pools", json={"name": "v6", "cidr": "2001:db8::/32"}).status_code == 201


def test_unknown_pool_404(client):
    assert client.get("/pools/nope").status_code == 404
    assert client.delete("/pools/nope").status_code == 404
    assert client.post("/pools/nope/unavailable", json={"cidr": "10.0.0.0/25"}).status_code == 404
    assert client.post(
        "/pools/nope/allocations", json={"requests": [{"prefix_len": 24}]}
    ).status_code == 404


# ---------------------------------------------------------------------------
# 不可用子网
# ---------------------------------------------------------------------------

def test_unavailable_validation(client):
    make_pool(client)
    # 越界
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.1.0.0/24"}).status_code == 400
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/23"}).status_code == 400
    # 版本不符
    assert client.post(
        "/pools/eng/unavailable", json={"cidr": "2001:db8::/48"}
    ).status_code == 400
    # 正常登记
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"}).status_code == 201
    # 与已登记不可用段重叠
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/26"}).status_code == 409
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"}).status_code == 409


def test_unavailable_cannot_overlap_allocation(client):
    make_pool(client)
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    assert r.json()["allocated"] == ["10.0.0.0/26"]
    assert client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/26"}).status_code == 409


# ---------------------------------------------------------------------------
# 申请
# ---------------------------------------------------------------------------

def test_allocate_smallest_aligned(client):
    make_pool(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"})

    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    assert r.json()["allocated"] == ["10.0.0.128/26"]
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    assert r.json()["allocated"] == ["10.0.0.192/26"]
    # 空间耗尽
    assert client.post(
        "/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]}
    ).status_code == 409


def test_allocate_alignment_skips_unaligned_space(client):
    make_pool(client, cidr="10.0.0.0/22")
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"})
    # 10.0.0.128/25 空闲但放不下对齐的 /24，应跳到 10.0.1.0/24
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 24}]})
    assert r.json()["allocated"] == ["10.0.1.0/24"]


def test_allocate_prefix_len_bounds(client):
    make_pool(client)
    assert client.post(
        "/pools/eng/allocations", json={"requests": [{"prefix_len": 23}]}
    ).status_code == 400
    assert client.post(
        "/pools/eng/allocations", json={"requests": [{"prefix_len": 33}]}
    ).status_code == 400
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 32}]})
    assert r.status_code == 201
    assert r.json()["allocated"] == ["10.0.0.0/32"]


def test_batch_atomic_all_or_nothing(client):
    make_pool(client)
    # /24 最多容纳 2 个 /25，申请 3 个必须整体失败
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 25, "count": 3}]})
    assert r.status_code == 409
    st = client.get("/pools/eng").json()
    assert st["allocations"] == []
    assert st["remaining"] == ["10.0.0.0/24"]


def test_batch_success_in_order(client):
    make_pool(client)
    r = client.post(
        "/pools/eng/allocations",
        json={"requests": [{"prefix_len": 26, "count": 2}, {"prefix_len": 28}]},
    )
    assert r.status_code == 201
    assert r.json()["allocated"] == ["10.0.0.0/26", "10.0.0.64/26", "10.0.0.128/28"]


# ---------------------------------------------------------------------------
# 释放与合并
# ---------------------------------------------------------------------------

def test_release_merges_adjacent_free_space(client):
    make_pool(client)
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26, "count": 2}]})

    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/26"})
    st = client.get("/pools/eng").json()
    assert st["remaining"] == ["10.0.0.0/26", "10.0.0.128/25"]

    client.post("/pools/eng/release", json={"cidr": "10.0.0.64/26"})
    st = client.get("/pools/eng").json()
    assert st["remaining"] == ["10.0.0.0/24"]
    assert st["remaining_addresses"] == 256


def test_release_unknown_allocation(client):
    make_pool(client)
    assert client.post("/pools/eng/release", json={"cidr": "10.0.0.0/26"}).status_code == 404


# ---------------------------------------------------------------------------
# IPv6
# ---------------------------------------------------------------------------

def test_ipv6_pool(client):
    make_pool(client, name="v6", cidr="2001:db8::/32")
    client.post("/pools/v6/unavailable", json={"cidr": "2001:db8::/48"})
    r = client.post("/pools/v6/allocations", json={"requests": [{"prefix_len": 48}]})
    assert r.json()["allocated"] == ["2001:db8:1::/48"]
    st = client.get("/pools/v6").json()
    assert st["version"] == 6
    assert st["unavailable"] == ["2001:db8::/48"]
