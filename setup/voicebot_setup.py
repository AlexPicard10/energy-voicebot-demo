"""ENERGY voicebot — workspace setup, driven by the notebooks next to this file.

  01_build_data_assets   phase_uc, phase_gateway, phase_vs, phase_lakebase, phase_mlflow, phase_genie
                         (before deploy)
  02_grant_app_access    post_deploy — grants that need the deployed apps' service principals
  99_teardown            teardown — delete everything the phases created

Configuration comes from the bundle target in databricks.yml (see bundle_config.py).

  uc        schema; tables customers / invoices / consumption / knowledgebase (+ generated demo data);
            UC function get_consumption_history
  gateway   AI Gateway model service llm_endpoint (if absent), routing to a Foundation Model
  vs      Vector Search endpoint + delta-sync index knowledgebase_index
  lakebase  Lakebase project (if absent); tables tickets / supervisor_alerts; SNAPSHOT synced tables
            customer_sync / invoice_sync
  mlflow    experiment whose traces are stored in Unity Catalog tables
  genie     Genie space over customers / invoices

The customer data is generated (random names, masked phones); the knowledge base in data/ is synthetic.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import random
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

from bundle_config import Config

DATA_DIR = Path(__file__).resolve().parent / "data"

VS_ENDPOINT = "energy_voicebot_vs"
KB_INDEX = "knowledgebase_index"
EMBEDDING_MODEL = "databricks-gte-large-en"
PG_DATABASE = "databricks_postgres"
TRACE_TABLE_PREFIX = "voicebot_traces"
EXPERIMENT_DIR = "energy-voicebot-agent"
EXPERIMENT_NAME = "voicebot-app-uc"
GENIE_TITLE = "Energy Voicebot Customer Assistant"

# Deterministic data generation so re-runs produce the same rows.
SEED = 42
random.seed(SEED)

# Reference date for invoice/consumption generation (pinned for reproducibility).
TODAY = dt.date(2026, 5, 28)

# ---------------------------------------------------------------------------
# Italian demo data constants
# ---------------------------------------------------------------------------

REGIONS = [
    ("Lombardia", "Milano"), ("Lazio", "Roma"), ("Campania", "Napoli"),
    ("Sicilia", "Palermo"), ("Veneto", "Venezia"), ("Emilia-Romagna", "Bologna"),
    ("Piemonte", "Torino"), ("Puglia", "Bari"), ("Toscana", "Firenze"),
    ("Liguria", "Genova"), ("Marche", "Ancona"), ("Friuli-Venezia Giulia", "Trieste"),
]
FIRST = [
    "Marco", "Giulia", "Luca", "Sara", "Andrea", "Chiara", "Matteo", "Francesca",
    "Davide", "Alessia", "Stefano", "Martina", "Federico", "Elena", "Gabriele", "Paola",
    "Riccardo", "Valentina", "Tommaso", "Beatrice", "Simone", "Anna", "Lorenzo", "Laura",
]
LAST = [
    "Rossi", "Russo", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo", "Ricci",
    "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca", "Mancini", "Costa",
    "Giordano", "Rizzo", "Lombardi", "Moretti", "Barbieri", "Fontana", "Santoro",
]

# Italian seasonal usage multipliers and average temperatures (month → (factor, temp_c))
SEASON = {
    1: (1.40, 5.0), 2: (1.30, 6.5), 3: (1.10, 11.5), 4: (1.00, 16.0),
    5: (0.95, 20.0), 6: (1.05, 25.0), 7: (1.20, 29.0), 8: (1.20, 29.5),
    9: (1.00, 24.0), 10: (1.00, 18.0), 11: (1.15, 11.0), 12: (1.35, 7.0),
}

# "Picker" customers always get a cold-snap spike in the latest month for the
# "why is my bill higher?" demo scenario.
PICKER_CIDS = {"CUST-000142", "CUST-000528", "CUST-000091", "CUST-000663"}
SEED_CUSTOMER = "CUST-000142"

PRICE_E, PRICE_G = 0.32, 1.05


def _generate_data():
    random.seed(SEED)

    # 5 monthly periods anchored on TODAY
    periods = []
    for m in range(5, 0, -1):
        d = TODAY - dt.timedelta(days=m * 30 + 3)
        periods.append((d.strftime("%Y-%m"), d.month,
                        calendar.monthrange(d.year, d.month)[1], m == 1))

    customers, invoices = [], []
    overdue_per: dict[str, dict] = {}

    for i in range(300):
        cid = f"CUST-{i + 1:06d}"
        fn, ln = random.choice(FIRST), random.choice(LAST)
        region, city = random.choice(REGIONS)
        segment = "business" if random.random() < 0.08 else "residential"
        phone = f"+39 0{random.randint(2, 99)} ***{random.randint(1000, 9999)}"
        has_e = random.random() < 0.80
        has_g = random.random() < 0.55
        if not (has_e or has_g):
            has_e = True
        sdd = random.random() < 0.62
        avg_kwh = round(random.uniform(180, 480), 1) if has_e else None
        avg_smc = round(random.uniform(40, 220), 1) if has_g else None

        customers.append({
            "customer_id": cid, "phone_masked": phone,
            "first_name": fn, "last_name": ln,
            "region": region, "city": city, "segment": segment,
            "has_electricity": has_e, "has_gas": has_g, "sdd_enabled": sdd,
            "monthly_avg_kwh": avg_kwh, "monthly_avg_smc": avg_smc,
            "overdue_amount_eur": 0.0, "overdue_invoice_id": None,
            "has_active_installment_plan": False,
        })

        for m in range(5, 0, -1):
            inv_date = TODAY - dt.timedelta(days=m * 30 + 3)
            due_date = inv_date + dt.timedelta(days=30)
            base = avg_kwh or avg_smc or 200
            price = 0.32 if has_e else 1.05
            amount = round(max(15, base * price * random.uniform(0.85, 1.25)), 2)
            if due_date >= TODAY:
                status = "pending"
            else:
                status = random.choices(["paid", "overdue", "paid"],
                                        weights=[0.86, 0.10, 0.04])[0]
            plan_id = None
            if status == "overdue":
                cur = overdue_per.get(cid)
                if cur is None or amount > cur["amount"]:
                    overdue_per[cid] = {"amount": amount,
                                        "invoice_id": f"INV-{cid[-6:]}-{m:02d}"}
                if random.random() < 0.45:
                    plan_id = f"PLN-{cid[-6:]}-{m:02d}"
            invoices.append({
                "invoice_id": f"INV-{cid[-6:]}-{m:02d}",
                "customer_id": cid,
                "invoice_date": inv_date.isoformat(),
                "amount_eur": amount,
                "due_date": due_date.isoformat(),
                "status": status,
                "payment_method": "direct_debit" if sdd else "payment_slip",
                "installment_plan_id": plan_id,
            })

    # Back-fill overdue fields on customers
    for c in customers:
        o = overdue_per.get(c["customer_id"])
        if o:
            c["overdue_amount_eur"] = o["amount"]
            c["overdue_invoice_id"] = o["invoice_id"]
        c["has_active_installment_plan"] = any(
            inv["customer_id"] == c["customer_id"] and inv["installment_plan_id"]
            for inv in invoices
        )

    # Ensure the demo spotlight customer exists with known data
    existing_cids = {c["customer_id"] for c in customers}
    for cid in PICKER_CIDS:
        if cid not in existing_cids:
            customers.append({
                "customer_id": cid, "phone_masked": f"+39 06 ***{random.randint(1000,9999)}",
                "first_name": random.choice(FIRST), "last_name": random.choice(LAST),
                "region": "Lazio", "city": "Roma", "segment": "residential",
                "has_electricity": True, "has_gas": True, "sdd_enabled": True,
                "monthly_avg_kwh": 280.0, "monthly_avg_smc": 110.0,
                "overdue_amount_eur": 287.0, "overdue_invoice_id": f"INV-{cid[-6:]}-04",
                "has_active_installment_plan": False,
            })
            invoices.append({
                "invoice_id": f"INV-{cid[-6:]}-04",
                "customer_id": cid,
                "invoice_date": (TODAY - dt.timedelta(days=10)).isoformat(),
                "amount_eur": 287.0,
                "due_date": (TODAY + dt.timedelta(days=3)).isoformat(),
                "status": "overdue",
                "payment_method": "SDD",
                "installment_plan_id": None,
            })

    # Consumption
    consumption = []
    for c in customers:
        commodity = "electricity" if c["has_electricity"] else "gas"
        unit = "kWh" if commodity == "electricity" else "SMC"
        price = PRICE_E if commodity == "electricity" else PRICE_G
        base = (c["monthly_avg_kwh"] if commodity == "electricity"
                else c["monthly_avg_smc"]) or 200.0
        is_picker = c["customer_id"] in PICKER_CIDS
        for period, month, days, is_latest in periods:
            if is_picker:
                factor = 1.45 if is_latest else 1.00
                temp = 6.0 if is_latest else SEASON[month][1]
            else:
                sf, temp = SEASON[month]
                factor = sf * random.uniform(0.92, 1.08)
                temp = round(temp + random.uniform(-1.5, 1.5), 1)
            usage = round(base * factor, 1)
            consumption.append({
                "customer_id": c["customer_id"],
                "period": period, "commodity": commodity, "unit": unit,
                "usage": usage, "amount_eur": round(usage * price, 2),
                "avg_temp_c": float(temp), "days_in_period": int(days),
            })

    # Convert to tuples in column order
    cust_rows = [
        (c["customer_id"], c["phone_masked"], c["first_name"], c["last_name"],
         c["region"], c["city"], c["segment"], c["has_electricity"], c["has_gas"],
         c["sdd_enabled"], c["monthly_avg_kwh"], c["monthly_avg_smc"],
         c["overdue_amount_eur"], c["overdue_invoice_id"], c["has_active_installment_plan"])
        for c in customers
    ]
    inv_rows = [
        (i["invoice_id"], i["customer_id"], i["invoice_date"], i["amount_eur"],
         i["due_date"], i["status"], i["payment_method"], i["installment_plan_id"])
        for i in invoices
    ]
    cons_rows = [
        (c["customer_id"], c["period"], c["commodity"], c["unit"],
         c["usage"], c["amount_eur"], c["avg_temp_c"], c["days_in_period"])
        for c in consumption
    ]
    return cust_rows, inv_rows, cons_rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def _v(val: Any) -> str:
    """Python value → SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    return "'" + str(val).replace("'", "''") + "'"


def _sql(w, cfg: Config, statement: str) -> None:
    """Run one statement on the warehouse and wait for it."""
    from databricks.sdk.service.sql import StatementState

    resp = w.statement_execution.execute_statement(statement=statement, warehouse_id=cfg.warehouse_id,
                                                   wait_timeout="50s")
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {resp.status.error.message if resp.status.error else resp.status.state}\n"
                           f"{statement[:200]}")


def _insert(w, cfg: Config, table: str, rows: list[tuple], batch: int = 100) -> None:
    for i in range(0, len(rows), batch):
        values = ", ".join("(" + ", ".join(_v(c) for c in row) + ")" for row in rows[i:i + batch])
        _sql(w, cfg, f"INSERT INTO {cfg.fqs}.{table} VALUES {values}")


def _pg_connect(w, cfg: Config):
    """Connect to Lakebase as the current user (OAuth token as password)."""
    import psycopg

    host = w.postgres.get_endpoint(name=cfg.lakebase_endpoint).status.hosts.host
    token = w.postgres.generate_database_credential(endpoint=cfg.lakebase_endpoint).token
    return psycopg.connect(host=host, port=5432, dbname=PG_DATABASE, user=w.current_user.me().user_name,
                           password=token, sslmode="require", autocommit=True)


def _load_articles() -> list[tuple]:
    """The synthetic knowledge base (data/knowledge_base.json)."""
    articles = json.loads((DATA_DIR / "knowledge_base.json").read_text())
    return [(a.get("article_id"), a.get("title", ""), a.get("url_name", ""), a.get("content", ""))
            for a in articles]


# ---------------------------------------------------------------------------
# Phase: Unity Catalog
# ---------------------------------------------------------------------------

def phase_uc(w, cfg: Config, reload_data: bool = True) -> None:
    fqs = cfg.fqs
    _header(f"Unity Catalog — {fqs}")
    _sql(w, cfg, f"CREATE SCHEMA IF NOT EXISTS {fqs}")

    if not reload_data:
        print("  reload_data=False: tables left as they are")
    else:
        for table in ("customers", "invoices", "consumption", "knowledgebase"):
            _sql(w, cfg, f"DROP TABLE IF EXISTS {fqs}.{table}")
        _sql(w, cfg, f"""
            CREATE TABLE {fqs}.customers (
              customer_id STRING, phone_masked STRING, first_name STRING, last_name STRING,
              region STRING, city STRING, segment STRING, has_electricity BOOLEAN, has_gas BOOLEAN,
              sdd_enabled BOOLEAN, monthly_avg_kwh DOUBLE, monthly_avg_smc DOUBLE,
              overdue_amount_eur DOUBLE, overdue_invoice_id STRING, has_active_installment_plan BOOLEAN)""")
        _sql(w, cfg, f"""
            CREATE TABLE {fqs}.invoices (
              invoice_id STRING, customer_id STRING, invoice_date STRING, amount_eur DOUBLE,
              due_date STRING, status STRING, payment_method STRING, installment_plan_id STRING)""")
        _sql(w, cfg, f"""
            CREATE TABLE {fqs}.consumption (
              customer_id STRING, period STRING, commodity STRING, unit STRING, usage DOUBLE,
              amount_eur DOUBLE, avg_temp_c DOUBLE, days_in_period INT)""")
        # Vector Search embeds `content` directly (one row per article); CDF enables delta sync.
        _sql(w, cfg, f"""
            CREATE TABLE {fqs}.knowledgebase (article_id STRING, title STRING, url_name STRING, content STRING)
            TBLPROPERTIES (delta.enableChangeDataFeed = true)""")

        customers, invoices, consumption = _generate_data()
        _insert(w, cfg, "customers", customers)
        _insert(w, cfg, "invoices", invoices, batch=150)
        _insert(w, cfg, "consumption", consumption, batch=150)
        articles = _load_articles()
        _insert(w, cfg, "knowledgebase", articles, batch=25)  # articles can be ~20k chars
        print(f"  {len(customers)} customers, {len(invoices)} invoices, {len(consumption)} consumption rows, "
              f"{len(articles)} articles")

    _sql(w, cfg, f"""
CREATE OR REPLACE FUNCTION {fqs}.get_consumption_history(
  customer_id STRING COMMENT 'Customer id, e.g. CUST-000142',
  months INT DEFAULT 6 COMMENT 'How many recent months to return (1-12)')
RETURNS TABLE(period STRING, month STRING, commodity STRING, unit STRING,
  usage DOUBLE, amount_eur DOUBLE, avg_temp_c DOUBLE)
COMMENT 'Recent monthly energy consumption for a customer (one row per month, oldest to latest, with usage, amount in EUR and average temperature). Use to explain how or why a bill changed month over month.'
RETURN
  SELECT period, date_format(to_date(concat(period,'-01')), 'MMMM yyyy') AS month,
         commodity, unit, usage, amount_eur, avg_temp_c
  FROM (SELECT * FROM (
          SELECT *, row_number() OVER (ORDER BY period DESC) AS rn
          FROM {fqs}.consumption
          WHERE customer_id = get_consumption_history.customer_id)
        WHERE rn <= least(greatest(months, 1), 12))
  ORDER BY period""")

    print("  UC function get_consumption_history")


# ---------------------------------------------------------------------------
# Phase: AI Gateway
# ---------------------------------------------------------------------------

MODEL_SERVICES_API = "/api/2.1/unity-catalog/model-services"


def phase_gateway(w, cfg: Config) -> None:
    """The AI Gateway model service the agent calls (llm_endpoint), routed to the pay-per-token
    Foundation Model gateway_model. An existing service with that name is left as it is."""
    from databricks.sdk.errors import NotFound

    _header(f"AI Gateway — {cfg.llm_endpoint}")
    if not cfg.llm_endpoint:
        raise ValueError(f"Set `llm_endpoint` in target '{cfg.target}' of databricks.yml (or remove it to use "
                         "the default).")
    try:
        w.api_client.do("GET", f"{MODEL_SERVICES_API}/{cfg.llm_endpoint}")
        print("  model service exists — kept as it is")
        return
    except NotFound:
        pass
    catalog, schema, service_id = cfg.llm_endpoint.split(".")
    w.api_client.do("POST", MODEL_SERVICES_API,
                    query={"parent": f"schemas/{catalog}.{schema}", "model_service_id": service_id},
                    body={
                        "comment": "ENERGY voicebot agent LLM (guardrails: setup/competitor_guardrail.md)",
                        "config": {"routing": {"destinations": [{
                            "name": "primary",
                            "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
                            "pay_per_token_config": {"model": f"models/system.ai.{cfg.gateway_model}"},
                        }]}},
                    })
    print(f"  model service created → system.ai.{cfg.gateway_model}")


# ---------------------------------------------------------------------------
# Phase: Vector Search
# ---------------------------------------------------------------------------

def phase_vs(w, cfg: Config) -> None:
    from databricks.sdk.service.vectorsearch import (
        DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, EndpointType,
        PipelineType, VectorIndexType,
    )

    index = f"{cfg.fqs}.{KB_INDEX}"
    _header(f"Vector Search — {VS_ENDPOINT} / {index}")
    if VS_ENDPOINT not in {e.name for e in w.vector_search_endpoints.list_endpoints()}:
        print(f"  creating endpoint {VS_ENDPOINT} (a few minutes)…")
        w.vector_search_endpoints.create_endpoint_and_wait(name=VS_ENDPOINT, endpoint_type=EndpointType.STANDARD)

    if index in {i.name for i in w.vector_search_indexes.list_indexes(endpoint_name=VS_ENDPOINT)}:
        w.vector_search_indexes.sync_index(index_name=index)
        print("  index exists — sync triggered")
    else:
        w.vector_search_indexes.create_index(
            name=index, endpoint_name=VS_ENDPOINT, primary_key="article_id",
            index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=f"{cfg.fqs}.knowledgebase", pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[EmbeddingSourceColumn(
                    name="content", embedding_model_endpoint_name=EMBEDDING_MODEL)]),
        )
        print("  index created — the first sync takes a few minutes")


# ---------------------------------------------------------------------------
# Phase: Lakebase
# ---------------------------------------------------------------------------

def phase_lakebase(w, cfg: Config) -> None:
    from databricks.sdk.service import postgres as pg

    s = cfg.schema
    _header(f"Lakebase — {cfg.lakebase_branch}")
    project = f"projects/{cfg.lakebase_project}"
    try:
        w.postgres.get_project(name=project)
    except Exception:  # noqa: BLE001 — not found
        print(f"  creating project {cfg.lakebase_project} (a few minutes)…")
        w.postgres.create_project(project=pg.Project(spec=pg.ProjectSpec(display_name=cfg.lakebase_project)),
                                  project_id=cfg.lakebase_project).wait()
    w.postgres.get_branch(name=cfg.lakebase_branch)  # fails clearly if lakebase_branch is wrong

    with _pg_connect(w, cfg) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {s}.tickets (
              ticket_id text PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
              action_type text NOT NULL, customer_id text NOT NULL, payload_json jsonb, status text)""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS tickets_customer_ts_idx ON {s}.tickets (customer_id, ts DESC)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {s}.supervisor_alerts (
              alert_id text PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), session_id text,
              customer_id text, intent text, sentiment double precision, excerpt text,
              status text NOT NULL DEFAULT 'open')""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS supervisor_alerts_ts_idx ON {s}.supervisor_alerts (ts DESC)")
        # Account history for the demo's spotlight customer.
        for tid, ago, action, status, payload in [
            ("TKT-SEED0001", "5 days", "installment_plan", "created",
             {"invoice_id": "INV-000142-03", "months": 6, "admin_fee_pct": 0.015}),
            ("TKT-SEED0002", "2 days", "meter_reading", "recorded",
             {"supply_point_id": "SP-000142-E", "reading_value": 47230, "unit": "kWh", "validation": "in_range"}),
        ]:
            cur.execute(f"INSERT INTO {s}.tickets (ticket_id, ts, action_type, customer_id, payload_json, status) "
                        "VALUES (%s, now() - %s::interval, %s, %s, %s::jsonb, %s) ON CONFLICT DO NOTHING",
                        (tid, ago, action, SEED_CUSTOMER, json.dumps(payload), status))
        print(f"  tables {s}.tickets, {s}.supervisor_alerts")

    # Delta → Postgres SNAPSHOT copies, so get_customer_summary is a sub-ms OLTP read.
    for synced, source, pk in [("customer_sync", "customers", "customer_id"),
                               ("invoice_sync", "invoices", "invoice_id")]:
        name = f"{cfg.fqs}.{synced}"
        try:
            w.postgres.get_synced_table(name=f"synced_tables/{name}")
            print(f"  synced table {synced} exists")
            continue
        except Exception:  # noqa: BLE001 — not found
            pass
        w.postgres.create_synced_table(
            synced_table_id=name,
            synced_table=pg.SyncedTable(spec=pg.SyncedTableSyncedTableSpec(
                source_table_full_name=f"{cfg.fqs}.{source}", branch=cfg.lakebase_branch,
                postgres_database=PG_DATABASE, primary_key_columns=[pk],
                scheduling_policy=pg.SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.SNAPSHOT,
                create_database_objects_if_missing=True)),
        )
        print(f"  synced table {synced} created (first snapshot takes a few minutes)")


# ---------------------------------------------------------------------------
# Phase: MLflow + Genie
# ---------------------------------------------------------------------------

def phase_mlflow(w, cfg: Config) -> str:
    import mlflow
    from mlflow.entities.trace_location import UnityCatalog

    _header("MLflow experiment (traces in Unity Catalog)")
    mlflow.set_tracking_uri("databricks")
    folder = f"/Users/{w.current_user.me().user_name}/{EXPERIMENT_DIR}"
    w.workspace.mkdirs(path=folder)
    exp = mlflow.set_experiment(
        experiment_name=f"{folder}/{EXPERIMENT_NAME}",
        trace_location=UnityCatalog(catalog_name=cfg.catalog, schema_name=cfg.schema,
                                    table_prefix=TRACE_TABLE_PREFIX),
    )
    print(f"  experiment {exp.experiment_id}; traces in {cfg.fqs}.{TRACE_TABLE_PREFIX}_otel_*")
    return exp.experiment_id


def phase_genie(w, cfg: Config) -> str:
    _header("Genie space")
    if cfg.genie_space_id:
        print(f"  using {cfg.genie_space_id} from the bundle target")
        return cfg.genie_space_id
    # serialized_space (version 2): id-keyed items need a 32-hex id and must be sorted by it; text is
    # given as arrays of strings; at most one text_instructions item.
    new_id = lambda: uuid.uuid4().hex  # noqa: E731
    space = {
        "version": 2,
        "config": {"sample_questions": sorted([{"id": new_id(), "question": [q]} for q in (
            "For customer CUST-000142, what are their open invoices?",
            "What is the total outstanding balance across all of CUST-000142's invoices?",
            "Top 10 customers by overdue amount.",
        )], key=lambda q: q["id"])},
        "data_sources": {"tables": [{"identifier": f"{cfg.fqs}.customers"},
                                    {"identifier": f"{cfg.fqs}.invoices"}]},
        "instructions": {"text_instructions": [{"id": new_id(), "content": [
            line + "\n" for line in textwrap.dedent("""
            - customer_id is the PK in customers (CUST-XXXXXX); invoices join on customer_id.
            - 'Overdue' means status='overdue' OR (due_date < current_date AND status='pending').
            - Currency is EUR. Consumption is kWh (electricity) and SMC (gas).
            - Italian regions are spelled in Italian (Lombardia, Lazio, Toscana, etc.).
            - When a request is on behalf of one caller, always filter by their customer_id.
            - For counts/sums spanning more than one table, compute each metric in its own
              subquery and combine the scalars -- don't join detail rows directly.""").strip().splitlines()]}]},
    }
    space_id = w.genie.create_space(
        warehouse_id=cfg.warehouse_id, serialized_space=json.dumps(space), title=GENIE_TITLE,
        description="Customer profiles and invoices for ENERGY's voicebot. Questions name one "
                    "customer_id; answer for that customer only.",
    ).space_id
    print(f"  created {space_id}")
    return space_id


# ---------------------------------------------------------------------------
# Post-deploy grants (need the apps' service principals)
# ---------------------------------------------------------------------------

def post_deploy(w, cfg: Config) -> None:
    _header("Post-deploy grants")
    agent = w.apps.get(name=cfg.apps["energy_voicebot_agent"]).service_principal_client_id
    ui = w.apps.get(name=cfg.apps["energy_voicebot_ui"]).service_principal_client_id
    s = cfg.schema

    # The Vector Search endpoint is not an app resource type.
    ep = w.api_client.do("GET", f"/api/2.0/vector-search/endpoints/{VS_ENDPOINT}")
    w.api_client.do("PATCH", f"/api/2.0/permissions/vector-search-endpoints/{ep['id']}", body={
        "access_control_list": [{"service_principal_name": agent, "permission_level": "CAN_USE"}]})
    print(f"  agent: CAN_USE on Vector Search endpoint {VS_ENDPOINT}")

    # Writing traces to Unity Catalog needs SELECT + MODIFY on the experiment's trace tables.
    for t in ("otel_spans", "otel_annotations", "otel_logs", "otel_metrics"):
        _sql(w, cfg, f"GRANT SELECT, MODIFY ON TABLE {cfg.fqs}.{TRACE_TABLE_PREFIX}_{t} TO `{agent}`")
    print("  agent: SELECT, MODIFY on the MLflow trace tables")

    # The `postgres` app resource creates each app's Postgres role; table privileges are granted here.
    with _pg_connect(w, cfg) as conn, conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA {s} TO "{agent}", "{ui}"')
        cur.execute(f'GRANT SELECT, INSERT ON {s}.tickets, {s}.supervisor_alerts TO "{agent}"')
        cur.execute(f'GRANT SELECT ON {s}.customer_sync, {s}.invoice_sync TO "{agent}"')
        cur.execute(f'GRANT SELECT, UPDATE ON {s}.supervisor_alerts TO "{ui}"')
    print("  agent: tickets, supervisor_alerts, customer_sync, invoice_sync   ui: supervisor_alerts (read, resolve)")

    # A model service is not an app resource type yet: querying it needs EXECUTE (plus USE CATALOG /
    # USE SCHEMA on its parent, which the app's resources in the same schema already give).
    try:
        w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/model_service/{cfg.llm_endpoint}",
                        body={"changes": [{"principal": agent, "add": ["EXECUTE"]}]})
        print(f"  agent: EXECUTE on the AI Gateway model service {cfg.llm_endpoint}")
    except Exception as e:  # noqa: BLE001 — e.g. a shared service you don't manage
        print(f"\n  Could not grant EXECUTE on {cfg.llm_endpoint} ({str(e)[:120]}). Ask its owner to run:")
        print(f"    databricks grants update model_service {cfg.llm_endpoint} \\\n"
              f"      --json '{{\"changes\": [{{\"principal\": \"{agent}\", \"add\": [\"EXECUTE\"]}}]}}'"
              f"   # {cfg.apps['energy_voicebot_agent']}")


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def teardown(w, cfg: Config) -> None:
    _header("Teardown")
    owns_gateway = cfg.llm_endpoint.rsplit(".", 1)[0] == cfg.fqs  # created by phase_gateway, in the schema
    print(f"  Deletes: UC schema {cfg.fqs} (CASCADE), Vector Search endpoint {VS_ENDPOINT}, Lakebase schema\n"
          f"  {cfg.schema}, synced tables, Genie space {cfg.genie_space_id or '-'}"
          f"{f', AI Gateway service {cfg.llm_endpoint}' if owns_gateway else ''}. The apps and the Lakebase\n"
          "  project are kept (delete the apps from Compute → Apps).")

    def attempt(label, fn):
        try:
            fn()
            print(f"  deleted {label}")
        except Exception as e:  # noqa: BLE001
            print(f"  skipped {label}: {str(e)[:120]}")

    if cfg.genie_space_id:
        attempt("Genie space", lambda: w.api_client.do("DELETE", f"/api/2.0/genie/spaces/{cfg.genie_space_id}"))
    for synced in ("customer_sync", "invoice_sync"):
        attempt(synced, lambda n=synced: w.postgres.delete_synced_table(name=f"synced_tables/{cfg.fqs}.{n}"))

    def drop_pg_schema():
        with _pg_connect(w, cfg) as conn, conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {cfg.schema} CASCADE")

    attempt("Lakebase schema", drop_pg_schema)
    attempt("Vector Search endpoint", lambda: w.vector_search_endpoints.delete_endpoint(endpoint_name=VS_ENDPOINT))
    if owns_gateway:
        attempt("AI Gateway service", lambda: w.api_client.do("DELETE", f"{MODEL_SERVICES_API}/{cfg.llm_endpoint}"))
    attempt("UC schema", lambda: _sql(w, cfg, f"DROP SCHEMA IF EXISTS {cfg.fqs} CASCADE"))
