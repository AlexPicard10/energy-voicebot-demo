# Databricks notebook source
# MAGIC %md
# MAGIC # Teardown
# MAGIC
# MAGIC Deletes what `01_build_data_assets` created: the Unity Catalog schema (CASCADE), the Vector Search
# MAGIC endpoint, the Lakebase schema and synced tables, the Genie space, and the AI Gateway service if it
# MAGIC lives in the schema. The apps and the Lakebase project are kept: delete the apps from **Compute → Apps**.
# MAGIC
# MAGIC Type `DELETE` in the **confirm** widget, then click **Run all**.

# COMMAND ----------

# MAGIC %pip install -q -r requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("target", "dev", "Bundle target")
dbutils.widgets.text("confirm", "", "Type DELETE to confirm")

# COMMAND ----------

from databricks.sdk import WorkspaceClient

import voicebot_setup as steps
from bundle_config import load_target

if dbutils.widgets.get("confirm") != "DELETE":
    dbutils.notebook.exit("Not confirmed: type DELETE in the confirm widget.")
steps.teardown(WorkspaceClient(), load_target(dbutils.widgets.get("target")))
