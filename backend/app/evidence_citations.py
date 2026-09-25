"""Resolve citations through current PostgreSQL permissions, never index metadata."""
from uuid import UUID
from fastapi import HTTPException
from app.models import Document, DocumentVersion, DocumentChunk, ChunkingRun, ParseJob, KnowledgeBase


def resolve_citation(db, chunk_id: UUID, *, tenant_id: str, permission_scopes: list[str]):
    def unavailable():
        raise HTTPException(404, '引用不可用或无访问权限')
    chunk = db.get(DocumentChunk, chunk_id)
    if chunk is None: # chunk是否存在
        unavailable()
    version = db.get(DocumentVersion, chunk.document_version_id)
    if version is None: # chunk版本检查
        unavailable()
    doc = db.get(Document, version.document_id)

    # chunk 权限等检查
    if (doc is None or doc.tenant_id != tenant_id or doc.status != 'active'
            or doc.permission_scope not in permission_scopes
            or chunk.permission_scope not in permission_scopes
            or chunk.candidate_id != doc.candidate_id):
        unavailable()
    # chunk 是否在知识库内
    if doc.knowledge_base_id:
        kb = db.get(KnowledgeBase, doc.knowledge_base_id)
        if (kb is None or kb.tenant_id != tenant_id or kb.status != 'active'
                or kb.permission_scope not in permission_scopes):
            unavailable()
    # chunk 切片策略等查询
    run = db.get(ChunkingRun, chunk.chunking_run_id)
    job = db.get(ParseJob, run.parse_job_id) if run else None
    return {
        'citation_id': str(chunk.id), 'chunk_id': str(chunk.id),
        'candidate_id': doc.candidate_id, 'document_id': str(doc.id),
        'document_title': doc.title, 'document_version_id': str(version.id),
        'version_no': version.version_no,
        'version_state': 'current' if version.is_current else 'historical',
        'permission_scope': doc.permission_scope, 'content': chunk.content,
        'heading_path': chunk.heading_path, 'markdown_start': chunk.markdown_start,
        'markdown_end': chunk.markdown_end, 'source_locators': chunk.source_locators,
        'page_start': chunk.page_start, 'page_end': chunk.page_end,
        'timestamp_start': chunk.timestamp_start, 'timestamp_end': chunk.timestamp_end,
        'parser_version': job.parser_version if job else None,
        'parent_chunk_id': str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
    }


def load_pack_sources(db, chunks, *, tenant_id, permission_scopes):
    """Rehydrate trusted text and optionally add same-version parent context."""
    sources = {}
    for hit in chunks:
        try:
            row = resolve_citation(db, UUID(hit['chunk_id']), tenant_id=tenant_id,
                permission_scopes=permission_scopes)
        except (ValueError, HTTPException):
            continue
        if row['candidate_id'] != hit['candidate_id'] or row['version_state'] != 'current':
            continue
        expanded = [row]
        if row['parent_chunk_id']: # 尝试获取父chunk内容
            try:
                parent = resolve_citation(db, UUID(row['parent_chunk_id']), tenant_id=tenant_id,
                    permission_scopes=permission_scopes)
                if (parent['candidate_id'] == row['candidate_id']
                        and parent['document_version_id'] == row['document_version_id']
                        and len(parent['content']) <= 8000):
                    expanded.append(parent)
            except (ValueError, HTTPException):
                pass
        for entry in expanded:
            existing = sources.setdefault(entry['chunk_id'], {**entry, 'requirement_ids': []})
            existing['requirement_ids'] = sorted(set(existing['requirement_ids'] + hit['requirement_ids']))
    return list(sources.values())
