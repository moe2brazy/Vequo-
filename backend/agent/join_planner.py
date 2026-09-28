"""JOIN 路径规划（P0-2）— 基于外键图的候选 JOIN 路径枚举

目标：把「LLM 自由 JOIN」升级为「LLM 在候选 JOIN 路径上选择」。

- 构建无向外键图（表 = 节点，FK = 带 JOIN 键的边）
- 对候选表两两做 BFS，枚举 ≤ max_hops 跳的 JOIN 路径
- 路径经过的「中间表」自动并入 schema 上下文（让 LLM 拿得到其 DDL）
- 识别桥接表（路径中、且被多次穿越的中间节点），供 schema 头标注

纯函数、可单测，不依赖 LLM。表名统一用 db.tools 的命名规则（非 public 带 schema 前缀）。
"""

from collections import deque
from dataclasses import dataclass


@dataclass
class JoinPath:
    tables: list[str]   # 路径经过的表（含两端），顺序为 JOIN 顺序
    edges: list[str]    # 每条边的 JOIN 键，形如 "child.col = parent.col"
    hops: int           # 跳数 = 边数
    via: list[str]      # 中间表（两端之外）


def _bare(name: str) -> str:
    return name.split(".")[-1].strip().lower()


def build_adjacency(fk_map: dict[str, list[dict]]) -> dict[str, list[tuple[str, str, str]]]:
    """fk_map: {child: [{column, ref_table, ref_column}]} → 无向邻接表

    返回: {node: [(neighbor, node_col, neighbor_col), ...]}
    含义: node.node_col = neighbor.neighbor_col
    """
    adj: dict[str, list[tuple[str, str, str]]] = {}

    def add(a: str, a_col: str, b: str, b_col: str) -> None:
        if not a or not b:
            return
        adj.setdefault(a, []).append((b, a_col, b_col))

    for child, fks in (fk_map or {}).items():
        for fk in fks or []:
            parent = fk.get("ref_table", "")
            col = fk.get("column", "")
            ref_col = fk.get("ref_column", "")
            if not parent or parent == child:
                continue  # 跳过自引用，避免环
            add(child, col, parent, ref_col)
            add(parent, ref_col, child, col)
    return adj


def _bfs_paths(src: str, targets: set[str], adj: dict, max_hops: int):
    """从 src 做 BFS，返回 {target: (path_nodes, edges)}（只含可达的 target）"""
    dist = {src: 0}
    parent: dict[str, tuple[str, str]] = {}  # node -> (prev_node, edge_str)
    q = deque([src])
    while q:
        cur = q.popleft()
        if dist[cur] >= max_hops:
            continue
        for nbr, cur_col, nbr_col in adj.get(cur, []):
            if nbr in dist:
                continue
            dist[nbr] = dist[cur] + 1
            parent[nbr] = (cur, f"{cur}.{cur_col} = {nbr}.{nbr_col}")
            q.append(nbr)

    out: dict[str, tuple[list[str], list[str]]] = {}
    for tgt in targets:
        if tgt == src or tgt not in dist:
            continue
        edges: list[str] = []
        nodes: list[str] = [tgt]
        cur = tgt
        while cur in parent:
            prev, e = parent[cur]
            edges.append(e)
            cur = prev
            nodes.append(prev)
        edges.reverse()
        nodes.reverse()
        out[tgt] = (nodes, edges)
    return out


def plan_joins(
    candidate_names: list[str],
    fk_map: dict[str, list[dict]],
    max_hops: int = 2,
    max_paths: int = 20,
    max_total: int = 12,
    allowed_tables: set[str] | None = None,
) -> dict:
    """枚举候选表之间的 JOIN 路径并扩充候选集合。

    返回:
      expanded: 候选表 + 中间表（受权限与 max_total 约束）
      paths:    [JoinPath]（多跳路径）
      bridge:   set[str]（桥接表 = 路径中间节点且外键边 ≥2）
    """
    candidate_names = list(dict.fromkeys([n for n in candidate_names if n]))
    adj = build_adjacency(fk_map)
    allowed = {_bare(n) for n in (allowed_tables or [])} if allowed_tables is not None else None

    paths: list[JoinPath] = []
    intermediates: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    for src in candidate_names:
        if len(paths) >= max_paths:
            break
        found = _bfs_paths(src, set(candidate_names), adj, max_hops)
        for tgt in candidate_names:
            if len(paths) >= max_paths:
                break
            key = tuple(sorted([src, tgt]))
            if tgt == src or key in seen_pairs or tgt not in found:
                continue
            seen_pairs.add(key)
            nodes, edges = found[tgt]
            via = nodes[1:-1]
            # 权限：中间表不可访问则整条路径不可用
            if allowed is not None and any(_bare(n) not in allowed for n in via):
                continue
            for n in via:
                intermediates.add(n)
            paths.append(JoinPath(tables=nodes, edges=edges, hops=len(edges), via=via))

    # 桥接表：路径中间节点 + 外键边 ≥2
    bridge = {n for n in intermediates if len(adj.get(n, [])) >= 2}

    # 扩充候选：加入中间表（受权限与上限约束）
    expanded = list(candidate_names)
    for it in sorted(intermediates):
        if allowed is not None and _bare(it) not in allowed:
            continue
        if it not in expanded and len(expanded) < max_total:
            expanded.append(it)

    return {"expanded": expanded, "paths": paths, "bridge": bridge}
