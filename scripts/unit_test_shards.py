#!/usr/bin/env python3
"""Split the Zig unit tests into source-namespace shards.

Zig names a test `<path relative to the module root, '/' to '.', '.zig'
dropped>.test.<name>` (or `.decltest.<name>` for a decltest), and
`addTest(.{ .filters = ... })` compiles only the tests whose name contains one
of the filter strings. A filter such as `core.agent.` therefore shrinks both
the test binary a shard compiles and the tests it runs.

Tests are counted per file, prefixes start at each top-level entry of src/,
and every prefix heavier than `total / count` splits into its children until
each shard is near the target. A directory and a file with the same stem share
one prefix, because Zig gives both the same namespace.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "src"

NAMED_TEST_RE = re.compile(
    r'^[ \t]*test[ \t]+"((?:[^"\\]|\\.)*)"[ \t\r\n]*\{',
    re.MULTILINE,
)
DECL_TEST_RE = re.compile(
    r"^[ \t]*test[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t\r\n]*\{",
    re.MULTILINE,
)


class TestDecl(NamedTuple):
    kind: str
    name: str


class SourceFile(NamedTuple):
    relative: str
    namespace: str
    tests: Tuple[TestDecl, ...]


class Plan(NamedTuple):
    files: Tuple[SourceFile, ...]
    prefixes: Dict[str, Tuple[SourceFile, ...]]
    shards: Tuple[Tuple[Tuple[str, int], ...], ...]
    loads: Tuple[int, ...]
    predicted: Tuple[int, ...]
    covered: int
    total: int


def read_source_file(path: pathlib.Path, root: pathlib.Path) -> SourceFile:
    relative = path.relative_to(root)
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = []
    for match in NAMED_TEST_RE.finditer(text):
        matches.append((match.start(), TestDecl("test", match.group(1))))
    for match in DECL_TEST_RE.finditer(text):
        matches.append((match.start(), TestDecl("decltest", match.group(1))))
    matches.sort(key=lambda entry: entry[0])
    namespace = ".".join(relative.with_suffix("").parts)
    return SourceFile(relative.as_posix(), namespace, tuple(test for _, test in matches))


def load_source_files(root: pathlib.Path) -> Tuple[SourceFile, ...]:
    return tuple(
        read_source_file(path, root)
        for path in sorted(root.rglob("*.zig"))
        if path.is_file()
    )


def file_weight(files: Iterable[SourceFile]) -> int:
    return sum(len(source.tests) for source in files)


def belongs(namespace: str, prefix: str) -> bool:
    return namespace == prefix[:-1] or namespace.startswith(prefix)


def top_level_prefix(namespace: str) -> str:
    return namespace.split(".", 1)[0] + "."


def split_prefix(prefix: str, files: Sequence[SourceFile]) -> Tuple[List[SourceFile], Dict[str, List[SourceFile]]]:
    parent = prefix[:-1]
    own = [source for source in files if source.namespace == parent]
    groups: Dict[str, List[SourceFile]] = {}
    for source in files:
        if source.namespace.startswith(prefix):
            rest = source.namespace[len(prefix):]
            component = rest.split(".", 1)[0]
            groups.setdefault(prefix + component + ".", []).append(source)
    return own, groups


def build_prefixes(
    files: Sequence[SourceFile], count: int
) -> Dict[str, Tuple[SourceFile, ...]]:
    total = file_weight(files)
    top: Dict[str, List[SourceFile]] = {}
    for source in files:
        top.setdefault(top_level_prefix(source.namespace), []).append(source)

    chosen: Dict[str, Tuple[SourceFile, ...]] = {}

    def choose(prefix: str, group: Sequence[SourceFile]) -> None:
        weight = file_weight(group)
        if weight * count <= total:
            chosen[prefix] = tuple(group)
            return
        own, children = split_prefix(prefix, group)
        if own:
            chosen[prefix] = tuple(own)
        for child in sorted(children):
            choose(child, children[child])

    for prefix in sorted(top):
        choose(prefix, top[prefix])
    return chosen


def assign_shards(
    prefixes: Dict[str, Tuple[SourceFile, ...]], count: int
) -> Tuple[Tuple[Tuple[str, int], ...], ...]:
    entries = [(prefix, file_weight(files)) for prefix, files in prefixes.items()]
    entries.sort(key=lambda entry: (-entry[1], entry[0]))
    shards: List[List[Tuple[str, int]]] = [[] for _ in range(count)]
    loads = [0] * count
    for prefix, weight in entries:
        shard = min(range(count), key=lambda index: (loads[index], index))
        shards[shard].append((prefix, weight))
        loads[shard] += weight
    return tuple(tuple(sorted(shard, key=lambda entry: entry[0])) for shard in shards)


def qualified_test_names(files: Sequence[SourceFile]) -> List[Tuple[Tuple[str, int, str, str], str]]:
    names = []
    for source in files:
        for ordinal, test in enumerate(source.tests):
            identity = (source.relative, ordinal, test.kind, test.name)
            names.append((identity, source.namespace + "." + test.kind + "." + test.name))
    return names


def predict(
    files: Sequence[SourceFile], shards: Sequence[Sequence[Tuple[str, int]]]
) -> Tuple[Tuple[int, ...], int, int]:
    names = qualified_test_names(files)
    predicted = []
    covered = set()
    for shard in shards:
        filters = [prefix for prefix, _ in shard]
        matches = {
            identity for identity, qualified in names if any(f in qualified for f in filters)
        }
        predicted.append(len(matches))
        covered.update(matches)
    return tuple(predicted), len(covered), len(names)


def make_plan(root: pathlib.Path, count: int) -> Plan:
    if count < 1:
        raise ValueError("count must be at least 1")
    files = load_source_files(root)
    prefixes = build_prefixes(files, count)
    shards = assign_shards(prefixes, count)
    loads = tuple(sum(weight for _, weight in shard) for shard in shards)
    predicted, covered, total = predict(files, shards)
    return Plan(files, prefixes, shards, loads, predicted, covered, total)


def deepest_prefix(namespace: str, prefixes: Iterable[str]) -> List[str]:
    matches = [prefix for prefix in prefixes if belongs(namespace, prefix)]
    if not matches:
        return []
    longest = max(len(prefix) for prefix in matches)
    return sorted(prefix for prefix in matches if len(prefix) == longest)


def check_plan(plan: Plan) -> List[str]:
    errors: List[str] = []
    assigned: Dict[str, int] = {}
    for index, shard in enumerate(plan.shards):
        for prefix, _ in shard:
            if prefix in assigned:
                errors.append(f"{prefix} is in shards {assigned[prefix]} and {index + 1}")
            assigned[prefix] = index + 1
    for prefix in plan.prefixes:
        if prefix not in assigned:
            errors.append(f"{prefix} is in no shard")
    for index, shard in enumerate(plan.shards):
        if not shard or plan.loads[index] == 0:
            errors.append(f"shard {index + 1} has no tests")
    mapped: Dict[str, List[str]] = {prefix: [] for prefix in plan.prefixes}
    for source in plan.files:
        best = deepest_prefix(source.namespace, plan.prefixes)
        if not best:
            errors.append(f"{source.relative} maps to no prefix")
        elif len(best) > 1:
            errors.append(f"{source.relative} maps to several prefixes: {', '.join(best)}")
        else:
            mapped[best[0]].append(source.relative)
    for prefix, files in plan.prefixes.items():
        if not files:
            errors.append(f"{prefix} has no files")
        expected = sorted(source.relative for source in files)
        actual = sorted(mapped.get(prefix, []))
        if actual != expected:
            errors.append(
                f"{prefix} holds {expected} but {actual or 'no files'} map to it"
            )
    if plan.covered != plan.total:
        errors.append(f"{plan.covered} of {plan.total} tests are covered")
    return errors


def print_plan(plan: Plan, count: int) -> None:
    print(
        f"unit test shards: {count} shards, {plan.total} named tests, "
        f"{len(plan.prefixes)} prefixes, target {plan.total / count:.1f}",
        file=sys.stderr,
    )
    for index, shard in enumerate(plan.shards):
        print(f"\nshard {index + 1}/{count}: {plan.loads[index]} named tests", file=sys.stderr)
        for prefix, weight in shard:
            print(f"  {prefix} {weight}", file=sys.stderr)
    print("", file=sys.stderr)
    for index, predicted in enumerate(plan.predicted):
        print(f"predicted shard {index + 1}/{count}: {predicted} tests", file=sys.stderr)
    total_predicted = sum(plan.predicted)
    print(
        f"predicted coverage: {plan.covered}/{plan.total} distinct tests; "
        f"{total_predicted} matches; {total_predicted - plan.covered} duplicates",
        file=sys.stderr,
    )


def plan_json(plan: Plan, count: int, source: pathlib.Path) -> str:
    payload = {
        "source": source.as_posix(),
        "shard_count": count,
        "total_named_tests": plan.total,
        "target_tests_per_shard": plan.total / count,
        "prefix_count": len(plan.prefixes),
        "predicted_distinct_tests": plan.covered,
        "predicted_matches": sum(plan.predicted),
        "predicted_duplicates": sum(plan.predicted) - plan.covered,
        "shards": [
            {
                "shard": index + 1,
                "weight": plan.loads[index],
                "predicted_tests": plan.predicted[index],
                "prefixes": [
                    {"prefix": prefix, "weight": weight} for prefix, weight in shard
                ],
            }
            for index, shard in enumerate(plan.shards)
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split fx's Zig unit tests into source-namespace shards.",
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=str(DEFAULT_SOURCE),
        help="source tree to inspect (default: the repository's src/)",
    )
    parser.add_argument("--count", type=int, help="number of shards")
    parser.add_argument("--index", type=int, help="this shard, 1-based")
    parser.add_argument("--plan", action="store_true", help="print the plan to stderr")
    parser.add_argument("--check", action="store_true", help="check that every file maps to one prefix")
    parser.add_argument("--json", action="store_true", help="print the plan and predictions as JSON")
    args = parser.parse_args(argv)
    if args.index is not None and args.count is None:
        parser.error("--index requires --count")
    if args.index is not None and not 1 <= args.index <= args.count:
        parser.error("--index must be between 1 and --count")
    if args.json and args.index is not None:
        parser.error("--json cannot be combined with --count and --index")
    if args.index is None and not (args.plan or args.check or args.json):
        parser.error("give --count and --index, --plan, --check, or --json")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = pathlib.Path(args.source)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    count = args.count if args.count is not None else 4
    plan = make_plan(root, count)
    if args.json:
        sys.stdout.write(plan_json(plan, count, root))
        return 0
    if args.plan:
        print_plan(plan, count)
    if args.check:
        errors = check_plan(plan)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        print(
            f"ok: {len(plan.files)} files map to {len(plan.prefixes)} prefixes, "
            f"{plan.covered}/{plan.total} named tests covered",
            file=sys.stderr,
        )
    if args.index is not None:
        for prefix, _ in plan.shards[args.index - 1]:
            print(f"-Dtest-filter={prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
