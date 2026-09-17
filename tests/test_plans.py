"""两阶段分配计划测试：计划创建不占用地址；提交需满足未过期、未提交、池状态未变。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def make_plan(client, name="eng", requests=None):
    if requests is None:
        requests = [{"prefix_len": 26}]
    r = client.post(f"/pools/{name}/plans", json={"requests": requests})
    assert r.status_code == 201, r.text
    return r.json()


def commit(client, plan, name="eng"):
    return client.post(f"/pools/{name}/plans/{plan['plan_id']}/commit")


# ---------------------------------------------------------------------------
# 创建计划
# ---------------------------------------------------------------------------

def test_plan_computes_but_does_not_occupy(client):
    make_pool(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.0/25"})

    plan = make_plan(client, requests=[{"prefix_len": 26, "count": 2}])
    assert plan["pool"] == "eng"
    assert plan["plan_id"]
    # 与直接分配相同的最小地址规则
    assert plan["cidrs"] == ["10.0.0.128/26", "10.0.0.192/26"]
    # 有效期约 10 分钟
    expires = datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00"))
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=9) < delta <= timedelta(minutes=10)

    # 计划不占用地址：池状态无任何变化
    st = client.get("/pools/eng").json()
    assert st["allocations"] == []
    assert st["remaining_addresses"] == 128

    # 直接分配仍能拿到计划预选的网段（原分配接口不受计划影响）
    r = client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 26}]})
    assert r.json()["allocated"] == ["10.0.0.128/26"]


def test_plan_validation_errors(client):
    make_pool(client)
    assert client.post(
        "/pools/eng/plans", json={"requests": [{"prefix_len": 23}]}
    ).status_code == 400
    assert client.post(
        "/pools/eng/plans", json={"requests": [{"prefix_len": 33}]}
    ).status_code == 400
    # 空间不足：/24 放不下 3 个 /25，整体失败
    assert client.post(
        "/pools/eng/plans", json={"requests": [{"prefix_len": 25, "count": 3}]}
    ).status_code == 409
    # 地址池不存在
    assert client.post(
        "/pools/nope/plans", json={"requests": [{"prefix_len": 26}]}
    ).status_code == 404


def test_pending_plans_do_not_block_each_other(client):
    make_pool(client)
    p1 = make_plan(client)
    p2 = make_plan(client)
    # 都不占用地址，预选结果相同
    assert p1["cidrs"] == p2["cidrs"] == ["10.0.0.0/26"]
    assert commit(client, p1).status_code == 201
    # p1 提交改变了池状态，p2 随之失效
    assert commit(client, p2).status_code == 409


# ---------------------------------------------------------------------------
# 提交计划
# ---------------------------------------------------------------------------

def test_commit_success_allocates_planned_cidrs(client):
    make_pool(client)
    plan = make_plan(client, requests=[{"prefix_len": 26, "count": 2}])

    r = commit(client, plan)
    assert r.status_code == 201, r.text
    assert r.json()["pool"] == "eng"
    assert r.json()["plan_id"] == plan["plan_id"]
    assert r.json()["allocated"] == ["10.0.0.0/26", "10.0.0.64/26"]

    st = client.get("/pools/eng").json()
    assert [a["cidr"] for a in st["allocations"]] == ["10.0.0.0/26", "10.0.0.64/26"]
    assert st["remaining_addresses"] == 128


def test_commit_only_once(client):
    make_pool(client)
    plan = make_plan(client)
    assert commit(client, plan).status_code == 201
    assert commit(client, plan).status_code == 409
    # 重复提交不产生额外分配
    st = client.get("/pools/eng").json()
    assert [a["cidr"] for a in st["allocations"]] == ["10.0.0.0/26"]


def test_commit_fails_after_allocation_change(client):
    make_pool(client)
    plan = make_plan(client)  # 预选 10.0.0.0/26
    # 计划创建后直接分配，池状态变化
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})
    assert commit(client, plan).status_code == 409
    st = client.get("/pools/eng").json()
    assert [a["cidr"] for a in st["allocations"]] == ["10.0.0.0/28"]


def test_commit_fails_after_unavailable_change(client):
    make_pool(client)
    plan = make_plan(client)
    client.post("/pools/eng/unavailable", json={"cidr": "10.0.0.128/25"})
    assert commit(client, plan).status_code == 409
    assert client.get("/pools/eng").json()["allocations"] == []


def test_commit_fails_after_release(client):
    make_pool(client)
    client.post("/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]})
    plan = make_plan(client)  # 预选 10.0.0.64/26
    client.post("/pools/eng/release", json={"cidr": "10.0.0.0/28"})
    assert commit(client, plan).status_code == 409
    assert client.get("/pools/eng").json()["allocations"] == []


def test_commit_expired_plan(client, monkeypatch):
    monkeypatch.setenv("CIDR_PLAN_TTL_SECONDS", "-1")
    make_pool(client)
    plan = make_plan(client)
    assert commit(client, plan).status_code == 409
    assert client.get("/pools/eng").json()["allocations"] == []


def test_commit_unknown_plan(client):
    make_pool(client)
    assert client.post("/pools/eng/plans/does-not-exist/commit").status_code == 404
    assert client.post("/pools/nope/plans/does-not-exist/commit").status_code == 404


def test_commit_plan_from_other_pool(client):
    make_pool(client)
    make_pool(client, name="other", cidr="10.1.0.0/24")
    plan = make_plan(client, name="other")
    assert commit(client, plan, name="eng").status_code == 404
    assert commit(client, plan, name="other").status_code == 201


def test_plan_deleted_with_pool(client):
    make_pool(client)
    plan = make_plan(client)
    client.delete("/pools/eng")
    make_pool(client)  # 同名新池不继承旧计划
    assert commit(client, plan).status_code == 404
