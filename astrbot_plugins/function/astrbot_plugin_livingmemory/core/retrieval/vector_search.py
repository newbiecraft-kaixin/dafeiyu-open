"""Shared query embeddings and bounded, scope-aware vector candidate expansion."""

import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace

from ..faiss_async_persist import get_async_persister

query_embeddings = ContextVar("livingmemory_query_embeddings", default=None)

# 向量召回只是增强项：本地 embedding 被聊天模型抢占时，最多等这么久就退回 BM25，
# 不让一次记忆检索把整条回复卡住。
EMBED_TIMEOUT_S = 2.5


async def _embedding_or_none(provider, query, cache):
    try:
        if cache is None:
            return await asyncio.wait_for(
                provider.get_embedding(query), timeout=EMBED_TIMEOUT_S
            )
        key = (id(provider), query)
        if key not in cache:
            cache[key] = asyncio.create_task(provider.get_embedding(query))
        # shield 让超时后任务继续跑完，下一条消息还能复用这次结果
        return await asyncio.wait_for(
            asyncio.shield(cache[key]), timeout=EMBED_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001
        return None


async def retrieve_vectors(db, query, k, filters, *, source_key="id"):
    """Retrieve unique active candidates without repeating the embedding request.

    Args:
        db: AstrBot vector database.
        query: Query text; the same budget applies to both vector routes.
        k: Maximum number of results.
        filters: Session and persona restrictions.
        source_key: Document field used to deduplicate graph source memories.

    Returns:
        Ranked vector hits. Candidate expansion is capped at 8192 vectors.
    """
    if k <= 0:
        return []
    query = query[:2000]
    storage = getattr(db, "embedding_storage", None)
    total = getattr(getattr(storage, "index", None), "ntotal", None)
    native = isinstance(total, int)
    cap = min(total, 8192) if native else 8192
    if native and not total:
        return []
    fetch_k = min(cap, max(k * (4 if filters else 2), k))

    if native:
        import numpy as np

        provider = db.embedding_provider
        embedding = await _embedding_or_none(
            provider, query, query_embeddings.get()
        )
        if embedding is None:
            # embedding 超时/失败：本轮只走关键词召回
            return []
        vector = np.asarray(embedding, dtype=np.float32).ravel()
        if vector.size != storage.dimension or not np.isfinite(vector).all():
            raise ValueError("Invalid query embedding")

    # 候选窗口翻倍重查时复用已取回的文档，避免每轮重复 SQL + JSON 解析；
    # 每轮只需查询新增的 kNN id。
    documents_by_id: dict[int, dict] = {}

    while True:
        if native:
            persister = get_async_persister(storage)
            if persister is not None:
                scores, indices = await persister.search(vector, fetch_k)
            else:
                scores, indices = await storage.search(vector, fetch_k)
            ids = [int(value) for value in indices[0] if value >= 0]
            new_ids = [doc_id for doc_id in ids if doc_id not in documents_by_id]
            if new_ids:
                documents = await db.document_storage.get_documents(
                    metadata_filters=filters,
                    ids=new_ids,
                    limit=len(new_ids),
                )
                documents_by_id.update(
                    {int(document["id"]): document for document in documents}
                )
            by_id = documents_by_id
            raw = [
                SimpleNamespace(
                    data=by_id[int(doc_id)], similarity=1.0 - float(score) / 2.0
                )
                for score, doc_id in zip(scores[0], indices[0])
                if int(doc_id) in by_id
            ]
            exhausted = len(ids) < fetch_k
        else:
            # Older host adapters expose only retrieve; preserve their public API.
            raw = await db.retrieve(
                query=query,
                k=fetch_k,
                fetch_k=fetch_k,
                rerank=False,
                metadata_filters=filters or None,
            )
            exhausted = not filters and len(raw) < fetch_k

        results = []
        seen = set()
        for hit in raw:
            metadata = hit.data.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (ValueError, TypeError):
                    continue
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("status") or "active") != "active":
                continue
            if any(metadata.get(key) != value for key, value in filters.items()):
                continue
            identity = (
                hit.data.get("id") if source_key == "id" else metadata.get(source_key)
            )
            if identity is None or identity in seen:
                continue
            seen.add(identity)
            results.append(hit)
            if len(results) >= k:
                return results
        if exhausted or fetch_k >= cap:
            return results
        fetch_k = min(cap, fetch_k * 2)
