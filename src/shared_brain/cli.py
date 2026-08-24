"""Administrative and diagnostic CLI; runtime I/O stays in native plugins."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from typing import Any, Dict, Optional

from .client import BrainClientError, SharedBrainClient
from .config import load_config, save_config


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _client(config_path: Optional[str] = None) -> SharedBrainClient:
    config = load_config(config_path)
    missing = [key for key in ("server_url", "token", "agent_id") if not config.get(key)]
    if missing:
        raise SystemExit(f"missing config: {', '.join(missing)}; run `amm config` first")
    return SharedBrainClient(
        config["server_url"],
        config["token"],
        config["agent_id"],
        config.get("project_key"),
        config.get("queue_path"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amm", description="Shared Brain admin and diagnostics")
    parser.add_argument("--config-path")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="write client configuration")
    config.add_argument("server_url")
    config.add_argument("agent_id")
    config.add_argument("--project-key")
    config.add_argument("--token", help="prefer BRAIN_TOKEN or the secure prompt")

    sub.add_parser("doctor", help="check server, auth, database, project, FTS, and retry queue").add_argument(
        "--json", action="store_true", help="machine-readable output"
    )

    queue = sub.add_parser("queue", help="inspect or flush offline writes")
    queue.add_argument("action", choices=("list", "flush", "remove", "retry"))
    queue.add_argument("op_key", nargs="?")
    queue.add_argument("--verbose", action="store_true", help="show full payloads in list")

    memory = sub.add_parser("memory", help="manual memory operations")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    search = memory_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    remember = memory_sub.add_parser("remember")
    remember.add_argument("title")
    remember.add_argument("content")
    remember.add_argument("--scope", choices=("global", "user", "project"), default="project")
    remember.add_argument("--kind", choices=("fact", "preference", "decision", "pitfall"), default="fact")
    remember.add_argument("--trust-level", type=int, default=0)
    update = memory_sub.add_parser("update")
    update.add_argument("memory_id")
    update.add_argument("expected_version", type=int)
    update.add_argument("--title")
    update.add_argument("--content")
    forget = memory_sub.add_parser("forget")
    forget.add_argument("memory_id")
    forget.add_argument("expected_version", type=int)
    return parser


def _doctor_report(client: SharedBrainClient) -> Dict[str, Any]:
    """分项健康检查：network / server / auth / project / fts / queue。"""
    checks: Dict[str, Any] = {}

    try:
        health = client.health()
        checks["network"] = {"ok": True, "detail": "reachable"}
        checks["server"] = {"ok": True, "detail": f"v{health.get('version', '?')}"}
    except Exception as exc:
        checks["network"] = {"ok": False, "detail": str(exc)}
        checks["server"] = {"ok": False, "detail": "unreachable"}

    try:
        whoami = client.whoami()
        checks["auth"] = {"ok": True, "detail": "token valid"}
        if checks.get("server", {}).get("ok"):
            checks["server"]["detail"] = (
                f"v{whoami.get('server_version', '?')} · schema v{whoami.get('schema_version', '?')}"
            )
    except BrainClientError as exc:
        status = f"{exc.status} " if exc.status else ""
        checks["auth"] = {"ok": False, "detail": f"{status}{exc}"}
    except Exception as exc:
        checks["auth"] = {"ok": False, "detail": str(exc)}

    project = getattr(client, "project_key", None)
    checks["project"] = {
        "ok": bool(project),
        "detail": project or "missing project_key in config",
    }

    try:
        client.search("doctor-probe-never-matches-12345")
        checks["fts"] = {"ok": True, "detail": "search endpoint ok"}
    except Exception as exc:
        checks["fts"] = {"ok": False, "detail": str(exc)}

    pending = client.queue.count()
    failed = client.queue.count("failed")
    checks["queue"] = {
        "ok": failed == 0,
        "detail": f"{pending} pending · {failed} failed",
        "pending": pending,
        "failed": failed,
    }

    ok = all(item.get("ok", True) for item in checks.values())
    return {"ok": ok, "checks": checks}


def _render_doctor(report: Dict[str, Any]) -> str:
    lines = ["Shared Brain doctor"]
    for name, check in report["checks"].items():
        mark = "✓" if check["ok"] else "✗"
        lines.append(f"  {name:<8} {mark} {check.get('detail', '')}")
    lines.append("doctor: " + ("OK" if report["ok"] else "FAILED"))
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "config":
        token = args.token or os.environ.get("BRAIN_TOKEN") or getpass.getpass("Brain token: ")
        if len(token) < 24:
            raise SystemExit("Brain token must be at least 24 characters")
        target = save_config(
            {
                "server_url": args.server_url.rstrip("/"),
                "token": token,
                "agent_id": args.agent_id,
                "project_key": args.project_key,
            },
            args.config_path,
        )
        print(f"wrote {target} with mode 0600")
        return

    client = _client(args.config_path)
    try:
        if args.command == "doctor":
            report = _doctor_report(client)
            if args.json:
                _json(report)
            else:
                print(_render_doctor(report))
            if not report["ok"]:
                raise SystemExit(1)
        elif args.command == "queue":
            if args.action == "list":
                _json(client.queue.list(limit=500, include_payload=args.verbose))
            elif args.action == "flush":
                _json(client.flush_queue())
            elif args.action == "remove":
                if not args.op_key:
                    raise SystemExit("usage: amm queue remove <op_key>")
                if not client.queue.remove(args.op_key):
                    raise SystemExit(f"no queued operation with op_key {args.op_key}")
                print(f"removed {args.op_key}")
            elif args.action == "retry":
                if not args.op_key:
                    raise SystemExit("usage: amm queue retry <op_key>")
                if not client.queue.retry(args.op_key):
                    raise SystemExit(f"no queued operation with op_key {args.op_key}")
                print(f"retry scheduled for {args.op_key}")
        elif args.command == "memory" and args.memory_command == "search":
            _json(client.search(args.query, limit=args.limit))
        elif args.command == "memory" and args.memory_command == "remember":
            _json(
                client.remember(
                    args.title,
                    args.content,
                    scope=args.scope,
                    kind=args.kind,
                    trust_level=args.trust_level,
                )
            )
        elif args.command == "memory" and args.memory_command == "update":
            _json(
                client.update(
                    args.memory_id,
                    args.expected_version,
                    title=args.title,
                    content_text=args.content,
                )
            )
        elif args.command == "memory" and args.memory_command == "forget":
            _json(client.forget(args.memory_id, args.expected_version))
    except BrainClientError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        client.close()


def server_main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="amm-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=os.environ.get("BRAIN_DB_PATH", "./data/shared-brain.db"))
    parser.add_argument("--token", help="prefer BRAIN_TOKEN; generated when omitted")
    args = parser.parse_args(argv)
    token = args.token or os.environ.get("BRAIN_TOKEN") or secrets.token_urlsafe(32)
    if not (args.token or os.environ.get("BRAIN_TOKEN")):
        print(f"Generated one-time Brain token: {token}", file=sys.stderr)
        print("Save it now; only its hash is persisted.", file=sys.stderr)
    os.environ["BRAIN_TOKEN"] = token
    os.environ["BRAIN_DB_PATH"] = args.db
    import uvicorn

    uvicorn.run("shared_brain.api:app_from_env", host=args.host, port=args.port, factory=True)

