"""Connection lifecycle at the service/repository boundary."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.embeddings.provider import FakeEmbeddingProvider
from app.search.repository import SearchRepository
from app.search.service import SearchService


@pytest.mark.parametrize("fail_semantic", [False, True])
def test_inference_precedes_checkout_and_candidate_queries_share_a_connection(fail_semantic):
    events = []

    class Connection:
        def execute(self, statement, params):
            if "vector" in params:
                events.append(("semantic", self))
                if fail_semantic:
                    raise RuntimeError("query failed")
            elif "unquoted" in params:
                events.append(("lexical", self))
            return SimpleNamespace(all=lambda: [])

    class Engine:
        @contextmanager
        def connect(self):
            connection = Connection()
            events.append(("checkout", connection))
            try:
                yield connection
            finally:
                events.append(("release", connection))

    class Embeddings(FakeEmbeddingProvider):
        def embed_text(self, text):
            assert not events, "No database connection may precede inference"
            events.append(("embed", text))
            return [1.0, 0.0]

    service = SearchService(SearchRepository(Engine()), Embeddings(dimensions=2))
    args = dict(
        q="coastal",
        creator_id=None,
        location=None,
        year_from=None,
        year_to=None,
        limit=20,
        cursor=None,
    )
    if fail_semantic:
        with pytest.raises(RuntimeError, match="query failed"):
            service.search(**args)
    else:
        assert service.search(**args).items == []
    assert [event[0] for event in events[:5]] == [
        "embed",
        "checkout",
        "lexical",
        "semantic",
        "release",
    ]
    connection = events[1][1]
    assert all(event[1] is connection for event in events[2:5])
