"""CIDR 核心算法：空闲区间计算、最小对齐子网选择、最小集合归并。

地址一律转换为整数区间处理，IPv4 / IPv6 逻辑完全一致。
"""
from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

Network = IPv4Network | IPv6Network


def max_prefixlen(version: int) -> int:
    return 32 if version == 4 else 128


def parse_network(cidr: str) -> Network:
    """严格解析 CIDR（主机位必须为零），非法输入抛 ValueError。"""
    return ipaddress.ip_network(cidr, strict=True)


def free_intervals(pool: Network, occupied: list[Network]) -> list[tuple[int, int]]:
    """池内地址减去占用网段，返回升序的闭区间列表 [(start, end), ...]。"""
    intervals = [(int(pool.network_address), int(pool.broadcast_address))]
    for net in sorted(occupied, key=lambda n: int(n.network_address)):
        s, e = int(net.network_address), int(net.broadcast_address)
        remaining = []
        for a, b in intervals:
            if e < a or s > b:
                remaining.append((a, b))
                continue
            if s > a:
                remaining.append((a, s - 1))
            if e < b:
                remaining.append((e + 1, b))
        intervals = remaining
        if not intervals:
            break
    return intervals


def smallest_aligned_network(
    free: list[tuple[int, int]], prefix_len: int, version: int
) -> Network | None:
    """在空闲区间中找网络地址最小的、按前缀长度对齐的子网。"""
    size = 1 << (max_prefixlen(version) - prefix_len)
    addr_cls = IPv4Address if version == 4 else IPv6Address
    for a, b in free:
        start = (a + size - 1) // size * size  # 区间内第一个对齐地址
        if start + size - 1 <= b:
            return ipaddress.ip_network(f"{addr_cls(start)}/{prefix_len}")
    return None


def resize_network(net: Network, new_prefix_len: int, version: int) -> Network:
    """计算调整前缀长度后的目标网段。

    - 缩小（new_prefix_len 更大）：保留原网络地址，取以其为网络地址的更小子网，
      即把原网段尾部空间释放出来；
    - 扩展（new_prefix_len 更小）：取唯一包含原网段的目标前缀父网——给定前缀长度
      时任意网段有且只有一个同长度父网，父网是否仍在池内、是否与其他占用重叠
      由调用方在同一事务内校验。
    """
    base = int(net.network_address)
    if new_prefix_len >= net.prefixlen:
        target_base = base  # 缩小：网络地址保持不变
    else:
        size = 1 << (max_prefixlen(version) - new_prefix_len)
        target_base = base // size * size  # 扩展：对齐到唯一父网
    addr_cls = IPv4Address if version == 4 else IPv6Address
    return ipaddress.ip_network(f"{addr_cls(target_base)}/{new_prefix_len}")


def summarize_free(free: list[tuple[int, int]], version: int) -> list[str]:
    addr_cls = IPv4Address if version == 4 else IPv6Address
    nets: list[Network] = []
    for a, b in free:
        nets.extend(ipaddress.summarize_address_range(addr_cls(a), addr_cls(b)))
    return [str(n) for n in ipaddress.collapse_addresses(nets)]
