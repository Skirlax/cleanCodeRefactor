from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccr.knowledge.loaders import ReferenceContext, ReferenceDocument, load_reference_context
from ccr.schemas.unit import CodeUnit
from ccr.snapshots.git import GitRepo
from ccr.workflow.run import analyze_project

LANGCHAIN_DOCUMENT_EXPORT_VERSION = 1


@dataclass(frozen=True)
class RunDocumentExport:
    output_dir: Path
    documents_path: Path
    manifest_path: Path
    document_count: int
    skipped_artifacts: list[str]

    def as_json(self) -> dict[str, object]:
        return {
            "output_dir": str(self.output_dir),
            "documents_path": str(self.documents_path),
            "manifest_path": str(self.manifest_path),
            "document_count": self.document_count,
            "skipped_artifacts": self.skipped_artifacts,
        }


def build_project_documents(
    project: Path,
    *,
    language: str = "python",
    include_methods: bool = False,
    unit_mode: str = "code",
    unit_sort: str = "value",
    model: str | None = None,
    target_unit_count: int = 5,
    references_root: Path | None = None,
    include_references: bool = True,
) -> list[dict[str, object]]:
    units = analyze_project(
        project.resolve(),
        language=language,
        include_methods=include_methods,
        unit_mode=unit_mode,
        unit_sort=unit_sort,
        model=model,
        target_unit_count=target_unit_count,
    )
    documents = [_code_unit_document(unit, index=index) for index, unit in enumerate(units)]

    if include_references:
        kwargs = {"language": language}
        if references_root is not None:
            kwargs["references_root"] = references_root
        references = load_reference_context(**kwargs)
        documents.extend(_reference_documents(references))

    return documents


def write_jsonl_documents(documents: list[dict[str, object]], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        for document in documents:
            handle.write(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n")
    return output


def export_run_documents(run_dir: Path, *, output_dir: Path | None = None) -> RunDocumentExport:
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        msg = f"Run directory does not exist: {run_dir}"
        raise FileNotFoundError(msg)

    output_dir = _create_run_output_dir(run_dir, output_dir)
    documents, skipped_artifacts = build_run_documents(run_dir)
    documents_path = output_dir / "documents.jsonl"
    manifest_path = output_dir / "manifest.json"

    write_jsonl_documents(documents, documents_path)
    manifest = {
        "version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
        "generated_at": _utc_timestamp(),
        "run_dir": str(run_dir),
        "documents_path": str(documents_path),
        "document_count": len(documents),
        "skipped_artifacts": skipped_artifacts,
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    return RunDocumentExport(
        output_dir=output_dir,
        documents_path=documents_path,
        manifest_path=manifest_path,
        document_count=len(documents),
        skipped_artifacts=skipped_artifacts,
    )


def build_run_documents(run_dir: Path) -> tuple[list[dict[str, object]], list[str]]:
    run_dir = run_dir.resolve()
    documents: list[dict[str, object]] = []
    skipped_artifacts: list[str] = []

    for name, document_type in _RUN_JSON_ARTIFACTS.items():
        path = run_dir / name
        if not path.exists():
            skipped_artifacts.append(f"{name}: missing")
            continue
        documents.append(
            _artifact_document(
                path,
                run_dir=run_dir,
                document_type=document_type,
                page_content=_pretty_json_or_text(path),
            )
        )

    for name, document_type in _RUN_JSONL_ARTIFACTS.items():
        path = run_dir / name
        if not path.exists():
            skipped_artifacts.append(f"{name}: missing")
            continue
        documents.extend(
            _jsonl_artifact_documents(path, run_dir=run_dir, document_type=document_type)
        )

    for name, document_type in _RUN_TEXT_ARTIFACTS.items():
        path = run_dir / name
        if not path.exists():
            skipped_artifacts.append(f"{name}: missing")
            continue
        documents.append(
            _artifact_document(
                path,
                run_dir=run_dir,
                document_type=document_type,
                page_content=path.read_text(encoding="utf-8", errors="ignore"),
            )
        )

    diff_document = _workspace_diff_document(run_dir)
    if diff_document is None:
        skipped_artifacts.append("workspace diff: unavailable or empty")
    else:
        documents.append(diff_document)

    return documents, skipped_artifacts


_RUN_JSON_ARTIFACTS = {
    "state.json": "run_state",
    "summary.json": "run_summary",
    "config.json": "run_config",
    "characterization-baseline.json": "characterization_baseline",
}

_RUN_JSONL_ARTIFACTS = {
    "events.jsonl": "run_event",
    "ledger.jsonl": "ledger_entry",
    "codex-calls.jsonl": "codex_call",
}

_RUN_TEXT_ARTIFACTS = {
    "dashboard.html": "dashboard_html",
    "diffs.html": "diffs_html",
}


def _code_unit_document(unit: CodeUnit, *, index: int) -> dict[str, object]:
    return _document_record(
        unit.text,
        {
            "ccr_document_type": "code_unit",
            "export_version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
            "index": index,
            "unit_id": unit.unit_id,
            "language": unit.language,
            "kind": unit.kind.value,
            "name": unit.name,
            "qualified_name": unit.qualified_name,
            "path": unit.path,
            "location": unit.location,
            "start_line": unit.start_line,
            "end_line": unit.end_line,
            "sha256": unit.sha256,
            "member_paths": unit.member_paths,
            "owned_paths": unit.owned_paths,
            "context_paths": unit.context_paths,
            "estimated_tokens": unit.estimated_tokens,
            "source_token_budget": unit.source_token_budget,
        },
    )


def _reference_documents(context: ReferenceContext) -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    for index, document in enumerate([*context.guides, *context.examples]):
        documents.append(_reference_document(document, index=index))
    return documents


def _reference_document(document: ReferenceDocument, *, index: int) -> dict[str, object]:
    return _document_record(
        document.text,
        {
            "ccr_document_type": "reference_document",
            "export_version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
            "index": index,
            "source_path": document.path,
            "purpose": document.purpose,
        },
    )


def _artifact_document(
    path: Path,
    *,
    run_dir: Path,
    document_type: str,
    page_content: str,
) -> dict[str, object]:
    return _document_record(
        page_content,
        {
            "ccr_document_type": document_type,
            "export_version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
            "run_dir": str(run_dir),
            "source_path": str(path),
            "relative_source_path": str(path.relative_to(run_dir)),
        },
    )


def _jsonl_artifact_documents(
    path: Path, *, run_dir: Path, document_type: str
) -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line_number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        metadata: dict[str, object] = {
            "ccr_document_type": document_type,
            "export_version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
            "run_dir": str(run_dir),
            "source_path": str(path),
            "relative_source_path": str(path.relative_to(run_dir)),
            "line_number": line_number,
        }
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            page_content = raw_line
            metadata["json_parse_status"] = "invalid"
        else:
            page_content = json.dumps(payload, indent=2, sort_keys=True)
            metadata["json_parse_status"] = "ok"
            if isinstance(payload, dict):
                for key in ("type", "event", "unit_id", "outcome", "name"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        metadata[key] = value
        documents.append(_document_record(page_content, metadata))
    return documents


def _workspace_diff_document(run_dir: Path) -> dict[str, object] | None:
    try:
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        workspace = Path(str(state["copied_workspace"]))
        baseline = str(state["baseline_commit"])
        if not workspace.exists():
            return None
        diff = GitRepo(workspace).diff(baseline, "HEAD")
    except Exception:
        return None
    if not diff.strip():
        return None
    return _document_record(
        diff,
        {
            "ccr_document_type": "workspace_diff",
            "export_version": LANGCHAIN_DOCUMENT_EXPORT_VERSION,
            "run_dir": str(run_dir),
            "source": "git diff",
            "workspace": str(workspace),
            "baseline_commit": baseline,
            "head": "HEAD",
        },
    )


def _document_record(page_content: str, metadata: dict[str, object]) -> dict[str, object]:
    json_metadata = _jsonable(metadata)
    try:
        from langchain_core.documents import Document
    except ImportError:
        return {"page_content": page_content, "metadata": json_metadata}

    document = Document(page_content=page_content, metadata=json_metadata)
    return {"page_content": document.page_content, "metadata": dict(document.metadata)}


def _pretty_json_or_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(payload, indent=2, sort_keys=True)


def _create_run_output_dir(run_dir: Path, output_dir: Path | None) -> Path:
    if output_dir is not None:
        output_dir = output_dir.resolve()
        if output_dir.exists():
            msg = f"Output directory already exists: {output_dir}"
            raise FileExistsError(msg)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir()
        return output_dir

    parent = run_dir / "analysis"
    parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for suffix in ["", *[f"-{index}" for index in range(1, 1000)]]:
        candidate = parent / f"langchain-documents-{timestamp}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    msg = f"Could not allocate a unique LangChain document export directory under {parent}"
    raise FileExistsError(msg)


def _jsonable(value: object) -> Any:
    return json.loads(json.dumps(value, default=str))


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()
