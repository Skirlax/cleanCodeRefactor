# ruff: noqa: E501

from __future__ import annotations

import ast
import hashlib
import html
import json
import re
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


class RunEventLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event_type,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def read(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.path)


def record_run_event(
    run_dir: Path,
    event_log: RunEventLog,
    event_type: str,
    **payload: Any,
) -> dict[str, Any]:
    record = event_log.append(event_type, **payload)
    write_run_dashboard(run_dir)
    return record


def write_run_dashboard(run_dir: Path) -> Path:
    snapshot = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(UTC).isoformat(),
        "state": _read_json(run_dir / "state.json"),
        "summary": _read_json(run_dir / "summary.json"),
        "events": _read_jsonl(run_dir / "events.jsonl"),
        "ledger": _read_jsonl(run_dir / "ledger.jsonl"),
        "codex_calls": _read_jsonl(run_dir / "codex-calls.jsonl"),
    }
    path = run_dir / "dashboard.html"
    path.write_text(_render_dashboard(snapshot), encoding="utf-8")
    (run_dir / "diffs.html").write_text(_render_diff_page(snapshot), encoding="utf-8")
    return path


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _render_dashboard(snapshot: dict[str, Any]) -> str:
    state = snapshot.get("state") or {}
    summary = snapshot.get("summary") or {}
    events = snapshot.get("events") or []
    ledger = snapshot.get("ledger") or []
    calls = snapshot.get("codex_calls") or []
    running = state.get("status") == "running"
    status = str(state.get("status") or "unknown")
    title = f"CCR Run Dashboard - {state.get('run_id') or Path(snapshot['run_dir']).name}"
    started_at = _run_started_at(events) or snapshot.get("generated_at") or ""
    elapsed = _elapsed_snapshot(events, status, str(snapshot["generated_at"]))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --surface: #ffffff;
      --surface-soft: #eef3f8;
      --text: #17202a;
      --muted: #647184;
      --border: #d9e1ea;
      --accent: #2458d3;
      --good: #0f8a5f;
      --warn: #a66300;
      --bad: #b42318;
      --code: #101828;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}
    header {{
      background: #111827;
      color: #f9fafb;
      padding: 22px 28px;
      border-bottom: 1px solid #0b1220;
    }}
    main {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 22px 28px 48px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 22px; font-weight: 720; }}
    h2 {{ font-size: 16px; font-weight: 700; margin-bottom: 12px; }}
    h3 {{ font-size: 14px; font-weight: 700; margin-bottom: 8px; }}
    a {{ color: var(--accent); }}
    .subhead {{ color: #cbd5e1; margin-top: 5px; }}
    .nav-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .nav-link {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 5px 10px;
      border: 1px solid #374151;
      border-radius: 999px;
      color: #f9fafb;
      text-decoration: none;
      font-size: 12px;
      font-weight: 700;
    }}
    .nav-link.active {{
      border-color: #93c5fd;
      background: #1f2937;
    }}
    .status-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
      align-items: center;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 26px;
      padding: 4px 9px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface);
      color: var(--text);
      font-size: 12px;
      font-weight: 650;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    header .pill {{ background: #1f2937; border-color: #374151; color: #f9fafb; }}
    .pill.running {{ color: #fef3c7; border-color: #92400e; }}
    .pill.complete {{ color: #bbf7d0; border-color: #166534; }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(360px, 0.95fr);
      gap: 18px;
      align-items: start;
    }}
    .section {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 18px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 13px 14px;
    }}
    .metric-label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .metric-value {{ margin-top: 4px; font-size: 18px; font-weight: 760; }}
    .timeline {{
      display: grid;
      gap: 10px;
    }}
    .event {{
      display: grid;
      grid-template-columns: minmax(104px, 128px) minmax(0, 1fr);
      gap: 12px;
      padding: 11px 0;
      border-top: 1px solid var(--border);
    }}
    .event:first-child {{ border-top: 0; padding-top: 0; }}
    .time {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    .event-name {{ font-weight: 720; }}
    .muted {{ color: var(--muted); min-width: 0; overflow-wrap: anywhere; }}
    .text-value {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .path-value, .command-value {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .call {{
      border-top: 1px solid var(--border);
      padding: 14px 0;
    }}
    .call:first-of-type {{ border-top: 0; padding-top: 0; }}
    .call-head {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .call-title {{ font-weight: 760; font-size: 15px; }}
    .kv {{
      display: grid;
      grid-template-columns: 138px minmax(0, 1fr);
      gap: 8px 12px;
      margin: 8px 0;
    }}
    .kv dt {{ color: var(--muted); font-weight: 650; }}
    .kv dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; }}
    ul.clean {{
      margin: 8px 0 0;
      padding-left: 18px;
    }}
    li + li {{ margin-top: 7px; }}
    details {{
      margin-top: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fbfcfe;
    }}
    summary {{
      cursor: pointer;
      padding: 9px 11px;
      font-weight: 680;
      color: var(--text);
    }}
    .details-body {{
      padding: 12px;
      border-top: 1px solid var(--border);
      border-radius: 0 0 8px 8px;
      background: var(--code);
      color: #e5e7eb;
    }}
    pre {{
      margin: 0;
      padding: 0;
      overflow: auto;
      background: transparent;
      color: #e5e7eb;
      font-size: 12px;
      line-height: 1.45;
      tab-size: 2;
      white-space: pre;
      overflow-wrap: normal;
    }}
    pre.diff {{ background: #0b1220; }}
    .text-block {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: inherit;
      color: inherit;
    }}
    .code-block {{
      display: block;
      max-width: 100%;
      padding: 9px;
      border: 1px solid #263449;
      border-radius: 6px;
      background: #111827;
      color: #e5e7eb;
      white-space: pre;
      overflow: auto;
      overflow-wrap: normal;
    }}
    .details-body > .code-block {{
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }}
    .json-tree {{
      display: grid;
      gap: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
    }}
    .json-pair, .json-item {{
      display: grid;
      grid-template-columns: minmax(96px, max-content) minmax(0, 1fr);
      gap: 8px;
      align-items: start;
    }}
    .json-key {{ color: #93c5fd; overflow-wrap: anywhere; }}
    .json-index {{ color: #c4b5fd; }}
    .json-string {{ color: #d1fae5; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .json-number {{ color: #fcd34d; }}
    .json-bool {{ color: #fca5a5; }}
    .json-null {{ color: #9ca3af; }}
    .json-empty {{ color: #9ca3af; }}
    .json-tree .code-block {{
      margin-top: 2px;
      padding: 9px;
    }}
    .empty {{
      color: var(--muted);
      padding: 10px 0;
    }}
    .outcome-test_generation_failed, .level-error {{ color: var(--bad); }}
    .outcome-tests_added, .outcome-accepted, .level-ok {{ color: var(--good); }}
    .level-warn {{ color: var(--warn); }}
    .two-col {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 14px;
    }}
    @media (max-width: 980px) {{
      main {{ padding: 18px 14px 36px; }}
      .grid, .metrics, .two-col {{ grid-template-columns: 1fr; }}
      .event {{ grid-template-columns: 1fr; gap: 4px; }}
      .kv {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body data-running="{_h(str(running).lower())}" data-started-at="{_h(str(started_at))}" data-elapsed-seconds="{_h(str(elapsed["seconds"]))}" data-elapsed-recorded-at="{_h(str(snapshot["generated_at"]))}" data-active-started-at="{_h(str(elapsed["active_started_at"] or ""))}">
  <header>
    <h1>{_h(title)}</h1>
    <div class="subhead">{_h(str(snapshot["run_dir"]))}</div>
    <nav class="nav-row" aria-label="Dashboard pages">
      <a class="nav-link active" href="dashboard.html">Run timeline</a>
      <a class="nav-link" href="diffs.html">Diff viewer</a>
    </nav>
    <div class="status-row">
      <span class="pill {_h(status)}">Status: {_h(status)}</span>
      <span class="pill">Provider: {_h(str(state.get("provider") or "unknown"))}</span>
      <span class="pill">Model: {_h(str(state.get("model") or "config default"))}</span>
      <span class="pill">Reasoning: {_h(str(state.get("reasoning_effort") or "config default"))}</span>
      <span class="pill">Units: {_h(str(state.get("units_done", 0)))} / {_h(str(state.get("units_total", 0)))}</span>
      <span class="pill">Elapsed: <span data-elapsed-counter>{_h(str(elapsed["text"]))}</span></span>
      <span class="pill">Updated: {_time_tag(str(snapshot["generated_at"]))}</span>
      {_refresh_note(running)}
    </div>
  </header>
  <main>
    {_render_metrics(state, events, ledger, calls)}
    <div class="grid">
      <div>
        {_render_events(events)}
        {_render_ledger(ledger)}
      </div>
      <div>
        {_render_codex_calls(calls)}
        {_render_summary(summary)}
      </div>
    </div>
  </main>
  <script>
    (() => {{
      const refreshMs = 5000;
      const running = document.body.dataset.running === "true";
      const startedAt = document.body.dataset.startedAt;
      const elapsedSeconds = Number(document.body.dataset.elapsedSeconds || "NaN");
      const elapsedRecordedAt = document.body.dataset.elapsedRecordedAt;
      const detailStorageKey = `ccr-dashboard-details:${{location.pathname}}`;

      function formatLocalMinute(value) {{
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value || "";
        const pad = (part) => String(part).padStart(2, "0");
        return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}`;
      }}

      function formatElapsedSeconds(totalSeconds) {{
        if (!Number.isFinite(totalSeconds)) return "unknown";
        const totalMinutes = Math.max(0, Math.floor(totalSeconds / 60));
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        const parts = [];
        if (days) parts.push(`${{days}}d`);
        if (hours || days) parts.push(`${{hours}}h`);
        parts.push(`${{minutes}}m`);
        return parts.join(" ");
      }}

      function elapsedText(startValue, endValue = new Date().toISOString()) {{
        const start = new Date(startValue);
        const end = new Date(endValue);
        if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "unknown";
        return formatElapsedSeconds((end.getTime() - start.getTime()) / 1000);
      }}

      function openDetailsIds() {{
        return Array.from(document.querySelectorAll("details[open][data-details-id]"))
          .map((element) => element.dataset.detailsId);
      }}

      function restoreDetails() {{
        try {{
          const ids = new Set(JSON.parse(localStorage.getItem(detailStorageKey) || "[]"));
          document.querySelectorAll("details[data-details-id]").forEach((element) => {{
            if (ids.has(element.dataset.detailsId)) element.open = true;
          }});
        }} catch {{
          return;
        }}
      }}

      function persistDetails() {{
        try {{
          localStorage.setItem(detailStorageKey, JSON.stringify(openDetailsIds()));
        }} catch {{
          return;
        }}
      }}

      document.querySelectorAll("time.local-time").forEach((element) => {{
        element.textContent = formatLocalMinute(element.dateTime || element.getAttribute("datetime"));
      }});

      const elapsedCounter = document.querySelector("[data-elapsed-counter]");
      if (elapsedCounter && Number.isFinite(elapsedSeconds)) {{
        const updateElapsed = () => {{
          let currentSeconds = elapsedSeconds;
          if (running && elapsedRecordedAt) {{
            const recordedAt = new Date(elapsedRecordedAt);
            if (!Number.isNaN(recordedAt.getTime())) {{
              currentSeconds += Math.max(0, (Date.now() - recordedAt.getTime()) / 1000);
            }}
          }}
          elapsedCounter.textContent = formatElapsedSeconds(currentSeconds);
        }};
        updateElapsed();
        setInterval(updateElapsed, 1000);
      }} else if (elapsedCounter && startedAt) {{
        elapsedCounter.textContent = elapsedText(startedAt);
      }}

      restoreDetails();
      document.addEventListener("toggle", (event) => {{
        if (event.target instanceof HTMLDetailsElement) persistDetails();
      }}, true);

      if (running) {{
        const refreshPill = document.querySelector("[data-refresh-status]");
        setInterval(() => {{
          const hasOpenDetails = document.querySelector("details[open]") !== null;
          if (refreshPill) refreshPill.textContent = hasOpenDetails ? "Refresh: paused" : "Refresh: 5s";
          if (!hasOpenDetails && !document.hidden) location.reload();
        }}, refreshMs);
      }}
    }})();
  </script>
</body>
</html>
"""


def _render_diff_page(snapshot: dict[str, Any]) -> str:
    state = snapshot.get("state") or {}
    ledger = snapshot.get("ledger") or []
    events = snapshot.get("events") or []
    run_dir = Path(str(snapshot["run_dir"]))
    running = state.get("status") == "running"
    status = str(state.get("status") or "unknown")
    title = f"CCR Diff Viewer - {state.get('run_id') or run_dir.name}"
    started_at = _run_started_at(events) or snapshot.get("generated_at") or ""
    elapsed = _elapsed_snapshot(events, status, str(snapshot["generated_at"]))
    unit_diffs = _build_unit_diffs(run_dir, state, ledger)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --surface: #ffffff;
      --text: #17202a;
      --muted: #647184;
      --border: #d9e1ea;
      --accent: #2458d3;
      --good: #0f8a5f;
      --bad: #b42318;
      --code: #101828;
      --code-border: #263449;
      --line-added: rgba(22, 163, 74, 0.2);
      --line-removed: rgba(220, 38, 38, 0.2);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 10;
      background: #111827;
      color: #f9fafb;
      padding: 18px 28px;
      border-bottom: 1px solid #0b1220;
    }}
    main {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 22px 28px 56px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 22px; font-weight: 720; }}
    h2 {{ font-size: 16px; font-weight: 740; }}
    h3 {{ font-size: 14px; font-weight: 720; }}
    .subhead {{ color: #cbd5e1; margin-top: 5px; overflow-wrap: anywhere; }}
    .nav-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .nav-link {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 5px 10px;
      border: 1px solid #374151;
      border-radius: 999px;
      color: #f9fafb;
      text-decoration: none;
      font-size: 12px;
      font-weight: 700;
    }}
    .nav-link.active {{
      border-color: #93c5fd;
      background: #1f2937;
    }}
    .status-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
      align-items: center;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 26px;
      padding: 4px 9px;
      border: 1px solid #374151;
      border-radius: 999px;
      background: #1f2937;
      color: #f9fafb;
      font-size: 12px;
      font-weight: 650;
      white-space: normal;
      overflow-wrap: anywhere;
    }}
    .pill.running {{ color: #fef3c7; border-color: #92400e; }}
    .summary-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .summary-card, .unit-card, .subdiff {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    .summary-card {{ padding: 13px 14px; }}
    .summary-label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .summary-value {{ margin-top: 4px; font-size: 18px; font-weight: 760; }}
    .unit-list {{
      display: grid;
      gap: 18px;
    }}
    .unit-card {{
      padding: 16px;
    }}
    .unit-head {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
      margin-bottom: 12px;
    }}
    .unit-title {{
      display: grid;
      gap: 4px;
      min-width: 0;
    }}
    .unit-id, .file-path {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .file-select-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin: 10px 0 14px;
    }}
    label {{ color: var(--muted); font-weight: 650; }}
    select {{
      min-height: 32px;
      max-width: min(100%, 720px);
      border: 1px solid var(--border);
      border-radius: 7px;
      background: #ffffff;
      color: var(--text);
      padding: 5px 8px;
      font: inherit;
    }}
    .file-diff[hidden] {{ display: none; }}
    .compare-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }}
    .code-panel {{
      min-width: 0;
      border: 1px solid var(--code-border);
      border-radius: 8px;
      background: var(--code);
      color: #e5e7eb;
      overflow: hidden;
    }}
    .code-head {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--code-border);
      background: #111827;
      font-weight: 720;
      font-size: 12px;
    }}
    .code-path {{
      color: #cbd5e1;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }}
    .code-scroll {{
      max-height: 560px;
      overflow: auto;
    }}
    .code-table {{
      display: grid;
      min-width: max-content;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.5;
    }}
    .code-line {{
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr);
      min-height: 18px;
    }}
    .line-number {{
      user-select: none;
      color: #64748b;
      padding: 0 10px;
      text-align: right;
      border-right: 1px solid #1f2a3a;
      background: #0b1220;
    }}
    .line-code {{
      padding: 0 10px;
      white-space: pre;
    }}
    .code-line.changed-old .line-code {{ background: var(--line-removed); }}
    .code-line.changed-new .line-code {{ background: var(--line-added); }}
    .subdiffs {{
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }}
    .subdiff {{ padding: 14px; }}
    .empty {{
      color: var(--muted);
      padding: 12px 0;
    }}
    .highlight .c, .highlight .cm, .highlight .cp, .highlight .c1 {{ color: #94a3b8; font-style: italic; }}
    .highlight .k, .highlight .kd, .highlight .kn, .highlight .kr, .highlight .ow {{ color: #93c5fd; font-weight: 700; }}
    .highlight .s, .highlight .s1, .highlight .s2, .highlight .sd {{ color: #86efac; }}
    .highlight .mi, .highlight .mf, .highlight .mh {{ color: #fcd34d; }}
    .highlight .nf, .highlight .fm {{ color: #f9a8d4; }}
    .highlight .nc, .highlight .nn {{ color: #c4b5fd; font-weight: 700; }}
    .highlight .o, .highlight .p {{ color: #e5e7eb; }}
    @media (max-width: 1100px) {{
      main {{ padding: 18px 14px 40px; }}
      .summary-row, .compare-grid {{ grid-template-columns: 1fr; }}
      .unit-head {{ display: grid; }}
    }}
  </style>
</head>
<body data-running="{_h(str(running).lower())}" data-started-at="{_h(str(started_at))}" data-elapsed-seconds="{_h(str(elapsed["seconds"]))}" data-elapsed-recorded-at="{_h(str(snapshot["generated_at"]))}" data-active-started-at="{_h(str(elapsed["active_started_at"] or ""))}">
  <header>
    <h1>{_h(title)}</h1>
    <div class="subhead">{_h(str(run_dir))}</div>
    <nav class="nav-row" aria-label="Dashboard pages">
      <a class="nav-link" href="dashboard.html">Run timeline</a>
      <a class="nav-link active" href="diffs.html">Diff viewer</a>
    </nav>
    <div class="status-row">
      <span class="pill {_h(status)}">Status: {_h(status)}</span>
      <span class="pill">Provider: {_h(str(state.get("provider") or "unknown"))}</span>
      <span class="pill">Model: {_h(str(state.get("model") or "config default"))}</span>
      <span class="pill">Units: {_h(str(state.get("units_done", 0)))} / {_h(str(state.get("units_total", 0)))}</span>
      <span class="pill">Elapsed: <span data-elapsed-counter>{_h(str(elapsed["text"]))}</span></span>
      <span class="pill">Updated: {_time_tag(str(snapshot["generated_at"]))}</span>
    </div>
  </header>
  <main>
    <section class="summary-row">
      {_diff_metric("Units With Diffs", str(len(unit_diffs)))}
      {_diff_metric("Main File Panels", str(sum(len(unit["main_files"]) for unit in unit_diffs)))}
      {_diff_metric("Integration Panels", str(sum(len(unit["other_files"]) for unit in unit_diffs)))}
    </section>
    {_render_unit_diff_list(unit_diffs)}
  </main>
  <script>
    (() => {{
      function formatLocalMinute(value) {{
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value || "";
        const pad = (part) => String(part).padStart(2, "0");
        return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}`;
      }}
      function elapsedText(startValue, endValue = new Date().toISOString()) {{
        const start = new Date(startValue);
        const end = new Date(endValue);
        if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "unknown";
        return formatElapsedSeconds((end.getTime() - start.getTime()) / 1000);
      }}
      function formatElapsedSeconds(totalSeconds) {{
        if (!Number.isFinite(totalSeconds)) return "unknown";
        const totalMinutes = Math.max(0, Math.floor(totalSeconds / 60));
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        const parts = [];
        if (days) parts.push(`${{days}}d`);
        if (hours || days) parts.push(`${{hours}}h`);
        parts.push(`${{minutes}}m`);
        return parts.join(" ");
      }}
      document.querySelectorAll("time.local-time").forEach((element) => {{
        element.textContent = formatLocalMinute(element.dateTime || element.getAttribute("datetime"));
      }});
      const startedAt = document.body.dataset.startedAt;
      const running = document.body.dataset.running === "true";
      const elapsedSeconds = Number(document.body.dataset.elapsedSeconds || "NaN");
      const elapsedRecordedAt = document.body.dataset.elapsedRecordedAt;
      const elapsedCounter = document.querySelector("[data-elapsed-counter]");
      if (elapsedCounter && Number.isFinite(elapsedSeconds)) {{
        const updateElapsed = () => {{
          let currentSeconds = elapsedSeconds;
          if (running && elapsedRecordedAt) {{
            const recordedAt = new Date(elapsedRecordedAt);
            if (!Number.isNaN(recordedAt.getTime())) {{
              currentSeconds += Math.max(0, (Date.now() - recordedAt.getTime()) / 1000);
            }}
          }}
          elapsedCounter.textContent = formatElapsedSeconds(currentSeconds);
        }};
        updateElapsed();
        setInterval(updateElapsed, 1000);
      }} else if (elapsedCounter && startedAt) {{
        elapsedCounter.textContent = elapsedText(startedAt);
      }}
      document.querySelectorAll("[data-file-selector]").forEach((select) => {{
        const unitId = select.dataset.fileSelector;
        const update = () => {{
          document.querySelectorAll(`[data-file-panel="${{unitId}}"]`).forEach((panel) => {{
            panel.hidden = panel.dataset.filePath !== select.value;
          }});
        }};
        select.addEventListener("change", update);
        update();
      }});
    }})();
  </script>
</body>
</html>
"""


def _diff_metric(label: str, value: str) -> str:
    return f"""
      <div class="summary-card">
        <div class="summary-label">{_h(label)}</div>
        <div class="summary-value">{_h(value)}</div>
      </div>
"""


def _render_unit_diff_list(unit_diffs: list[dict[str, Any]]) -> str:
    if not unit_diffs:
        return '<section class="unit-card"><h2>Diff Viewer</h2><div class="empty">No accepted unit commits are available yet.</div></section>'
    return f'<section class="unit-list">{"".join(_render_unit_diff(unit) for unit in unit_diffs)}</section>'


def _render_unit_diff(unit: dict[str, Any]) -> str:
    main_files = unit["main_files"]
    other_files = unit["other_files"]
    unit_dom_id = _dom_id(str(unit["unit_id"]))
    selectable = (unit["is_package"] or unit["is_cluster"]) and len(main_files) > 1
    selector = ""
    if selectable:
        selector_label = "Cluster file" if unit["is_cluster"] else "Package file"
        options = "".join(
            f'<option value="{_h(file["path"])}">{_h(file["path"])}</option>' for file in main_files
        )
        selector = f"""
          <div class="file-select-row">
            <label for="select-{_h(unit_dom_id)}">{_h(selector_label)}</label>
            <select id="select-{_h(unit_dom_id)}" data-file-selector="{_h(unit_dom_id)}">{options}</select>
          </div>
"""
    main_panels = "".join(
        _render_file_diff(
            file,
            unit_dom_id=unit_dom_id,
            selectable=selectable,
            initially_visible=index == 0,
        )
        for index, file in enumerate(main_files)
    )
    other_panels = ""
    if other_files:
        other_panels = f"""
          <div class="subdiffs">
            <h3>Other changes in this commit</h3>
            {"".join(f'<div class="subdiff">{_render_file_diff(file)}</div>' for file in other_files)}
          </div>
"""
    return f"""
      <article class="unit-card" id="{_h(unit_dom_id)}">
        <div class="unit-head">
          <div class="unit-title">
            <h2>{_h(unit["title"])}</h2>
            <div class="unit-id">{_h(str(unit["unit_id"]))}</div>
          </div>
          <span class="pill">commit {_h(str(unit["commit"])[:10])}</span>
        </div>
        {selector}
        {main_panels or '<div class="empty">No changed file matched this unit.</div>'}
        {other_panels}
      </article>
"""


def _render_file_diff(
    file: dict[str, Any],
    *,
    unit_dom_id: str | None = None,
    selectable: bool = False,
    initially_visible: bool = True,
) -> str:
    hidden = " hidden" if selectable and not initially_visible else ""
    attrs = ""
    if selectable and unit_dom_id is not None:
        attrs = f' data-file-panel="{_h(unit_dom_id)}" data-file-path="{_h(file["path"])}"'
    return f"""
      <section class="file-diff"{attrs}{hidden}>
        <div class="file-path">{_h(file["path"])}</div>
        <div class="compare-grid">
          {_render_code_panel("Old", file["old_text"], file["path"], file["old_changed"], changed_class="changed-old")}
          {_render_code_panel("New", file["new_text"], file["path"], file["new_changed"], changed_class="changed-new")}
        </div>
      </section>
"""


def _render_code_panel(
    label: str,
    text: str,
    path: str,
    changed_lines: set[int],
    *,
    changed_class: str,
) -> str:
    return f"""
      <div class="code-panel">
        <div class="code-head">
          <span>{_h(label)}</span>
          <span class="code-path">{_h(path)}</span>
        </div>
        <div class="code-scroll">
          <div class="code-table highlight">
            {_render_code_lines(text, path, changed_lines, changed_class=changed_class)}
          </div>
        </div>
      </div>
"""


def _render_code_lines(
    text: str,
    path: str,
    changed_lines: set[int],
    *,
    changed_class: str,
) -> str:
    lines = text.splitlines() or [""]
    highlighted_lines = _highlighted_lines(path, text)
    if len(highlighted_lines) != len(lines):
        highlighted_lines = [_h(line) for line in lines]
    rows = []
    for index, line_html in enumerate(highlighted_lines, start=1):
        classes = "code-line"
        if index in changed_lines:
            classes += f" {changed_class}"
        rows.append(
            f'<div class="{classes}"><span class="line-number">{index}</span><span class="line-code">{line_html or " "}</span></div>'
        )
    return "".join(rows)


def _build_unit_diffs(
    run_dir: Path,
    state: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    workspace = Path(str(state.get("copied_workspace") or run_dir / "workspace"))
    if not (workspace / ".git").exists():
        return []
    units: list[dict[str, Any]] = []
    for entry in ledger:
        if entry.get("outcome") != "accepted" or not entry.get("commit"):
            continue
        commit = str(entry["commit"])
        parent = _git_output(workspace, "rev-parse", f"{commit}^")
        if not parent:
            continue
        changed_files = _changed_files_for_entry(workspace, parent, commit, entry)
        if not changed_files:
            continue
        unit_id = str(entry.get("unit_id") or "")
        unit_path, _, unit_name = unit_id.partition("::")
        main_paths = _main_paths_for_unit(
            workspace,
            commit,
            unit_path,
            unit_name,
            changed_files,
            entry,
        )
        main_files = [
            _file_snapshot_diff(workspace, parent, commit, path, unit_id=unit_id)
            for path in main_paths
        ]
        other_files = [
            _file_snapshot_diff(workspace, parent, commit, path)
            for path in changed_files
            if path not in set(main_paths)
        ]
        units.append(
            {
                "unit_id": unit_id,
                "title": _unit_title(unit_id),
                "commit": commit,
                "is_cluster": _is_cluster_unit(unit_name),
                "is_package": _is_package_unit(unit_path, unit_name),
                "main_files": [file for file in main_files if file is not None],
                "other_files": [file for file in other_files if file is not None],
            }
        )
    return units


def _changed_files_for_entry(
    workspace: Path,
    parent: str,
    commit: str,
    entry: dict[str, Any],
) -> list[str]:
    names = [str(path) for path in entry.get("changed_files") or [] if str(path)]
    if not names:
        diff_names = _git_output(workspace, "diff", "--name-only", parent, commit)
        names = [line.strip() for line in (diff_names or "").splitlines() if line.strip()]
    return sorted(dict.fromkeys(names))


def _main_paths_for_unit(
    workspace: Path,
    commit: str,
    unit_path: str,
    unit_name: str,
    changed_files: list[str],
    entry: dict[str, Any],
) -> list[str]:
    if _is_cluster_unit(unit_name):
        owned_paths = [
            str(path)
            for path in entry.get("owned_paths") or entry.get("member_paths") or []
            if str(path)
        ]
        owned_path_set = set(owned_paths)
        paths = [
            path
            for path in changed_files
            if path in owned_path_set and _is_supported_source_path(path)
        ]
        return paths or [path for path in owned_paths if _is_supported_source_path(path)]
    if _is_package_unit(unit_path, unit_name):
        prefix = "" if unit_path == "." else unit_path.rstrip("/") + "/"
        paths = [
            path
            for path in changed_files
            if path.startswith(prefix) and _is_supported_source_path(path)
        ]
        return paths or _package_source_paths(workspace, commit, unit_path)
    if unit_path:
        return [unit_path]
    return changed_files[:1]


def _package_source_paths(workspace: Path, commit: str, unit_path: str) -> list[str]:
    tree_path = "" if unit_path == "." else unit_path.rstrip("/")
    output = _git_output(workspace, "ls-tree", "-r", "--name-only", commit, "--", tree_path)
    if not output:
        return []
    return sorted(
        line.strip()
        for line in output.splitlines()
        if line.strip() and _is_supported_source_path(line.strip())
    )


def _file_snapshot_diff(
    workspace: Path,
    parent: str,
    commit: str,
    path: str,
    *,
    unit_id: str | None = None,
) -> dict[str, Any] | None:
    old_full = _git_file(workspace, parent, path)
    new_full = _git_file(workspace, commit, path)
    if old_full is None and new_full is None:
        return None
    old_text = old_full or ""
    new_text = new_full or ""
    if unit_id and _is_code_unit(unit_id):
        old_text = _extract_unit_text(old_text, unit_id) or old_text
        new_text = _extract_unit_text(new_text, unit_id) or new_text
    old_changed, new_changed = _changed_line_numbers(old_text, new_text)
    return {
        "path": path,
        "old_text": old_text,
        "new_text": new_text,
        "old_changed": old_changed,
        "new_changed": new_changed,
    }


def _extract_unit_text(source: str, unit_id: str) -> str | None:
    if not source.strip():
        return ""
    _, _, qualified_name = unit_id.partition("::")
    if not qualified_name or qualified_name in {"<file>", "package"}:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    parts = qualified_name.split(".")
    target = _find_ast_unit(tree.body, parts)
    if target is None:
        return None
    start_line = min(
        [
            getattr(decorator, "lineno", target.lineno)
            for decorator in getattr(target, "decorator_list", [])
        ]
        or [target.lineno]
    )
    end_line = getattr(target, "end_lineno", None)
    if end_line is None:
        return None
    lines = source.splitlines()
    return "\n".join(lines[start_line - 1 : end_line])


def _find_ast_unit(nodes: Iterable[Any], parts: list[str]) -> Any | None:
    if not parts:
        return None
    for node in nodes:
        if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if getattr(node, "name", None) != parts[0]:
            continue
        if len(parts) == 1:
            return node
        if isinstance(node, ast.ClassDef):
            return _find_ast_unit(node.body, parts[1:])
    return None


def _changed_line_numbers(old_text: str, new_text: str) -> tuple[set[int], set[int]]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = SequenceMatcher(a=old_lines, b=new_lines)
    old_changed: set[int] = set()
    new_changed: set[int] = set()
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_changed.update(range(old_start + 1, old_end + 1))
        new_changed.update(range(new_start + 1, new_end + 1))
    return old_changed, new_changed


def _highlighted_lines(path: str, text: str) -> list[str]:
    lines = text.splitlines() or [""]
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_for_filename(path, text)
        except ClassNotFound:
            lexer = get_lexer_for_filename(f"file.{_language_extension(path)}", text)
        highlighted = highlight(text, lexer, HtmlFormatter(nowrap=True)).splitlines()
        return (
            highlighted
            if len(highlighted) == len(lines)
            else [_fallback_highlight_line(line, path) for line in lines]
        )
    except Exception:
        return [_fallback_highlight_line(line, path) for line in lines]


def _fallback_highlight_line(line: str, path: str) -> str:
    escaped = _h(line)
    language = _language_extension(path)
    keyword_groups = {
        "py": "False|None|True|and|as|assert|async|await|break|class|continue|def|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield",
        "js": "const|let|var|function|return|if|else|for|while|class|new|import|export|await|async|try|catch|throw|switch|case|break|continue|extends",
        "ts": "const|let|var|function|return|if|else|for|while|class|new|import|export|await|async|try|catch|throw|switch|case|break|continue|extends|interface|type|implements|readonly",
        "java": "class|interface|enum|public|private|protected|static|final|void|return|if|else|for|while|try|catch|throw|new|extends|implements",
        "go": "func|package|import|return|if|else|for|range|struct|type|interface|go|defer|switch|case|break|continue|var|const",
        "rs": "fn|let|mut|pub|impl|trait|struct|enum|match|if|else|for|while|loop|return|use|mod|crate|async|await",
    }
    keywords = keyword_groups.get(language, keyword_groups["py"])
    escaped = re.sub(rf"\b({keywords})\b", r'<span class="k">\1</span>', escaped)
    escaped = re.sub(r"(&quot;.*?&quot;|&#x27;.*?&#x27;)", r'<span class="s">\1</span>', escaped)
    comment_marker = (
        "//" if language in {"js", "ts", "java", "go", "rs", "cpp", "cs", "kt", "swift"} else "#"
    )
    if comment_marker in escaped:
        before, marker, after = escaped.partition(comment_marker)
        escaped = f'{before}<span class="c">{marker}{after}</span>'
    return escaped


def _git_output(workspace: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_file(workspace: Path, revision: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _unit_title(unit_id: str) -> str:
    path, _, name = unit_id.partition("::")
    if name == "package":
        return f"Package: {path}"
    if name == "cluster":
        return f"Cluster: {path}"
    if name == "<file>":
        return f"File: {path}"
    return name or unit_id


def _is_package_unit(unit_path: str, unit_name: str) -> bool:
    return unit_name == "package" and not unit_path.endswith((".py", ".js", ".ts", ".java"))


def _is_cluster_unit(unit_name: str) -> bool:
    return unit_name == "cluster"


def _is_code_unit(unit_id: str) -> bool:
    _, _, unit_name = unit_id.partition("::")
    return unit_name not in {"", "<file>", "package", "cluster"}


def _is_supported_source_path(path: str) -> bool:
    return Path(path).suffix.lower() in {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".go",
        ".rs",
        ".cs",
        ".kt",
        ".kts",
        ".swift",
        ".rb",
        ".php",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sql",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".md",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
    }


def _language_extension(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return {
        "jsx": "js",
        "tsx": "ts",
        "cxx": "cpp",
        "cc": "cpp",
        "hpp": "cpp",
        "h": "cpp",
        "kts": "kt",
    }.get(suffix, suffix or "text")


def _dom_id(value: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower() or "unit"
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{stem}-{digest}"


def _render_metrics(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> str:
    failed = len([entry for entry in ledger if "failed" in str(entry.get("outcome", ""))])
    accepted = len(
        [entry for entry in ledger if entry.get("outcome") in {"accepted", "tests_added"}]
    )
    return f"""
    <section class="metrics">
      {_metric("Run", str(state.get("run_id") or "unknown"))}
      {_metric("Events", str(len(events)))}
      {_metric("Codex Calls", str(len(calls)))}
      {_metric("Accepted Steps", f"{accepted} accepted, {failed} failed")}
    </section>
"""


def _metric(label: str, value: str) -> str:
    return f"""
      <div class="metric">
        <div class="metric-label">{_h(label)}</div>
        <div class="metric-value">{_h(value)}</div>
      </div>
"""


def _render_events(events: list[dict[str, Any]]) -> str:
    if not events:
        body = '<div class="empty">No workflow events recorded yet.</div>'
    else:
        body = "\n".join(_render_event(event) for event in reversed(events))
    return f'<section class="section"><h2>Workflow Timeline</h2><div class="timeline">{body}</div></section>'


def _render_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("event") or "event")
    payload = {
        key: value
        for key, value in event.items()
        if key not in {"timestamp", "event", "diff", "test_diff", "refactor_diff"}
    }
    diff = event.get("diff") or event.get("test_diff") or event.get("refactor_diff")
    return f"""
    <article class="event">
      <div class="time">{_time_tag(str(event.get("timestamp") or ""))}</div>
      <div>
        <div class="event-name">{_h(event_type.replace("_", " ").title())}</div>
        {_compact_payload(payload)}
        {_json_details("Event JSON", event)}
        {_code_details("Diff", str(diff), language="diff") if diff else ""}
      </div>
    </article>
"""


def _render_ledger(ledger: list[dict[str, Any]]) -> str:
    if not ledger:
        return '<section class="section"><h2>Ledger</h2><div class="empty">No ledger entries yet.</div></section>'
    rows = []
    for entry in ledger:
        outcome = str(entry.get("outcome") or "")
        rows.append(
            f"""
            <div class="call">
              <div class="call-head">
                <div class="call-title outcome-{_h(outcome)}">{_h(outcome)}</div>
                <span class="pill path-value">{_h(str(entry.get("unit_id") or ""))}</span>
              </div>
              <dl class="kv">
                <dt>Message</dt><dd>{_text_html(str(entry.get("message") or ""))}</dd>
                <dt>Changed files</dt><dd>{_list_text_html(entry.get("changed_files") or [], empty="none", css_class="path-value")}</dd>
                <dt>Checks</dt><dd>{_list_text_html(entry.get("checks_run") or [], separator="; ", empty="none", css_class="command-value")}</dd>
              </dl>
              {_json_details("Ledger JSON", entry)}
            </div>
"""
        )
    return f'<section class="section"><h2>Ledger</h2>{"".join(rows)}</section>'


def _render_codex_calls(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return '<section class="section"><h2>Codex Calls</h2><div class="empty">No Codex calls recorded yet.</div></section>'
    return f'<section class="section"><h2>Codex Calls</h2>{"".join(_render_codex_call(call) for call in calls)}</section>'


def _render_codex_call(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "codex")
    error = call.get("error")
    parsed = call.get("parsed_output")
    level = "level-error" if error or call.get("returncode") else "level-ok"
    body = _render_parsed_output(name, parsed)
    langfuse = _langfuse_line(call)
    return f"""
    <article class="call">
      <div class="call-head">
        <div>
          <div class="call-title {level}">{_h(name.replace("_", " ").title())}</div>
          <div class="muted">{_h(str(call.get("schema_model") or ""))} in {_h(str(call.get("duration_seconds") or "?"))}s</div>
        </div>
        <span class="pill">returncode {_h(str(call.get("returncode")))}</span>
      </div>
      {body}
      {langfuse}
      {_text_details("Prompt", str(call.get("prompt") or ""))}
      {_json_details("Parsed Output JSON", parsed) if parsed is not None else ""}
      {_text_details("Stdout", str(call.get("stdout") or "")) if call.get("stdout") else ""}
      {_text_details("Stderr", str(call.get("stderr") or "")) if call.get("stderr") else ""}
      {_json_details("Output Schema", call.get("output_schema"))}
    </article>
"""


def _render_parsed_output(name: str, parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return '<div class="empty">No parsed output.</div>'
    if name == "test_audit":
        recs = parsed.get("recommendations") or []
        return f"""
          <dl class="kv">
            <dt>Adequate</dt><dd>{_json_scalar_html(parsed.get("adequate"))}</dd>
            <dt>Reason</dt><dd>{_text_html(str(parsed.get("reason") or ""))}</dd>
          </dl>
          {_render_recommendations(recs)}
"""
    if name == "test_write":
        return f"""
          <dl class="kv">
            <dt>Message</dt><dd>{_text_html(str(parsed.get("message") or ""))}</dd>
            <dt>Changed files</dt><dd>{_list_text_html(parsed.get("changed_files") or [], empty="none", css_class="path-value")}</dd>
            <dt>Test commands</dt><dd>{_list_text_html(parsed.get("test_commands") or [], separator="; ", empty="none", css_class="command-value")}</dd>
            <dt>Assumptions</dt><dd>{_list_text_html(parsed.get("assumptions") or [], separator="; ", empty="none")}</dd>
          </dl>
"""
    if name == "retrieval":
        return _render_retrieval_ideas(parsed.get("ideas") or [])
    if name == "refactor":
        return f"""
          <dl class="kv">
            <dt>Outcome</dt><dd>{_text_html(str(parsed.get("outcome") or ""))}</dd>
            <dt>Changed files</dt><dd>{_list_text_html(parsed.get("changed_files") or [], empty="none", css_class="path-value")}</dd>
            <dt>Message</dt><dd>{_text_html(str(parsed.get("message") or ""))}</dd>
            <dt>Assumptions</dt><dd>{_list_text_html(parsed.get("assumptions") or [], separator="; ", empty="none")}</dd>
          </dl>
"""
    if name == "judge":
        return f"""
          <dl class="kv">
            <dt>Accepted</dt><dd>{_json_scalar_html(parsed.get("accepted"))}</dd>
            <dt>Summary</dt><dd>{_text_html(str(parsed.get("summary") or ""))}</dd>
            <dt>Issues</dt><dd>{_list_text_html(parsed.get("issues") or [], separator="; ", empty="none")}</dd>
          </dl>
"""
    return _json_details("Output", parsed)


def _render_recommendations(recommendations: list[dict[str, Any]]) -> str:
    if not recommendations:
        return '<div class="empty">No recommendations.</div>'
    items = []
    for recommendation in recommendations:
        items.append(
            f"""
            <li>
              <strong>{_h(str(recommendation.get("name") or ""))}</strong>
              <div>{_text_html(str(recommendation.get("behavior") or ""))}</div>
              <div class="muted"><span class="path-value">{_h(str(recommendation.get("suggested_location") or ""))}</span> · {_text_html(str(recommendation.get("reason") or ""))}</div>
            </li>
"""
        )
    return f'<h3>Proposed Tests</h3><ul class="clean">{"".join(items)}</ul>'


def _render_retrieval_ideas(ideas: list[dict[str, Any]]) -> str:
    if not ideas:
        return '<div class="empty">No retrieval ideas.</div>'
    items = []
    for idea in ideas:
        code_example = str(idea.get("code_example") or "")
        example_html = _text_html(code_example)
        if not _looks_like_source_code(code_example):
            example_html = f"<strong>{example_html}</strong>"
        items.append(
            f"""
            <li>
              <div class="idea-example">{example_html}</div>
              <dl class="kv">
                <dt>Why</dt><dd>{_text_html(str(idea.get("why") or ""))}</dd>
                <dt>How</dt><dd>{_text_html(str(idea.get("how") or ""))}</dd>
              </dl>
            </li>
"""
        )
    return f'<h3>Retrieval Ideas</h3><ul class="clean">{"".join(items)}</ul>'


def _render_summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return '<section class="section"><h2>Summary</h2><div class="empty">Summary is not written yet.</div></section>'
    return f"""
    <section class="section">
      <h2>Summary</h2>
      <dl class="kv">
        <dt>Apply command</dt><dd>{_text_html(str(summary.get("apply_command") or ""), css_class="command-value")}</dd>
        <dt>Applied</dt><dd>{_list_text_html(summary.get("applied_changes") or [], separator="; ", empty="none")}</dd>
        <dt>Skipped</dt><dd>{_list_text_html(summary.get("skipped_changes") or [], separator="; ", empty="none")}</dd>
      </dl>
      {_json_details("Summary JSON", summary)}
    </section>
"""


def _compact_payload(payload: dict[str, Any]) -> str:
    unit = payload.get("unit_id") or (payload.get("unit") or {}).get("unit_id")
    message = payload.get("message") or payload.get("reason")
    changed = payload.get("changed_files")
    parts = []
    if unit:
        parts.append(
            f'<div class="muted">Unit: <span class="path-value">{_h(str(unit))}</span></div>'
        )
    if message:
        parts.append(f"<div>{_text_html(str(message))}</div>")
    if changed:
        parts.append(
            f'<div class="muted">Changed: {_list_text_html(changed, css_class="path-value")}</div>'
        )
    return "".join(parts)


def _json_details(label: str, value: Any) -> str:
    return _details_html(label, _json_html(value), kind="json", identity=_json_text(value))


def _text_details(label: str, body: str) -> str:
    return _details_html(label, _text_html(body), kind="text", identity=body)


def _code_details(label: str, body: str, *, language: str = "") -> str:
    return _details_html(
        label,
        _code_html(body, language=language),
        kind=f"code {language}".strip(),
        identity=body,
    )


def _details_html(label: str, body_html: str, *, kind: str, identity: str) -> str:
    details_id = _details_id(label, identity)
    return (
        f'<details data-details-id="{_h(details_id)}">'
        f"<summary>{_h(label)}</summary>"
        f'<div class="details-body {_h(kind)}-body">{body_html}</div>'
        "</details>"
    )


def _langfuse_line(call: dict[str, Any]) -> str:
    trace_id = call.get("langfuse_trace_id")
    observation_id = call.get("langfuse_observation_id")
    error = call.get("langfuse_error")
    if trace_id:
        return f'<div class="muted">Langfuse trace: {_h(str(trace_id))}; observation: {_h(str(observation_id or ""))}</div>'
    if error:
        return f'<div class="muted">Langfuse unavailable: {_h(str(error))}</div>'
    return ""


def _refresh_note(running: bool) -> str:
    return '<span class="pill running" data-refresh-status>Refresh: 5s</span>' if running else ""


def _time_tag(value: str) -> str:
    if not value:
        return ""
    return f'<time class="local-time" datetime="{_h(value)}">{_h(_minute_time(value))}</time>'


def _minute_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value.split(".")[0].replace("T", " ")[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _run_started_at(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("event") == "run_started" and event.get("timestamp"):
            return str(event["timestamp"])
    if events and events[0].get("timestamp"):
        return str(events[0]["timestamp"])
    return None


def _elapsed_snapshot(
    events: list[dict[str, Any]],
    status: str,
    generated_at: str,
) -> dict[str, object]:
    start_events = {"run_started", "run_resumed"}
    stop_events = {"run_interrupted", "run_completed", "final_verification_failed"}
    generated = _parse_dashboard_time(generated_at)
    active_start: datetime | None = None
    active_started_at: str | None = None
    total_seconds = 0.0
    saw_active_event = False

    for event in events:
        event_type = str(event.get("event") or "")
        timestamp = _parse_dashboard_time(str(event.get("timestamp") or ""))
        if timestamp is None:
            continue
        if event_type in start_events:
            if active_start is not None:
                total_seconds += max(0.0, (timestamp - active_start).total_seconds())
            active_start = timestamp
            saw_active_event = True
            continue
        if event_type in stop_events and active_start is not None:
            total_seconds += max(0.0, (timestamp - active_start).total_seconds())
            active_start = None

    if active_start is not None and generated is not None:
        total_seconds += max(0.0, (generated - active_start).total_seconds())
        if status == "running":
            active_started_at = active_start.isoformat()

    if saw_active_event:
        seconds = max(0, int(total_seconds))
        return {
            "seconds": seconds,
            "active_started_at": active_started_at,
            "text": _elapsed_seconds_text(seconds),
        }

    started_at = _run_started_at(events)
    if started_at and generated_at:
        return {
            "seconds": None,
            "active_started_at": None,
            "text": _elapsed_text(started_at, generated_at),
        }
    return {"seconds": None, "active_started_at": None, "text": "unknown"}


def _elapsed_text(started_at: str, ended_at: str) -> str:
    if not started_at:
        return "unknown"
    start = _parse_dashboard_time(started_at)
    end = _parse_dashboard_time(ended_at)
    if start is None or end is None:
        return "unknown"
    return _elapsed_seconds_text(max(0, int((end - start).total_seconds())))


def _elapsed_seconds_text(total_seconds: int) -> str:
    total_minutes = max(0, total_seconds // 60)
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _parse_dashboard_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _json_html(value: Any) -> str:
    return f'<div class="json-tree">{_json_value_html(value)}</div>'


def _json_value_html(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return '<span class="json-empty">{}</span>'
        rows = []
        for key, item in value.items():
            rows.append(
                '<div class="json-pair">'
                f'<span class="json-key">{_h(json.dumps(str(key), ensure_ascii=False))}</span>'
                f'<div class="json-value">{_json_value_html(item)}</div>'
                "</div>"
            )
        return f'<div class="json-object">{"".join(rows)}</div>'
    if isinstance(value, list):
        if not value:
            return '<span class="json-empty">[]</span>'
        rows = []
        for index, item in enumerate(value):
            rows.append(
                '<div class="json-item">'
                f'<span class="json-index">[{index}]</span>'
                f'<div class="json-value">{_json_value_html(item)}</div>'
                "</div>"
            )
        return f'<div class="json-array">{"".join(rows)}</div>'
    if isinstance(value, str):
        if _looks_like_source_code(value):
            return _code_html(value, language=_detect_language(value))
        return f'<span class="json-string">{_h(value)}</span>'
    return _json_scalar_html(value)


def _json_scalar_html(value: Any) -> str:
    if value is None:
        return '<span class="json-null">null</span>'
    if isinstance(value, bool):
        return f'<span class="json-bool">{str(value).lower()}</span>'
    if isinstance(value, int | float):
        return f'<span class="json-number">{_h(str(value))}</span>'
    return f'<span class="json-string">{_h(str(value))}</span>'


def _text_html(value: str, *, css_class: str = "") -> str:
    if _looks_like_source_code(value):
        return _code_html(value, language=_detect_language(value))
    classes = " ".join(part for part in ("text-value", css_class) if part)
    return f'<span class="{_h(classes)}">{_h(value)}</span>'


def _list_text_html(
    values: list[Any],
    *,
    separator: str = ", ",
    empty: str = "",
    css_class: str = "",
) -> str:
    if not values:
        return _text_html(empty)
    return _text_html(separator.join(str(value) for value in values), css_class=css_class)


def _code_html(value: str, *, language: str = "") -> str:
    classes = " ".join(
        part for part in ("code-block", f"language-{language}" if language else "") if part
    )
    return f'<pre class="{_h(classes)}"><code>{_h(value)}</code></pre>'


def _looks_like_source_code(value: str) -> bool:
    if not value.strip():
        return False
    if value.startswith("diff --git") or "\ndiff --git " in value:
        return True
    if "```" in value:
        return True
    code_patterns = [
        r"^\s*(async\s+def|def|class)\s+\w+",
        r"^\s*(from\s+\S+\s+import|import\s+\S+)",
        r"^\s*(if|elif|for|while|try|except|with|match|case)\b.*:",
        r"^\s*return\b",
        r"^\s*@\w+",
    ]
    lines = value.splitlines()
    if len(lines) < 2:
        return False
    matches = 0
    for line in lines:
        if any(re.search(pattern, line) for pattern in code_patterns):
            matches += 1
    return matches >= 2


def _detect_language(value: str) -> str:
    stripped = value.lstrip()
    if stripped.startswith("diff --git"):
        return "diff"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if re.search(r"^\s*(async\s+def|def|class)\s+\w+", value, re.MULTILINE):
        return "python"
    if re.search(r"^\s*(from\s+\S+\s+import|import\s+\S+)", value, re.MULTILINE):
        return "python"
    return "text"


def _details_id(label: str, identity: str) -> str:
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label.strip().lower()).strip("-")
    checksum = hashlib.sha256(f"{label}\n{identity}".encode()).hexdigest()[:12]
    return f"{safe_label}-{checksum}"


def _h(value: str) -> str:
    return html.escape(value, quote=True)
