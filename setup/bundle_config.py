"""One bundle target's settings, read straight from databricks.yml (no Databricks CLI needed).

  load_target("dev") -> Config    variable defaults + the target's values + the app names

Used by the setup notebooks and the eval notebook, so every value lives in databricks.yml only.
Values still set to a "<...>" placeholder read as empty.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Resolved bundle variables + app names for one target."""

    def __init__(self, target: str):
        bundle = yaml.safe_load((REPO_ROOT / "databricks.yml").read_text())
        if target not in bundle.get("targets", {}):
            raise ValueError(f"No target '{target}' in databricks.yml (targets: {', '.join(bundle['targets'])})")
        v = {name: str(spec.get("default", "")) for name, spec in bundle["variables"].items()}
        v.update({name: str(val) for name, val in (bundle["targets"][target].get("variables") or {}).items()})
        for name, val in v.items():  # resolve ${var.x} references (e.g. lakebase_branch_path)
            v[name] = re.sub(r"\$\{var\.(\w+)\}", lambda m: v[m.group(1)], val)
        unset = lambda s: "" if (not s or s.startswith("<")) else s  # noqa: E731

        self.target = target
        self.catalog = unset(v["catalog"])
        self.schema = v["schema"]
        self.warehouse_id = unset(v["warehouse_id"])
        self.llm_endpoint = unset(v["llm_endpoint"])
        self.gateway_model = v["gateway_model"]
        self.genie_space_id = unset(v["genie_space_id"])
        self.experiment_id = unset(v["experiment_id"])
        self.lakebase_project = v["lakebase_project"]
        self.lakebase_branch = v["lakebase_branch_path"]
        self.lakebase_endpoint = f"{self.lakebase_branch}/endpoints/primary"
        self.apps = {key: app["name"] for key, app in bundle["resources"]["apps"].items()}
        if not (self.catalog and self.warehouse_id):
            raise ValueError(f"Set `catalog` and `warehouse_id` in target '{target}' of databricks.yml first.")

    @property
    def fqs(self) -> str:
        return f"{self.catalog}.{self.schema}"


def load_target(target: str = "dev") -> Config:
    return Config(target)
