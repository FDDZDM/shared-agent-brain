"""Shared Brain self-test suite (Hermes side).

Mirrors integrations/deepseek-harness/src/selftest.ts so that `/brain test`
produces the same report on both platforms. Runs against the live server
through the real client (T1/T2/T9 use a bare unauthenticated HTTP client to
exercise the auth and idempotency paths). Test data lives in the isolated
``__selftest__`` project and is cleaned up afterwards.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Tuple

import httpx

from .client import BrainClientError, SharedBrainClient

SELFTEST_PROJECT = "__selftest__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_test(id_: str, name: str, fn: Callable[[], Tuple[bool, str]]) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        passed, detail = fn()
        status = "pass" if passed else "fail"
    except BrainClientError as exc:
        status, detail = "fail", str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 - report any unexpected failure
        status, detail = "fail", str(exc)[:200]
    return {
        "id": id_,
        "name": name,
        "status": status,
        "detail": detail,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def render_test_report(report: Dict[str, Any]) -> str:
    lines = [
        "🧠 Shared Brain 自检报告",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"环境: {report['server_url']} · project: {report['project_key']} · agent: {report['agent_id']}",
        f"时间: {report['started_at']} · 模式: {report['mode']} · 耗时: {report['duration_ms'] / 1000:.1f}s",
        "",
    ]
    for r in report["results"]:
        icon = {"pass": "✅", "fail": "❌", "skip": "⏭"}[r["status"]]
        suffix = "" if r["status"] == "skip" else f" ({r['duration_ms']}ms)"
        lines.append(f"{icon} {r['id']} {r['name']} | {r['detail']}{suffix}")
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for r in report["results"]:
        counts[r["status"]] += 1
    verdict = "✅ PASS" if report["passed"] else "❌ FAIL"
    lines.append("")
    lines.append(f"结果: {counts['pass']} 通过 / {counts['fail']} 失败 / {counts['skip']} 跳过 → {verdict}")
    return "\n".join(lines)


def run_selftest(client: SharedBrainClient, quick: bool = False) -> Dict[str, Any]:
    mode = "quick" if quick else "full"
    started_at = _now_iso()
    started = time.perf_counter()
    base = client.server_url.rstrip("/")
    results: List[Dict[str, Any]] = []
    aborted = False

    def t(id_: str, name: str, fn: Callable[[], Tuple[bool, str]]) -> None:
        nonlocal aborted
        if aborted:
            results.append(
                {"id": id_, "name": name, "status": "skip", "detail": "前置失败，跳过", "duration_ms": 0}
            )
            return
        result = _run_test(id_, name, fn)
        results.append(result)
        if result["status"] == "fail" and id_ in ("T1", "T3"):
            aborted = True

    # 测试数据：独立项目 + 唯一 marker；T12 负责全部清理。
    marker = f"selftest-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:8]}"
    created: List[Dict[str, str]] = []
    seeded = {
        "title": f"自测检索验证 {marker}",
        "content": f"自测检索验证 {marker} Shared Brain selftest",
    }

    def no_data() -> Tuple[bool, str]:
        return False, "无测试数据（T4 失败）"

    def first_created() -> Dict[str, str] | None:
        return created[0] if created else None

    # T1/T2/T9 需要不带 Authorization 的裸请求（T2 本身就要验证 401）。
    bare = httpx.Client(timeout=5.0)
    try:
        def t1() -> Tuple[bool, str]:
            response = bare.get(f"{base}/health")
            return response.status_code == 200, f"HTTP {response.status_code}"

        t("T1", "服务器可达", t1)

        def t2() -> Tuple[bool, str]:
            response = bare.get(f"{base}/v1/memories/search", params={"q": "selftest"})
            return response.status_code == 401, f"期望 401, 实得 {response.status_code}"

        t("T2", "鉴权拦截", t2)

        def t3() -> Tuple[bool, str]:
            missing = []
            if not client.server_url:
                missing.append("server_url")
            if not client.token:
                missing.append("token")
            if not client.project_key:
                missing.append("project_key")
            return (not missing, "已配置" if not missing else f"缺少: {', '.join(missing)}")

        t("T3", "配置完整性", t3)

        def t4() -> Tuple[bool, str]:
            record = client.remember(
                seeded["title"],
                seeded["content"],
                scope="project",
                kind="fact",
                project_key=SELFTEST_PROJECT,
            )
            if "queued" in record:
                return False, "写入了离线队列（服务器不可达？）"
            created.append({"id": record["id"], "version": str(record["current_version"])})
            return True, f"id={record['id'][:8]}…, v{record['current_version']}"

        t("T4", "写入", t4)

        def t5() -> Tuple[bool, str]:
            if not first_created():
                return no_data()
            items = client.search("自测检索", project_key=SELFTEST_PROJECT, limit=5)
            return (
                any(marker in item["content_text"] for item in items),
                f"{len(items)} 条命中" if any(marker in item["content_text"] for item in items)
                else f"期望命中含 {marker}, 实得 {len(items)} 条",
            )

        t("T5", "中文检索(FTS)", t5)

        if mode == "full":
            def t6() -> Tuple[bool, str]:
                if not first_created():
                    return no_data()
                items = client.search("自测", project_key=SELFTEST_PROJECT, limit=5)
                return (
                    any(marker in item["content_text"] for item in items),
                    f"{len(items)} 条命中" if any(marker in item["content_text"] for item in items)
                    else f"期望命中含 {marker}, 实得 {len(items)} 条",
                )

            t("T6", "短词检索(LIKE)", t6)

            def t7() -> Tuple[bool, str]:
                record = first_created()
                if not record:
                    return no_data()
                try:
                    client.update(record["id"], 99_999, content_text="stale write")
                    return False, "期望 409, 实得成功"
                except BrainClientError as exc:
                    status_code = int(str(exc).split(":", 1)[0]) if str(exc)[:3].isdigit() else 0
                    return status_code == 409, f"期望 409, 实得 {status_code}"

            t("T7", "乐观锁", t7)

            def t8() -> Tuple[bool, str]:
                record = first_created()
                if not record:
                    return no_data()
                previous = int(record["version"])
                updated = client.update(record["id"], previous, content_text=f"{seeded['content']} v2")
                if "queued" in updated:
                    return False, "入队离线（未同步）"
                record["version"] = str(updated["current_version"])
                return (
                    updated["current_version"] == previous + 1,
                    f"v{previous}→v{updated['current_version']}"
                    if updated["current_version"] == previous + 1
                    else f"期望 v{previous + 1}, 实得 v{updated['current_version']}",
                )

            t("T8", "版本更新", t8)

            def t9() -> Tuple[bool, str]:
                op_key = f"selftest-idem-{marker}"
                payload = {
                    "scope": "project",
                    "kind": "fact",
                    "project_key": SELFTEST_PROJECT,
                    "title": f"幂等验证 {marker}",
                    "content_text": f"幂等验证 {marker}",
                    "source_agent": client.agent_id,
                    "trust_level": 0,
                }
                headers = {
                    "Authorization": f"Bearer {client.token}",
                    "Idempotency-Key": op_key,
                }
                first = bare.post(f"{base}/v1/memories", json=payload, headers=headers).json()
                second = bare.post(f"{base}/v1/memories", json=payload, headers=headers).json()
                if first.get("id") != second.get("id"):
                    return False, f"同 op_key 两次返回不同 id: {first.get('id')} vs {second.get('id')}"
                if not any(item["id"] == first["id"] for item in created):
                    created.append({"id": first["id"], "version": str(first.get("current_version", 1))})
                return True, f"同 id={first['id'][:8]}…"

            t("T9", "幂等", t9)

            def t10() -> Tuple[bool, str]:
                if not first_created():
                    return no_data()
                items = client.search(marker, limit=5)
                return items == [], f"期望 0 命中, 实得 {len(items)}"

            t("T10", "项目隔离", t10)

            def t11() -> Tuple[bool, str]:
                target = first_created()
                if not target:
                    return no_data()
                result = client.forget(target["id"], int(target["version"]))
                if "queued" in result:
                    return False, "入队离线（未同步）"
                # FTS 使用 trigram OR，T9 的幂等记录共享本轮 marker，也可能被召回。
                # tombstone 的正确判据是目标 id 消失，而不是整个结果集必须为空。
                items = client.search(
                    f"自测检索验证 {marker}", project_key=SELFTEST_PROJECT, limit=5
                )
                target_still_visible = any(item["id"] == target["id"] for item in items)
                return (
                    not target_still_visible,
                    f"目标已隐藏（另有 {len(items)} 条同批次命中）" if not target_still_visible
                    else f"删除目标 {target['id'][:8]}… 仍可见",
                )

            t("T11", "tombstone 删除", t11)

        def t12() -> Tuple[bool, str]:
            # 1) 本轮创建的记录（含幂等测试记录）。
            for item in created:
                try:
                    client.forget(item["id"], int(item["version"]))
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            # 2) 历史残留：此前运行中断或 quick 模式遗留的 `自测检索验证` 记录。
            leftovers = client.search("自测检索验证", project_key=SELFTEST_PROJECT, limit=20)
            for item in leftovers:
                try:
                    client.forget(item["id"], item["current_version"])
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            items = client.search("selftest-", project_key=SELFTEST_PROJECT, limit=20)
            return items == [], f"残留 {len(items)} 条"

        t("T12", "数据清理", t12)
    finally:
        bare.close()

    report = {
        "server_url": client.server_url,
        "project_key": client.project_key or "",
        "agent_id": client.agent_id,
        "mode": mode,
        "started_at": started_at,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "results": results,
        "passed": all(result["status"] != "fail" for result in results),
    }
    report["text"] = render_test_report(report)
    return report
