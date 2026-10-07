"""Optional local semantic search. No remote requests or automatic model downloads.

Install the embeddings extra and configure an immutable local model directory.
Small-pilot index: PostgreSQL arrays, exact cosine ranking after permission filters.
"""

import asyncio
import hashlib
import math
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

_LOCK = Lock()


def configuration():
    path = os.environ.get("AI_MEMORY_EMBEDDING_PATH", "").strip()
    revision = os.environ.get("AI_MEMORY_EMBEDDING_REVISION", "").strip()
    if not path or not revision or not Path(path).is_dir():
        return None
    return path, hashlib.sha256((path + "\0" + revision).encode()).hexdigest()


@lru_cache(maxsize=1)
def model(path):
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(2)

    return SentenceTransformer(path, device="cpu", local_files_only=True, trust_remote_code=False)


def encode_sync(path, texts):
    with _LOCK:
        return (
            model(path)
            .encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
            .tolist()
        )


def checked(vectors, count):
    if len(vectors) != count:
        raise ValueError("Embedding count mismatch")
    width = len(vectors[0]) if vectors else 0
    if not 1 <= width <= 4096:
        raise ValueError("Unsupported embedding dimensions")
    for vector in vectors:
        if len(vector) != width or any(not math.isfinite(x) for x in vector):
            raise ValueError("Invalid embedding values")
        norm = math.sqrt(sum(x * x for x in vector))
        if not norm:
            raise ValueError("Empty embedding")
        vector[:] = [x / norm for x in vector]
    return vectors


async def encode(texts):
    config = configuration()
    if not config:
        raise ValueError("Local embedding model is not configured")
    vectors = await asyncio.to_thread(encode_sync, config[0], texts)
    return config[1], checked(vectors, len(texts))


async def index_owned(pool, app, owner, limit=32):
    config = configuration()
    if not config:
        raise ValueError("Install embeddings extra and set local model path and revision")
    # Serialize per process to bound CPU work. Cross-process updates are version fenced.
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                """SELECT m.id,m.version,m.content FROM platform_memories m
            LEFT JOIN memory_vectors v ON v.memory_id=m.id
            WHERE m.app_id=%s AND m.owner_id=%s AND m.status='VALIDATED' AND m.expires_at>now()
            AND (v.memory_id IS NULL OR v.version<>m.version OR v.model<>%s)
            ORDER BY m.id LIMIT %s""",
                (app, owner, config[1], min(limit, 32)),
            )
        ).fetchall()
    if not rows:
        return 0
    name, vectors = await encode([r["content"] for r in rows])
    indexed = 0
    async with pool.connection() as conn, conn.transaction():
        for row, vector in zip(rows, vectors, strict=True):
            # Lock and revalidate after expensive inference, so revocation wins.
            current = await (
                await conn.execute(
                    """SELECT 1 FROM platform_memories
                WHERE id=%s AND app_id=%s AND owner_id=%s AND version=%s
                AND status='VALIDATED' AND expires_at>now() FOR SHARE""",
                    (row["id"], app, owner, row["version"]),
                )
            ).fetchone()
            if not current:
                continue
            await conn.execute(
                """INSERT INTO memory_vectors VALUES(%s,%s,%s,%s,now())
                ON CONFLICT(memory_id) DO UPDATE SET version=EXCLUDED.version,model=EXCLUDED.model,
                embedding=EXCLUDED.embedding,updated_at=now()""",
                (row["id"], row["version"], name, vector),
            )
            indexed += 1
    return indexed


async def semantic(pool, app, owner, query, namespace, shared_only, limit):
    config = configuration()
    if not config:
        return []
    name, vectors = await encode([query[:4000]])
    async with pool.connection() as conn:
        # Permission and version checks happen in SQL before vectors leave storage.
        rows = await (
            await conn.execute(
                """SELECT m.*,v.embedding FROM platform_memories m
            JOIN memory_vectors v ON v.memory_id=m.id AND v.version=m.version
            WHERE m.app_id=%s AND m.namespace=%s AND m.status='VALIDATED' AND m.expires_at>now()
            AND (m.visibility='app' OR (m.owner_id=%s AND NOT %s)) AND v.model=%s
            ORDER BY m.id LIMIT 10000""",
                (app, namespace, owner, shared_only, name),
            )
        ).fetchall()
    ranked = []
    for row in rows:
        if len(row["embedding"]) != len(vectors[0]):
            continue
        score = sum(a * b for a, b in zip(row.pop("embedding"), vectors[0], strict=True))
        if math.isfinite(score) and score >= 0.35:
            ranked.append((score, str(row["id"]), row))
    return [r[2] for r in sorted(ranked, key=lambda r: (-r[0], r[1]))[:limit]]
