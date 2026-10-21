"""Local dense retrieval over unchanged runbooks; no keyword/error fallback."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import httpx

ROOT = Path(__file__).resolve().parents[1]


def normalize(vector):
    if not isinstance(vector, list) or not vector or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("Invalid embedding vector")
    norm = math.sqrt(sum(v*v for v in vector))
    if not norm:
        raise ValueError("Zero embedding vector")
    return [v/norm for v in vector]


class OllamaEmbedder:
    def __init__(self, model="embeddinggemma:latest", base_url="http://127.0.0.1:11434"):
        self.model, self.base_url = model, base_url.rstrip("/")
        self._identity = None

    async def identity(self):
        if self._identity is None:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(self.base_url + "/api/tags")
                response.raise_for_status()
                model = next((m for m in response.json()["models"] if m["name"] == self.model or m["name"] == self.model + ":latest"), None)
                if not model:
                    raise RuntimeError("Embedding model is not installed")
                self._identity = self.model + ":" + model["digest"]
        return self._identity

    async def embed(self, texts):
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(self.base_url + "/api/embed", json={"model":self.model,"input":texts,"truncate":False})
            response.raise_for_status()
            vectors = response.json()["embeddings"]
        if len(vectors) != len(texts):
            raise ValueError("Embedding response count mismatch")
        return [normalize(v) for v in vectors]


def scoped_query(query, name, targets):
    """For multi-system questions, exclude clauses assigned to other systems."""
    if len(targets) < 2:
        return query
    mentions = list(re.finditer("|".join(re.escape(t) for t in sorted(targets, key=len, reverse=True)), query, re.I))
    # A에서 B로의 타임아웃 / A와 B 상태처럼 공통 술어는 함께 유지한다.
    if len(mentions) >= 2 and all(
        re.fullmatch(r"\s*(?:에서|와|과|및|,)\s*", query[a.end():b.start()])
        for a, b in zip(mentions, mentions[1:])
    ):
        return query
    parts = [query[m.start():mentions[i+1].start() if i+1<len(mentions) else len(query)]
             for i,m in enumerate(mentions) if m.group().lower()==name.lower()]
    return " ".join(parts)


def system_search_query(query, name, targets, monitoring):
    # System identifiers and empty connector clauses are not incident symptoms.
    clause=scoped_query(query,name,targets)
    names="|".join(re.escape(t) for t in sorted(targets,key=len,reverse=True))
    if names:
        clause=re.sub(names," ",clause,flags=re.I)
    clause=re.sub(r"^\s*(?:와|과|에서|의|로)\s*", "", clause).strip()
    if re.fullmatch(r"상태\s*확인", clause):
        clause = ""
    alerts=[re.sub(r"[a-z][a-z0-9_-]*-service(?:-\d+)?", "", a, flags=re.I).strip()
            for a in monitoring.get("alerts", [])]
    return "\n".join(part for part in [clause, "; ".join(alerts)] if part)



# Explicit request scope is a guard around dense retrieval, not a keyword fallback.
TOPICS = {
    "cpu-spike": (r"cpu|프로세서", "CPU 사용률 급증 원인과 대응"),
    "memory-leak": (r"메모리|memory|heap|힙|누수", "메모리 사용량 증가 메모리 누수 대응"),
    "high-latency": (r"latency|응답\s*지연|느려|느린|느립|커넥션\s*풀", "응답 지연 커넥션 풀 대응"),
    "network-timeout": (r"timeout|타임아웃|네트워크|network", "네트워크 연결 타임아웃 대응"),
    "disk-full": (r"disk|디스크|저장\s*공간", "디스크 용량 부족 대응"),
    "db-down": (r"데이터베이스|\bdb\b|database", "데이터베이스 연결 장애 대응"),
}
UNSUPPORTED = r"\btls\b|\bssl\b|인증서|certificate"


def request_scope(query):
    included, excluded, positive = set(), set(), []
    # Semicolon/sentence boundaries keep a trailing exclusion from swallowing
    # an earlier positive request. A coordinated exclusion list stays together.
    for clause in re.split(r"[.!?;\n]+", query):
        topics = {kind for kind, (pattern, _) in TOPICS.items()
                  if re.search(pattern, clause, re.I)}
        if re.search(r"제외|빼고|말고|다루지|하지\s*마|ignore|exclude", clause, re.I):
            excluded.update(topics)
        else:
            included.update(topics)
            positive.append(clause)
    included -= excluded
    unsupported = bool(re.search(UNSUPPORTED, " ".join(positive), re.I))
    return {"included": sorted(included), "excluded": sorted(excluded),
            "unsupported": unsupported, "positive": " ".join(positive).strip()}


class RunbookSearch:
    def __init__(self, embedder=None, *, directory=None, cache_path=None,
                 min_score=0.43, score_margin=0.08, top_k=2):
        if not 0 <= min_score <= 1 or not 0 <= score_margin <= 1 or top_k < 1:
            raise ValueError("Invalid retrieval settings")
        self.embedder = embedder or OllamaEmbedder()
        self.directory = Path(directory or ROOT / "data/runbooks")
        self.cache_path = Path(cache_path) if cache_path else None
        self.min_score, self.score_margin, self.top_k = min_score, score_margin, top_k
        self._manifest, self._docs, self._vectors = None, [], []
        self._lock = asyncio.Lock()

    def documents(self):
        docs=[]
        for path in sorted(self.directory.glob("*.md")):
            text=path.read_text()
            docs.append({"document_id":path.stem,"incident_type":path.stem,
                "title":text.splitlines()[0] if text.splitlines() else path.stem,
                "sha256":hashlib.sha256(text.encode()).hexdigest(),"text":text})
        if not docs:
            raise ValueError("No runbooks available")
        return docs

    async def ensure_index(self):
        async with self._lock:
            docs=self.documents()
            manifest={"schema":1,"prefix_version":1,"model":await self.embedder.identity(),
                      "documents":[(d["document_id"], d["sha256"]) for d in docs]}
            # JSON normalization makes tuple/list representations stable on disk.
            manifest=json.loads(json.dumps(manifest))
            if manifest==self._manifest:
                return
            vectors=None
            if self.cache_path and self.cache_path.exists():
                try:
                    cache=json.loads(self.cache_path.read_text())
                    if cache["manifest"]==manifest and len(cache["vectors"])==len(docs):
                        vectors=[normalize(v) for v in cache["vectors"]]
                except (ValueError, KeyError, TypeError):
                    vectors=None
            if vectors is None:
                vectors=await self.embedder.embed([f"title: {d['title']} | text: {d['text']}" for d in docs])
                vectors=[normalize(v) for v in vectors]
            if len(vectors)!=len(docs) or len({len(v) for v in vectors})!=1:
                raise ValueError("Inconsistent document embeddings")
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=self.cache_path.parent,delete=False) as handle:
                    temp=Path(handle.name)
                    json.dump({"manifest":manifest,"vectors":vectors},handle,ensure_ascii=False)
                try:
                    os.replace(temp,self.cache_path)
                finally:
                    temp.unlink(missing_ok=True)
            self._docs,self._vectors,self._manifest=docs,vectors,manifest

    async def rank(self, query):
        await self.ensure_index()
        # Snapshot protects an in-flight search from a simultaneous index refresh.
        docs,vectors=self._docs,self._vectors
        embedded=await self.embedder.embed(["task: search result | query: " + query])
        if len(embedded)!=1:
            raise ValueError("Expected one query vector")
        vector=normalize(embedded[0])
        if len(vector)!=len(vectors[0]):
            raise ValueError("Query/document dimensions differ")
        hits=[{k:v for k,v in doc.items() if k!="text"} | {"score":sum(a*b for a,b in zip(vector,stored))}
              for doc,stored in zip(docs,vectors)]
        return sorted(hits,key=lambda d:(-d["score"],d["document_id"]))

    async def search(self, query):
        ranked=await self.rank(query)
        cutoff=max(self.min_score, ranked[0]["score"]-self.score_margin)
        return [dict(h,score=round(h["score"],6)) for h in ranked[:self.top_k] if h["score"]>=cutoff]


    async def search_request(self, query, name, targets, monitoring):
        """Search each explicit symptom separately; alerts cannot expand scope.

        This vocabulary covers the six supplied runbooks. Unknown expressions
        still use dense retrieval; TLS/certificate requests are unsupported.
        """
        clause = scoped_query(query, name, targets)
        scope = request_scope(clause)
        if not scope["included"] and not scope["excluded"] and not scope["unsupported"] and re.search(
                r"상태.*(?:확인|점검|조사)|각각.*상태", scope["positive"]):
            # Generic status requests use each system's own alert symptoms.
            scope["included"] = request_scope(" ".join(monitoring.get("alerts", [])))["included"]
            scope["source"] = "monitoring_alerts"
        hits, missing = {}, []
        if scope["included"]:
            for kind in scope["included"]:
                # Ranking remains embedding-based; no document is forced in.
                candidates = await self.search(TOPICS[kind][1])
                matches = [h for h in candidates if h["incident_type"] == kind]
                if not matches:
                    missing.append(kind)
                for hit in matches:
                    hits[hit["incident_type"]] = hit
        elif not scope["unsupported"]:
            # Explicit exclusions must not re-enter through monitoring alerts.
            positive = scope["positive"]
            generic = re.sub(r"[a-z][a-z0-9_-]*-service", "", positive, flags=re.I)
            generic = bool(re.fullmatch(r"[\s와과의,]*(?:상태|장애|문제|확인|조사|해주세요|알려주세요|해줘|해|줘|운영|점검|및|각각|을|를|이|가|는|은|에|서|좀|해|요|[\s,])+", generic))
            text = (system_search_query(query, name, targets, monitoring)
                    if generic and not scope["excluded"] else positive)
            for hit in await self.search(text):
                if hit["incident_type"] not in scope["excluded"]:
                    hits[hit["incident_type"]] = hit
        if scope["unsupported"]:
            missing.append("TLS 인증서: 현재 도구·매뉴얼로 만료 여부 확인 불가")
        return {"hits": list(hits.values()), "missing": missing,
                "scope": scope, "query": clause}
