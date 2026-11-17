from __future__ import annotations

import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from scripts import unit_test_shards


class UnitTestShardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name) / "src"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(
        self,
        relative: str,
        *,
        named: int = 0,
        decltests: int = 0,
        unnamed: int = 0,
        extra: str = "",
    ) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for index in range(named):
            lines.append(f'test "case_{index}" {{}}')
        for index in range(decltests):
            lines.append(f"const decl_{index} = 0;")
            lines.append(f"test decl_{index} {{}}")
        for _ in range(unnamed):
            lines.append("test {}")
        if extra:
            lines.append(extra)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = unit_test_shards.main(list(args) + [str(self.root)])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_every_file_maps_to_exactly_one_prefix_and_every_prefix_to_one_shard(self) -> None:
        self.write("alpha/a.zig", named=2, unnamed=1, extra='// test "commented" {}')
        self.write("alpha/deep/b.zig", named=1)
        self.write("beta/c.zig", named=1)
        self.write("standalone.zig", named=1)
        plan = unit_test_shards.make_plan(self.root, 2)
        self.assertEqual(plan.total, 5)
        self.assertEqual(
            set(plan.prefixes),
            {"alpha.a.", "alpha.deep.", "beta.", "standalone."},
        )
        self.assertEqual(unit_test_shards.check_plan(plan), [])
        seen = [prefix for shard in plan.shards for prefix, _ in shard]
        self.assertEqual(sorted(seen), sorted(plan.prefixes))
        status, _, stderr = self.run_main("--check")
        self.assertEqual(status, 0, stderr)
        self.assertIn("5/5 named tests covered", stderr)

    def test_a_directory_and_a_file_with_one_stem_share_a_prefix(self) -> None:
        self.write("merge.zig", named=1)
        self.write("merge/child.zig", named=2)
        plan = unit_test_shards.make_plan(self.root, 1)
        self.assertEqual(list(plan.prefixes), ["merge."])
        self.assertEqual(len(plan.prefixes["merge."]), 2)
        self.assertEqual(plan.loads, (3,))
        self.assertEqual(unit_test_shards.check_plan(plan), [])

        split = unit_test_shards.make_plan(self.root, 3)
        self.assertEqual(set(split.prefixes), {"merge.", "merge.child."})
        self.assertEqual(split.loads, (2, 1, 0))
        self.assertEqual(split.predicted, (2, 3, 0))
        self.assertEqual(split.covered, 3)
        self.assertEqual(unit_test_shards.check_plan(split), [])

    def test_output_is_deterministic(self) -> None:
        self.write("alpha/a.zig", named=2)
        self.write("alpha/deep/b.zig", named=1)
        self.write("beta/c.zig", named=1)
        first = unit_test_shards.make_plan(self.root, 2)
        second = unit_test_shards.make_plan(self.root, 2)
        self.assertEqual(first, second)
        status_one, stdout_one, _ = self.run_main("--count", "2", "--index", "1")
        status_two, stdout_two, _ = self.run_main("--count", "2", "--index", "1")
        self.assertEqual((status_one, stdout_one), (status_two, stdout_two))
        self.assertGreater(len(stdout_one.splitlines()), 0)

    def test_a_heavy_directory_splits_into_its_children(self) -> None:
        self.write("heavy/one.zig", named=5)
        self.write("heavy/two.zig", named=5)
        self.write("light.zig", named=1)
        plan = unit_test_shards.make_plan(self.root, 3)
        self.assertEqual(set(plan.prefixes), {"heavy.one.", "heavy.two.", "light."})
        self.assertEqual(plan.loads, (5, 5, 1))
        self.assertEqual(unit_test_shards.check_plan(plan), [])

    def test_count_one_keeps_one_filter_for_each_top_level_entry(self) -> None:
        self.write("alpha/a.zig", named=2)
        self.write("beta/deep/b.zig", named=1)
        self.write("gamma.zig", named=1)
        plan = unit_test_shards.make_plan(self.root, 1)
        self.assertEqual(set(plan.prefixes), {"alpha.", "beta.", "gamma."})
        self.assertEqual(len(plan.shards), 1)
        status, stdout, _ = self.run_main("--count", "1", "--index", "1")
        self.assertEqual(status, 0)
        self.assertEqual(
            set(stdout.splitlines()),
            {
                "-Dtest-filter=alpha.",
                "-Dtest-filter=beta.",
                "-Dtest-filter=gamma.",
            },
        )

    def test_predicted_counts_include_cross_shard_substring_matches(self) -> None:
        self.write("ui/a.zig", named=1)
        self.write("core/ui/b.zig", named=1)
        self.write("domain/c.zig", named=1)
        self.write("main.zig", named=1)
        plan = unit_test_shards.make_plan(self.root, 2)
        self.assertEqual(set(plan.prefixes), {"core.", "domain.", "main.", "ui."})
        self.assertEqual(plan.loads, (2, 2))
        self.assertEqual(plan.predicted, (3, 3))
        self.assertEqual(plan.covered, 4)
        self.assertEqual(plan.total, 4)
        self.assertEqual(sum(plan.predicted) - plan.covered, 2)
        status, _, stderr = self.run_main("--count", "2", "--plan")
        self.assertEqual(status, 0)
        self.assertIn("predicted coverage: 4/4 distinct tests; 6 matches; 2 duplicates", stderr)

    def test_decltests_get_their_own_qualified_name(self) -> None:
        self.write("thing.zig", decltests=1)
        plan = unit_test_shards.make_plan(self.root, 1)
        self.assertEqual(plan.total, 1)
        names = unit_test_shards.qualified_test_names(plan.files)
        self.assertEqual(names, [(("thing.zig", 0, "decltest", "decl_0"), "thing.decltest.decl_0")])
        self.assertEqual(plan.predicted, (1,))


if __name__ == "__main__":
    unittest.main()
