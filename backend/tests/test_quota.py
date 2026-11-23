"""票量凑批算法的单元测试：与暴力枚举最优解逐项比对。

只用标准库，可在无网络/无第三方依赖的环境运行：
    python -m unittest backend.tests.test_quota -v
"""

import itertools
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.quota import _gain_text, normalize_quotas, select_quota  # noqa: E402

OUTCOMES = [(True, False), (False, True), (True, True), (False, False)]
WEIGHT = {(True, False): -1, (False, True): 1, (True, True): 0, (False, False): 0}
VAT, RECEIPT = "增值税发票", "零售小票"


def make_rows(spec):
    rows = []
    counter = 1
    for batch_id, doc_type, outcome in spec:
        rows.append({
            "document_id": f"CS-{counter:04d}",
            "batch_id": batch_id,
            "document_type": doc_type,
            "a_pass": outcome[0],
            "b_pass": outcome[1],
        })
        counter += 1
    return rows


def brute_force(rows, quotas):
    """枚举所有满足两套票量的组合，返回 (最小净增, 字典序最小票号组) 或 None。"""
    batch_keys = sorted(set(quotas["batches"]) | {row["batch_id"] for row in rows})
    type_keys = sorted(set(quotas["types"]) | {row["document_type"] for row in rows})
    wanted_batches = {key: quotas["batches"].get(key, 0) for key in batch_keys}
    wanted_types = {key: quotas["types"].get(key, 0) for key in type_keys}
    best = None
    for size in range(1, len(rows) + 1):
        for combo in itertools.combinations(rows, size):
            batch_count = {key: 0 for key in batch_keys}
            type_count = {key: 0 for key in type_keys}
            cost = 0
            for row in combo:
                batch_count[row["batch_id"]] += 1
                type_count[row["document_type"]] += 1
                cost += WEIGHT[(row["a_pass"], row["b_pass"])]
            if batch_count != wanted_batches or type_count != wanted_types:
                continue
            ids = tuple(sorted(row["document_id"] for row in combo))
            if best is None or (cost, ids) < (best[0], best[1]):
                best = (cost, ids)
    return best


class QuotaSelectionTests(unittest.TestCase):
    def assert_matches_brute_force(self, rows, quotas):
        result = select_quota(rows, {"quotas": quotas})
        expected = brute_force(rows, quotas)
        self.assertIsNotNone(expected, "暴力枚举应能找到可行解")
        expected_cost, expected_ids = expected
        self.assertTrue(result["feasible"])
        self.assertEqual(result["selected_ids"], list(expected_ids))
        chosen = [row for row in rows if row["document_id"] in set(expected_ids)]
        a_only = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (True, False))
        b_only = sum(1 for row in chosen if (row["a_pass"], row["b_pass"]) == (False, True))
        self.assertEqual(b_only - a_only, expected_cost)
        expected_gain = Fraction(100 * expected_cost, len(expected_ids))
        self.assertEqual(result["metrics"][3]["value"], _gain_text(expected_gain))
        # 两套票量逐维达成。
        for table in result["tables"][1:3]:
            for line in table["rows"]:
                self.assertEqual(line["status"], "凑齐")
                self.assertEqual(line["selected"], line["required"])

    def test_small_mixed_batches_matches_brute_force(self):
        rows = make_rows([
            ("B1", VAT, (True, False)),
            ("B1", VAT, (False, True)),
            ("B1", VAT, (True, True)),
            ("B1", RECEIPT, (False, True)),
            ("B1", RECEIPT, (False, False)),
            ("B2", VAT, (False, True)),
            ("B2", VAT, (True, False)),
            ("B2", RECEIPT, (True, True)),
            ("B2", RECEIPT, (False, True)),
            ("B2", RECEIPT, (True, False)),
        ])
        quotas = {"batches": {"B1": 3, "B2": 4},
                  "types": {VAT: 4, RECEIPT: 3}}
        self.assert_matches_brute_force(rows, quotas)

    def test_lexicographic_tie_break_matches_brute_force(self):
        # 多个零贡献组合并列，验证取字典序最小票号组。
        rows = make_rows([
            ("B1", VAT, (True, True)),
            ("B1", VAT, (True, True)),
            ("B1", RECEIPT, (True, True)),
            ("B2", VAT, (True, True)),
            ("B2", RECEIPT, (True, True)),
            ("B2", RECEIPT, (False, False)),
        ])
        quotas = {"batches": {"B1": 2, "B2": 2},
                  "types": {VAT: 2, RECEIPT: 2}}
        self.assert_matches_brute_force(rows, quotas)

    def test_randomized_matches_brute_force(self):
        # 小规模随机数据：正确性由暴力枚举背书，覆盖各种判定分布。
        import random
        rng = random.Random(20261123)
        for _ in range(40):
            size = rng.randint(6, 12)
            rows = make_rows([
                (rng.choice(["B1", "B2"]), rng.choice([VAT, RECEIPT]), rng.choice(OUTCOMES))
                for _ in range(size)
            ])
            batches = sorted({row["batch_id"] for row in rows})
            types = sorted({row["document_type"] for row in rows})
            batch_counts, type_counts = {}, {}
            for row in rows:
                batch_counts[row["batch_id"]] = batch_counts.get(row["batch_id"], 0) + 1
                type_counts[row["document_type"]] = type_counts.get(row["document_type"], 0) + 1
            total = rng.randint(2, size)
            batch_quota = {}
            remaining = total
            for index, batch in enumerate(batches):
                if index == len(batches) - 1:
                    batch_quota[batch] = min(remaining, batch_counts.get(batch, 0))
                else:
                    cap = min(remaining, batch_counts.get(batch, 0))
                    batch_quota[batch] = rng.randint(0, cap)
                    remaining -= batch_quota[batch]
            if sum(batch_quota.values()) != total:
                continue
            type_quota = {}
            remaining = total
            for index, doc_type in enumerate(types):
                if index == len(types) - 1:
                    type_quota[doc_type] = min(remaining, type_counts.get(doc_type, 0))
                else:
                    cap = min(remaining, type_counts.get(doc_type, 0))
                    type_quota[doc_type] = rng.randint(0, cap)
                    remaining -= type_quota[doc_type]
            if sum(type_quota.values()) != total:
                continue
            quotas = {"batches": batch_quota, "types": type_quota}
            expected = brute_force(rows, quotas)
            result = select_quota(rows, {"quotas": quotas})
            if expected is None:
                self.assertFalse(result["feasible"])
            else:
                self.assertTrue(result["feasible"])
                self.assertEqual(result["selected_ids"], list(expected[1]))

    def test_prefers_a_only_to_minimize_gain(self):
        rows = make_rows([
            ("B1", VAT, (True, False)),
            ("B1", VAT, (False, True)),
            ("B1", VAT, (False, True)),
            ("B2", VAT, (True, False)),
            ("B2", VAT, (False, True)),
        ])
        quotas = {"batches": {"B1": 2, "B2": 1}, "types": {VAT: 3}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertTrue(result["feasible"])
        ids = set(result["selected_ids"])
        picked = [row for row in rows if row["document_id"] in ids]
        a_only = sum(1 for row in picked if (row["a_pass"], row["b_pass"]) == (True, False))
        b_only = sum(1 for row in picked if (row["a_pass"], row["b_pass"]) == (False, True))
        self.assertEqual((a_only, b_only), (2, 1))

    def test_infeasible_total_mismatch(self):
        rows = make_rows([
            ("B1", VAT, (True, True)),
            ("B2", VAT, (True, True)),
        ])
        quotas = {"batches": {"B1": 1, "B2": 1}, "types": {VAT: 1}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertFalse(result["feasible"])
        self.assertEqual(result["selected_ids"], [])
        self.assertTrue(any("总量不一致" in line["kind"] for line in result["tables"][0]["rows"]))

    def test_total_mismatch_never_reports_feasible(self):
        # 回归：批次合计与票种合计不同时，绝不能只满足一侧就误报凑齐。
        rows = make_rows([
            ("B1", VAT, (True, True)), ("B1", RECEIPT, (False, True)),
            ("B2", VAT, (True, False)), ("B2", RECEIPT, (True, True)),
        ])
        quotas = {"batches": {"B1": 1, "B2": 1}, "types": {VAT: 1, RECEIPT: 2}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertFalse(result["feasible"])
        self.assertIsNone(brute_force(rows, quotas))
        self.assertTrue(any("总量不一致" in line["kind"] for line in result["tables"][0]["rows"]))

    def test_infeasible_batch_shortfall(self):
        rows = make_rows([
            ("B1", VAT, (True, True)),
            ("B2", VAT, (True, True)),
            ("B2", VAT, (False, True)),
        ])
        quotas = {"batches": {"B1": 2, "B2": 1}, "types": {VAT: 3}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertFalse(result["feasible"])
        kinds = [line["kind"] for line in result["tables"][0]["rows"]]
        self.assertIn("扫描批次票量不足", kinds)
        conflict = next(line for line in result["tables"][0]["rows"]
                        if line["kind"] == "扫描批次票量不足")
        self.assertIn("B1", conflict["batches"])

    def test_infeasible_hall_combination_conflict(self):
        # 每个单点票量都够、总量也够，但 B1 只持有增值税票，
        # 而票种要求 2 张零售小票，组合层面（Hall 条件）无法同时满足。
        rows = make_rows([
            ("B1", VAT, (True, True)),
            ("B1", VAT, (True, False)),
            ("B2", VAT, (True, True)),
            ("B2", VAT, (False, True)),
            ("B3", RECEIPT, (True, True)),
            ("B3", RECEIPT, (False, False)),
        ])
        quotas = {"batches": {"B1": 2, "B2": 2, "B3": 1},
                  "types": {VAT: 3, RECEIPT: 2}}
        # 单维与总量校验都通过：每批次票量不超存量，增值税 4 张、零售 2 张，总量各 5。
        # 但 B1/B2 只持增值税票且合计要 4 张，增值税票种却只要 3 张（Hall 条件不成立）。
        result = select_quota(rows, {"quotas": quotas})
        self.assertFalse(result["feasible"])
        self.assertIsNone(brute_force(rows, quotas))
        kinds = [line["kind"] for line in result["tables"][0]["rows"]]
        self.assertEqual(kinds, ["批次与票种组合冲突"])
        conflict = result["tables"][0]["rows"][0]
        self.assertEqual(conflict["batches"], "B1、B2")
        self.assertEqual(conflict["types"], VAT)
        # 冲突批次要求 4 张，而其能送达的票种（增值税）在两套票量里只承接 3 张。
        self.assertEqual((conflict["required"], conflict["available"]), (4, 4))
        self.assertIn("4", conflict["detail"])

    def test_normalize_quotas_rejects_unknown_batch(self):
        rows = make_rows([("B1", VAT, (True, True))])
        with self.assertRaises(ValueError):
            normalize_quotas({"batches": {"B9": 1}, "types": {VAT: 1}}, rows)

    def test_normalize_quotas_rejects_non_positive(self):
        rows = make_rows([("B1", VAT, (True, True))])
        with self.assertRaises(ValueError):
            normalize_quotas({"batches": {"B1": -1}, "types": {VAT: 1}}, rows)

    def test_normalize_quotas_accepts_zero(self):
        rows = make_rows([("B1", VAT, (True, True)), ("B2", RECEIPT, (True, True))])
        cleaned = normalize_quotas(
            {"batches": {"B1": 1, "B2": 0}, "types": {VAT: 1, RECEIPT: 0}}, rows)
        self.assertEqual(cleaned["batches"]["B2"], 0)

    def test_all_zero_quotas_rejected(self):
        rows = make_rows([("B1", VAT, (True, True)), ("B1", RECEIPT, (True, True))])
        with self.assertRaises(ValueError):
            select_quota(rows, {"quotas": {"batches": {"B1": 0}, "types": {VAT: 0, RECEIPT: 0}}})

    def test_missing_quotas_rejected(self):
        rows = make_rows([("B1", VAT, (True, True))])
        with self.assertRaises(ValueError):
            select_quota(rows, {})

    def test_feasible_result_keeps_quotas_per_dimension(self):
        rows = make_rows([
            ("B1", VAT, (True, False)),
            ("B1", RECEIPT, (False, True)),
            ("B2", VAT, (False, True)),
            ("B2", RECEIPT, (True, True)),
        ])
        quotas = {"batches": {"B1": 1, "B2": 1}, "types": {VAT: 1, RECEIPT: 1}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertTrue(result["feasible"])
        # 批次表与票种表逐维显示「凑齐」且入选票量等于要求票量。
        for table in result["tables"][1:3]:
            self.assertTrue(all(row["status"] == "凑齐" for row in table["rows"]))
        # 入选票号在原始数据中存在且唯一。
        ids = set(result["selected_ids"])
        self.assertEqual(len(ids), 2)
        self.assertTrue(ids <= {row["document_id"] for row in rows})

    def test_demo_scale_dataset(self):
        # 与 seed_demo 同构的 64 张两批次样本，验证规模与口径。
        rows = []
        counter = 1
        for batch_id, doc_type, counts in (
            ("CS-FP-1121-AM", VAT, (3, 24, 8, 2)),
            ("CS-XP-1121-AM", RECEIPT, (0, 18, 7, 2)),
        ):
            for outcome, count in zip(OUTCOMES, counts):
                for _ in range(count):
                    rows.append({
                        "document_id": f"CS-20261121-{counter:03d}",
                        "batch_id": batch_id,
                        "document_type": doc_type,
                        "a_pass": outcome[0],
                        "b_pass": outcome[1],
                    })
                    counter += 1
        # 批次 1 全是增值税票、批次 2 全是零售小票，两套票量在该结构下必须相等。
        quotas = {"batches": {"CS-FP-1121-AM": 20, "CS-XP-1121-AM": 15},
                  "types": {VAT: 20, RECEIPT: 15}}
        result = select_quota(rows, {"quotas": quotas})
        self.assertTrue(result["feasible"])
        self.assertEqual(len(result["selected_ids"]), 35)
        self.assertEqual(result["selected_ids"], sorted(result["selected_ids"]))
        self.assertEqual(len(set(result["selected_ids"])), 35)


if __name__ == "__main__":
    unittest.main()
