from __future__ import annotations

import json
from pathlib import Path

from ccr import cli
from ccr.knowledge.loaders import ReferenceContext, ReferenceDocument
from ccr.langchain_utils import documents as langchain_documents
from ccr.langchain_utils import parsers as langchain_parsers
from ccr.schemas.unit import CodeUnit, UnitKind


def test_langchain_export_documents_cli_writes_jsonl_without_overwriting(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "documents.jsonl"

    def fake_analyze_project(*args, **kwargs) -> list[CodeUnit]:
        return [
            CodeUnit(
                unit_id="pkg/example.py::example",
                kind=UnitKind.FUNCTION,
                name="example",
                qualified_name="example",
                path="pkg/example.py",
                start_line=1,
                end_line=2,
                start_byte=0,
                end_byte=24,
                text="def example():\n    return 1\n",
                sha256="abc123",
            )
        ]

    def fake_load_reference_context(**kwargs) -> ReferenceContext:
        return ReferenceContext(
            guides=[
                ReferenceDocument(
                    path="/refs/clean-code-python.md",
                    text="Prefer names that reveal intent.",
                    purpose="clean-code guide",
                )
            ],
            examples=[],
        )

    monkeypatch.setattr(langchain_documents, "analyze_project", fake_analyze_project)
    monkeypatch.setattr(
        langchain_documents,
        "load_reference_context",
        fake_load_reference_context,
    )

    exit_code = cli.main(
        [
            "langchain",
            "export-documents",
            str(project),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_count"] == 2
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[0]["metadata"]["ccr_document_type"] == "code_unit"
    assert records[0]["metadata"]["unit_id"] == "pkg/example.py::example"
    assert records[1]["metadata"]["ccr_document_type"] == "reference_document"

    assert (
        cli.main(
            [
                "langchain",
                "export-documents",
                str(project),
                "--output",
                str(output),
            ]
        )
        == 1
    )


def test_langchain_export_run_documents_creates_additive_analysis_directory(
    tmp_path: Path,
    capsys,
) -> None:
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "run-test",
                "original_path": "/original",
                "copied_workspace": str(workspace),
                "language": "python",
                "provider": "heuristic",
                "baseline_commit": "abc123",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "run-test",
                "original_path": "/original",
                "copied_workspace": str(workspace),
                "applied_changes": [],
                "skipped_changes": [],
                "apply_command": "ccr apply /original --run /tmp/run --yes",
                "warning": "review first",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "run_started", "timestamp": "2026-06-16T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "dashboard.html").write_text("<html>dashboard</html>", encoding="utf-8")

    exit_code = cli.main(["langchain", "export-run-documents", "--run", str(run_dir)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    output_dir = Path(payload["output_dir"])
    assert output_dir.parent == run_dir / "analysis"
    assert output_dir.exists()
    assert Path(payload["documents_path"]).exists()
    assert Path(payload["manifest_path"]).exists()
    assert (run_dir / "state.json").exists()
    assert (run_dir / "summary.json").exists()

    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    assert (
        cli.main(
            [
                "langchain",
                "export-run-documents",
                "--run",
                str(run_dir),
                "--output-dir",
                str(existing_dir),
            ]
        )
        == 1
    )


def test_langchain_parser_diagnostics_cli_uses_parser_format_instructions(
    monkeypatch,
    capsys,
) -> None:
    class FakeJsonOutputParser:
        def __init__(self, *, pydantic_object):
            self.pydantic_object = pydantic_object

        def get_format_instructions(self) -> str:
            return f"Return JSON for {self.pydantic_object.__name__}."

        def parse(self, text: str):
            return json.loads(text)

    monkeypatch.setattr(
        langchain_parsers,
        "_load_json_output_parser",
        lambda: FakeJsonOutputParser,
    )

    exit_code = cli.main(["langchain", "parser-diagnostics", "--schema", "retrieval"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "retrieval"
    assert payload["model"] == "RetrievalResult"
    assert payload["format_instructions"] == "Return JSON for RetrievalResult."
    assert payload["valid_parse"]["status"] == "ok"
