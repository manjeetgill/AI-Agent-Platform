import importlib.util
from pathlib import Path

import httpx
import pytest

from agent_platform.memory_client import MemoryClient


def test_private_client_identity_timeout_and_no_redirect():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    with MemoryClient(
        "https://agents.example", "private-service-key", "clinic-a", transport=httpx.MockTransport(respond)
    ) as client:
        assert client.search("clinic hours")["items"] == []
    assert seen[0].headers["x-external-user-id"] == "clinic-a"
    assert seen[0].url.path == "/v1/memory/search"
    assert seen[0].url.params["q"] == "clinic hours"
    assert seen[0].extensions["timeout"]["read"] == 60
    with pytest.raises(ValueError):
        MemoryClient("http://public.example", "key", "user")


def test_dataset_split_deduplicates_questions_and_rejects_conflicts():
    spec = importlib.util.spec_from_file_location(
        "prepare_training_dataset", Path(__file__).parents[2] / "scripts/prepare_training_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def item(question, answer="Reviewed business response"):
        return {
            "consent_ref": "synthetic-consent",
            "messages": [{"role": "user", "content": question}, {"role": "assistant", "content": answer}],
        }

    data = {
        "format": "provider-neutral-messages-v1",
        "examples": [item("Question " + str(i)) for i in range(6)],
    }
    data["examples"].append(item("  QUESTION  0  "))
    train, holdout = module.prepare(data)
    assert len(train) == 5 and len(holdout) == 1
    assert not ({r["messages"][0]["content"] for r in train} & {r["messages"][0]["content"] for r in holdout})
    data["examples"].append(item("Question 0", "Conflicting response"))
    with pytest.raises(ValueError, match="Conflicting"):
        module.prepare(data)
