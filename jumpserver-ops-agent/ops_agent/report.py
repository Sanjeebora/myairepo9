from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .redact import Redactor
from .ssh_runner import ScriptResult

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def _md_escape(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(plan: dict, results: list[ScriptResult], generated_at: datetime) -> str:
    lines = [
        "# Server Health Check - Action Plan",
        "",
        f"Generated: {generated_at:%Y-%m-%d %H:%M:%S %Z}".rstrip(),
        f"Overall status: **{plan.get('overall_status', 'UNKNOWN')}**",
        "",
        "## Summary",
        "",
        str(plan.get("summary", "")),
        "",
        "## Script execution",
        "",
        "| Server | Script | Status | Exit code | Duration |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.server} | {r.script} | {r.status} | {r.exit_code} | {r.duration_s:.1f}s |")

    actions = sorted(plan.get("action_plan") or [], key=lambda a: a.get("priority", 99))
    lines += ["", "## Action plan", ""]
    if not actions:
        lines.append("No actions required.")
    for action in actions:
        lines += [
            f"### {action.get('priority', '-')}. {action.get('title', '')}",
            "",
            f"- Servers: {', '.join(action.get('servers') or []) or '-'}",
            f"- Owner: {action.get('owner', '-')}",
            f"- Risk: {action.get('risk', '-')}",
            f"- Reason: {action.get('reason', '')}",
        ]
        steps = action.get("steps") or []
        if steps:
            lines += ["", "Steps:", ""]
            lines += [f"{i}. {step}" for i, step in enumerate(steps, 1)]
        commands = action.get("suggested_commands") or []
        if commands:
            lines += ["", "Suggested commands (review before running):", "", "```bash", *commands, "```"]
        lines.append("")

    lines += ["## Findings by server", ""]
    for server in plan.get("servers") or []:
        lines += [f"### {server.get('name')} - {server.get('status', '')}", ""]
        findings = sorted(server.get("findings") or [],
                          key=lambda f: _SEVERITY_ORDER.get(str(f.get("severity")).upper(), 9))
        if not findings:
            lines += ["No findings.", ""]
            continue
        lines += ["| Severity | Finding | Evidence | Impact |", "| --- | --- | --- | --- |"]
        for f in findings:
            lines.append(f"| {_md_escape(f.get('severity', ''))} | {_md_escape(f.get('title', ''))} "
                         f"| `{_md_escape(f.get('evidence', ''))}` | {_md_escape(f.get('impact', ''))} |")
        lines.append("")

    review = plan.get("needs_human_review") or []
    if review:
        lines += ["## Needs human review", ""] + [f"- {item}" for item in review] + [""]
    if "raw_response" in plan:
        lines += ["## Raw LLM response", "", "```", str(plan["raw_response"]), "```", ""]
    return "\n".join(lines)


def write_report(reports_dir: str | Path, plan: dict | None, results: list[ScriptResult],
                 redactor: Redactor) -> Path:
    now = datetime.now().astimezone()
    out_dir = Path(reports_dir) / now.strftime("%Y%m%d-%H%M%S")
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for r in results:
        body = (f"# server={r.server} host={r.host} script={r.script} status={r.status} "
                f"exit_code={r.exit_code}\n# command: {r.command}\n")
        if r.error:
            body += f"# error: {r.error}\n"
        body += f"\n--- stdout ---\n{redactor(r.stdout)}\n--- stderr ---\n{redactor(r.stderr)}\n"
        (raw_dir / f"{r.server}__{r.script}.log").write_text(body)

    summary = {
        "generated_at": now.isoformat(),
        "results": [{k: v for k, v in r.to_dict().items() if k not in {"stdout", "stderr"}}
                    for r in results],
        "plan": plan,
    }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2))
    if plan is not None:
        (out_dir / "report.md").write_text(render_markdown(plan, results, now))
    return out_dir
