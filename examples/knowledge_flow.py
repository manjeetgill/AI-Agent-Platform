"""Server-side RAG example. Run only with synthetic or approved document content."""

import os
from uuid import uuid4

import httpx

with httpx.Client(
    base_url=os.environ["AGENT_API_URL"],
    timeout=35,
    headers={
        "Authorization": "Bearer " + os.environ["AGENT_SERVICE_KEY"],
        "X-External-User-ID": os.environ["AGENT_EXTERNAL_USER_ID"],
    },
) as client:
    response = client.post(
        "/v1/knowledge/documents",
        json={
            "external_id": "synthetic-refunds",
            "title": "Synthetic support policy",
            "text": "For this fictional business, unused items can be returned within thirty days. Proof of purchase is required.",
            "source_label": "Synthetic demo; not a real policy",
            "visibility": "private",
        },
    )
    response.raise_for_status()
    question = "What is the return period and what proof is required?"
    response = client.post("/v1/knowledge/search", json={"question": question})
    response.raise_for_status()
    print("Retrieved passages:", len(response.json()["passages"]))
    # In production persist this key BEFORE sending and reuse it after uncertain HTTP failures.
    key = uuid4().hex
    response = client.post("/v1/knowledge/ask", json={"question": question, "idempotency_key": key})
    response.raise_for_status()
    print(response.json())
