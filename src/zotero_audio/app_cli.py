"""Short human commands and JSON contracts, without loading TTS at startup."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from .app_library import artifact_path, import_existing, visible_article
from .app_state import Store, TERMINAL


def daemon_status(store: Store):
    info = store.state("daemon", {})
    port = info.get("port", 8765)
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
            data = json.load(response)
            if data.get("version") and data.get("capabilities", {}).get("local_files"):
                return data
    except (OSError, ValueError):
        pass
    return None


def ensure_daemon(store: Store | None = None):
    store = store or Store()
    if daemon_status(store):
        return
    from .app_install import start_installed_service
    log = store.root / "daemon.log"
    if not start_installed_service(store):
        env = {**os.environ, "ZOTERO_AUDIO_RUNTIME": str(store.runtime)}
        with log.open("a", encoding="utf-8") as output:
            subprocess.Popen([sys.executable, "-m", "zotero_audio.app_cli", "serve"], stdin=subprocess.DEVNULL,
                             stdout=output, stderr=output, env=env, start_new_session=True)
    for _ in range(60):
        if daemon_status(store):
            return
        time.sleep(0.15)
    raise RuntimeError(f"The local worker did not start. Read {log}; an older audio batch may still hold its lock.")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _is_daemon_process(pid: int) -> bool:
    # A stale database PID must never signal an unrelated process after PID reuse.
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    return result.returncode == 0 and bool(re.search(r"(?:\bzotero_audio\.app_cli|(?:^|/)za)\s+serve(?:\s|$)", result.stdout))


def stop_daemon(store: Store, *, timeout=120.0):
    from .app_install import disable_installed_service, unload_installed_service
    installed = disable_installed_service(store)
    info = store.state("daemon", {})
    pid = info.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and _is_daemon_process(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while _process_alive(pid):
            # Wait for the process we signalled. KeepAlive can already have recorded
            # a replacement PID; waiting for an empty record would never finish.
            if time.monotonic() >= deadline:
                raise RuntimeError("The current audio stage is still stopping safely. Cached work is preserved; run za restart after it finishes.")
            time.sleep(0.1)
    if installed:
        # Unload only after the original process finished its current atomic stage.
        unload_installed_service(store)


def publication_notice(action: str, settings: dict) -> str:
    if action not in {"full", "brief", "both"}:
        return ""
    if settings.get("auto_publish", True):
        return "Eligible requested episodes publish as each is ready. Private audio goes to your iCloud output folder."
    return "Automatic podcast publishing is off. Audio remains available on your Mac; private audio goes to your iCloud output folder."


def emit(value, machine=False):
    if machine:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def pick_article(store: Store, query: str | None, *, machine=False):
    if query:
        try:
            return store.article(query)
        except KeyError:
            pass
    articles = store.library(query or "", limit=500)["items"]
    exact = [a for a in articles if a["title"].casefold() == (query or "").casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(articles) == 1:
        return articles[0]
    if not articles:
        raise ValueError("No matching article. Run za sync to read saved Zotero PDFs.")
    if machine or not sys.stdin.isatty():
        raise ValueError("Selection is ambiguous. Use za list --json and supply an exact title or machine ID.")
    for i, article in enumerate(articles, 1):
        print(f"{i:>3}. {article['title']}")
        print(f"     {', '.join(article.get('authors', []))} · {article.get('year') or 'Year unknown'}")
    choice = input("Article number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(articles):
        raise ValueError("Choose one of the displayed article numbers")
    return articles[int(choice) - 1]


def parser():
    root = argparse.ArgumentParser(prog="za", description="1 More Paper — saved Zotero research, readable Markdown, optional audio.")
    root.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = root.add_subparsers(dest="command")
    for name in ("list", "search", "open", "review"):
        command = sub.add_parser(name)
        command.add_argument("query", nargs="?")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "open":
            command.add_argument("--audio", action="store_true")
            command.add_argument("--reveal", action="store_true")
            command.add_argument("--edition", choices=("full", "brief"), default="full")
    for name in ("markdown", "full", "brief", "both", "sync"):
        command = sub.add_parser(name)
        if name != "sync":
            command.add_argument("selection", nargs="?", help="new, all, or an article title; omit to choose from a list")
        command.add_argument("--qa", action=argparse.BooleanOptionalAction, default=None)
        command.add_argument("--force", action="store_true", help="Explicitly replace research Markdown from the PDF")
        command.add_argument("--wait", action="store_true", help="Watch until generation completes; delivery continues independently")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--idempotency-key")
    for name in ("status", "doctor", "dashboard", "settings", "stop", "restart", "install", "mcp"):
        command = sub.add_parser(name)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "settings":
            command.add_argument("key", nargs="?")
            command.add_argument("value", nargs="?")
    for name in ("retry", "cancel"):
        command = sub.add_parser(name)
        command.add_argument("job", nargs="?", help="Job ID; omitted uses the most recent eligible job")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    server = sub.add_parser("serve")
    server.add_argument("--port", type=int, default=8765)
    return root


def wait_job(store, job_id, *, machine=False):
    previous = None
    while True:
        job = store.job(job_id)
        fingerprint = (job["title"], job["stage"], job["message"])
        if fingerprint != previous and not machine:
            print(f"{job['title']}\n  {job['stage'].replace('_', ' ')} · {job['message']}", flush=True)
            previous = fingerprint
        if job["status"] in TERMINAL:
            if machine:
                emit({"job": {k: v for k, v in job.items() if k != "remote"}}, True)
            return 1 if job["status"] == "failed" else 0
        time.sleep(0.5)


def menu():
    actions = [("Create Markdown for new articles", ["markdown", "new"]),
               ("Create a Full episode for one article", ["full"]), ("Create a Brief for one article", ["brief"]),
               ("Create Markdown for the whole library", ["markdown", "all"]), ("Search what I have read", ["search"]),
               ("See progress", ["status"]), ("Open dashboard and settings", ["dashboard"]), ("Refresh from Zotero", ["sync"])]
    print("1 More Paper\n")
    for i, (label, _) in enumerate(actions, 1):
        print(f"{i}. {label}")
    choice = input("Command number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
        return 0
    return main(actions[int(choice) - 1][1])


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store()
    command = args.command
    try:
        if not command:
            if sys.stdin.isatty() and not args.json:
                return menu()
            parser().print_help()
            return 0
        if command == "serve":
            from .app_server import serve
            serve(store, args.port)
            return 0
        if command == "mcp":
            from .app_mcp import main as mcp_main
            return mcp_main()
        if command == "install":
            from .app_install import install
            emit(install(store), args.json)
            return 0
        if command in {"stop", "restart"}:
            stop_daemon(store)
            if command == "restart":
                ensure_daemon(store)
            message = "Worker restarted; cached work is preserved." if command == "restart" else "Worker stopped. Use za restart to start it again."
            emit({"status": "restarted" if command == "restart" else "stopped", "cached_work_preserved": True} if args.json else message, args.json)
            return 0
        if command == "settings":
            if args.key:
                if args.value is None:
                    raise ValueError("Supply a setting value")
                try:
                    value = json.loads(args.value)
                except ValueError:
                    value = args.value
                result = store.update_settings({args.key: value})
            else:
                result = store.settings()
            emit(result, args.json)
            return 0
        if command == "doctor":
            import importlib.util
            import platform
            import shutil
            checks = {"architecture": platform.machine(), "python": platform.python_version(), "ffmpeg": bool(shutil.which("ffmpeg")),
                      "kokoro_installed": importlib.util.find_spec("mlx_audio") is not None,
                      "mcp_installed": importlib.util.find_spec("mcp") is not None,
                      "runtime": str(store.runtime), "daemon_online": daemon_status(store) is not None,
                      "cloud_configured": (store.root / "cloud.json").is_file(), "icloud_configured": bool(store.settings()["icloud_folder"])}
            emit(checks, args.json)
            return 0 if checks["ffmpeg"] else 1
        if command == "list":
            articles = store.library(args.query or "", limit=500)
            if args.json:
                emit({"items": [visible_article(a) for a in articles["items"]], "total": articles["total"]}, True)
            else:
                for article in articles["items"]:
                    print(f"{article['title']}\n  {', '.join(article.get('authors', []))} · {article.get('year') or 'Year unknown'} · Markdown: {article['markdown_status']} · Audio: {article['audio_status']}")
                print(f"\n{articles['total']} articles")
            return 0
        if command == "search":
            query = args.query or (input("Search: ") if sys.stdin.isatty() else "")
            results = store.search(query)
            if args.json:
                emit(results, True)
            else:
                for article in results["results"]:
                    print(f"{article['title']}\n  {article['snippet']}\n")
                if not results["results"]:
                    print("No matching articles")
            return 0
        if command in {"open", "review"}:
            article = pick_article(store, args.query, machine=args.json)
            kind = "review" if command == "review" else ("audio" if args.audio else "markdown")
            path = artifact_path(article, kind, getattr(args, "edition", "full"))
            if args.json:
                emit({"title": article["title"], "path": str(path), "artifact": kind}, True)
            elif command == "review":
                print(path.read_text(encoding="utf-8"))
            else:
                subprocess.run(["/usr/bin/open", *(["-R"] if args.reveal else []), str(path)], check=True)
                print(f"Opened {kind}: {article['title']}")
            return 0
        if command == "status":
            from .app_server import public_job, status
            summary = daemon_status(store) or status(store)
            jobs = [public_job(j) for j in store.jobs(10)["jobs"]]
            if args.json:
                emit({**summary, "jobs": jobs}, True)
            else:
                print(f"Worker: {'online' if summary['worker']['online'] else 'offline'} · {summary['counts']['articles']} articles")
                for job in jobs:
                    print(f"\n{job['title']}\n  {job['status']} · {job['stage']} · {job['message']}")
                print(f"\nDeliveries pending: {summary['deliveries']}")
            return 0
        ensure_daemon(store)
        if command == "dashboard":
            url = f"http://127.0.0.1:{store.state('daemon', {}).get('port',8765)}/"
            if not args.json:
                subprocess.run(["/usr/bin/open", url], check=True)
            emit({"url": url} if args.json else url, args.json)
            return 0
        if command in {"retry", "cancel"}:
            candidates = [j for j in store.jobs()["jobs"] if (j["status"] in TERMINAL if command == "retry" else j["status"] not in TERMINAL)]
            job = store.job(args.job) if args.job else (candidates[0] if candidates else None)
            if not job:
                raise ValueError("No eligible job")
            if command == "cancel":
                result = store.cancel(job["id"])
            else:
                if job["status"] not in TERMINAL:
                    raise ValueError("The job is still active")
                result = store.create_job({k: job[k] for k in ("action", "scope", "article_id", "qa", "force")})
            emit({"job": {k: v for k, v in result.items() if k != "remote"}} if args.json else f"{result['title']}\n  {result['status']}", args.json)
            return 0
        if command in {"markdown", "full", "brief", "both", "sync"}:
            selection = getattr(args, "selection", None)
            scope = selection if selection in {"new", "all"} else ("all" if command == "sync" else "one")
            article = pick_article(store, selection, machine=args.json) if scope == "one" else None
            request = {"action": command, "scope": scope, "article_id": article["id"] if article else None,
                       "force": args.force, "idempotency_key": args.idempotency_key}
            if args.qa is not None:
                request["qa"] = args.qa
            job = store.create_job(request)
            policy = publication_notice(command, store.settings())
            if args.wait:
                if policy and not args.json:
                    print(policy, flush=True)
                return wait_job(store, job["id"], machine=args.json)
            message = f"Queued: {job['title']}\n" + (policy + "\n" if policy else "") + "Use za status or za dashboard to follow progress."
            emit({"job": {k: v for k, v in job.items() if k != "remote"}, **({"publication_policy": policy} if policy else {})} if args.json else message, args.json)
            return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        if args.json:
            emit({"error": {"type": type(exc).__name__, "message": str(exc)}}, True)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
