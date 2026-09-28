# Databricks notebook source
# MAGIC %md
# MAGIC # Evaluate the agent (MLflow)
# MAGIC
# MAGIC Runs the agent inside this notebook (the code in `apps/agent`, against the resources of a bundle target)
# MAGIC and scores it with `mlflow.genai.evaluate()`. Results land in the same MLflow experiment as the app's
# MAGIC traces, grouped under the app's git-based model version, so revisions are comparable.
# MAGIC
# MAGIC - **curated**: the 16 questions in `setup/data/eval_questions.json`
# MAGIC - **traces**: the app's real traffic from the last `hours`
# MAGIC
# MAGIC Scorers: `relevance_to_query` and `safety` (LLM judges), plus `is_concise` (the 2-4 sentence voice
# MAGIC constraint). Run it after `setup/02_grant_app_access`, on **serverless** compute: **Run all**.

# COMMAND ----------

# MAGIC %pip install -q -r requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("target", "dev", "Bundle target")
dbutils.widgets.dropdown("dataset", "curated", ["curated", "traces"], "Dataset")
dbutils.widgets.text("hours", "24", "Hours of traces (dataset = traces)")
dbutils.widgets.text("judge_model", "databricks:/databricks-gpt-5-4-mini", "Judge model")

# COMMAND ----------

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient

REPO_ROOT = Path.cwd().parent  # a notebook runs in its own folder: <repo>/eval
sys.path.insert(0, str(REPO_ROOT / "setup"))
from bundle_config import load_target  # noqa: E402

cfg = load_target(dbutils.widgets.get("target"))
w = WorkspaceClient()

# The same environment the agent app gets from databricks.yml.
os.environ.update({
    "ENERGY_CATALOG": cfg.catalog,
    "ENERGY_SCHEMA": cfg.schema,
    "ENERGY_LLM_ENDPOINT": cfg.llm_endpoint,
    "ENERGY_GENIE_SPACE_ID": cfg.genie_space_id,
    "ENERGY_LAKEBASE_ENDPOINT": cfg.lakebase_endpoint,
    "PGHOST": w.postgres.get_endpoint(name=cfg.lakebase_endpoint).status.hosts.host,
    "MLFLOW_TRACKING_URI": "databricks",
    "MLFLOW_EXPERIMENT_ID": cfg.experiment_id,
    # Traces are stored in Unity Catalog tables; reading them back needs a warehouse.
    "MLFLOW_TRACING_SQL_WAREHOUSE_ID": cfg.warehouse_id,
    # One prediction at a time: evaluate() otherwise calls predict_fn from several threads, which
    # mixes the traces of concurrent turns.
    "MLFLOW_GENAI_EVAL_MAX_WORKERS": "1",
})

# COMMAND ----------

import mlflow  # noqa: E402
from mlflow.entities import Feedback  # noqa: E402
from mlflow.genai.scorers import RelevanceToQuery, Safety, scorer  # noqa: E402


def build_predict_fn():
    """predict_fn(query) -> final reply text. All turns run on ONE background event loop so the
    agent's MCP sessions and HTTP connection pools are reused across cases."""
    sys.path.insert(0, str(REPO_ROOT / "apps" / "agent"))
    import agent
    from mlflow.types.responses import ResponsesAgentRequest

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def reply(query: str) -> str:
        request = ResponsesAgentRequest(input=[{"role": "user", "content": query}])
        text = ""
        async for event in agent.streaming(request):
            if event.type == "response.output_item.done" and event.item.get("type") == "message":
                text = "".join(p.get("text", "") for p in event.item.get("content") or [])
        return text

    return lambda query: asyncio.run_coroutine_threadsafe(reply(query), loop).result()


@scorer
def is_concise(outputs) -> Feedback:
    """Voice constraint: 2-4 short sentences."""
    text = outputs if isinstance(outputs, str) else json.dumps(outputs, default=str)
    n = sum(text.count(p) for p in (".", "!", "?"))
    return Feedback(value=n <= 4, rationale=f"~{n} sentence-ending marks (target <= 4)")


judge = dbutils.widgets.get("judge_model")
scorers = [RelevanceToQuery(model=judge), Safety(model=judge), is_concise]
mlflow.set_experiment(experiment_id=cfg.experiment_id)

# COMMAND ----------

if dbutils.widgets.get("dataset") == "traces":
    hours = int(dbutils.widgets.get("hours"))
    since_ms = int((time.time() - hours * 3600) * 1000)
    data = mlflow.search_traces(filter_string=f"attributes.timestamp_ms > {since_ms}", max_results=200)
    print(f"Scoring {len(data)} app traces from the last {hours}h…")
    results = mlflow.genai.evaluate(data=data, scorers=scorers)
else:
    questions = json.loads((REPO_ROOT / "setup" / "data" / "eval_questions.json").read_text())
    cases = [{"inputs": {"query": q["request"]}} for q in questions]
    print(f"Scoring {len(cases)} curated questions…")
    results = mlflow.genai.evaluate(data=cases, predict_fn=build_predict_fn(), scorers=scorers)

for name, value in (results.metrics or {}).items():
    print(f"  {name}: {value}")
print(f"\nRun: {results.run_id}")
