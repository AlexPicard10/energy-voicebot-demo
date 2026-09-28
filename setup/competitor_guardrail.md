# AI Gateway guardrails for the demo

Guardrails live in the **AI Gateway**, not in the agent code. When the gateway blocks a request, the
agent only answers with a polite refusal and tags the MLflow trace `guardrail_block`
(see `apps/agent/agent.py`). Service policies are in beta: an account admin enables them from the
account console **Previews** page.

## Competitor mentions (custom LLM-as-a-judge policy)

The model happily compares ENERGY with other suppliers, so brand safety needs a custom policy. An
LLM-as-a-judge policy classifies the customer's message against criteria written in plain language;
Databricks adds the output format itself (`flagged`, `confidence`, `reason`), so the prompt must not
mention ALLOW/DENY or JSON.

### Prompt

```text
You are a content classifier for the competitor policy of ENERGY, a gas and electricity supplier in Italy. Analyze the text and flag any content that violates the rules below.

Flag content that contains:
- Requests to look up, describe, compare, recommend or criticise another energy supplier or its tariffs, prices, offers or service (e.g. Enel, Eni Plenitude, Edison, A2A, Iberdrola, Octopus Energy, or any other supplier)
- Questions about which energy supplier is cheaper or better

DO NOT flag:
- A customer switching to ENERGY from another supplier, even when they name that supplier
- Questions about the customer's own ENERGY account or contract, including how to close it
- Supplier-neutral market or regulatory questions (PUN price index, fixed vs variable prices, ARERA rules, social bonuses)
- Other suppliers mentioned only in search results or tool outputs
```

### Attach it

1. **AI Gateway** → **Models** tab → your model service (the `llm_endpoint` of `databricks.yml`) →
   **Policies** tab → **New policy**.
2. **Name**: `block-competitors` · **Applied to**: All account users.
3. **Guardrail type**: **Custom** → **Type**: **LLM-as-a-judge** → paste the prompt in **Prompt**.
4. Phase: **Input guardrails** (before the model runs).
5. The **Evaluator model service** is preselected; to change it, open **Advanced options** (you need
   `CAN QUERY` on it). Click **Create policy** and allow 1-2 minutes to propagate.

### Demo

- Before: in the UI, **AI Safety & Governance → Competitor lookup**. Sofia engages with the question.
- After attaching the policy, click it again: the gateway blocks the request before the model runs,
  Sofia gives her polite refusal, and the MLflow trace carries the `guardrail_block` tag.
- Still allowed: "I'm with another supplier today and want to switch to ENERGY, what do I need?"

### Good to know

- The judge runs on every call to the gateway, and one customer turn can make up to 4 calls: choose a
  fast evaluator model to keep the voice reply quick.
- Policies fail closed: if the evaluator errors, the gateway blocks the request, so Sofia refuses until
  it recovers.

## Built-in guardrails (optional)

The UI's **Jailbreak** and **PII detection** scenarios show the gateway only if these built-in policies
are attached (same **Policies** tab, choose the built-in guardrail as **Guardrail type**):

| Policy | Phase | Check |
|---|---|---|
| `system.ai.block_jailbreak` | input | LLM-as-a-judge |
| `system.ai.block_unsafe_content` | input and output | LLM-as-a-judge |
| `system.ai.block_hallucination` | output | LLM-as-a-judge |
| `system.ai.detect_sensitive_data` | input and output | pattern-based (can redact) |

Without them, those scenarios show the model refusing on its own.
