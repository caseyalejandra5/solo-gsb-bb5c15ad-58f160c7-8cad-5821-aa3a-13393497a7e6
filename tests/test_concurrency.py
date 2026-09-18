"""并发测试：对真实运行的服务并发申请，验证不会分配到重叠网段。"""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
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


def test_concurrent_resizes_disjoint_targets_both_succeed(tmp_path):
    """两个互不冲突的扩展并发执行：BEGIN IMMEDIATE 串行化后都应成功，无丢失更新。"""
    with run_server(tmp_path / "conc-resize.db") as base:
        httpx.post(f"{base}/pools", json={"name": "eng", "cidr": "10.0.0.0/24"}).raise_for_status()

        def alloc(prefix_len):
            r = httpx.post(
                f"{base}/pools/eng/allocations",
                json={"requests": [{"prefix_len": prefix_len}]},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["allocated"][0]

        def release(cidr):
            httpx.post(
                f"{base}/pools/eng/release", json={"cidr": cidr}, timeout=30
            ).raise_for_status()

        # 目标：留下 0/28 与 128/28 两个分配。先占满 0..127（两个 /26），
        # 再分配 /28 得到 128/28；释放一个 /26 让首个 /28 落到 0/28。
        assert alloc(26) == "10.0.0.0/26"
        assert alloc(26) == "10.0.0.64/26"
        assert alloc(28) == "10.0.0.128/28"
        release("10.0.0.0/26")
        release("10.0.0.64/26")
        assert alloc(28) == "10.0.0.0/28"
        # 当前分配：0/28、128/28

        def resize(payload):
            return httpx.post(
                f"{base}/pools/eng/allocations/resize", json=payload, timeout=30
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as ex:
            codes = list(ex.map(resize, [
                {"cidr": "10.0.0.0/28", "new_prefix_len": 27},
                {"cidr": "10.0.0.128/28", "new_prefix_len": 27},
            ]))

        assert sorted(codes) == [200, 200], codes
        st = httpx.get(f"{base}/pools/eng", timeout=30).json()
        cidrs = sorted(a["cidr"] for a in st["allocations"])
        assert cidrs == ["10.0.0.0/27", "10.0.0.128/27"]
        nets = [ipaddress.ip_network(c) for c in cidrs]
        assert not nets[0].overlaps(nets[1])


def test_concurrent_resizes_competing_targets_exactly_one_succeeds(tmp_path):
    """两个扩展的目标互相排斥：无论谁先拿到写锁，都恰好一个成功，结果无重叠。

    初始分配 0/27 与 32/28：
    - 0/27 -> 0/26：目标包含 32/28，因此 32/28 仍在时必被挡（409）；
    - 32/28 -> 32/27：目标 32..63 不包含 0/27，因此 0/27 仍在时可以成功。
    串行化后若后者先提交则前者失败；若前者先拿到锁，它自身校验失败回滚，
    后者随后成功——两种交错结果一致：恰好一次 200，最终为相邻不重叠的
    0/27 与 32/27。
    """
    with run_server(tmp_path / "conc-resize-collide.db") as base:
        httpx.post(f"{base}/pools", json={"name": "eng", "cidr": "10.0.0.0/24"}).raise_for_status()
        r = httpx.post(
            f"{base}/pools/eng/allocations", json={"requests": [{"prefix_len": 27}]},
            timeout=30,
        )
        r.raise_for_status()
        assert r.json()["allocated"] == ["10.0.0.0/27"]
        r = httpx.post(
            f"{base}/pools/eng/allocations", json={"requests": [{"prefix_len": 28}]},
            timeout=30,
        )
        r.raise_for_status()
        assert r.json()["allocated"] == ["10.0.0.32/28"]

        def resize(payload):
            return httpx.post(
                f"{base}/pools/eng/allocations/resize", json=payload, timeout=30
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as ex:
            codes = list(ex.map(resize, [
                {"cidr": "10.0.0.0/27", "new_prefix_len": 26},
                {"cidr": "10.0.0.32/28", "new_prefix_len": 27},
            ]))

        assert sorted(codes) == [200, 409], codes
        st = httpx.get(f"{base}/pools/eng", timeout=30).json()
        cidrs = sorted(a["cidr"] for a in st["allocations"])
        assert cidrs == ["10.0.0.0/27", "10.0.0.32/27"], cidrs
        nets = [ipaddress.ip_network(c) for c in cidrs]
        assert not nets[0].overlaps(nets[1])
