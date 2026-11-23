"""按扫描批次与票种票量，从已配对票中凑出 B 相对 A 净增最低的票号组。

财务在切换引擎 B 前，会分别给出每个扫描批次和每个票种需要的票量。本模块在
同时满足两套票量的前提下，使入选票据中「仅 B 通过」与「仅 A 通过」之差最小
（即 B 比 A 的净增最低）；净增相同时，取排序后票号字典序最靠前的一组。

优化用最小费用流（SPFA，允许负费用）完成，票号并列最优通过逐张贪心 + 同一流
网络的可行性判定恢复字典序最小解。全部算法只依赖标准库。
"""

from collections import deque
from fractions import Fraction

from .analysis import validate_rows

# 四类配对判定的净贡献：仅 B 通过 +1，仅 A 通过 -1，其余 0。
_OUTCOMES = (
    ((True, False), "仅 A 通过", -1),
    ((False, True), "仅 B 通过", 1),
    ((True, True), "均通过", 0),
    ((False, False), "均未通过", 0),
)
_OUTCOME_LABEL = {key: label for key, label, _ in _OUTCOMES}
_OUTCOME_WEIGHT = {key: weight for key, _, weight in _OUTCOMES}
_QUOTA_MAX = 100_000


def normalize_quotas(raw, documents):
    """校验财务给出的两套票量，并核对批次、票种是否都来自配对数据。"""
    if not isinstance(raw, dict) or set(raw) != {"batches", "types"}:
        raise ValueError("票量计划须同时包含 batches（扫描批次）和 types（票据类型）两套票量。")

    known_batches = {row["batch_id"] for row in documents}
    known_types = {row["document_type"] for row in documents}
    normalized = {}
    for dimension, known, label in (
        ("batches", known_batches, "扫描批次"),
        ("types", known_types, "票据类型"),
    ):
        obj = raw[dimension]
        if not isinstance(obj, dict) or not obj:
            raise ValueError(f"{label}票量不能为空。")
        cleaned = {}
        for key, amount in obj.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{label}名称格式有误。")
            name = key.strip()
            if name not in known:
                raise ValueError(f"{label}「{name}」不在当前配对数据中，请先核对导入批次。")
            # 票量允许为 0（该维度本批不选），但必须是非负整数且不超上限。
            if type(amount) is not int or not 0 <= amount <= _QUOTA_MAX:
                raise ValueError(f"{label}「{name}」的票量须为 0 至 {_QUOTA_MAX} 之间的整数。")
            cleaned[name] = amount
        normalized[dimension] = cleaned
    return normalized


def _min_cost_flow(graph, source, sink, required):
    """在残量图上用 SPFA 逐次增广，返回 (实际流量, 最小费用)。"""
    node_count = len(graph)
    flow = cost = 0
    inf = float("inf")
    while flow < required:
        dist = [inf] * node_count
        prev_node = [-1] * node_count
        prev_edge = [-1] * node_count
        in_queue = [False] * node_count
        dist[source] = 0
        queue = deque([source])
        in_queue[source] = True
        while queue:
            u = queue.popleft()
            in_queue[u] = False
            for edge_index, edge in enumerate(graph[u]):
                v, rev, cap, edge_cost = edge
                if cap > 0 and dist[u] + edge_cost < dist[v]:
                    dist[v] = dist[u] + edge_cost
                    prev_node[v] = u
                    prev_edge[v] = edge_index
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if dist[sink] == inf:
            break
        added = required - flow
        v = sink
        while v != source:
            u = prev_node[v]
            added = min(added, graph[u][prev_edge[v]][2])
            v = u
        v = sink
        while v != source:
            u = prev_node[v]
            edge = graph[u][prev_edge[v]]
            edge[2] -= added
            graph[v][edge[1]][2] += added
            v = u
        flow += added
        cost += added * dist[sink]
    return flow, cost


def _add_edge(graph, u, v, cap, cost):
    graph[u].append([v, len(graph[v]), cap, cost])
    graph[v].append([u, len(graph[u]) - 1, 0, -cost])


def _build_graph(batches, types, class_caps, batch_quota, type_quota, weights):
    """S → 批次 → (批次,票种,判定类) → 票种 → T。

    节点布局：0=S，随后批次节点、单元节点（每批×每票种×4 判定类）、票种节点、T。
    费用只挂在判定类单元上，等于该类对净增的贡献（-1/0/+1）。
    """
    node_s = 0
    batch_start = 1
    cell_start = batch_start + len(batches)
    cells_per_type = len(batches) * len(weights)
    type_start = cell_start + len(types) * cells_per_type
    node_t = type_start + len(types)
    graph = [[] for _ in range(node_t + 1)]

    def batch_node(i):
        return batch_start + i

    def cell_node(i, t, k):
        return cell_start + t * cells_per_type + i * len(weights) + k

    def type_node(t):
        return type_start + t

    for i, batch in enumerate(batches):
        _add_edge(graph, node_s, batch_node(i), batch_quota[i], 0)
        for t, doc_type in enumerate(types):
            for k, weight in enumerate(weights):
                cap = class_caps[i][t][k]
                if cap:
                    _add_edge(graph, batch_node(i), cell_node(i, t, k), cap, weight)
                    _add_edge(graph, cell_node(i, t, k), type_node(t), cap, 0)
    for t in range(len(types)):
        _add_edge(graph, type_node(t), node_t, type_quota[t], 0)
    return graph, node_s, node_t


def _residual_reachable(graph, source):
    seen = {source}
    queue = deque([source])
    while queue:
        u = queue.popleft()
        for v, _rev, cap, _cost in graph[u]:
            if cap > 0 and v not in seen:
                seen.add(v)
                queue.append(v)
    return seen


def _diagnose(batches, types, class_caps, batch_quota, type_quota, total_needed, total_available):
    """凑不齐时给出冲突的批次与票种，附带要求票量和可用票量。"""
    conflicts = []
    batch_total = sum(batch_quota)
    type_total = sum(type_quota)
    if batch_total != type_total:
        conflicts.append({
            "kind": "两套票量总量不一致",
            "batches": list(batches),
            "types": list(types),
            "required": batch_total,
            "available": type_total,
            "detail": f"扫描批次票量合计 {batch_total} 张，票种票量合计 {type_total} 张，两者必须相等。",
        })
    if total_needed > total_available:
        conflicts.append({
            "kind": "配对票总量不足",
            "batches": list(batches),
            "types": list(types),
            "required": total_needed,
            "available": total_available,
            "detail": f"两套票量各需 {total_needed} 张，配对数据仅 {total_available} 张。",
        })

    batch_available = [
        sum(class_caps[i][t][k] for t in range(len(types)) for k in range(4))
        for i in range(len(batches))
    ]
    type_available = [
        sum(class_caps[i][t][k] for i in range(len(batches)) for k in range(4))
        for t in range(len(types))
    ]
    for i, batch in enumerate(batches):
        if batch_quota[i] > batch_available[i]:
            conflicts.append({
                "kind": "扫描批次票量不足",
                "batches": [batch],
                "types": [t for t in types],
                "required": batch_quota[i],
                "available": batch_available[i],
                "detail": f"批次「{batch}」要求 {batch_quota[i]} 张，实际配对 {batch_available[i]} 张。",
            })
    for t, doc_type in enumerate(types):
        if type_quota[t] > type_available[t]:
            conflicts.append({
                "kind": "票种票量不足",
                "batches": list(batches),
                "types": [doc_type],
                "required": type_quota[t],
                "available": type_available[t],
                "detail": f"票种「{doc_type}」要求 {type_quota[t]} 张，实际配对 {type_available[t]} 张。",
            })
    if conflicts:
        return conflicts

    # 单点票量都够却仍不可行：用最大流最小割定位 Hall 冲突组合。
    weights = [weight for _key, _label, weight in _OUTCOMES]
    graph, source, sink = _build_graph(
        batches, types, class_caps, batch_quota, type_quota, weights)
    flow, _cost = _min_cost_flow(graph, source, sink, total_needed)
    if flow >= total_needed:
        return conflicts  # 理论上不应发生，交给上层兜底

    # 节点布局与 _build_graph 保持一致，由残量可达集合反推最小割的 S 侧。
    batch_count, type_count = len(batches), len(types)
    cell_start = 1 + batch_count
    cells_per_type = batch_count * 4
    type_start = cell_start + type_count * cells_per_type
    reachable = _residual_reachable(graph, source)

    violating_indexes = [i for i in range(batch_count) if 1 + i in reachable]
    if not violating_indexes:
        conflicts.append({
            "kind": "票量组合冲突",
            "batches": list(batches),
            "types": list(types),
            "required": total_needed,
            "available": total_available,
            "detail": "现有配对票的批次与票种结构无法同时承载两套票量，请放宽票量或补充配对票。",
        })
        return conflicts

    reachable_types = [
        types[t] for t in range(type_count) if type_start + t in reachable
    ]
    # 仍可从冲突批次流向 T 的票，才是真正能满足这些批次票量的供给。
    # 单元节点相对偏移为 t*cells_per_type + i*4 + k，需遍历全部票种块。
    available = 0
    for t in range(type_count):
        for i in violating_indexes:
            for k in range(4):
                cell_offset = t * cells_per_type + i * 4 + k
                if cell_start + cell_offset in reachable:
                    available += class_caps[i][t][k]
    required = sum(batch_quota[i] for i in violating_indexes)
    violating_batches = [batches[i] for i in violating_indexes]
    reachable_type_quotas = sum(
        type_quota[t] for t, name in enumerate(types) if name in reachable_types)
    conflicts.append({
        "kind": "批次与票种组合冲突",
        "batches": violating_batches,
        "types": reachable_types,
        "required": required,
        "available": available,
        "detail": (
            f"批次 {('、'.join(violating_batches))} 只能供给票种 "
            f"{('、'.join(reachable_types) or '（无可达票种）')}，这些批次合计要求 {required} 张，"
            f"而对应票种的票量合计仅 {reachable_type_quotas} 张，"
            f"多出的 {required - reachable_type_quotas} 张无法在两套票量内消化。"
        ),
    })
    return conflicts


def _table(title, columns, rows):
    return {
        "title": title,
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": rows,
    }


def _gain_text(gain):
    return f"{float(gain):+.4f}".rstrip("0").rstrip(".")


def select_quota(rows, params):
    """主入口：基于配对票和票量参数，返回与统计验收同构的结果字典。"""
    documents = validate_rows(rows)
    if not isinstance(params, dict) or "quotas" not in params:
        raise ValueError("缺少票量计划参数。")
    quotas = normalize_quotas(params["quotas"], documents)

    batches = sorted({row["batch_id"] for row in documents})
    types = sorted({row["document_type"] for row in documents})
    weights = [weight for _key, _label, weight in _OUTCOMES]
    class_index = {key: k for k, (key, _label, _w) in enumerate(_OUTCOMES)}

    # class_caps[i][t][k]：批次 i、票种 t、判定类 k 的可用票数。
    class_caps = [[[0] * 4 for _ in types] for _ in batches]
    for row in documents:
        i = batches.index(row["batch_id"])
        t = types.index(row["document_type"])
        k = class_index[(row["a_pass"], row["b_pass"])]
        class_caps[i][t][k] += 1

    batch_quota = [quotas["batches"].get(name, 0) for name in batches]
    type_quota = [quotas["types"].get(name, 0) for name in types]
    total_needed = sum(batch_quota)
    type_needed = sum(type_quota)
    total_available = len(documents)
    if total_needed == 0 or type_needed == 0:
        raise ValueError("两套票量至少各需要一张票据，不能全部为 0。")

    # 一套入选票据的批次票量合计与票种票量合计必然相等，总量不同则无可行解。
    if total_needed != type_needed:
        conflicts = _diagnose(
            batches, types, class_caps, batch_quota, type_quota,
            total_needed, total_available)
        return _infeasible_result(
            batches, types, class_caps, batch_quota, type_quota, conflicts)

    graph, source, sink = _build_graph(
        batches, types, class_caps, batch_quota, type_quota, weights)
    flow, optimal_cost = _min_cost_flow(graph, source, sink, total_needed)

    if flow < total_needed:
        conflicts = _diagnose(
            batches, types, class_caps, batch_quota, type_quota,
            total_needed, total_available)
        return _infeasible_result(
            batches, types, class_caps, batch_quota, type_quota, conflicts)

    return _feasible_result(
        documents, batches, types, weights, class_caps, class_index,
        batch_quota, type_quota, total_needed, optimal_cost)


def _feasible_result(documents, batches, types, weights, class_caps, class_index,
                     batch_quota, type_quota, total_needed, optimal_cost):
    """逐张按票号贪心：纳入后仍存在费用不劣于最优解的可行流才入选。"""
    caps = [
        [[class_caps[i][t][k] for k in range(4)] for t in range(len(types))]
        for i in range(len(batches))
    ]
    residual_batch = list(batch_quota)
    residual_type = list(type_quota)
    spent = 0
    chosen = []

    def feasible(limit):
        need = sum(residual_batch)
        if need == 0:
            return limit >= 0
        graph, source, sink = _build_graph(
            batches, types, caps, residual_batch, residual_type, weights)
        flow, cost = _min_cost_flow(graph, source, sink, need)
        return flow == need and cost <= limit

    for row in sorted(documents, key=lambda item: item["document_id"]):
        i = batches.index(row["batch_id"])
        t = types.index(row["document_type"])
        k = class_index[(row["a_pass"], row["b_pass"])]
        # 该批次或票种的票量已经凑满，后续同维票据一律不再纳入。
        if residual_batch[i] <= 0 or residual_type[t] <= 0:
            continue
        caps[i][t][k] -= 1
        residual_batch[i] -= 1
        residual_type[t] -= 1
        if feasible(optimal_cost - spent - weights[k]):
            chosen.append(row)
            spent += weights[k]
        else:
            caps[i][t][k] += 1
            residual_batch[i] += 1
            residual_type[t] += 1

    if len(chosen) != total_needed or spent != optimal_cost:
        raise RuntimeError("票量凑批恢复最优解失败，请检查配对数据与票量。")

    a_only = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (True, False))
    b_only = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (False, True))
    both_pass = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (True, True))
    both_fail = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (False, False))
    gain_pp = Fraction(100 * (b_only - a_only), total_needed)
    gain_text = _gain_text(gain_pp)
    selected_ids = [row["document_id"] for row in chosen]

    selected_rows = [{
        "document_id": row["document_id"],
        "batch_id": row["batch_id"],
        "document_type": row["document_type"],
        "outcome": _OUTCOME_LABEL[(row["a_pass"], row["b_pass"])],
        "contribution": _OUTCOME_WEIGHT[(row["a_pass"], row["b_pass"])],
    } for row in chosen]

    batch_rows = []
    for i, name in enumerate(batches):
        picked = sum(1 for row in chosen if row["batch_id"] == name)
        available = sum(class_caps[i][t][k] for t in range(len(types)) for k in range(4))
        batch_rows.append({"batch_id": name, "required": batch_quota[i],
                           "selected": picked, "available": available,
                           "status": "凑齐" if picked == batch_quota[i] else "未凑齐"})
    type_rows = []
    for t, name in enumerate(types):
        picked = sum(1 for row in chosen if row["document_type"] == name)
        available = sum(class_caps[i][t][k] for i in range(len(batches)) for k in range(4))
        type_rows.append({"document_type": name, "required": type_quota[t],
                          "selected": picked, "available": available,
                          "status": "凑齐" if picked == type_quota[t] else "未凑齐"})

    headline = (
        f"已凑齐两套票量共 {total_needed} 张，B 比 A 净增最低为 {gain_text} 个百分点，"
        f"对应票号 {len(selected_ids)} 个。"
    )
    note = (
        f"在同时满足各扫描批次、各票种票量的全部组合中，本组合仅 B 通过 {b_only} 张、"
        f"仅 A 通过 {a_only} 张，净增 {b_only - a_only} 张为可实现的最小值；"
        "净增相同的组合之间按票号排序取字典序最靠前的一组。"
    )
    return {
        "state": "ok",
        "feasible": True,
        "headline": headline,
        "note": note,
        "selected_ids": selected_ids,
        "metrics": [
            {"label": "凑齐票量（张）", "value": total_needed, "detail": "两套票量各维度均已满足"},
            {"label": "仅 A 通过（张）", "value": a_only, "detail": "对净增贡献 -1"},
            {"label": "仅 B 通过（张）", "value": b_only, "detail": "对净增贡献 +1"},
            {"label": "B 比 A 净增（百分点）", "value": gain_text,
             "detail": f"100 ×（{b_only} − {a_only}）/ {total_needed}"},
            {"label": "净增张数（B-A）", "value": b_only - a_only, "detail": "已取全部可行组合中的最小值"},
        ],
        "tables": [
            _table("入选票号清单", (
                ("document_id", "票据编号"), ("batch_id", "扫描批次"),
                ("document_type", "票据类型"), ("outcome", "配对判定"),
                ("contribution", "净贡献"),
            ), selected_rows),
            _table("扫描批次票量达成", (
                ("batch_id", "扫描批次"), ("required", "要求票量"),
                ("selected", "入选票量"), ("available", "配对可用"), ("status", "状态"),
            ), batch_rows),
            _table("票种票量达成", (
                ("document_type", "票据类型"), ("required", "要求票量"),
                ("selected", "入选票量"), ("available", "配对可用"), ("status", "状态"),
            ), type_rows),
        ],
        "charts": [{
            "title": "入选票据的四类配对判定",
            "kind": "bar",
            "series": [
                {"label": label, "value": count}
                for label, count in (("均通过", both_pass), ("仅 A 通过", a_only),
                                     ("仅 B 通过", b_only), ("均未通过", both_fail))
            ],
        }],
        "sections": [
            {"title": "择优口径", "text": (
                "每张仅 B 通过的票使净增 +1，每张仅 A 通过的票使净增 -1，判定一致的票贡献 0。"
                "最小费用流在批次票量、票种票量和单元可用量约束下求最小总贡献，"
                "再按票号逐张贪心恢复字典序最小的票号组。"
            )},
            {"title": "复核与交付", "text": (
                "入选票号可直接交财务变更负责人核对；验收页面展示与 CSV 下载读取同一份运行结果，"
                "计划更正后旧计划在途运行一律作废，不会产生可下载结果。"
            )},
        ],
    }


def _infeasible_result(batches, types, class_caps, batch_quota, type_quota, conflicts):
    conflict_rows = [{
        "kind": item["kind"],
        "batches": "、".join(item["batches"]) or "—",
        "types": "、".join(item["types"]) or "—",
        "required": item["required"],
        "available": item["available"],
        "detail": item["detail"],
    } for item in conflicts]
    batch_rows = [{
        "batch_id": batches[i],
        "required": batch_quota[i],
        "available": sum(class_caps[i][t][k] for t in range(len(types)) for k in range(4)),
    } for i in range(len(batches))]
    type_rows = [{
        "document_type": types[t],
        "required": type_quota[t],
        "available": sum(class_caps[i][t][k] for i in range(len(batches)) for k in range(4)),
    } for t in range(len(types))]
    headline = "两套票量凑不齐，未生成切换票号组；请按冲突清单更正计划。"
    detail = "；".join(item["detail"] for item in conflicts) or "现有配对票无法同时满足两套票量。"
    return {
        "state": "warning",
        "feasible": False,
        "headline": headline,
        "note": detail,
        "selected_ids": [],
        "metrics": [
            {"label": "要求票量（张）", "value": sum(batch_quota), "detail": "按扫描批次票量合计"},
            {"label": "配对可用（张）",
             "value": sum(class_caps[i][t][k]
                          for i in range(len(batches))
                          for t in range(len(types)) for k in range(4))},
            {"label": "冲突项", "value": len(conflicts), "detail": "见冲突清单中的批次与票种"},
        ],
        "tables": [
            _table("冲突清单", (
                ("kind", "冲突类型"), ("batches", "涉及扫描批次"), ("types", "涉及票种"),
                ("required", "要求票量"), ("available", "可用票量"), ("detail", "说明"),
            ), conflict_rows),
            _table("扫描批次票量", (
                ("batch_id", "扫描批次"), ("required", "要求票量"), ("available", "配对可用"),
            ), batch_rows),
            _table("票种票量", (
                ("document_type", "票据类型"), ("required", "要求票量"), ("available", "配对可用"),
            ), type_rows),
        ],
        "charts": [],
        "sections": [
            {"title": "冲突处理", "text": (
                "冲突清单同时给出涉及的扫描批次、票种以及要求与可用票量。"
                "财务更正计划后会产生新版本并重新计算，旧版本在途运行自动作废。"
            )},
        ],
    }
