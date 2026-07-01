import asyncio
import time

from ...service.qase import QaseService
from ...service.zephyr_scale import ZephyrScaleService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

# Zephyr Scale execution status names → Qase result status slugs
_EXEC_STATUS_MAP = {
    "pass": "passed",
    "passed": "passed",
    "fail": "failed",
    "failed": "failed",
    "blocked": "blocked",
    "in progress": "in_progress",
    "not executed": "untested",
    "unexecuted": "untested",
    "wip": "in_progress",
    "skip": "skipped",
    "skipped": "skipped",
}

# Statuses we skip (leave the case as untested in the Qase run)
_SKIP_STATUSES = {"in_progress", "untested"}

# Identity map so send_bulk_results can resolve our Qase slugs (it expects a mapping dict)
_RESULT_STATUSES = {
    "passed": "passed",
    "failed": "failed",
    "blocked": "blocked",
    "skipped": "skipped",
    "in_progress": "in_progress",
    "untested": "untested",
    "invalid": "invalid",
}


def _now_ts() -> int:
    return int(time.time())


class Runs:
    """Import Zephyr Scale test cycles + executions → Qase test runs + results."""

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrScaleService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        project: dict,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.project = project
        self.pools = pools

    def import_runs(self) -> Mappings:
        return asyncio.run(self._import_runs_async())

    async def _import_runs_async(self) -> Mappings:
        code = self.project["code"]
        zephyr_key = self.project["zephyr_key"]
        created_after = int(self.config.get("runs.created_after") or 0)

        jira_email = str(self.config.get("jira.email") or "").strip()
        jira_api_token = str(self.config.get("jira.api_token") or "").strip()

        cdn_cookie = None  # CDN inline images not downloadable without browser auth

        # Pre-fetch execution statuses so we can resolve numeric ids to names.
        # The execution objects only carry testExecutionStatus.id, not the name.
        exec_status_id_to_name: dict = {}
        try:
            for s in self.zephyr.get_statuses(zephyr_key, "TEST_EXECUTION"):
                sid = s.get("id")
                sname = (s.get("name") or "").lower().strip()
                if sid is not None and sname:
                    exec_status_id_to_name[int(sid)] = sname
            self.logger.log(
                f"[{code}][Runs] Loaded {len(exec_status_id_to_name)} execution status(es)"
            )
        except Exception as e:
            self.logger.log(f"[{code}][Runs] Failed to fetch execution statuses: {e}", "warning")

        self.logger.log(f"[{code}][Runs] Fetching test cycles from Zephyr Scale")

        all_cycles = []
        for page in self.zephyr.get_test_cycles(zephyr_key, created_after):
            all_cycles.extend(page)

        if not all_cycles:
            self.logger.log(f"[{code}][Runs] No test cycles found")
            return self.mappings

        total = len(all_cycles)
        self.logger.log(f"[{code}][Runs] Found {total} cycle(s)")
        self.mappings.stats.add_entity_count(code, "runs", "zephyr-scale", total)

        created = 0

        async def import_cycle(cycle: dict):
            nonlocal created
            qase_run_id = await self._import_one_cycle(
                code, zephyr_key, cycle, exec_status_id_to_name,
                jira_email, jira_api_token, cdn_cookie,
            )
            if qase_run_id is not None:
                created += 1

        await asyncio.gather(*[import_cycle(cycle) for cycle in all_cycles])

        self.mappings.stats.add_entity_count(code, "runs", "qase", created)
        self.logger.log(f"[{code}][Runs] Created {created}/{total} run(s) in Qase")
        return self.mappings

    async def _import_one_cycle(
        self,
        code: str,
        zephyr_key: str,
        cycle: dict,
        exec_status_id_to_name: dict,
        jira_email: str = "",
        jira_api_token: str = "",
        cdn_cookie: str = None,
    ):
        cycle_key = cycle.get("key") or cycle.get("id") or ""
        name = (cycle.get("name") or cycle_key or "Unnamed Cycle").strip()

        self.logger.log(f"[{code}][Runs] Importing cycle: {name} [{cycle_key}]")

        # Fetch all executions for this cycle
        all_executions = []
        for page in self.zephyr.get_test_executions(zephyr_key, cycle_key):
            all_executions.extend(page)

        if not all_executions:
            self.logger.log(f"[{code}][Runs] Cycle {name!r} has no executions; skipping")
            return None

        # Fetch execution-level attachments in parallel, upload to Qase
        exec_attachments: dict = {}  # {ex_id: [qase_hash, ...]}
        if all_executions:
            semaphore = asyncio.Semaphore(8)

            async def fetch_exec_attachments(ex: dict):
                ex_id = ex.get("id")
                if not ex_id:
                    return
                async with semaphore:
                    try:
                        atts = await self.pools.source(
                            self.zephyr.get_test_execution_attachments, ex_id
                        )
                        if not atts:
                            return
                        hashes = []
                        for att in atts:
                            att_url = att.get("url") or ""
                            att_name = att.get("filename") or "attachment"
                            if not att_url:
                                continue
                            raw = await self.pools.source(
                                self.zephyr.download_attachment_bytes,
                                att_url, jira_email, jira_api_token, cdn_cookie,
                            )
                            if raw is None:
                                continue
                            result = await self.pools.qs(
                                self.qase.upload_attachment, code, (att_name, raw)
                            )
                            if result and result.get("hash"):
                                hashes.append(result["hash"])
                        if hashes:
                            exec_attachments[str(ex_id)] = hashes
                    except Exception as e:
                        self.logger.log(
                            f"[{code}][Runs] Attachment fetch failed for exec {ex_id}: {e}",
                            "warning",
                        )

            await asyncio.gather(*[fetch_exec_attachments(ex) for ex in all_executions])
            if exec_attachments:
                self.logger.log(
                    f"[{code}][Runs] Uploaded attachments for "
                    f"{len(exec_attachments)} execution(s) in cycle {name!r}"
                )

        # Resolve case ids: Zephyr Scale testcase → Qase case id.
        # Execution objects only expose testCase.id (numeric), not testCase.key.
        tc_map = self.mappings.zephyr_tc_id_to_qase_case_id.get(code) or {}
        case_ids = []
        for ex in all_executions:
            tc = ex.get("testCase") or {}
            tc_lookup = tc.get("key") or str(tc.get("id") or "")
            qase_case_id = tc_map.get(tc_lookup)
            if qase_case_id is not None and qase_case_id not in case_ids:
                case_ids.append(qase_case_id)

        if not case_ids:
            self.logger.log(
                f"[{code}][Runs] Cycle {name!r}: no mapped cases found; skipping", "warning"
            )
            return None

        # Build the run dict for QaseService
        created_on = self._parse_ts(cycle.get("createdOn") or cycle.get("plannedStartDate"))
        completed_on = self._parse_ts(cycle.get("completedOn") or cycle.get("plannedEndDate"))
        is_done = str(cycle.get("status", "")).lower() in ("done", "completed")

        run_dict = {
            "name": name,
            "description": (cycle.get("description") or "").strip() or None,
            "created_on": created_on or _now_ts(),
            "completed_on": completed_on or (created_on or _now_ts()),
            "is_completed": is_done,
            "author_id": self.mappings.default_user,
            "plan_name": None,
        }

        try:
            qase_run_id = await self.pools.qs(
                self.qase.create_run, run_dict, code, case_ids, None
            )
        except Exception as e:
            self.logger.log(f"[{code}][Runs] Failed to create run {name!r}: {e}", "error")
            return None

        if qase_run_id is None:
            self.logger.log(f"[{code}][Runs] create_run returned None for {name!r}", "error")
            return None

        self.logger.log(f"[{code}][Runs] Created run id={qase_run_id} for cycle {name!r}")

        # Send bulk results
        results = self._build_results(all_executions, tc_map, exec_status_id_to_name, exec_attachments)
        if results:
            try:
                await self.pools.qs(
                    self.qase.send_bulk_results,
                    run_dict,
                    results,
                    qase_run_id,
                    code,
                    self.mappings,
                    {r["test_id"]: r["_qase_case_id"] for r in results},
                    _RESULT_STATUSES,
                )
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Failed to send results for run {qase_run_id}: {e}", "error"
                )

        # Complete the run if it was done in Zephyr
        if is_done:
            try:
                await self.pools.qs(self.qase.complete_run, code, qase_run_id)
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Failed to complete run {qase_run_id}: {e}", "warning"
                )

        return qase_run_id

    def _build_results(
        self,
        executions: list,
        tc_map: dict,
        exec_status_id_to_name: dict,
        exec_attachments: dict = None,
    ) -> list:
        """Map Zephyr Scale executions to Qase result dicts."""
        exec_attachments = exec_attachments or {}
        results = []
        for ex in executions:
            tc = ex.get("testCase") or {}
            tc_lookup = tc.get("key") or str(tc.get("id") or "")
            qase_case_id = tc_map.get(tc_lookup)
            if qase_case_id is None:
                continue

            status_obj = ex.get("testExecutionStatus") or {}
            # Execution status objects may only carry an id (no name) — resolve via pre-fetched map.
            if isinstance(status_obj, dict):
                status_name = (status_obj.get("name") or "").lower().strip()
                if not status_name:
                    sid = status_obj.get("id")
                    status_name = exec_status_id_to_name.get(sid, "") if sid is not None else ""
            else:
                status_name = str(status_obj).lower().strip()
            qase_status = _EXEC_STATUS_MAP.get(status_name, "untested")

            if qase_status in _SKIP_STATUSES:
                continue

            comment = (ex.get("comment") or "").strip()
            executed_on = self._parse_ts(ex.get("executedOn") or ex.get("createdOn"))

            # executionTime / estimatedTime are in milliseconds; Qase wants seconds
            raw_duration = ex.get("executionTime") or ex.get("estimatedTime")
            elapsed = max(0, int(raw_duration / 1000)) if raw_duration else 0

            result = {
                "test_id": tc_lookup,
                "_qase_case_id": qase_case_id,
                "status_id": qase_status,
                "comment": comment,
                "created_on": executed_on or _now_ts(),
                "elapsed": elapsed,
                "created_by": self.mappings.default_user,
            }

            ex_id = ex.get("id")
            hashes = exec_attachments.get(str(ex_id)) if ex_id else None
            if hashes:
                result["attachments"] = hashes

            results.append(result)

        return results

    @staticmethod
    def _parse_ts(value) -> int:
        """Parse a Zephyr Scale timestamp (epoch ms or ISO string) to epoch seconds."""
        if value is None:
            return 0
        if isinstance(value, (int, float)) and value > 0:
            # Zephyr Scale returns epoch milliseconds
            if value > 1e10:
                return int(value / 1000)
            return int(value)
        if isinstance(value, str):
            from datetime import datetime, timezone
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(value[:26], fmt[:len(fmt)])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue
        return 0
