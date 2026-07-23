import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List


class FallbackEmbeddings:
    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def _vectorize(self, text: str) -> List[float]:
        tokens = re.findall(r"\w+", text.lower())
        vector = [0.0] * self.dimensions
        if not tokens:
            return vector
        for token in tokens:
            index = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % self.dimensions
            vector[index] = 1.0
        return vector

    def embed_query(self, text: str) -> List[float]:
        return self._vectorize(text)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vectorize(text) for text in texts]


class FallbackIndex:
    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def upsert(self, vectors: List[tuple]) -> None:
        for chunk_id, vector, metadata in vectors:
            self._items[chunk_id] = {"id": chunk_id, "values": list(vector), "metadata": metadata}

    def query(self, vector: List[float], top_k: int = 5, include_metadata: bool = True, filter: Dict[str, Any] | None = None):
        matches = []
        for chunk_id, item in self._items.items():
            if filter:
                metadata = item.get("metadata", {})
                if not all(metadata.get(k) == v for k, v in filter.items()):
                    continue
            similarity = self._cosine_similarity(vector, item["values"])
            matches.append(SimpleNamespace(id=chunk_id, score=similarity, metadata=item.get("metadata", {})))
        matches.sort(key=lambda match: match.score, reverse=True)
        return SimpleNamespace(matches=matches[:top_k])

    def describe_index_stats(self):
        return SimpleNamespace(total_vector_count=len(self._items))

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return round(dot / (norm_a * norm_b), 6)


class FallbackOpenAIClient:
    class _Completions:
        def create(self, model: str, messages: List[Dict[str, str]], max_tokens: int = 300, temperature: float = 0.3, **kwargs):
            prompt = messages[-1].get("content", "") if messages else ""
            content = self._fallback_content(prompt)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        @staticmethod
        def _fallback_content(prompt: str) -> str:
            if "Break this question into" in prompt:
                question = prompt.split("Question:", 1)[-1].strip().splitlines()[0].strip()
                return json.dumps([f"Search for {question} in available records"])
            if "Extract a chronological timeline" in prompt:
                return json.dumps([
                    {
                        "date": "2024-01-01",
                        "event_type": "discussion",
                        "title": "Fallback timeline entry",
                        "description": "This response was generated locally because the external AI service was unavailable.",
                        "participants": [],
                        "sentiment": "neutral",
                        "is_critical": False,
                        "doc_id": "LOCAL-001",
                    }
                ])
            if "OUTCOME:" in prompt or "CONFIDENCE:" in prompt:
                return "OUTCOME: A local fallback analysis was used because the external AI service was unavailable.\nCONFIDENCE: 0.4"
            return "Local fallback response generated because the external AI service was unavailable."

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self._Completions())
