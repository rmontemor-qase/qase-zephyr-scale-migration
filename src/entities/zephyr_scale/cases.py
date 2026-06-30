import asyncio
import re

from qaseio.models import TestCasebulkCasesInner

from ...service.qase import QaseService
from ...service.zephyr_scale import ZephyrScaleService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

# Zephyr Scale priority names → Qase priority slugs
_PRIORITY_MAP = {
    "highest": "critical",
    "high": "high",
    "normal": "normal",
    "medium": "normal",
    "low": "low",
    "lowest": "low",
}

# Zephyr Scale status names → Qase status slugs
_STATUS_MAP = {
    "approved": "actual",
    "draft": "draft",
    "deprecated": "deprecated",
}


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode common entities. <img> tags are replaced with [Image: filename] notes."""
    if not text:
        return ""
    # Replace <img ...> with a readable note so the image filename is preserved in text
    def _img_note(m):
        src = re.search(r'src=["\']([^"\']+)["\']', m.group(0))
        if src:
            url = src.group(1)
            # Extract filename from URL (handles both plain filenames and ZS CDN paths)
            fname = url.rstrip("/").split("/")[-1]
            # ZS CDN filenames look like: {uuid}-{timestamp}-{filename}
            cdn_match = re.match(r'^[a-f0-9-]+-\d+-(.+)$', fname)
            if cdn_match:
                fname = cdn_match.group(1).replace("+", " ")
            return f" [Image: {fname}] "
        return ""
    text = re.sub(r"<img[^>]*>", _img_note, text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return text.strip()


def _extract_img_urls(html: str) -> list:
    """Return all src URLs from <img> tags in HTML."""
    if not html:
        return []
    return re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)


class Cases:
    """Import Zephyr Scale test cases → Qase test cases.

    For each project, fetches all test cases (paginated), resolves their suite
    (folder) mapping, fetches step details, then bulk-creates in Qase.
    """

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrScaleService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.pools = pools

    def import_cases(self, project: dict):
        asyncio.run(self._import_cases_async(project))

    async def _import_cases_async(self, project: dict):
        code = project["code"]
        zephyr_key = project["zephyr_key"]

        # Jira credentials for downloading file attachments
        jira_email = str(self.config.get("jira.email") or "").strip()
        jira_api_token = str(self.config.get("jira.api_token") or "").strip()
        # CDN inline images cannot be downloaded without a browser-session Forge JWT.
        # cdn_cookie stays None; downloads fall back to ZS Bearer token (works for
        # file attachments) and skip CDN-only inline images gracefully.
        cdn_cookie = None

        # Load Qase system field option ids (populated by Fields step)
        priority_map = self.mappings.qase_priority_keys_to_id
        status_map = self.mappings.qase_case_status_keys_to_id

        # Pre-fetch Zephyr Scale priority/status name maps.
        # The API returns priority/status as {id, self} objects — name is not inline.
        zephyr_priority_id_to_name: dict = {}
        for p in self.zephyr.get_priorities(zephyr_key):
            pid = p.get("id")
            pname = (p.get("name") or "").lower().strip()
            if pid is not None and pname:
                zephyr_priority_id_to_name[int(pid)] = pname

        zephyr_status_id_to_name: dict = {}
        for s in self.zephyr.get_statuses(zephyr_key, "TEST_CASE"):
            sid = s.get("id")
            sname = (s.get("name") or "").lower().strip()
            if sid is not None and sname:
                zephyr_status_id_to_name[int(sid)] = sname

        self.logger.log(
            f"[{code}][Cases] Loaded {len(zephyr_priority_id_to_name)} priority(s), "
            f"{len(zephyr_status_id_to_name)} TC status(es) from Zephyr Scale"
        )

        # Fetch all pages of test cases from Zephyr Scale
        self.logger.log(f"[{code}][Cases] Fetching test cases from Zephyr Scale")
        all_cases = []
        for page in self.zephyr.get_test_cases(zephyr_key):
            all_cases.extend(page)

        if not all_cases:
            self.logger.log(f"[{code}][Cases] No test cases found")
            return

        total = len(all_cases)
        self.logger.log(f"[{code}][Cases] Found {total} test case(s)")
        self.mappings.stats.add_entity_count(code, "cases", "zephyr-scale", total)

        # Fetch steps and attachments for all cases concurrently
        self.logger.log(f"[{code}][Cases] Fetching steps for {total} case(s)")
        steps_cache = {}
        attachments_cache = {}  # {tc_key: [qase_hash, ...]}
        semaphore = asyncio.Semaphore(16)

        async def fetch_steps(tc_key: str):
            async with semaphore:
                try:
                    steps = await self.pools.source(self.zephyr.get_test_steps, tc_key)
                    steps_cache[tc_key] = steps or []
                except Exception as e:
                    self.logger.log(
                        f"[{code}][Cases] Failed to fetch steps for {tc_key}: {e}", "warning"
                    )
                    steps_cache[tc_key] = []

        async def fetch_and_upload_attachments(tc: dict):
            tc_key = tc.get("key") or ""
            async with semaphore:
                try:
                    hashes = []

                    # 1. File attachments via GET /testcases/{key}/attachments
                    atts = await self.pools.source(
                        self.zephyr.get_test_case_attachments, tc_key
                    )
                    for att in (atts or []):
                        att_url = att.get("url") or ""
                        att_name = att.get("filename") or "attachment"
                        if not att_url:
                            continue
                        raw = await self.pools.source(
                            self.zephyr.download_attachment_bytes,
                            att_url, jira_email, jira_api_token, cdn_cookie,
                        )
                        if raw is None:
                            self.logger.log(
                                f"[{code}][Cases] Could not download attachment "
                                f"{att_name!r} for {tc_key}", "warning"
                            )
                            continue
                        result = await self.pools.qs(
                            self.qase.upload_attachment, code, (att_name, raw)
                        )
                        if result and result.get("hash"):
                            hashes.append(result["hash"])

                    # 2. Inline images embedded in objective / precondition / description HTML
                    html_fields = [
                        tc.get("objective") or "",
                        tc.get("precondition") or "",
                        tc.get("description") or "",
                    ]
                    for html in html_fields:
                        for img_url in _extract_img_urls(html):
                            # Derive filename from URL
                            raw_name = (img_url.rstrip("/").split("/")[-1]
                                        .split("?")[0].replace("+", " "))
                            cdn_m = re.match(r'^[a-f0-9-]+-\d+-(.+)$', raw_name)
                            img_name = cdn_m.group(1) if cdn_m else raw_name or "inline_image"
                            raw = await self.pools.source(
                                self.zephyr.download_attachment_bytes,
                                img_url, jira_email, jira_api_token, cdn_cookie,
                            )
                            if raw is None:
                                self.logger.log(
                                    f"[{code}][Cases] Could not download inline image "
                                    f"{img_name!r} for {tc_key} (CDN auth failed)",
                                    "warning",
                                )
                                continue
                            result = await self.pools.qs(
                                self.qase.upload_attachment, code, (img_name, raw)
                            )
                            if result and result.get("hash"):
                                hashes.append(result["hash"])

                    if hashes:
                        attachments_cache[tc_key] = hashes
                        self.logger.log(
                            f"[{code}][Cases] Uploaded {len(hashes)} attachment(s) for {tc_key}"
                        )
                except Exception as e:
                    self.logger.log(
                        f"[{code}][Cases] Attachment processing failed for {tc_key}: {e}", "warning"
                    )

        tc_keys_with_key = [tc.get("key") or "" for tc in all_cases if tc.get("key")]
        await asyncio.gather(*[fetch_steps(k) for k in tc_keys_with_key])
        await asyncio.gather(*[fetch_and_upload_attachments(tc) for tc in all_cases if tc.get("key")])

        n_with_att = sum(1 for k in tc_keys_with_key if k in attachments_cache)
        if n_with_att:
            self.logger.log(f"[{code}][Cases] {n_with_att} case(s) have attachment(s)")

        # Build Qase case objects
        suite_map = self.mappings.suites.get(code) or {}
        qase_cases = []
        for tc in all_cases:
            case_obj = self._build_qase_case(
                tc, suite_map, steps_cache, attachments_cache,
                priority_map, status_map,
                zephyr_priority_id_to_name, zephyr_status_id_to_name,
            )
            if case_obj is not None:
                # Keep both the string key (e.g. "ZSM-T2") and the numeric id so that
                # run executions (which only expose testCase.id) can still be resolved.
                qase_cases.append((tc.get("key") or "", tc.get("id"), case_obj))

        if not qase_cases:
            self.logger.log(f"[{code}][Cases] Nothing to create after mapping")
            return

        # Bulk create in Qase (100 per chunk with per-case fallback)
        tc_keys = [k for k, _, _ in qase_cases]
        tc_ids = [tid for _, tid, _ in qase_cases]
        case_objs = [c for _, _, c in qase_cases]

        self.logger.log(f"[{code}][Cases] Creating {len(case_objs)} case(s) in Qase")
        _, qase_ids = self.qase.create_cases(code, case_objs)

        created = 0
        for i, qase_id in enumerate(qase_ids):
            if qase_id is not None:
                if tc_keys[i]:
                    self.mappings.register_zephyr_testcase_qase_case_id(code, tc_keys[i], qase_id)
                if tc_ids[i] is not None:
                    self.mappings.register_zephyr_testcase_qase_case_id(code, str(tc_ids[i]), qase_id)
                created += 1

        self.mappings.stats.add_entity_count(code, "cases", "qase", created)
        self.logger.log(
            f"[{code}][Cases] Created {created}/{len(case_objs)} case(s) in Qase"
        )

    def _build_qase_case(
        self,
        tc: dict,
        suite_map: dict,
        steps_cache: dict,
        attachments_cache: dict,
        priority_map: dict,
        status_map: dict,
        zephyr_priority_id_to_name: dict,
        zephyr_status_id_to_name: dict,
    ):
        """Map a Zephyr Scale test case to a Qase TestCasebulkCasesInner object."""
        tc_key = tc.get("key") or ""
        title = (tc.get("name") or "").strip()
        if not title:
            title = tc_key or "Untitled"

        # Suite / folder mapping
        folder = tc.get("folder") or {}
        folder_id = folder.get("id") if isinstance(folder, dict) else None
        suite_id = suite_map.get(folder_id) if folder_id else None

        # Description / preconditions
        description = _strip_html(tc.get("objective") or tc.get("description") or "")
        preconditions = _strip_html(tc.get("precondition") or "")

        # Priority — API returns {id, self}; resolve name via pre-fetched map
        priority_obj = tc.get("priority") or {}
        if isinstance(priority_obj, dict):
            priority_name = (priority_obj.get("name") or "").lower().strip()
            if not priority_name:
                pid = priority_obj.get("id")
                priority_name = zephyr_priority_id_to_name.get(int(pid), "") if pid is not None else ""
        else:
            priority_name = str(priority_obj).lower().strip()
        qase_priority_slug = _PRIORITY_MAP.get(priority_name, "normal")
        priority_id = priority_map.get(qase_priority_slug) or priority_map.get("normal")

        # Status — same id-only pattern
        status_obj = tc.get("status") or {}
        if isinstance(status_obj, dict):
            status_name = (status_obj.get("name") or "").lower().strip()
            if not status_name:
                sid = status_obj.get("id")
                status_name = zephyr_status_id_to_name.get(int(sid), "") if sid is not None else ""
        else:
            status_name = str(status_obj).lower().strip()
        qase_status_slug = _STATUS_MAP.get(status_name, "actual")
        status_id = status_map.get(qase_status_slug) or status_map.get("actual")

        # Steps
        steps = self._build_steps(steps_cache.get(tc_key) or [])

        data = {
            "title": title[:255],
            "description": description or None,
            "preconditions": preconditions or None,
            "steps": steps or None,
        }

        if suite_id is not None:
            data["suite_id"] = suite_id
        if priority_id is not None:
            data["priority"] = priority_id
        if status_id is not None:
            data["status"] = status_id

        # Attachments uploaded during pre-fetch
        att_hashes = attachments_cache.get(tc_key) or []
        if att_hashes:
            data["attachments"] = att_hashes

        # Tags from labels
        labels = tc.get("labels") or []
        if labels:
            data["tags"] = [str(lbl) for lbl in labels if lbl][:10]

        # Custom fields
        custom_fields = tc.get("customFields") or {}
        cf_map = self.mappings.custom_fields or {}
        if custom_fields and cf_map:
            qase_cf = {}
            for cf_name, value in custom_fields.items():
                qase_cf_id = cf_map.get(cf_name)
                if qase_cf_id is None or value is None:
                    continue
                if isinstance(value, bool):
                    serialised = "true" if value else "false"
                elif isinstance(value, list):
                    if not value:
                        continue
                    serialised = ",".join(str(v) for v in value)
                elif isinstance(value, str):
                    # Strip ISO time suffix from date values ("2026-06-30T00:00:00Z" → "2026-06-30")
                    serialised = value.split("T")[0] if "T" in value and value.endswith("Z") else value
                    if not serialised:
                        continue
                else:
                    serialised = str(value)
                qase_cf[str(qase_cf_id)] = serialised
            if qase_cf:
                data["custom_field"] = qase_cf

        return TestCasebulkCasesInner(**data)

    def _build_steps(self, raw_steps: list) -> list:
        """Convert Zephyr Scale step objects to Qase step dicts."""
        steps = []
        for step in raw_steps:
            action = _strip_html(step.get("inline", {}).get("description") or step.get("description") or "")
            expected = _strip_html(step.get("inline", {}).get("expectedResult") or step.get("expectedResult") or "")
            test_data = _strip_html(step.get("inline", {}).get("testData") or step.get("testData") or "")

            if not action:
                action = "Step"

            step_data = {
                "action": action[:1000],
                "expected_result": expected[:1000] if expected else None,
                "data": test_data[:1000] if test_data else None,
            }
            steps.append(step_data)
        return steps
