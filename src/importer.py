from concurrent.futures import ThreadPoolExecutor

from .entities.zephyr_scale import (
    Projects,
    Fields,
    Attachments,
    Suites,
    Cases,
    Runs,
    Milestones,
)
from .service import QaseService, QaseScimService, ZephyrScaleService
from .support import ConfigManager, Logger, Mappings, ThrottledThreadPoolExecutor, Pools

_ZEPHYR_SOURCE_POOL_WORKERS = 8


class Importer:
    def __init__(self, config: ConfigManager, logger: Logger) -> None:
        self.pools = Pools(
            qase_pool=ThrottledThreadPoolExecutor(max_workers=8, requests=250, interval=12),
            source_pool=ThreadPoolExecutor(max_workers=_ZEPHYR_SOURCE_POOL_WORKERS),
        )

        self.logger = logger
        self.config = config
        self.qase_scim_service = None

        self.qase_service = QaseService(config, logger)
        if config.get("qase.scim_token"):
            self.qase_scim_service = QaseScimService(config, logger)

        self.source_service = ZephyrScaleService(config, logger)
        self.mappings = Mappings("zephyr-scale", self.config.get("users.default") or 1)

    def start(self):
        self.logger.log("Starting Zephyr Scale → Qase migration")

        # Step 1. Import projects and build project map
        self.mappings = Projects(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_projects()

        if not self.mappings.projects:
            self.logger.log("[Importer] No projects to migrate. Exiting.")
            return

        # Step 2. Attachments (no-op for Zephyr Scale)
        self.mappings = Attachments(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_all_attachments()

        # Step 3. Register Qase system fields (priority / status option ids)
        self.mappings = Fields(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_fields()

        # Step 4. Import per-project data (suites → cases → runs) in parallel
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self._import_project_data, project)
                for project in self.mappings.projects
            ]
            for future in futures:
                future.result()

        self.mappings.stats.print()
        self.mappings.stats.save(str(self.config.get("prefix") or ""))
        self.mappings.stats.save_xlsx(str(self.config.get("prefix") or ""))

    def _import_project_data(self, project: dict):
        self.logger.print_group(f'Importing project: {project["name"]} [{project["code"]}]')

        # Suites (folders)
        self.mappings = Suites(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_suites(project)

        # Milestones (test plans)
        self.mappings = Milestones(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_milestones(project)

        # Test cases
        Cases(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_cases(project)

        # Test runs (cycles + executions)
        self.mappings = Runs(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            project,
            self.pools,
        ).import_runs()
