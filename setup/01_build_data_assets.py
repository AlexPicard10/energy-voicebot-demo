# Databricks notebook source
# MAGIC %md
# MAGIC # 1 · Build the data assets
# MAGIC
# MAGIC Creates everything the two apps need, in the `catalog` and with the `warehouse_id` set in the `dev`
# MAGIC target of `databricks.yml`:
# MAGIC
# MAGIC - **Unity Catalog** schema `energy_voicebot`: generated customers, invoices and consumption, the
# MAGIC   knowledge base, and the UC function `get_consumption_history`
# MAGIC - **AI Gateway** model service `llm_endpoint` (unless it already exists), routing to the Foundation
# MAGIC   Model `gateway_model`
# MAGIC - **AI Search** (formerly Vector Search) endpoint and index over the knowledge base
# MAGIC - **Lakebase** project, tables `tickets` and `supervisor_alerts`, synced tables for the customer lookups
# MAGIC - **MLflow** experiment, with traces stored in Unity Catalog tables
# MAGIC - **Genie** space over customers and invoices
# MAGIC
# MAGIC Attach **serverless** compute and click **Run all**. The first run can take up to an hour: a new AI
# MAGIC Search endpoint takes a while before it can host the index. Meanwhile, don't click **Sync** on the
# MAGIC index or **Run** on its pipeline: that makes the index fail. Re-running is safe.

# COMMAND ----------

# MAGIC %pip install -q -r requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("target", "dev", "Bundle target")
dbutils.widgets.dropdown("reload_data", "yes", ["yes", "no"], "Re-create the tables")

# COMMAND ----------

from databricks.sdk import WorkspaceClient

import voicebot_setup as steps
from bundle_config import load_target

cfg = load_target(dbutils.widgets.get("target"))
w = WorkspaceClient()
print(f"Workspace {w.config.host} · target {cfg.target} · schema {cfg.fqs}")

# COMMAND ----------

steps.phase_uc(w, cfg, reload_data=dbutils.widgets.get("reload_data") == "yes")
steps.phase_gateway(w, cfg)
steps.phase_vs(w, cfg)
steps.phase_lakebase(w, cfg)
experiment_id = steps.phase_mlflow(w, cfg)
genie_space_id = steps.phase_genie(w, cfg)

# COMMAND ----------

print(f"""Done. Put these two values in target '{cfg.target}' of databricks.yml:

      genie_space_id: "{genie_space_id}"
      experiment_id: "{experiment_id}"

Next (README, step 5): deploy the bundle.""")
