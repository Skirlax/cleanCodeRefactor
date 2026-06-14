from __future__ import annotations

import json
from pathlib import Path

from ccr.snapshots.git import GitRepo
from ccr.workflow.dashboard import RunEventLog, _elapsed_snapshot, write_run_dashboard


def test_dashboard_renders_run_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "original_path": "/project",
                "copied_workspace": str(run_dir / "workspace"),
                "language": "python",
                "provider": "codex",
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "baseline_commit": "abc",
                "status": "running",
                "units_total": 1,
                "units_done": 0,
            }
        ),
        encoding="utf-8",
    )
    event_log = RunEventLog(run_dir / "events.jsonl")
    event_log.append(
        "test_write_completed",
        unit_id="pkg/game.py::Game",
        changed_files=["tests/test_game.py"],
        diff="diff --git a/tests/test_game.py b/tests/test_game.py",
    )
    _append_jsonl(
        run_dir / "codex-calls.jsonl",
        {
            "name": "test_audit",
            "schema_model": "TestAssessment",
            "duration_seconds": 1.2,
            "returncode": 0,
            "prompt": "audit prompt",
            "output_schema": {},
            "parsed_output": {
                "adequate": False,
                "reason": "No tests cover Game.",
                "recommendations": [
                    {
                        "name": "test_game_step",
                        "behavior": "Assert step updates the board.",
                        "suggested_location": "tests/test_game.py",
                        "reason": "Core behavior.",
                    }
                ],
            },
            "langfuse_trace_id": "trace-1",
            "langfuse_observation_id": "obs-1",
        },
    )
    _append_jsonl(
        run_dir / "codex-calls.jsonl",
        {
            "name": "retrieval",
            "schema_model": "RetrievalResult",
            "duration_seconds": 1.0,
            "returncode": 0,
            "prompt": "retrieval prompt",
            "output_schema": {},
            "parsed_output": {
                "ideas": [
                    {
                        "code_example": (
                            "def choose_move(board):\n"
                            "    if board.ready:\n"
                            "        return board.best_move()\n"
                            "    return None"
                        ),
                        "why": "Shows a small game loop.",
                        "how": "Reuse the state boundary.",
                    }
                ]
            },
        },
    )

    dashboard = write_run_dashboard(run_dir)

    html = dashboard.read_text(encoding="utf-8")
    assert "CCR Run Dashboard - run-1" in html
    assert 'http-equiv="refresh"' not in html
    assert "Refresh: 5s" in html
    assert "data-elapsed-counter" in html
    assert 'class="local-time"' in html
    assert "Model: gpt-5.5" in html
    assert "Reasoning: medium" in html
    assert "test_game_step" in html
    assert "language-python" in html
    assert "def choose_move(board):" in html
    assert "diff --git" in html
    assert "trace-1" in html
    assert "json-key" in html
    assert "details[open]" in html

    diff_html = (run_dir / "diffs.html").read_text(encoding="utf-8")
    assert "CCR Diff Viewer - run-1" in diff_html
    assert 'href="dashboard.html"' in diff_html
    assert "No accepted unit commits are available yet." in diff_html


def test_elapsed_snapshot_stops_when_run_is_interrupted() -> None:
    elapsed = _elapsed_snapshot(
        [
            {"event": "run_started", "timestamp": "2026-06-10T10:00:00Z"},
            {"event": "run_interrupted", "timestamp": "2026-06-10T10:07:30Z"},
        ],
        "interrupted",
        "2026-06-11T10:00:00Z",
    )

    assert elapsed == {
        "seconds": 450,
        "active_started_at": None,
        "text": "7m",
    }


def test_elapsed_snapshot_resumes_after_interruption() -> None:
    elapsed = _elapsed_snapshot(
        [
            {"event": "run_started", "timestamp": "2026-06-10T10:00:00Z"},
            {"event": "run_interrupted", "timestamp": "2026-06-10T10:10:00Z"},
            {"event": "run_resumed", "timestamp": "2026-06-11T08:00:00Z"},
        ],
        "running",
        "2026-06-11T08:05:00Z",
    )

    assert elapsed["seconds"] == 900
    assert elapsed["active_started_at"] == "2026-06-11T08:00:00+00:00"
    assert elapsed["text"] == "15m"


def test_diff_page_renders_code_unit_and_integration_changes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-code"
    workspace = run_dir / "workspace"
    package = workspace / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "game.py").write_text(
        "class Game:\n    def step(self):\n        return 1\n\nclass Other:\n    pass\n",
        encoding="utf-8",
    )
    (package / "integration.py").write_text(
        "from pkg.game import Game\n\nRESULT = Game().step()\n",
        encoding="utf-8",
    )
    repo = GitRepo(workspace)
    repo.ensure_baseline()

    (package / "game.py").write_text(
        "class Game:\n"
        "    def step(self):\n"
        "        value = 2\n"
        "        return value\n"
        "\n"
        "class Other:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (package / "integration.py").write_text(
        "from pkg.game import Game\n\nRESULT = Game().step() + 1\n",
        encoding="utf-8",
    )
    repo.add_all(force=True)
    repo.commit("ccr refactor Game")
    commit = repo.head()

    _write_state(run_dir, workspace, run_id="run-code", units_done=1, units_total=1)
    _append_jsonl(
        run_dir / "events.jsonl",
        {"timestamp": "2026-06-11T10:00:00Z", "event": "run_started"},
    )
    _append_jsonl(
        run_dir / "ledger.jsonl",
        {
            "unit_id": "pkg/game.py::Game",
            "outcome": "accepted",
            "changed_files": ["pkg/game.py", "pkg/integration.py"],
            "checks_run": ["pytest"],
            "commit": commit,
            "message": "Refactored Game.",
        },
    )

    write_run_dashboard(run_dir)

    diff_html = (run_dir / "diffs.html").read_text(encoding="utf-8")
    assert "CCR Diff Viewer - run-code" in diff_html
    assert "pkg/game.py::Game" in diff_html
    assert ">Old<" in diff_html
    assert ">New<" in diff_html
    expected_value_line = (
        '<span class="n">value</span> <span class="o">=</span> <span class="mi">2</span>'
    )
    assert expected_value_line in diff_html
    assert "class Other" not in diff_html
    assert "Other changes in this commit" in diff_html
    assert "pkg/integration.py" in diff_html
    assert "changed-old" in diff_html
    assert "changed-new" in diff_html


def test_diff_page_renders_file_unit(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-file"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    repo = GitRepo(workspace)
    repo.ensure_baseline()

    (workspace / "app.py").write_text(
        "def alpha():\n    return 10\n\n\ndef beta():\n    return 20\n",
        encoding="utf-8",
    )
    repo.add_all(force=True)
    repo.commit("ccr refactor app")
    commit = repo.head()

    _write_state(run_dir, workspace, run_id="run-file", units_done=1, units_total=1)
    _append_jsonl(
        run_dir / "events.jsonl",
        {"timestamp": "2026-06-11T10:00:00Z", "event": "run_started"},
    )
    _append_jsonl(
        run_dir / "ledger.jsonl",
        {
            "unit_id": "app.py::<file>",
            "outcome": "accepted",
            "changed_files": ["app.py"],
            "checks_run": ["pytest"],
            "commit": commit,
            "message": "Refactored file.",
        },
    )

    write_run_dashboard(run_dir)

    diff_html = (run_dir / "diffs.html").read_text(encoding="utf-8")
    assert "File: app.py" in diff_html
    assert "<select" not in diff_html
    assert "alpha" in diff_html
    assert "beta" in diff_html
    assert "changed-old" in diff_html
    assert "changed-new" in diff_html


def test_diff_page_renders_package_file_selector_and_other_changes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-package"
    workspace = run_dir / "workspace"
    package = workspace / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (package / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    (workspace / "outside.py").write_text("VALUE = 1\n", encoding="utf-8")
    repo = GitRepo(workspace)
    repo.ensure_baseline()

    (package / "alpha.py").write_text("def alpha():\n    return 10\n", encoding="utf-8")
    (package / "beta.py").write_text("def beta():\n    return 20\n", encoding="utf-8")
    (workspace / "outside.py").write_text("VALUE = 2\n", encoding="utf-8")
    repo.add_all(force=True)
    repo.commit("ccr refactor pkg")
    commit = repo.head()

    _write_state(run_dir, workspace, run_id="run-package", units_done=1, units_total=1)
    _append_jsonl(
        run_dir / "events.jsonl",
        {"timestamp": "2026-06-11T10:00:00Z", "event": "run_started"},
    )
    _append_jsonl(
        run_dir / "ledger.jsonl",
        {
            "unit_id": "pkg::package",
            "outcome": "accepted",
            "changed_files": ["pkg/alpha.py", "pkg/beta.py", "outside.py"],
            "checks_run": ["pytest"],
            "commit": commit,
            "message": "Refactored package.",
        },
    )

    write_run_dashboard(run_dir)

    diff_html = (run_dir / "diffs.html").read_text(encoding="utf-8")
    assert "Package: pkg" in diff_html
    assert "data-file-selector" in diff_html
    assert '<option value="pkg/alpha.py">pkg/alpha.py</option>' in diff_html
    assert '<option value="pkg/beta.py">pkg/beta.py</option>' in diff_html
    assert '<option value="outside.py"' not in diff_html
    assert 'data-file-path="pkg/beta.py" hidden' in diff_html
    assert "Other changes in this commit" in diff_html
    assert "outside.py" in diff_html


def test_diff_page_renders_cluster_owned_files_and_other_changes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-cluster"
    workspace = run_dir / "workspace"
    package = workspace / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (package / "service.py").write_text("def load_user():\n    return 'old'\n", encoding="utf-8")
    (workspace / "integration.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    repo = GitRepo(workspace)
    repo.ensure_baseline()

    (package / "models.py").write_text("class UserRecord:\n    pass\n", encoding="utf-8")
    (package / "service.py").write_text(
        "def load_user():\n    return 'new'\n",
        encoding="utf-8",
    )
    (workspace / "integration.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    repo.add_all(force=True)
    repo.commit("ccr refactor cluster")
    commit = repo.head()

    _write_state(run_dir, workspace, run_id="run-cluster", units_done=1, units_total=1)
    _append_jsonl(
        run_dir / "events.jsonl",
        {"timestamp": "2026-06-11T10:00:00Z", "event": "run_started"},
    )
    _append_jsonl(
        run_dir / "ledger.jsonl",
        {
            "unit_id": "cluster/01-pkg-service::cluster",
            "outcome": "accepted",
            "changed_files": ["pkg/models.py", "pkg/service.py", "integration.py"],
            "owned_paths": ["pkg/models.py", "pkg/service.py"],
            "context_paths": ["integration.py"],
            "checks_run": ["pytest"],
            "commit": commit,
            "message": "Refactored cluster.",
        },
    )

    write_run_dashboard(run_dir)

    diff_html = (run_dir / "diffs.html").read_text(encoding="utf-8")
    assert "Cluster: cluster/01-pkg-service" in diff_html
    assert "Cluster file" in diff_html
    assert '<option value="pkg/models.py">pkg/models.py</option>' in diff_html
    assert '<option value="pkg/service.py">pkg/service.py</option>' in diff_html
    assert '<option value="integration.py"' not in diff_html
    assert "Other changes in this commit" in diff_html
    assert "integration.py" in diff_html


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _write_state(
    run_dir: Path,
    workspace: Path,
    *,
    run_id: str,
    units_done: int,
    units_total: int,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "original_path": "/project",
                "copied_workspace": str(workspace),
                "language": "python",
                "provider": "codex",
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "status": "complete",
                "units_total": units_total,
                "units_done": units_done,
            }
        ),
        encoding="utf-8",
    )
