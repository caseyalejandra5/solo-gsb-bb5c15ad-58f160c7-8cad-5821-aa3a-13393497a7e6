"""并发测试：对真实运行的服务并发申请，验证不会分配到重叠网段。"""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def run_server(db_path: Path) -> Iterator[str]:
    """启动真实服务进程，就绪后返回 base URL，退出时终止进程。"""
    port = _free_port()
    env = dict(os.environ, CIDR_DB_PATH=str(db_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                httpx.get(f"{base}/healthz", timeout=1).raise_for_status()
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("server did not start")
                time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_concurrent_allocations_never_overlap(tmp_path):
    with run_server(tmp_path / "conc.db") as base:
        httpx.post(f"{base}/pools", json={"name": "eng", "cidr": "10.0.0.0/24"}).raise_for_status()

        def alloc(_: int) -> str:
            r = httpx.post(
                f"{base}/pools/eng/allocations",
                json={"requests": [{"prefix_len": 28}]},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["allocated"][0]

        # /24 恰好容纳 16 个 /28，16 个线程同时抢
        with ThreadPoolExecutor(max_workers=16) as ex:
            cidrs = list(ex.map(alloc, range(16)))

        assert len(set(cidrs)) == 16, f"出现重复分配: {cidrs}"
        nets = [ipaddress.ip_network(c) for c in cidrs]
        for i, a in enumerate(nets):
            for b in nets[i + 1:]:
                assert not a.overlaps(b), f"重叠分配: {a} 与 {b}"
        assert sum(n.num_addresses for n in nets) == 256

        # 池已满，再申请必须失败
        r = httpx.post(
            f"{base}/pools/eng/allocations",
            json={"requests": [{"prefix_len": 28}]},
            timeout=30,
        )
        assert r.status_code == 409


def test_concurrent_plan_commits_only_one_succeeds(tmp_path):
    with run_server(tmp_path / "conc-plan.db") as base:
        httpx.post(f"{base}/pools", json={"name": "eng", "cidr": "10.0.0.0/24"}).raise_for_status()
        r = httpx.post(
            f"{base}/pools/eng/plans",
            json={"requests": [{"prefix_len": 26, "count": 2}]},
            timeout=30,
        )
        r.raise_for_status()
        plan = r.json()

        def commit(_: int) -> int:
            return httpx.post(
                f"{base}/pools/eng/plans/{plan['plan_id']}/commit", timeout=30
            ).status_code

        # 8 个线程同时提交同一计划，只能成功一次
        with ThreadPoolExecutor(max_workers=8) as ex:
            codes = list(ex.map(commit, range(8)))

        assert codes.count(201) == 1, f"提交结果异常: {codes}"
        assert codes.count(409) == 7

        st = httpx.get(f"{base}/pools/eng", timeout=30).json()
        assert [a["cidr"] for a in st["allocations"]] == plan["cidrs"]


def test_concurrent_resize_and_allocate_stay_consistent(tmp_path):
    """扩展与并发申请竞争同一空间：校验与更新在同一事务内完成，
    因此恰好一方成功，且无论交错顺序如何，最终状态都无重叠。"""
    with run_server(tmp_path / "conc-resize.db") as base:
        httpx.post(f"{base}/pools", json={"name": "eng", "cidr": "10.0.0.0/23"}).raise_for_status()
        r = httpx.post(
            f"{base}/pools/eng/allocations",
            json={"requests": [{"prefix_len": 24}]},
            timeout=30,
        )
        r.raise_for_status()
        assert r.json()["allocated"] == ["10.0.0.0/24"]

        barrier = threading.Barrier(2)

        def grow() -> int:
            # 扩展 10.0.0.0/24 -> /23，需要另一半 10.0.1.0/24 空闲
            barrier.wait()
            return httpx.post(
                f"{base}/pools/eng/allocations/resize",
                json={"cidr": "10.0.0.0/24", "new_prefix_len": 23},
                timeout=30,
            ).status_code

        def alloc() -> int:
            # 并发申请 /24，空闲时必然拿到 10.0.1.0/24
            barrier.wait()
            return httpx.post(
                f"{base}/pools/eng/allocations",
                json={"requests": [{"prefix_len": 24}]},
                timeout=30,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_grow = ex.submit(grow)
            fut_alloc = ex.submit(alloc)
            grow_code = fut_grow.result()
            alloc_code = fut_alloc.result()

        # 恰好一方成功：扩展成功（200）则池已满（申请 409）；
        # 申请成功（201）则扩展必 409
        assert (grow_code, alloc_code) in [(200, 409), (409, 201)], (
            grow_code, alloc_code,
        )

        st = httpx.get(f"{base}/pools/eng", timeout=30).json()
        cidrs = [a["cidr"] for a in st["allocations"]]
        if grow_code == 200:
            assert cidrs == ["10.0.0.0/23"]
        else:
            assert cidrs == ["10.0.0.0/24", "10.0.1.0/24"]
        nets = [ipaddress.ip_network(c) for c in cidrs]
        for i, a in enumerate(nets):
            for b in nets[i + 1:]:
                assert not a.overlaps(b), f"重叠分配: {a} 与 {b}"
