"""Entry point of the agent Databricks App.

AgentServer("ResponsesAgent") serves the @invoke / @stream functions of agent.py as
POST /invocations (and /responses), plus GET /agent/info and /health.

Local dev:  python start_server.py --port 8000
"""

import agent  # noqa: F401 — registers @invoke / @stream
from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

agent_server = AgentServer("ResponsesAgent")
app = agent_server.app

# Groups traces by git commit, so each deployed revision is comparable in the MLflow experiment.
setup_mlflow_git_based_version_tracking()

if __name__ == "__main__":
    agent_server.run(app_import_string="start_server:app")
