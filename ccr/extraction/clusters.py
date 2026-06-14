from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccr.extraction.token_budget import (
    SourceBudget,
    SourceTokenCount,
    TokenCounter,
    count_source_tokens,
    source_budget_for_model,
)
from ccr.schemas.unit import CodeUnit, UnitKind

_IDENTIFIER_STOP_WORDS = {
    "a",
    "an",
    "and",
    "api",
    "as",
    "by",
    "for",
    "from",
    "get",
    "id",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "set",
    "the",
    "to",
    "with",
}
_CLUSTER_CONTEXT_FRACTION = 0.25
_MIN_CONTEXT_RETENTION_FRACTION = 0.5
_MAX_CONTEXT_PROFILES = 8
_MIN_COMPONENT_EDGE_SCORE = 55.0
_MIN_CONTEXT_EDGE_SCORE = 100.0
_MIN_SUBSTANTIAL_COMPONENT_FILE_COUNT = 2
_MIN_SUBSTANTIAL_COMPONENT_TOKEN_FRACTION = 0.5
_MERGE_BALANCE_PROFILE_SLACK = 1.5
_MERGE_BALANCE_TOKEN_SLACK = 1.35
_REFINEMENT_MAX_PASSES = 4
_REFINEMENT_MIN_AFFINITY_SCORE = 55.0
_REFINEMENT_MIN_ABSOLUTE_GAIN = 5.0
_REFINEMENT_MIN_RELATIVE_GAIN = 1.05
_REFINEMENT_TOKEN_BALANCE_SLACK = 1.05
_REFINEMENT_MIN_SOURCE_PROFILES_AFTER_MOVE = 2


@dataclass(frozen=True)
class FileProfile:
    profile_id: str
    path: str
    module: str
    text: str
    start_line: int
    end_line: int
    token_count: int
    line_count: int
    imported_modules: frozenset[str]
    imported_names: frozenset[str]
    defined_symbols: frozenset[str]
    referenced_names: frozenset[str]
    identifier_tokens: frozenset[str]
    locked: bool = False
    section_name: str | None = None


@dataclass
class ClusterGroup:
    profiles: list[FileProfile]
    locked: bool = False
    context_profiles: list[FileProfile] = field(default_factory=list)

    @property
    def profile_ids(self) -> set[str]:
        return {profile.profile_id for profile in self.profiles}

    @property
    def owned_paths(self) -> list[str]:
        return sorted(dict.fromkeys(profile.path for profile in self.profiles))

    @property
    def token_count(self) -> int:
        return sum(profile.token_count for profile in self.profiles)


@dataclass(frozen=True)
class ClusterComponent:
    profiles: tuple[FileProfile, ...]

    @property
    def owned_paths(self) -> list[str]:
        return sorted(dict.fromkeys(profile.path for profile in self.profiles))

    @property
    def token_count(self) -> int:
        return sum(profile.token_count for profile in self.profiles)


def extract_cluster_units(
    project_root: Path,
    *,
    model: str | None = None,
    target_unit_count: int = 5,
    excluded_dirs: set[str] | None = None,
) -> list[CodeUnit]:
    from ccr.extraction.units import iter_python_files

    target = max(1, target_unit_count)
    counter = TokenCounter.for_model(model)
    budget = source_budget_for_model(model)
    profiles: list[FileProfile] = []
    for path in iter_python_files(project_root, excluded_dirs):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            continue
        profile = _profile_file(project_root, path, text, counter)
        if profile.token_count > budget.source_tokens:
            profiles.extend(_split_oversized_profile(project_root, profile, counter, budget))
        else:
            profiles.append(profile)

    if not profiles:
        return []

    edges = _build_edges(profiles)
    groups = _build_initial_cluster_groups(
        profiles,
        edges,
        target=target,
        counter=counter,
        budget=budget,
    )
    while True:
        _attach_context(groups, profiles, edges, counter, budget)
        groups, split_occurred = _fit_groups_to_source_budget(
            project_root,
            groups,
            counter=counter,
            budget=budget,
        )
        if not split_occurred:
            break
    groups = sorted(groups, key=lambda group: (group.owned_paths[0], len(group.owned_paths)))
    return [
        _cluster_unit(
            index=index,
            project_root=project_root,
            group=group,
            counter=counter,
            budget=budget,
        )
        for index, group in enumerate(groups, start=1)
    ]


def _profile_file(
    project_root: Path,
    path: Path,
    text: str,
    counter: TokenCounter,
) -> FileProfile:
    relative_path = path.relative_to(project_root).as_posix()
    module = _module_name(relative_path)
    imported_modules, imported_names, defined_symbols, referenced_names = _inspect_python(
        text,
        module=module,
    )
    identifier_tokens = _identifier_tokens(
        [relative_path, module, *defined_symbols, *referenced_names, *imported_names]
    )
    line_count = max(1, text.count("\n") + 1)
    return FileProfile(
        profile_id=relative_path,
        path=relative_path,
        module=module,
        text=text,
        start_line=1,
        end_line=line_count,
        token_count=counter.count(text),
        line_count=line_count,
        imported_modules=frozenset(imported_modules),
        imported_names=frozenset(imported_names),
        defined_symbols=frozenset(defined_symbols),
        referenced_names=frozenset(referenced_names),
        identifier_tokens=frozenset(identifier_tokens),
    )


def _inspect_python(
    text: str,
    *,
    module: str,
) -> tuple[set[str], set[str], set[str], set[str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set(), set(), set(), _identifier_tokens([text])

    visitor = _ProfileVisitor(module=module)
    visitor.visit(tree)
    return (
        visitor.imported_modules,
        visitor.imported_names,
        visitor.defined_symbols,
        visitor.referenced_names,
    )


class _ProfileVisitor(ast.NodeVisitor):
    def __init__(self, *, module: str) -> None:
        self.module = module
        self.imported_modules: set[str] = set()
        self.imported_names: set[str] = set()
        self.defined_symbols: set[str] = set()
        self.referenced_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.imported_modules.add(alias.name)
            self.imported_names.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        base_module = _resolve_import_from_module(self.module, node)
        if base_module:
            self.imported_modules.add(base_module)
        for alias in node.names:
            if alias.name == "*":
                continue
            self.imported_names.add(alias.asname or alias.name)
            if base_module:
                self.imported_modules.add(f"{base_module}.{alias.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.defined_symbols.add(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.defined_symbols.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.defined_symbols.add(node.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        for target in node.targets:
            self._add_assignment_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        self._add_assignment_target(node.target)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.referenced_names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        self.referenced_names.add(node.attr)
        self.generic_visit(node)

    def _add_assignment_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.defined_symbols.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._add_assignment_target(element)


def _split_oversized_profile(
    project_root: Path,
    profile: FileProfile,
    counter: TokenCounter,
    budget: SourceBudget,
) -> list[FileProfile]:
    try:
        tree = ast.parse(profile.text)
    except SyntaxError:
        return _line_chunk_profiles(profile, counter, budget)

    lines = profile.text.splitlines()
    outline = _file_outline(profile.path, tree)
    sections: list[FileProfile] = []
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end_line = getattr(node, "end_lineno", None)
        if end_line is None:
            continue
        section_text = "\n".join(lines[node.lineno - 1 : end_line]) + "\n"
        text = (
            f"{outline}\n\n"
            f"# Full source region: {profile.path}:{node.lineno}-{end_line}\n"
            f"{section_text}"
        )
        token_count = counter.count(text)
        if token_count > budget.source_tokens:
            sections.extend(
                _line_chunk_profiles(
                    profile,
                    counter,
                    budget,
                    start_line=node.lineno,
                    end_line=end_line,
                    prefix=outline,
                    section_name=node.name,
                )
            )
            continue
        sections.append(
            _section_profile(
                profile,
                text=text,
                start_line=node.lineno,
                end_line=end_line,
                section_name=node.name,
                token_count=token_count,
                counter=counter,
                project_root=project_root,
            )
        )

    return sections or _line_chunk_profiles(profile, counter, budget, prefix=outline)


def _section_profile(
    base: FileProfile,
    *,
    text: str,
    start_line: int,
    end_line: int,
    section_name: str,
    token_count: int,
    counter: TokenCounter,
    project_root: Path,
) -> FileProfile:
    imported_modules, imported_names, defined_symbols, referenced_names = _inspect_python(
        text,
        module=base.module,
    )
    identifier_tokens = _identifier_tokens(
        [base.path, base.module, section_name, *defined_symbols, *referenced_names, *imported_names]
    )
    return FileProfile(
        profile_id=f"{base.path}::{section_name}",
        path=base.path,
        module=base.module,
        text=text,
        start_line=start_line,
        end_line=end_line,
        token_count=token_count or counter.count(text),
        line_count=max(1, end_line - start_line + 1),
        imported_modules=frozenset(imported_modules),
        imported_names=frozenset(imported_names),
        defined_symbols=frozenset(defined_symbols or {section_name}),
        referenced_names=frozenset(referenced_names),
        identifier_tokens=frozenset(identifier_tokens),
        locked=True,
        section_name=section_name,
    )


def _line_chunk_profiles(
    profile: FileProfile,
    counter: TokenCounter,
    budget: SourceBudget,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    prefix: str = "",
    section_name: str | None = None,
) -> list[FileProfile]:
    all_lines = profile.text.splitlines()
    start = start_line or 1
    end = end_line or len(all_lines)
    chunk_budget = max(1, budget.source_tokens - counter.count(prefix) - 200)
    chunks: list[FileProfile] = []
    current: list[str] = []
    current_start = start
    for line_number in range(start, end + 1):
        current.append(all_lines[line_number - 1])
        text = _chunk_text(profile.path, current_start, line_number, current, prefix)
        if current and counter.count(text) > chunk_budget:
            overflow = current.pop()
            chunk_end = line_number - 1
            if current:
                chunks.append(
                    _chunk_profile(
                        profile,
                        _chunk_text(profile.path, current_start, chunk_end, current, prefix),
                        current_start,
                        chunk_end,
                        counter,
                        section_name=section_name,
                    )
                )
            current = [overflow]
            current_start = line_number
    if current:
        chunks.append(
            _chunk_profile(
                profile,
                _chunk_text(profile.path, current_start, end, current, prefix),
                current_start,
                end,
                counter,
                section_name=section_name,
            )
        )
    return chunks


def _chunk_text(
    path: str,
    start_line: int,
    end_line: int,
    lines: list[str],
    prefix: str,
) -> str:
    body = "\n".join(lines) + "\n"
    return f"{prefix}\n\n# Full source region: {path}:{start_line}-{end_line}\n{body}"


def _chunk_profile(
    base: FileProfile,
    text: str,
    start_line: int,
    end_line: int,
    counter: TokenCounter,
    *,
    section_name: str | None,
) -> FileProfile:
    label = section_name or f"lines-{start_line}-{end_line}"
    imported_modules, imported_names, defined_symbols, referenced_names = _inspect_python(
        text,
        module=base.module,
    )
    return FileProfile(
        profile_id=f"{base.path}::{label}:{start_line}-{end_line}",
        path=base.path,
        module=base.module,
        text=text,
        start_line=start_line,
        end_line=end_line,
        token_count=counter.count(text),
        line_count=max(1, end_line - start_line + 1),
        imported_modules=frozenset(imported_modules),
        imported_names=frozenset(imported_names),
        defined_symbols=frozenset(defined_symbols),
        referenced_names=frozenset(referenced_names),
        identifier_tokens=frozenset(
            _identifier_tokens([base.path, base.module, label, *defined_symbols, *referenced_names])
        ),
        locked=True,
        section_name=label,
    )


def _build_edges(profiles: list[FileProfile]) -> dict[tuple[str, str], float]:
    edges: dict[tuple[str, str], float] = {}
    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            score = _edge_score(left, right)
            if score > 0:
                edges[_edge_key(left.profile_id, right.profile_id)] = score
    return edges


def _build_initial_cluster_groups(
    profiles: list[FileProfile],
    edges: dict[tuple[str, str], float],
    *,
    target: int,
    counter: TokenCounter,
    budget: SourceBudget,
) -> list[ClusterGroup]:
    components = _profile_components(
        profiles,
        edges,
        min_score=_MIN_COMPONENT_EDGE_SCORE,
    )
    substantial_components, residual_components = _partition_cluster_components(components)
    component_targets = _allocate_component_targets(substantial_components, target=target)

    groups: list[ClusterGroup] = []
    for component, component_target in zip(
        substantial_components,
        component_targets,
        strict=True,
    ):
        component_groups = [
            ClusterGroup([profile], locked=profile.locked)
            for profile in component.profiles
        ]
        balance_limits = _merge_balance_limits(component_groups, target=component_target)
        groups.extend(
            _refine_group_ownership(
                _merge_groups(
                    component_groups,
                    edges,
                    target=component_target,
                    counter=counter,
                    budget=budget,
                ),
                edges,
                counter=counter,
                budget=budget,
                balance_limits=balance_limits,
            )
        )

    groups.extend(
        _pack_residual_components(
            residual_components,
            counter=counter,
            budget=budget,
        )
    )
    return groups


def _profile_components(
    profiles: list[FileProfile],
    edges: dict[tuple[str, str], float],
    *,
    min_score: float,
) -> list[ClusterComponent]:
    profiles_by_id = {profile.profile_id: profile for profile in profiles}
    adjacency: dict[str, set[str]] = {
        profile.profile_id: set()
        for profile in profiles
    }
    for (left_id, right_id), score in edges.items():
        if score < min_score:
            continue
        if left_id not in profiles_by_id or right_id not in profiles_by_id:
            continue
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)

    components: list[ClusterComponent] = []
    seen: set[str] = set()
    for profile in sorted(profiles, key=_profile_sort_key):
        if profile.profile_id in seen:
            continue
        component_ids: list[str] = []
        stack = [profile.profile_id]
        while stack:
            profile_id = stack.pop()
            if profile_id in seen:
                continue
            seen.add(profile_id)
            component_ids.append(profile_id)
            next_ids = sorted(adjacency[profile_id] - seen, reverse=True)
            stack.extend(next_ids)

        component_profiles = tuple(
            sorted(
                (profiles_by_id[profile_id] for profile_id in component_ids),
                key=_profile_sort_key,
            )
        )
        components.append(ClusterComponent(component_profiles))

    return sorted(components, key=lambda component: component.owned_paths[0])


def _partition_cluster_components(
    components: list[ClusterComponent],
) -> tuple[list[ClusterComponent], list[ClusterComponent]]:
    if not components:
        return [], []

    largest = max(
        components,
        key=lambda component: (
            component.token_count,
            len(component.profiles),
            tuple(reversed(component.owned_paths)),
        ),
    )
    largest_token_count = max(1, largest.token_count)
    substantial: list[ClusterComponent] = []
    residual: list[ClusterComponent] = []
    for component in components:
        if component is largest or _is_substantial_component(component, largest_token_count):
            substantial.append(component)
        else:
            residual.append(component)
    return substantial, residual


def _is_substantial_component(
    component: ClusterComponent,
    largest_token_count: int,
) -> bool:
    if len(component.profiles) >= _MIN_SUBSTANTIAL_COMPONENT_FILE_COUNT:
        return True
    return (
        component.token_count / largest_token_count
        >= _MIN_SUBSTANTIAL_COMPONENT_TOKEN_FRACTION
    )


def _allocate_component_targets(
    components: list[ClusterComponent],
    *,
    target: int,
) -> list[int]:
    if not components:
        return []

    allocations = [1 for _ in components]
    remaining = target - len(components)
    if remaining <= 0:
        return allocations

    total_tokens = sum(max(1, component.token_count) for component in components)
    ranked: list[tuple[float, int]] = []
    assigned = 0
    for index, component in enumerate(components):
        exact_extra = remaining * (max(1, component.token_count) / total_tokens)
        extra = int(exact_extra)
        allocations[index] += extra
        assigned += extra
        ranked.append((exact_extra - extra, index))

    for _, index in sorted(
        ranked,
        key=lambda item: (
            -item[0],
            -components[item[1]].token_count,
            components[item[1]].owned_paths[0],
        ),
    )[: remaining - assigned]:
        allocations[index] += 1

    return allocations


def _pack_residual_components(
    components: list[ClusterComponent],
    *,
    counter: TokenCounter,
    budget: SourceBudget,
) -> list[ClusterGroup]:
    residual_profiles = [
        profile
        for component in components
        for profile in component.profiles
    ]
    groups: list[ClusterGroup] = []
    current: list[FileProfile] = []

    def flush_current() -> None:
        if current:
            groups.append(ClusterGroup(list(current)))
            current.clear()

    for profile in sorted(residual_profiles, key=_profile_sort_key):
        if profile.locked:
            flush_current()
            groups.append(ClusterGroup([profile], locked=True))
            continue

        candidate = ClusterGroup([*current, profile])
        if current and not _group_fits_source_budget(candidate, counter, budget):
            flush_current()
            candidate = ClusterGroup([profile])

        current.append(profile)
        if not _group_fits_source_budget(candidate, counter, budget):
            flush_current()

    flush_current()
    return groups


def _edge_score(left: FileProfile, right: FileProfile) -> float:
    score = 0.0
    if _imports_profile(left, right):
        score += 120.0
    if _imports_profile(right, left):
        score += 120.0

    left_to_right_symbols = (left.referenced_names | left.imported_names) & right.defined_symbols
    right_to_left_symbols = (right.referenced_names | right.imported_names) & left.defined_symbols
    score += min(90.0, 22.0 * len(left_to_right_symbols | right_to_left_symbols))

    common_path_parts = _common_path_prefix_parts(left.path, right.path)
    if common_path_parts:
        score += min(35.0, 9.0 * common_path_parts)

    overlap = left.identifier_tokens & right.identifier_tokens
    union = left.identifier_tokens | right.identifier_tokens
    if union:
        score += min(45.0, 70.0 * (len(overlap) / len(union)))

    return score


def _imports_profile(importer: FileProfile, imported: FileProfile) -> bool:
    for imported_module in importer.imported_modules:
        if imported_module == imported.module:
            return True
        if imported_module.startswith(f"{imported.module}."):
            return True
        if imported.module.startswith(f"{imported_module}."):
            imported_leaf = imported.module.rsplit(".", 1)[-1]
            if imported_leaf in importer.imported_names:
                return True
    return False


def _merge_groups(
    groups: list[ClusterGroup],
    edges: dict[tuple[str, str], float],
    *,
    target: int,
    counter: TokenCounter,
    budget: SourceBudget,
) -> list[ClusterGroup]:
    balance_limits = _merge_balance_limits(groups, target=target)
    while len(groups) > target:
        merge_pair = (
            _best_merge_pair(
                groups,
                edges,
                counter=counter,
                budget=budget,
                balance_limits=balance_limits,
            )
            or _smallest_path_compatible_pair(
                groups,
                counter=counter,
                budget=budget,
                balance_limits=balance_limits,
            )
            or _best_merge_pair(
                groups,
                edges,
                counter=counter,
                budget=budget,
                balance_limits=None,
            )
            or _smallest_path_compatible_pair(
                groups,
                counter=counter,
                budget=budget,
                balance_limits=None,
            )
        )
        if merge_pair is None:
            break
        left_index, right_index = merge_pair
        groups = _merge_pair(groups, left_index, right_index)

    return groups


def _merge_balance_limits(
    groups: list[ClusterGroup],
    *,
    target: int,
) -> tuple[int, int] | None:
    if target <= 1 or len(groups) <= target:
        return None
    total_profiles = sum(len(group.profiles) for group in groups)
    total_tokens = sum(group.token_count for group in groups)
    largest_group_profiles = max(len(group.profiles) for group in groups)
    largest_group_tokens = max(group.token_count for group in groups)
    profile_limit = max(
        largest_group_profiles,
        math.ceil((total_profiles / target) * _MERGE_BALANCE_PROFILE_SLACK),
    )
    token_limit = max(
        largest_group_tokens,
        math.ceil((total_tokens / target) * _MERGE_BALANCE_TOKEN_SLACK),
    )
    return profile_limit, token_limit


def _best_merge_pair(
    groups: list[ClusterGroup],
    edges: dict[tuple[str, str], float],
    *,
    counter: TokenCounter,
    budget: SourceBudget,
    balance_limits: tuple[int, int] | None,
) -> tuple[int, int] | None:
    best: tuple[float, int, int] | None = None
    for left_index, left in enumerate(groups):
        if left.locked:
            continue
        for right_index, right in enumerate(groups[left_index + 1 :], start=left_index + 1):
            if right.locked:
                continue
            merged = ClusterGroup([*left.profiles, *right.profiles])
            if not _group_fits_source_budget(merged, counter, budget):
                continue
            if not _groups_fit_balance_limits(left, right, balance_limits):
                continue
            score = _group_edge_score(left, right, edges)
            if score <= 0:
                continue
            normalized = score / max(1, len(left.profiles) * len(right.profiles))
            candidate = (normalized, left_index, right_index)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return None
    return best[1], best[2]


def _smallest_path_compatible_pair(
    groups: list[ClusterGroup],
    *,
    counter: TokenCounter,
    budget: SourceBudget,
    balance_limits: tuple[int, int] | None,
) -> tuple[int, int] | None:
    best: tuple[int, int, int] | None = None
    for left_index, left in enumerate(groups):
        if left.locked:
            continue
        for right_index, right in enumerate(groups[left_index + 1 :], start=left_index + 1):
            if right.locked:
                continue
            merged = ClusterGroup([*left.profiles, *right.profiles])
            if not _group_fits_source_budget(merged, counter, budget):
                continue
            if not _groups_fit_balance_limits(left, right, balance_limits):
                continue
            if _common_path_prefix_parts(left.owned_paths[0], right.owned_paths[0]) <= 0:
                continue
            candidate = (left.token_count + right.token_count, left_index, right_index)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None
    return best[1], best[2]


def _groups_fit_balance_limits(
    left: ClusterGroup,
    right: ClusterGroup,
    balance_limits: tuple[int, int] | None,
) -> bool:
    if balance_limits is None:
        return True
    profile_limit, token_limit = balance_limits
    merged_profile_count = len(left.profiles) + len(right.profiles)
    merged_token_count = left.token_count + right.token_count
    return merged_profile_count <= profile_limit and merged_token_count <= token_limit


def _refine_group_ownership(
    groups: list[ClusterGroup],
    edges: dict[tuple[str, str], float],
    *,
    counter: TokenCounter,
    budget: SourceBudget,
    balance_limits: tuple[int, int] | None,
) -> list[ClusterGroup]:
    if len(groups) <= 1:
        return groups

    groups = [
        ClusterGroup(
            sorted(group.profiles, key=_profile_sort_key),
            locked=group.locked,
        )
        for group in groups
    ]
    for _ in range(_REFINEMENT_MAX_PASSES):
        moved = False
        for profile in sorted(
            (profile for group in groups for profile in group.profiles),
            key=_profile_sort_key,
        ):
            source_index = _group_index_for_profile(groups, profile.profile_id)
            if source_index is None:
                continue
            source = groups[source_index]
            if (
                source.locked
                or profile.locked
                or len(source.profiles) <= _REFINEMENT_MIN_SOURCE_PROFILES_AFTER_MOVE
            ):
                continue

            current_score = _profile_group_affinity_score(profile, source, edges)
            target_index = _best_refinement_target(
                profile,
                groups,
                source_index=source_index,
                current_score=current_score,
                edges=edges,
                counter=counter,
                budget=budget,
                balance_limits=balance_limits,
            )
            if target_index is None:
                continue

            groups[source_index] = ClusterGroup(
                [item for item in source.profiles if item.profile_id != profile.profile_id],
                locked=source.locked,
            )
            target = groups[target_index]
            groups[target_index] = ClusterGroup(
                sorted([*target.profiles, profile], key=_profile_sort_key),
                locked=target.locked,
            )
            moved = True

        if not moved:
            break

    return [
        ClusterGroup(sorted(group.profiles, key=_profile_sort_key), locked=group.locked)
        for group in groups
    ]


def _best_refinement_target(
    profile: FileProfile,
    groups: list[ClusterGroup],
    *,
    source_index: int,
    current_score: float,
    edges: dict[tuple[str, str], float],
    counter: TokenCounter,
    budget: SourceBudget,
    balance_limits: tuple[int, int] | None,
) -> int | None:
    best: tuple[float, int] | None = None
    for target_index, target in enumerate(groups):
        if target_index == source_index or target.locked:
            continue
        if not _group_accepts_refinement_profile(
            target,
            profile,
            counter=counter,
            budget=budget,
            balance_limits=balance_limits,
        ):
            continue

        target_score = _profile_group_affinity_score(profile, target, edges)
        if not _is_refinement_gain_clear(target_score, current_score):
            continue
        candidate = (target_score, -target_index)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        return None
    return -best[1]


def _group_accepts_refinement_profile(
    group: ClusterGroup,
    profile: FileProfile,
    *,
    counter: TokenCounter,
    budget: SourceBudget,
    balance_limits: tuple[int, int] | None,
) -> bool:
    candidate = ClusterGroup([*group.profiles, profile], locked=group.locked)
    if not _group_fits_source_budget(candidate, counter, budget):
        return False
    if balance_limits is None:
        return True
    profile_limit, token_limit = balance_limits
    return (
        len(candidate.profiles) <= profile_limit
        and candidate.token_count <= math.ceil(token_limit * _REFINEMENT_TOKEN_BALANCE_SLACK)
    )


def _profile_group_affinity_score(
    profile: FileProfile,
    group: ClusterGroup,
    edges: dict[tuple[str, str], float],
) -> float:
    scores = [
        edges.get(_edge_key(profile.profile_id, other.profile_id), 0.0)
        for other in group.profiles
        if other.profile_id != profile.profile_id
    ]
    if not scores:
        return 0.0
    return sum(scores) / math.sqrt(len(scores))


def _is_refinement_gain_clear(target_score: float, current_score: float) -> bool:
    if target_score < _REFINEMENT_MIN_AFFINITY_SCORE:
        return False
    return (
        target_score >= current_score + _REFINEMENT_MIN_ABSOLUTE_GAIN
        and target_score >= current_score * _REFINEMENT_MIN_RELATIVE_GAIN
    )


def _group_index_for_profile(
    groups: list[ClusterGroup],
    profile_id: str,
) -> int | None:
    for index, group in enumerate(groups):
        if any(profile.profile_id == profile_id for profile in group.profiles):
            return index
    return None


def _merge_pair(
    groups: list[ClusterGroup],
    left_index: int,
    right_index: int,
) -> list[ClusterGroup]:
    merged = ClusterGroup(
        profiles=[*groups[left_index].profiles, *groups[right_index].profiles],
        locked=False,
    )
    next_groups = [
        group for index, group in enumerate(groups) if index not in {left_index, right_index}
    ]
    next_groups.append(merged)
    return next_groups


def _group_edge_score(
    left: ClusterGroup,
    right: ClusterGroup,
    edges: dict[tuple[str, str], float],
) -> float:
    score = 0.0
    for left_profile in left.profiles:
        for right_profile in right.profiles:
            score += edges.get(_edge_key(left_profile.profile_id, right_profile.profile_id), 0.0)
    return score


def _group_fits_source_budget(
    group: ClusterGroup,
    counter: TokenCounter,
    budget: SourceBudget,
) -> bool:
    return counter.count(_cluster_text(group, budget)) <= budget.source_tokens


def _attach_context(
    groups: list[ClusterGroup],
    profiles: list[FileProfile],
    edges: dict[tuple[str, str], float],
    counter: TokenCounter,
    budget: SourceBudget,
) -> None:
    for group in groups:
        owned_ids = group.profile_ids
        candidates: list[tuple[float, FileProfile]] = []
        for profile in profiles:
            if profile.profile_id in owned_ids:
                continue
            score = _max_external_edge_score(owned_ids, profile.profile_id, edges)
            if score >= _MIN_CONTEXT_EDGE_SCORE:
                candidates.append((score, profile))

        context_budget = min(
            max(0, budget.source_tokens - group.token_count),
            int(budget.source_tokens * _CLUSTER_CONTEXT_FRACTION),
        )
        selected: list[FileProfile] = []
        selected_tokens = 0
        for _, profile in sorted(candidates, key=lambda item: (-item[0], item[1].token_count)):
            if len(selected) >= _MAX_CONTEXT_PROFILES:
                break
            candidate_tokens = selected_tokens + profile.token_count
            if candidate_tokens > context_budget:
                continue
            selected.append(profile)
            selected_tokens = candidate_tokens

        group.context_profiles = selected
        while selected and counter.count(_cluster_text(group, budget)) > budget.source_tokens:
            selected.pop()
            group.context_profiles = selected


def _max_external_edge_score(
    owned_ids: set[str],
    profile_id: str,
    edges: dict[tuple[str, str], float],
) -> float:
    if not owned_ids:
        return 0.0
    return max(
        edges.get(_edge_key(owned_id, profile_id), 0.0)
        for owned_id in owned_ids
    )


def _cluster_unit(
    *,
    index: int,
    project_root: Path,
    group: ClusterGroup,
    counter: TokenCounter,
    budget: SourceBudget,
) -> CodeUnit:
    text, token_count = _trim_context_to_source_budget(group, counter=counter, budget=budget)
    slug = _cluster_slug(group, index)
    unit_path = f"cluster/{index:02d}-{slug}"
    line_count = max(1, text.count("\n") + 1)
    owned_paths = group.owned_paths
    context_paths = sorted(dict.fromkeys(profile.path for profile in group.context_profiles))
    estimated_tokens = token_count.tokens
    budget_notes = [*budget.notes, *token_count.notes]
    return CodeUnit(
        unit_id=f"{unit_path}::cluster",
        kind=UnitKind.CLUSTER,
        name=slug,
        qualified_name=f"cluster.{slug.replace('-', '_')}",
        path=unit_path,
        start_line=min(profile.start_line for profile in group.profiles),
        end_line=max(line_count, max(profile.end_line for profile in group.profiles)),
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        member_paths=owned_paths,
        owned_paths=owned_paths,
        context_paths=context_paths,
        estimated_tokens=estimated_tokens,
        source_token_budget=budget.source_tokens,
        model_context_window_tokens=budget.context_window_tokens,
        model_max_output_tokens=budget.max_output_tokens,
        response_reserve_tokens=budget.response_reserve_tokens,
        budget_notes=budget_notes,
    )


def _fit_groups_to_source_budget(
    project_root: Path,
    groups: list[ClusterGroup],
    *,
    counter: TokenCounter,
    budget: SourceBudget,
) -> tuple[list[ClusterGroup], bool]:
    next_groups: list[ClusterGroup] = []
    split_occurred = False
    for group in groups:
        fitted_groups, group_split = _fit_group_to_source_budget(
            project_root,
            group,
            counter=counter,
            budget=budget,
        )
        next_groups.extend(fitted_groups)
        split_occurred = split_occurred or group_split
    return next_groups, split_occurred


def _fit_group_to_source_budget(
    project_root: Path,
    group: ClusterGroup,
    *,
    counter: TokenCounter,
    budget: SourceBudget,
) -> tuple[list[ClusterGroup], bool]:
    original_context = list(group.context_profiles)
    text = _cluster_text(group, budget)
    token_count = count_source_tokens(text, counter=counter, budget=budget)
    if token_count.tokens <= budget.source_tokens:
        return [group], False

    owned_only = ClusterGroup(list(group.profiles), locked=group.locked)
    owned_text = _cluster_text(owned_only, budget)
    owned_token_count = count_source_tokens(owned_text, counter=counter, budget=budget)
    if owned_token_count.tokens > budget.source_tokens:
        split_groups = _split_group_profiles(project_root, group, counter=counter, budget=budget)
        if _split_made_progress(group, split_groups):
            return split_groups, True
        group.context_profiles = []
        return [group], False

    group.context_profiles = list(original_context)
    _trim_context_to_source_budget(group, counter=counter, budget=budget)
    if _lost_too_much_context(original_context, group.context_profiles) and len(group.profiles) > 1:
        return _split_group_profiles(project_root, group, counter=counter, budget=budget), True
    return [group], False


def _split_group_profiles(
    project_root: Path,
    group: ClusterGroup,
    *,
    counter: TokenCounter,
    budget: SourceBudget,
) -> list[ClusterGroup]:
    split_profiles: list[FileProfile] = []
    for profile in sorted(group.profiles, key=lambda item: (item.path, item.start_line)):
        owned_group = ClusterGroup([profile], locked=profile.locked)
        owned_text = _cluster_text(owned_group, budget)
        token_count = count_source_tokens(owned_text, counter=counter, budget=budget)
        if token_count.tokens > budget.source_tokens:
            split_profiles.extend(_split_oversized_profile(project_root, profile, counter, budget))
        else:
            split_profiles.append(profile)
    return [
        ClusterGroup([profile], locked=profile.locked)
        for profile in split_profiles
    ]


def _split_made_progress(
    original: ClusterGroup,
    split_groups: list[ClusterGroup],
) -> bool:
    if len(split_groups) != 1:
        return True
    return split_groups[0].profile_ids != original.profile_ids


def _lost_too_much_context(
    original_context: list[FileProfile],
    retained_context: list[FileProfile],
) -> bool:
    original_tokens = sum(profile.token_count for profile in original_context)
    if original_tokens <= 0:
        return False
    retained_tokens = sum(profile.token_count for profile in retained_context)
    return retained_tokens / original_tokens < _MIN_CONTEXT_RETENTION_FRACTION


def _trim_context_to_source_budget(
    group: ClusterGroup,
    *,
    counter: TokenCounter,
    budget: SourceBudget,
) -> tuple[str, SourceTokenCount]:
    text = _cluster_text(group, budget)
    token_count = count_source_tokens(text, counter=counter, budget=budget)
    while group.context_profiles and token_count.tokens > budget.source_tokens:
        group.context_profiles.pop()
        text = _cluster_text(group, budget)
        token_count = count_source_tokens(text, counter=counter, budget=budget)
    return text, token_count


def _cluster_text(group: ClusterGroup, budget: SourceBudget) -> str:
    owned_paths = group.owned_paths
    context_paths = sorted(dict.fromkeys(profile.path for profile in group.context_profiles))
    lines = [
        "# CCR cluster unit",
        "# Owned files are the primary refactor targets.",
        "# Context files are included for understanding and should not be changed unless "
        "integration updates require it.",
        f"# Model context window: {budget.context_window_tokens} tokens",
        f"# Model max output: {budget.max_output_tokens} tokens",
        f"# Model source budget: {budget.source_tokens} tokens",
        "# Owned files:",
        *[f"# - {path}" for path in owned_paths],
    ]
    if context_paths:
        lines.extend(["# Context files:", *[f"# - {path}" for path in context_paths]])
    lines.append("")

    for profile in sorted(group.profiles, key=lambda item: (item.path, item.start_line)):
        lines.append(f"# Owned file: {profile.path}")
        if profile.section_name:
            lines.append(f"# Owned source region: {profile.start_line}-{profile.end_line}")
        lines.append(profile.text.rstrip())
        lines.append("")

    for profile in sorted(group.context_profiles, key=lambda item: (item.path, item.start_line)):
        lines.append(f"# Context file: {profile.path}")
        if profile.section_name:
            lines.append(f"# Context source region: {profile.start_line}-{profile.end_line}")
        lines.append(profile.text.rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _cluster_slug(group: ClusterGroup, index: int) -> str:
    path_tokens = _identifier_tokens(group.owned_paths)
    symbol_tokens = _identifier_tokens(
        [symbol for profile in group.profiles for symbol in profile.defined_symbols]
    )
    tokens = [
        token
        for token in [*path_tokens, *symbol_tokens]
        if token not in _IDENTIFIER_STOP_WORDS
    ]
    if not tokens:
        return f"unit-{index:02d}"
    counts = {token: tokens.count(token) for token in set(tokens)}
    ranked = sorted(counts, key=lambda token: (-counts[token], token))[:3]
    return _slug("-".join(ranked)) or f"unit-{index:02d}"


def _file_outline(path: str, tree: ast.Module) -> str:
    lines = [f"# File outline: {path}"]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno)
            lines.append(f"# - class {node.name}: lines {node.lineno}-{end_line}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            lines.append(f"# - function {node.name}: lines {node.lineno}-{end_line}")
    return "\n".join(lines)


def _resolve_import_from_module(current_module: str, node: ast.ImportFrom) -> str:
    module = node.module or ""
    if not node.level:
        return module
    parts = current_module.split(".")
    base = parts[: -node.level] if len(parts) >= node.level else []
    if module:
        base.extend(module.split("."))
    return ".".join(part for part in base if part)


def _module_name(relative_path: str) -> str:
    path = Path(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _identifier_tokens(values: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        for chunk in re.split(r"[^A-Za-z0-9]+", value):
            if not chunk:
                continue
            split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", chunk).split()
            for token in split:
                normalized = token.lower()
                if len(normalized) <= 1 or normalized in _IDENTIFIER_STOP_WORDS:
                    continue
                tokens.append(normalized)
    return tokens


def _profile_sort_key(profile: FileProfile) -> tuple[str, int, int, str]:
    return (profile.path, profile.start_line, profile.end_line, profile.profile_id)


def _common_path_prefix_parts(left: str, right: str) -> int:
    left_parts = Path(left).parts[:-1]
    right_parts = Path(right).parts[:-1]
    count = 0
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        if left_part != right_part:
            break
        count += 1
    return count


def _edge_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:48]
