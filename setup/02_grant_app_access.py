# Databricks notebook source
# MAGIC %md
# MAGIC # 2 · Grant the apps access
# MAGIC
# MAGIC Run this once the bundle is deployed and both apps have started. It grants what the apps' bundle
# MAGIC `resources` can't express yet:
# MAGIC
# MAGIC - `CAN_USE` on the Vector Search endpoint (agent)
# MAGIC - `SELECT` + `MODIFY` on the MLflow trace tables (agent)
# MAGIC - Lakebase table privileges: tickets and alerts (agent), reading and resolving alerts (UI)
# MAGIC
# MAGIC Attach **serverless** compute and click **Run all**.

# COMMAND ----------

# MAGIC %pip install -q -r requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("target", "dev", "Bundle target")

# COMMAND ----------

from databricks.sdk import WorkspaceClient

import voicebot_setup as steps
from bundle_config import load_target

steps.post_deploy(WorkspaceClient(), load_target(dbutils.widgets.get("target")))
