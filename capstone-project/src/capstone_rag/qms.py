"""Load the fictional QMS corpus and split it into retrievable chunks.

Chunking strategy: one chunk per populated `##` or `###` section, preserving
the heading path. This keeps a specific troubleshooting step small enough to
retrieve while retaining its parent context. Every chunk inherits the parent
document's control and audience metadata, so the validator can judge it
without re-reading the source file.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    procedure_id: str
    title: str
    model: str
    applicable_revisions: list
    document_version: str
    approval_status: str
    audiences: list
    section: str
    section_path: str
    heading_level: int
    text: str
    source_path: str
    effective_date: Optional[str] = None
    superseded_by: Optional[str] = None
    document_type: str = 'customer_troubleshooting_instruction'
    access_permissions: list = field(default_factory=lambda: ['customer_disclosable', 'agent_internal', 'representative'])
    permitted_uses: list = field(default_factory=lambda: ['customer_answer', 'revision_troubleshooting'])
    scope: str = 'exact_model_revision'
    revision_scope: str = 'listed_revisions'
    owning_function: str = 'Customer Service'
    required_approvals: list = field(default_factory=list)
    safety_restrictions: list = field(default_factory=lambda: ['stop_on_harm_or_hazard'])
    workflow_associations: list = field(default_factory=list)
    workflow_actions: list = field(default_factory=list)
    supersedes: Optional[str] = None
    # Controlled-document relationships used by QMS conflict review: the
    # decision a procedure answers (topic) and an explicit precedence link.
    topic: Optional[str] = None
    precedence_over: list = field(default_factory=list)

    @property
    def embed_text(self) -> str:
        """What actually gets embedded.

        The model and revision are prepended so that the vector carries some
        device context, but this is NOT a substitute for the deterministic
        metadata filter - it only nudges ranking.
        """
        return (f"{self.title}. Model {self.model} "
                f"revision {'/'.join(self.applicable_revisions)}. "
                f"Audience {'/'.join(self.audiences)}. "
                f"{self.section_path}. {self.text}")

    def as_dict(self):
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "procedure_id": self.procedure_id,
            "title": self.title,
            "model": self.model,
            "applicable_revisions": self.applicable_revisions,
            "document_version": self.document_version,
            "approval_status": self.approval_status,
            "audiences": self.audiences,
            "section": self.section,
            "section_path": self.section_path,
            "heading_level": self.heading_level,
            **{key: getattr(self, key) for key in (
                'document_type', 'access_permissions', 'permitted_uses', 'scope',
                'revision_scope', 'owning_function', 'required_approvals',
                'safety_restrictions', 'workflow_associations', 'workflow_actions',
                'effective_date', 'supersedes', 'superseded_by', 'approval_status')},
        }


_SCALAR_LIST = re.compile(r"^\[(.*)\]$")


def _parse_frontmatter(raw: str) -> dict:
    """Minimal frontmatter parser for the flat key: value blocks used here."""
    meta = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()

        m = _SCALAR_LIST.match(value)
        if m:
            inner = m.group(1).strip()
            meta[key] = [v.strip().strip("\"'") for v in inner.split(",") if v.strip()]
            continue

        if value.lower() in ("null", "none", ""):
            meta[key] = None
            continue

        meta[key] = value.strip("\"'")
    return meta


def _split_sections(body: str):
    """Yield populated sections with their `##`/`###` heading hierarchy."""
    headings = list(re.finditer(r"^(#{2,3})\s+(.+?)\s*$", body,
                                flags=re.MULTILINE))
    parents = {}
    for i, match in enumerate(headings):
        level = len(match.group(1))
        title = match.group(2).strip()
        parents[level] = title
        for deeper in tuple(k for k in parents if k > level):
            parents.pop(deeper)
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        text = body[start:end].strip()
        if text:
            path = " > ".join(parents[k] for k in sorted(parents))
            yield title, path, level, text


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40]


def load_chunks(qms_dir: Optional[str] = None) -> list:
    """Load every QMS document and return its section chunks."""
    from . import remote_services
    if qms_dir is None and remote_services.configured('qms'):
        documents=remote_services.get('qms','/api/qms/files')['documents'];chunks=[]
        for document in documents:
            raw=document['content'];_,fm,body=raw.split('---',2);meta=_parse_frontmatter(fm)
            for n,(section,section_path,heading_level,text) in enumerate(_split_sections(body),start=1):
                chunks.append(Chunk(chunk_id=f"{meta['document_id']}#S{n}-{_slug(section)}",document_id=meta['document_id'],procedure_id=meta['procedure_id'],title=meta['title'],model=meta['model'],applicable_revisions=list(meta['applicable_revisions']),document_version=str(meta['document_version']),approval_status=meta['approval_status'],audiences=list(meta['audiences']),section=section,section_path=section_path,heading_level=heading_level,text=text,source_path='remote-qms/'+document['name'],effective_date=meta.get('effective_date'),superseded_by=meta.get('superseded_by'),document_type=meta.get('document_type','customer_troubleshooting_instruction'),access_permissions=meta.get('access_permissions',['customer_disclosable','agent_internal','representative']),permitted_uses=meta.get('permitted_uses',['customer_answer','revision_troubleshooting']),scope=meta.get('scope','exact_model_revision'),revision_scope=meta.get('revision_scope','listed_revisions'),owning_function=meta.get('owning_function',meta.get('owner','Customer Service')),required_approvals=meta.get('required_approvals',[]),safety_restrictions=meta.get('safety_restrictions',['stop_on_harm_or_hazard']),workflow_associations=meta.get('workflow_associations',[]),workflow_actions=meta.get('workflow_actions',[]),supersedes=meta.get('supersedes'),topic=meta.get('topic'),precedence_over=list(meta.get('precedence_over') or [])))
        return chunks
    qms_dir = qms_dir or config.QMS_DIR
    chunks = []

    for path in sorted(glob.glob(os.path.join(qms_dir, "*.md"))):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()

        if not raw.startswith("---"):
            raise ValueError(f"{path}: missing frontmatter block")
        _, fm, body = raw.split("---", 2)
        meta = _parse_frontmatter(fm)

        required = ("document_id", "procedure_id", "model", "applicable_revisions",
                    "document_version", "approval_status", "audiences", "title")
        missing = [k for k in required if not meta.get(k)]
        if missing:
            raise ValueError(f"{path}: frontmatter missing {missing}")

        for n, (section, section_path, heading_level, text) in enumerate(
                _split_sections(body), start=1):
            chunks.append(
                Chunk(
                    chunk_id=f"{meta['document_id']}#S{n}-{_slug(section)}",
                    document_id=meta["document_id"],
                    procedure_id=meta["procedure_id"],
                    title=meta["title"],
                    model=meta["model"],
                    applicable_revisions=list(meta["applicable_revisions"]),
                    document_version=str(meta["document_version"]),
                    approval_status=meta["approval_status"],
                    audiences=list(meta["audiences"]),
                    section=section,
                    section_path=section_path,
                    heading_level=heading_level,
                    text=text,
                    source_path=os.path.relpath(path, config.PROJECT_ROOT),
                    effective_date=meta.get("effective_date"),
                    superseded_by=meta.get("superseded_by"),
                    document_type=meta.get('document_type', 'superseded_historical' if meta['approval_status'] == 'Superseded' else 'qualified_service_instruction' if 'service' in meta['audiences'] else 'customer_troubleshooting_instruction'),
                    access_permissions=meta.get('access_permissions', ['audit_only'] if 'audit_only' in meta['audiences'] else ['qualified_service_only', 'representative'] if 'service' in meta['audiences'] else ['customer_disclosable', 'agent_internal', 'representative']),
                    permitted_uses=meta.get('permitted_uses', ['historical_review'] if 'audit_only' in meta['audiences'] else ['qualified_service', 'representative_reference'] if 'service' in meta['audiences'] else ['customer_answer', 'revision_troubleshooting']),
                    scope=meta.get('scope', 'exact_model_revision'),
                    revision_scope=meta.get('revision_scope', 'listed_revisions'),
                    owning_function=meta.get('owning_function', meta.get('owner', 'Customer Service')),
                    required_approvals=meta.get('required_approvals', []),
                    safety_restrictions=meta.get('safety_restrictions', ['stop_on_harm_or_hazard']),
                    workflow_associations=meta.get('workflow_associations', []),
                    workflow_actions=meta.get('workflow_actions', []),
                    supersedes=meta.get('supersedes'),
                    topic=meta.get('topic'),
                    precedence_over=list(meta.get('precedence_over') or []),
                )
            )

    if not chunks:
        raise ValueError(f"no QMS documents found in {qms_dir}")
    return chunks


def load_compatibility(path: Optional[str] = None) -> dict:
    """model|revision -> list of authorised procedure_ids."""
    from . import remote_services
    if path is None and remote_services.configured('qms'):
        return remote_services.get('qms','/api/qms/compatibility')['compatibility']
    path = path or config.COMPATIBILITY_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["compatibility"]
