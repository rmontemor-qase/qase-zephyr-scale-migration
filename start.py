import os
import sys

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.importer import Importer

# Config file: argv[1], or QASE_CONFIG_FILE / MIGRATION_CONFIG, else ./config.json
_config_path = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("QASE_CONFIG_FILE")
    or os.environ.get("MIGRATION_CONFIG")
    or "./config.json"
)

config = ConfigManager(config_file=_config_path)
try:
    config.load_config()
except Exception as e:
    config.build_config()

prefix = config.get('prefix')
if prefix is None:
    prefix = ''

logger = Logger(config.get('debug'), prefix=prefix)

importer = Importer(config, logger)
importer.start()
