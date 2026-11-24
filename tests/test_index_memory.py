"""Sol's knowledge index must fit the 512 MB API with a large CMS. The 24 Nov semantic layer didn't: with a few
thousand articles it needed gigabytes, so the new version never passed Render's health check."""
import os
import random
import string
import tracemalloc

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "solix_test")

import sol_search


def test_index_for_1500_articles_stays_small():
    rnd = random.Random(7)
    words = ["".join(rnd.choices(string.ascii_lowercase, k=rnd.randint(3, 10))) for _ in range(5000)]

    def para():
        return " ".join(rnd.choices(words, k=100)) + "."
    docs = [{"slug": f"a-{i}", "title": f"Article {i}", "type": "blog", "summary": para(),
             "body": "\n\n".join(f"## Section {j}\n{para()}" for j in range(4))} for i in range(1500)]
    chunks = sol_search.bundled_chunks() + [c for d in docs for c in sol_search.cms_chunks(d)]
    tracemalloc.start()
    idx = sol_search.Index(chunks)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(chunks) > 7000
    assert peak < 120e6, f"index build peaked at {peak / 1e6:.0f} MB"
    assert idx.hybrid("article section", 3)  # still searchable
