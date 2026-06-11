from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccr.extraction.units import iter_python_files
from ccr.schemas.unit import CodeUnit, UnitKind


@dataclass(frozen=True)
class _ParsedNode:
    kind: UnitKind
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


class PythonTreeSitterIndex:
    def __init__(self, project_root: Path, *, excluded_dirs: set[str] | None = None) -> None:
        self.project_root = project_root.resolve()
        self.excluded_dirs = excluded_dirs

    def extract(self, *, include_methods: bool = False) -> list[CodeUnit]:
        units: list[CodeUnit] = []
        for path in iter_python_files(self.project_root, self.excluded_dirs):
            source = path.read_text(encoding="utf-8")
            relative_path = path.relative_to(self.project_root).as_posix()
            parsed_nodes = self._parse_with_tree_sitter(source, include_methods=include_methods)
            if parsed_nodes is None:
                parsed_nodes = self._parse_with_ast(source, include_methods=include_methods)
            for node in parsed_nodes:
                text = source.encode("utf-8")[node.start_byte : node.end_byte].decode("utf-8")
                units.append(
                    CodeUnit(
                        unit_id=f"{relative_path}::{node.qualified_name}",
                        kind=node.kind,
                        name=node.name,
                        qualified_name=node.qualified_name,
                        path=relative_path,
                        start_line=node.start_line,
                        end_line=node.end_line,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                        text=text,
                        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    )
                )
        return units

    def _parse_with_tree_sitter(
        self, source: str, *, include_methods: bool
    ) -> list[_ParsedNode] | None:
        try:
            import tree_sitter_python
            from tree_sitter import Language, Parser
        except Exception:
            return None

        language_handle = tree_sitter_python.language()
        language = (
            language_handle if isinstance(language_handle, Language) else Language(language_handle)
        )
        parser = Parser()
        if hasattr(parser, "set_language"):
            parser.set_language(language)
        else:
            parser.language = language

        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node
        nodes: list[_ParsedNode] = []

        def node_name(ts_node: Any) -> str:
            name_node = ts_node.child_by_field_name("name")
            if name_node is None:
                return "<anonymous>"
            return source.encode("utf-8")[name_node.start_byte : name_node.end_byte].decode("utf-8")

        def add_node(ts_node: Any, kind: UnitKind, qualified_name: str) -> None:
            nodes.append(
                _ParsedNode(
                    kind=kind,
                    name=qualified_name.rsplit(".", maxsplit=1)[-1],
                    qualified_name=qualified_name,
                    start_line=ts_node.start_point[0] + 1,
                    end_line=ts_node.end_point[0] + 1,
                    start_byte=ts_node.start_byte,
                    end_byte=ts_node.end_byte,
                )
            )

        def walk_top_level(parent: Any) -> None:
            for child in parent.children:
                if child.type == "function_definition":
                    name = node_name(child)
                    add_node(child, UnitKind.FUNCTION, name)
                elif child.type == "class_definition":
                    class_name = node_name(child)
                    add_node(child, UnitKind.CLASS, class_name)
                    if include_methods:
                        body = child.child_by_field_name("body")
                        for body_child in body.children if body is not None else []:
                            if body_child.type == "function_definition":
                                method_name = node_name(body_child)
                                add_node(
                                    body_child,
                                    UnitKind.METHOD,
                                    f"{class_name}.{method_name}",
                                )

        walk_top_level(root)
        return sorted(nodes, key=lambda node: (node.start_line, node.end_line))

    def _parse_with_ast(self, source: str, *, include_methods: bool) -> list[_ParsedNode]:
        tree = ast.parse(source)
        line_offsets = _line_start_offsets(source)
        nodes: list[_ParsedNode] = []

        def byte_offset(line: int, col: int) -> int:
            return len(source[: line_offsets[line - 1] + col].encode("utf-8"))

        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                nodes.append(
                    _ParsedNode(
                        kind=UnitKind.FUNCTION,
                        name=node.name,
                        qualified_name=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        start_byte=byte_offset(node.lineno, node.col_offset),
                        end_byte=byte_offset(
                            node.end_lineno or node.lineno, node.end_col_offset or 0
                        ),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                nodes.append(
                    _ParsedNode(
                        kind=UnitKind.CLASS,
                        name=node.name,
                        qualified_name=node.name,
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        start_byte=byte_offset(node.lineno, node.col_offset),
                        end_byte=byte_offset(
                            node.end_lineno or node.lineno, node.end_col_offset or 0
                        ),
                    )
                )
                if include_methods:
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                            nodes.append(
                                _ParsedNode(
                                    kind=UnitKind.METHOD,
                                    name=child.name,
                                    qualified_name=f"{node.name}.{child.name}",
                                    start_line=child.lineno,
                                    end_line=child.end_lineno or child.lineno,
                                    start_byte=byte_offset(child.lineno, child.col_offset),
                                    end_byte=byte_offset(
                                        child.end_lineno or child.lineno,
                                        child.end_col_offset or 0,
                                    ),
                                )
                            )
        return sorted(nodes, key=lambda parsed: (parsed.start_line, parsed.end_line))


def _line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for index, character in enumerate(source):
        if character == "\n":
            offsets.append(index + 1)
    return offsets
