# Goose worker (retired)

The Playwright + Goose Cloud Run service `goose-worker` is no longer deployed. Mention handling is stock **buzz-acp** on the e2-micro; LiteLLM stays on Cloud Run behind [`listener/litellm_proxy.py`](../listener/litellm_proxy.py). Chat apply is [`listener/cloud_agents.py`](../listener/cloud_agents.py).

`infra/deploy-listener.ps1` deletes a leftover `goose-worker` service if it still exists.
