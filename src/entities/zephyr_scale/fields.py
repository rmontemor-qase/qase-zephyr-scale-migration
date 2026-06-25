import asyncio

from ...service.qase import QaseService
from ...service.zephyr_scale import ZephyrScaleService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

# Zephyr Scale custom field type name → Qase custom field type id
_CF_TYPE_MAP = {
    "plain_text": 2,
    "single_line_text": 1,
    "paragraph": 2,
    "single_select": 3,
    "list": 3,
    "multi_select": 6,
    "multilist": 6,
    "checkbox": 4,
    "integer": 0,
    "float": 0,
    "decimal": 0,
    "number": 0,
    "date": 9,
    "datetime": 9,
    "url": 7,
    "user_list": 8,
}


class Fields:
    """Register Qase system field option ids and mirror Zephyr Scale custom fields to Qase."""

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

    def import_fields(self) -> Mappings:
        return asyncio.run(self._import_fields_async())

    async def _import_fields_async(self) -> Mappings:
        # 1 — Register Qase system fields (priority/status option ids)
        self.logger.log("[Fields] Registering Qase system fields (priority/status options)")
        system_fields = await self.pools.qs(self.qase.get_system_fields)
        if system_fields:
            fields_as_dicts = []
            for f in system_fields:
                if hasattr(f, "to_dict"):
                    fields_as_dicts.append(f.to_dict())
                elif isinstance(f, dict):
                    fields_as_dicts.append(f)
            self.mappings.register_qase_system_fields(fields_as_dicts)
            self.logger.log(
                f"[Fields] Registered {len(fields_as_dicts)} system field(s); "
                f"priority options: {list(self.mappings.qase_priority_keys_to_id.keys())[:8]}; "
                f"status options: {list(self.mappings.qase_case_status_keys_to_id.keys())[:8]}"
            )
        else:
            self.logger.log("[Fields] No system fields returned from Qase", "warning")

        # 2 — Mirror Zephyr Scale custom fields → Qase custom fields
        await self._sync_custom_fields()

        return self.mappings

    async def _sync_custom_fields(self):
        """Create (or reuse) a Qase custom field for each ZS custom field."""
        if not self.mappings.projects:
            return

        # Collect ZS custom field definitions across all projects (dedup by id)
        zs_fields_by_id: dict = {}
        for proj in self.mappings.projects:
            zephyr_key = proj.get("zephyr_key", "")
            try:
                for cf in self.zephyr.get_custom_fields(zephyr_key):
                    cf_id = cf.get("id")
                    if cf_id is not None:
                        zs_fields_by_id[cf_id] = cf
            except Exception as e:
                self.logger.log(
                    f"[Fields] Failed to fetch ZS custom fields for {zephyr_key}: {e}", "warning"
                )

        if not zs_fields_by_id:
            self.logger.log("[Fields] No Zephyr Scale custom fields found")
            return

        self.logger.log(f"[Fields] Found {len(zs_fields_by_id)} Zephyr Scale custom field(s)")

        # Load existing Qase custom fields to avoid duplicates (match by title)
        existing_qase_cfs: dict = {}  # title.lower() → id
        try:
            qase_cfs = await self.pools.qs(self.qase.get_case_custom_fields)
            for cf in (qase_cfs or []):
                t = (getattr(cf, "title", None) or "").strip().lower()
                cid = getattr(cf, "id", None)
                if t and cid is not None:
                    existing_qase_cfs[t] = int(cid)
        except Exception as e:
            self.logger.log(
                f"[Fields] Failed to fetch existing Qase custom fields: {e}", "warning"
            )

        for zs_id, zs_cf in zs_fields_by_id.items():
            name = (zs_cf.get("name") or "").strip()
            if not name:
                continue

            name_lower = name.lower()
            if name_lower in existing_qase_cfs:
                qase_cf_id = existing_qase_cfs[name_lower]
                self.logger.log(f"[Fields] Reusing existing Qase CF '{name}' (id={qase_cf_id})")
            else:
                zs_type_name = (zs_cf.get("type") or {}).get("name", "plain_text").lower()
                qase_type = _CF_TYPE_MAP.get(zs_type_name, 2)

                cf_data = {
                    "title": name,
                    "entity": 0,  # 0 = case
                    "type": qase_type,
                    "is_required": False,
                    "is_visible": True,
                    "is_filterable": True,
                }

                # For select/multiselect, carry over option names
                options = zs_cf.get("options") or []
                if options and qase_type in (3, 6):
                    cf_data["options"] = [
                        {"title": str(opt.get("name") or opt), "default": False}
                        for opt in options if opt
                    ]

                qase_cf_id = await self.pools.qs(self.qase.create_custom_field, cf_data)
                if qase_cf_id:
                    self.logger.log(
                        f"[Fields] Created Qase CF '{name}' (id={qase_cf_id}, type={qase_type})"
                    )
                    existing_qase_cfs[name_lower] = qase_cf_id
                else:
                    self.logger.log(f"[Fields] Failed to create Qase CF '{name}'", "warning")
                    continue

            self.mappings.custom_fields[str(zs_id)] = qase_cf_id

        self.logger.log(f"[Fields] Custom field mapping: {self.mappings.custom_fields}")
