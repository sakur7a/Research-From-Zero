"""Versioned research identities and source-backed knowledge records.

The existing `papers` row remains the compatibility-facing library item. Its id is
the stable Work id; PaperVersion and SourceSnapshot records are append-only
projections so a bibliography edit cannot rewrite earlier observations.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4
from urllib.parse import urlsplit

from .models import normalize_arxiv, safe_url

SYSTEM_TEMPLATE_OWNER = "__re0_system__"
LAYOUT_TEMPLATE_ID = "layout-layer-template-v1"

LAYOUT_TEMPLATE = {
    "template_key": "layout-layer",
    "version": 1,
    "name": "Layout 与图层研究",
    "description": "可编辑分类词表；标签不自动构成论文结论或关系。",
    "dimensions": [
        {"key": "direction", "label": "方向"},
        {"key": "task", "label": "任务"},
        {"key": "method", "label": "方法"},
        {"key": "input", "label": "输入"},
        {"key": "output", "label": "输出"},
        {"key": "experiment_condition", "label": "实验条件"},
    ],
    "concepts": [
        {"key": "layout_and_layers", "kind": "direction", "label": "Layout / 图层",
         "aliases": ["图层分解 / 生成", "布局与图层"], "parent": None},
        {"key": "layout_generation", "kind": "task", "label": "Layout 生成",
         "aliases": ["布局生成", "版面布局生成"], "parent": "layout_and_layers"},
        {"key": "layer_decomposition", "kind": "task", "label": "图层分解",
         "aliases": ["图层拆分", "layer decomposition"], "parent": "layout_and_layers"},
        {"key": "diffusion", "kind": "method", "label": "扩散模型",
         "aliases": ["diffusion"], "parent": "layout_and_layers"},
        {"key": "layout_graph", "kind": "input", "label": "布局图",
         "aliases": ["layout graph"], "parent": "layout_and_layers"},
        {"key": "editable_layers", "kind": "output", "label": "可编辑图层",
         "aliases": ["editable layers"], "parent": "layout_and_layers"},
        {"key": "dataset_split", "kind": "experiment_condition", "label": "数据划分",
         "aliases": ["data split"], "parent": "layout_and_layers"},
        {"key": "evaluation_protocol", "kind": "experiment_condition", "label": "评测协议",
         "aliases": ["evaluator", "评测设置"], "parent": "layout_and_layers"},
    ],
}

VERSION_METADATA_FIELDS = (
    "title", "authors", "year", "venue", "abstract", "doi", "arxiv_id",
    "paper_url", "version_label",
)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def metadata_snapshot(paper: dict) -> dict:
    """Bibliographic fields only; private notes, topics and reading state stay on Work."""
    return {key: paper.get(key) for key in VERSION_METADATA_FIELDS}


def paper_version_key(paper: dict) -> str:
    """Prefer explicit version identity; never infer a version from a title."""
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    version_label = str(paper.get("version_label") or "").strip()
    parts = []
    if arxiv_id:
        parts.append("arxiv:" + arxiv_id)
    if version_label:
        parts.append("label:" + version_label.casefold())
    if parts:
        return "|".join(parts)
    doi = str(paper.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    return "unversioned"


def fulltext_matches_version(fulltext: dict, version: dict) -> bool:
    """Bind imported text only when its archive identity and version match explicitly."""
    metadata = version.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    if not isinstance(metadata, dict):
        return False

    identifier = str(fulltext.get("identifier") or "").strip()
    source = str(fulltext.get("source") or "")
    if source == "arxiv":
        try:
            source_id = normalize_arxiv(identifier)
            target_id = normalize_arxiv(str(version.get("arxiv_id") or metadata.get("arxiv_id") or ""))
        except ValueError:
            return False
        suffix = re.compile(r"v(\d{1,3})$", re.I)
        source_version = suffix.search(source_id) or suffix.search(str(fulltext.get("version") or ""))
        target_version = suffix.search(target_id) or suffix.search(str(version.get("version_label") or ""))
        source_base, target_base = suffix.sub("", source_id), suffix.sub("", target_id)
        return bool(source_base and source_base == target_base and source_version and target_version
                    and source_version.group(0).lower() == target_version.group(0).lower())

    # ACL Anthology IDs identify an archived paper version in the paper URL itself. No title-based
    # match is allowed: a same-title item can be a different work.
    if source == "acl" and re.fullmatch(r"(?:[a-z][0-9]{2}-[0-9]{3,4}|[0-9]{4}\.[a-z0-9-]+\.[0-9]{1,4})",
                                        identifier, re.I):
        paper_url = str(metadata.get("paper_url") or "")
        path_id = urlsplit(paper_url).path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".pdf")
        return bool(path_id and path_id.casefold() == identifier.casefold())
    return False


def _safe_source_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return safe_url(value)
    except ValueError:
        # The snapshot remains useful as a record of what arrived, but it cannot
        # be selected as evidence for a relation unless its URL is safe.
        return ""


def ensure_work(con, work_id: str, owner: str, created_at: str) -> None:
    con.execute("INSERT OR IGNORE INTO works(id,owner,created_at) VALUES (?,?,?)",
                (work_id, owner, created_at))


def ensure_paper_version(con, work_id: str, owner: str, paper: dict, *, origin: str,
                         created_at: str, version_key: str | None = None,
                         is_current: bool = True) -> str:
    ensure_work(con, work_id, owner, created_at)
    key = version_key or paper_version_key(paper)
    existing = con.execute("SELECT id FROM paper_versions WHERE work_id=? AND version_key=?",
                           (work_id, key)).fetchone()
    if existing:
        version_id = existing[0]
    else:
        version_id = str(uuid4())
        snapshot = metadata_snapshot(paper)
        con.execute("INSERT INTO paper_versions(id,work_id,version_key,arxiv_id,version_label,"
                    "doi,metadata,origin,created_at,is_current) VALUES (?,?,?,?,?,?,?,?,?,0)",
                    (version_id, work_id, key, snapshot.get("arxiv_id") or "",
                     snapshot.get("version_label") or "", snapshot.get("doi") or "",
                     encode(snapshot), origin[:40], created_at))
    if is_current:
        con.execute("UPDATE paper_versions SET is_current=CASE WHEN id=? THEN 1 ELSE 0 END "
                    "WHERE work_id=?", (version_id, work_id))
    return version_id


def sync_topic_assignments(con, paper_id: str, owner: str, labels: list[str],
                           created_at: str) -> None:
    """Keep the legacy string list as a projection without erasing versioned memberships."""
    wanted = list(dict.fromkeys(str(label).strip() for label in labels if str(label).strip()))
    existing = {row["label"] for row in con.execute(
        "SELECT label FROM paper_topic_assignments WHERE paper_id=?", (paper_id,))}
    for label in existing - set(wanted):
        con.execute("DELETE FROM paper_topic_assignments WHERE paper_id=? AND label=?",
                    (paper_id, label))
    template = con.execute("SELECT id,definition FROM topic_template_versions "
                           "WHERE id=? AND owner=?", (LAYOUT_TEMPLATE_ID, SYSTEM_TEMPLATE_OWNER)).fetchone()
    aliases = {}
    if template:
        for concept in json.loads(template["definition"]).get("concepts", []):
            aliases[concept["label"].strip().casefold()] = concept["key"]
            for alias in concept.get("aliases") or []:
                aliases[alias.strip().casefold()] = concept["key"]
    for label in wanted:
        if label in existing:
            continue
        key = aliases.get(label.casefold())
        con.execute("INSERT OR IGNORE INTO paper_topic_assignments(id,owner,paper_id,template_version_id,"
                    "concept_key,label,source_kind,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid4()), owner, paper_id, template["id"] if key and template else None,
                     key, label, "manual_edit", created_at))


def append_source_snapshot(con, *, work_id: str, owner: str, kind: str,
                           payload: dict, retrieved_at: str, source_url: str = "",
                           locator: str = "", paper_version_id: str | None = None,
                           observation_id: int | None = None) -> str:
    if not con.execute("SELECT 1 FROM works WHERE id=? AND owner=?", (work_id, owner)).fetchone():
        raise ValueError("SourceSnapshot 的 Work 不属于当前账户")
    source_id = str(uuid4())
    body = encode(payload)
    con.execute("INSERT INTO source_snapshots(id,owner,work_id,paper_version_id,snapshot_kind,"
                "source_url,locator,payload,content_sha256,retrieved_at,observation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, owner, work_id, paper_version_id, kind[:40],
                 _safe_source_url(source_url), locator[:1000], body,
                 hashlib.sha256(body.encode("utf-8")).hexdigest(), retrieved_at, observation_id))
    return source_id


def _snapshot_version(paper: dict, observation: dict) -> tuple[dict, str] | None:
    raw = observation.get("paper_version_snapshot")
    if isinstance(raw, dict):
        snapshot = {**paper, **{key: value for key, value in raw.items()
                               if key in {"doi", "arxiv_id", "version_label"}}}
        return snapshot, paper_version_key(snapshot)
    if isinstance(raw, str) and raw.strip():
        snapshot = {**paper, "version_label": raw.strip(), "arxiv_id": ""}
        return snapshot, "label:" + raw.strip().casefold()
    audit = observation.get("resource_audit") or observation
    explicit = str(audit.get("work_version") or "").strip() if isinstance(audit, dict) else ""
    if explicit:
        snapshot = {**paper, "version_label": explicit, "arxiv_id": ""}
        return snapshot, "audit:" + explicit.casefold()
    return None


def _observation_source(observation: dict, resource: dict) -> tuple[str, str]:
    candidates = [observation.get("evidence"),
                  (observation.get("resource_audit") or {}).get("evidence")
                  if isinstance(observation.get("resource_audit"), dict) else None]
    for group in candidates:
        if isinstance(group, list):
            for item in group:
                if isinstance(item, dict) and item.get("source_url"):
                    return str(item["source_url"]), str(item.get("locator") or item.get("category") or "")
    return str(resource.get("url") or ""), "resource observation"


def append_observation_snapshot(con, *, observation_id: int, work_id: str, owner: str,
                                paper: dict, resource: dict, observation: dict,
                                retrieved_at: str) -> str:
    version_snapshot = _snapshot_version(paper, observation)
    version_id = None
    if version_snapshot:
        snapshot_paper, key = version_snapshot
        version_id = ensure_paper_version(con, work_id, owner, snapshot_paper,
                                          origin="resource_observation",
                                          created_at=retrieved_at, version_key=key,
                                          is_current=False)
    source_url, locator = _observation_source(observation, resource)
    return append_source_snapshot(con, work_id=work_id, owner=owner,
                                  paper_version_id=version_id, kind="resource_observation",
                                  payload=observation, retrieved_at=retrieved_at,
                                  source_url=source_url, locator=locator,
                                  observation_id=observation_id)


def migrate_v3(con) -> None:
    """Add identity/version/snapshot projections and a versioned starter taxonomy."""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS works (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(id) REFERENCES papers(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS works_owner ON works(owner,id);

    CREATE TABLE IF NOT EXISTS paper_versions (
      id TEXT PRIMARY KEY,
      work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
      version_key TEXT NOT NULL,
      arxiv_id TEXT NOT NULL DEFAULT '',
      version_label TEXT NOT NULL DEFAULT '',
      doi TEXT NOT NULL DEFAULT '',
      metadata TEXT NOT NULL,
      origin TEXT NOT NULL,
      created_at TEXT NOT NULL,
      is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
      UNIQUE(work_id,version_key)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_current_paper_version ON paper_versions(work_id) WHERE is_current=1;

    CREATE TABLE IF NOT EXISTS source_snapshots (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
      paper_version_id TEXT REFERENCES paper_versions(id) ON DELETE SET NULL,
      snapshot_kind TEXT NOT NULL,
      source_url TEXT NOT NULL DEFAULT '',
      locator TEXT NOT NULL DEFAULT '',
      payload TEXT NOT NULL,
      content_sha256 TEXT NOT NULL,
      retrieved_at TEXT NOT NULL,
      observation_id INTEGER UNIQUE REFERENCES observations(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS source_snapshots_work ON source_snapshots(owner,work_id,retrieved_at,id);

    CREATE TABLE IF NOT EXISTS topic_template_versions (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      template_key TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version>0),
      name TEXT NOT NULL,
      definition TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(owner,template_key,version)
    );

    CREATE TABLE IF NOT EXISTS paper_topic_assignments (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
      template_version_id TEXT REFERENCES topic_template_versions(id) ON DELETE RESTRICT,
      concept_key TEXT,
      label TEXT NOT NULL,
      source_kind TEXT NOT NULL,
      created_at TEXT NOT NULL,
      CHECK((template_version_id IS NULL AND concept_key IS NULL) OR
            (template_version_id IS NOT NULL AND concept_key IS NOT NULL))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS paper_topic_manual_unique ON paper_topic_assignments(paper_id,label)
      WHERE template_version_id IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS paper_topic_template_unique ON paper_topic_assignments(
      paper_id,template_version_id,concept_key) WHERE template_version_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS paper_topics_owner ON paper_topic_assignments(owner,paper_id);

    CREATE TABLE IF NOT EXISTS research_relations (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
      paper_version_id TEXT REFERENCES paper_versions(id) ON DELETE SET NULL,
      relation_type TEXT NOT NULL CHECK(relation_type IN
        ('uses_method','evaluated_on','has_resource','claim')),
      target TEXT NOT NULL,
      statement TEXT NOT NULL,
      conditions TEXT NOT NULL DEFAULT '',
      assertion_kind TEXT NOT NULL CHECK(assertion_kind IN
        ('author_statement','model_inference','human_confirmation')),
      source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE RESTRICT,
      locator TEXT NOT NULL,
      created_at TEXT NOT NULL,
      supersedes_id TEXT REFERENCES research_relations(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS research_relations_work ON research_relations(owner,work_id,created_at);
    """)

    template_json = encode(LAYOUT_TEMPLATE)
    con.execute("INSERT OR IGNORE INTO topic_template_versions(id,owner,template_key,version,name,"
                "definition,created_at) VALUES (?,?,?,?,?,?,?)",
                (LAYOUT_TEMPLATE_ID, SYSTEM_TEMPLATE_OWNER, LAYOUT_TEMPLATE["template_key"], 1,
                 LAYOUT_TEMPLATE["name"], template_json, "2026-09-23T00:00:00+00:00"))
    aliases = {}
    for concept in LAYOUT_TEMPLATE["concepts"]:
        aliases[concept["label"].strip().casefold()] = concept["key"]
        for alias in concept["aliases"]:
            aliases[alias.strip().casefold()] = concept["key"]

    papers = list(con.execute("SELECT id,owner,data,created_at FROM papers ORDER BY id"))
    for row in papers:
        paper = json.loads(row["data"])
        ensure_work(con, row["id"], row["owner"], row["created_at"])
        version_id = ensure_paper_version(con, row["id"], row["owner"], paper,
                                           origin="migration", created_at=row["created_at"])
        snapshot = metadata_snapshot(paper)
        existing_snapshot = con.execute(
            "SELECT 1 FROM source_snapshots WHERE work_id=? AND snapshot_kind=? AND payload=? LIMIT 1",
            (row["id"], "legacy_paper_metadata", encode(snapshot))).fetchone()
        if not existing_snapshot:
            append_source_snapshot(con, work_id=row["id"], owner=row["owner"],
                                   paper_version_id=version_id, kind="legacy_paper_metadata",
                                   payload=snapshot, retrieved_at=row["created_at"],
                                   source_url=paper.get("paper_url") or "",
                                   locator="legacy paper row")
        for label in paper.get("topics") or []:
            concept_key = aliases.get(str(label).strip().casefold())
            con.execute("INSERT OR IGNORE INTO paper_topic_assignments(id,owner,paper_id,"
                        "template_version_id,concept_key,label,source_kind,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (str(uuid4()), row["owner"], row["id"],
                         LAYOUT_TEMPLATE_ID if concept_key else None, concept_key, str(label),
                         "legacy_topic", row["created_at"]))

    observations = list(con.execute(
        "SELECT o.id,o.data,o.checked_at,r.id AS resource_id,r.paper_id,r.data AS resource_data "
        "FROM observations o JOIN resources r ON r.id=o.resource_id ORDER BY o.id"))
    paper_by_id = {row["id"]: (row["owner"], json.loads(row["data"])) for row in papers}
    for row in observations:
        if con.execute("SELECT 1 FROM source_snapshots WHERE observation_id=?",
                       (row["id"],)).fetchone():
            continue
        owner, paper = paper_by_id[row["paper_id"]]
        observation = json.loads(row["data"])
        resource = json.loads(row["resource_data"])
        version_snapshot = _snapshot_version(paper, observation)
        version_id = None
        if version_snapshot:
            snapshot_paper, key = version_snapshot
            version_id = ensure_paper_version(con, row["paper_id"], owner, snapshot_paper,
                                              origin="legacy_observation_snapshot",
                                              created_at=row["checked_at"], version_key=key,
                                              is_current=False)
        source_url, locator = _observation_source(observation, resource)
        append_source_snapshot(con, work_id=row["paper_id"], owner=owner,
                               paper_version_id=version_id, kind="legacy_resource_observation",
                               payload=observation, retrieved_at=row["checked_at"],
                               source_url=source_url, locator=locator,
                               observation_id=row["id"])
