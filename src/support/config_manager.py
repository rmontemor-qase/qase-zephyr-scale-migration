import json
import os


class ConfigManager:

    def __init__(self, config_file="./config.json", env_vars_prefix="QASE_"):
        self.config_file = config_file
        self.env_vars_prefix = env_vars_prefix
        self.config = {}

    def load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as file:
                    self.config = json.load(file)
        except Exception as e:
            print(f"⚠️  Failed to load config from file {self.config_file}: {e}")

    def get(self, key, default=None):
        """Dot-path lookup like ``qase.host``. Missing keys return ``default``."""
        keys = key.split(".")
        config = self.config
        for k in keys[:-1]:
            if not isinstance(config, dict) or k not in config:
                return default
            config = config[k]
        if not isinstance(config, dict):
            return default
        last = keys[-1]
        if last not in config:
            return default
        return config[last]

    def _set_config(self, key, value):
        keys = key.split(".")
        config = self.config
        for key in keys[:-1]:
            config = config.setdefault(key, {})
        config[keys[-1]] = value

    def build_config(self):
        print("Interactive setup: Zephyr Scale → Qase. Press Enter for defaults where offered.")

        config = {
            "qase": {},
            "zephyr_scale": {"api": {}},
            "projects": {"import": [], "status": "all"},
        }

        config["qase"]["api_token"] = input("Qase API token: ").strip()
        config["qase"]["host"] = input("Qase host (default: qase.io): ").strip() or "qase.io"
        ssl = input("Use HTTPS for Qase? (y/n, default: y): ").strip().lower()
        config["qase"]["ssl"] = ssl != "n"

        scim = input("Qase SCIM token (optional, Enter to skip): ").strip()
        if scim:
            config["qase"]["scim_token"] = scim

        config["zephyr_scale"]["api"]["token"] = input("Zephyr Scale API token: ").strip()
        config["zephyr_scale"]["api"]["host"] = (
            input(
                "Zephyr Scale API host (default: https://api.zephyrscale.smartbear.com/v2): "
            ).strip()
            or "https://api.zephyrscale.smartbear.com/v2"
        )

        projects_import = input("Project keys to import, comma-separated (default: all): ").strip()
        if projects_import:
            config["projects"]["import"] = [p.strip() for p in projects_import.split(",") if p.strip()]

        out_path = "config.json"
        with open(out_path, "w") as config_file:
            json.dump(config, config_file, indent=4)

        print(f"Configuration saved to {out_path}")
