"""Run the explicitly enabled GitHub authorized connector."""

from __future__ import annotations

import argparse

from .config import settings
from .connector_runs import ConnectorRunStore
from .connector_scheduler import ConnectorBusyError, ConnectorScheduler, ScheduledConnectorTask
from .event_store import EventStore, ImportConflict, StorageUnavailable
from .github_connector import GitHubAuthorizedConnector, GitHubConnectorConfig, GitHubConnectorError
from .monitoring_store import MonitoringStore
from .settings_service import effective_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True, help="owner/repository allowlist entry")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--task-id", default="TASK-GITHUB-AUTHORIZED")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    runtime = effective_settings(settings)
    unauthorized = [repo for repo in args.repo if repo not in runtime.github_repositories]
    if unauthorized and not args.dry_run:
        print("github authorized scan refused: repository is not in the settings-page allowlist")
        return 1
    runs = ConnectorRunStore(runtime.connector_runs_path)
    scheduler = ConnectorScheduler(runs)
    config = GitHubConnectorConfig(
        enabled=runtime.connector_network_enabled and not args.dry_run,
        token=runtime.github_token,
        repositories=tuple(args.repo),
        branch=args.branch,
        api_base=runtime.github_api_base,
        timeout_seconds=runtime.connector_timeout_seconds,
        max_files=runtime.connector_max_files,
        max_file_bytes=runtime.connector_max_file_bytes,
        min_interval_seconds=runtime.connector_min_interval_seconds,
        max_retries=runtime.connector_max_retries,
        hmac_key=runtime.credential_hmac_key,
        checkpoint_store=runs,
        defer_checkpoints=True,
    )
    try:
        def runner():
            connector = GitHubAuthorizedConnector(config)
            records = connector.collect()
            result = EventStore(runtime.database_path).import_real(records)
            connector.commit_checkpoints()
            return len(records), result["inserted"]

        task = ScheduledConnectorTask(
            task_id=args.task_id,
            connector_name="github-authorized-rest",
            target=",".join(args.repo),
            interval_seconds=300,
            enabled=not args.dry_run and runtime.connector_network_enabled,
        )
        status_callback = None
        try:
            monitoring_store = MonitoringStore(runtime.monitoring_database_path)
            task_ids = {item["task_id"] for item in monitoring_store.list_tasks()}
            if args.task_id in task_ids:
                status_callback = lambda status, last_run, result: monitoring_store.update_runtime(args.task_id, status, last_run, result)
        except (OSError, ValueError):
            status_callback = None
        result = scheduler.run_once(task, runner, status_callback=status_callback)
        if result["status"] == "dry_run":
            print("github connector dry-run: no network request made")
        else:
            print(f"github authorized scan: candidates={result['candidates']}, inserted={result['inserted']}")
        return 0
    except ConnectorBusyError:
        print("github authorized scan skipped: task is already running")
        return 2
    except ImportConflict:
        print("github authorized scan rejected: event conflict; batch unchanged")
        return 1
    except (GitHubConnectorError, StorageUnavailable, ValueError):
        print("github authorized scan failed; no token, response body or candidate contents were logged")
        return 1
    except Exception:
        print("github authorized scan failed; internal error details were not exposed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
