"""Backend-only reusable memory client. Never put a platform credential in a browser."""

from urllib.parse import quote, urlsplit

import httpx


class MemoryClient:
    def __init__(self, base_url, credential, external_user, *, transport=None):
        url = urlsplit(base_url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("Use HTTPS outside localhost")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/v1/",
            timeout=60,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + credential, "X-External-User-ID": external_user},
            transport=transport,
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def request(self, path, method="GET", body=None):
        r = self.client.request(method, "memory/" + path, json=body)
        r.raise_for_status()
        return r.json() if r.status_code != 204 else None

    def settings(self):
        return self.request("settings")

    def configure(self, *, expected_revision, capture, retention_days=30, daily_candidates=25):
        return self.request(
            "settings",
            "PUT",
            {
                "expected_revision": expected_revision,
                "capture": capture,
                "retention_days": retention_days,
                "daily_candidates": daily_candidates,
            },
        )

    def candidate(self, body):
        return self.request("candidates", "POST", body)

    def search(self, question):
        return self.request("search?q=" + quote(question, safe=""))

    def records(self):
        return self.request("records")

    def review(self, memory_id, body):
        return self.request("records/" + quote(memory_id, safe="") + "/review", "POST", body)

    def checkpoint(self, task_id):
        return self.request("tasks/" + quote(task_id, safe="") + "/checkpoint")

    def feedback(self, body):
        return self.request("feedback", "POST", body)

    def metrics(self):
        return self.request("metrics")

    def create_example(self, body):
        return self.request("examples", "POST", body)

    def review_example(self, example_id, body):
        return self.request("examples/" + quote(example_id, safe="") + "/review", "POST", body)

    def evaluate(self, body):
        return self.request("evaluations", "POST", body)

    def export(self):
        return self.request("dataset/export", "POST", {})
