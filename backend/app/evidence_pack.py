"""Lesson 9: evidence organization. Model judgments remain reviewable hypotheses."""
from __future__ import annotations

import json
import logging
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FactSource(StrictModel):
    chunk_id: str
    quote: str = Field(min_length=1, max_length=2000)


class ExtractedFact(StrictModel):
    event: str = Field(min_length=1, max_length=200)    # 含义：项目、岗位或事项；缺失时：不同项目的职责可能被错误合并
    period: str = Field(pattern=r'^(?:unknown|\d{4}|\d{4}-\d{2}/\d{4}-\d{2})$') # 含义：事实生效时间；缺失时：不同时期的职责变化可能被判为冲突
    claim: str = Field(min_length=1, max_length=200)    # 含义：当前判断的命题；缺失时：不同措辞无法落到同一判断目标
    answer: Literal['yes', 'no']    # 含义：对命题的 yes/no 回答；缺失时：支持与明确否定无法统一比较
    sources: list[FactSource] = Field(min_length=1, max_length=24)  # 含义：事实无法回到材料核查；缺失时：Chunk ID 与连续原文


class EvidenceExtraction(StrictModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=30)
    fully_supported: bool = False # 模型认为材料是否直接覆盖当前要求的全部要素
    missing_information: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10) # 模型指出为了完整回答要求还缺什么，例如“需要核查项目职责记录”。列表为空表示模型没有报告缺口；它不是字符串，也不能用 [""] 充当空列表。非空会阻止状态成为 sufficient，除非更高优先级的冲突已经使状态成为 conflicting


EXTRACTION_PROMPT = """根据给定查询要求整理材料中的事实陈述。
只抽取与 requirement 相关的项目、时期、职责或成果，保留原文限定词，不补全未知事实。
event 填写材料明确出现的项目、岗位或事项名称。材料出现星河项目时填写星河项目，只有材料没有可识别事项时才填写 unknown。
period 对同一时间范围统一写成 YYYY-MM/YYYY-MM，例如 2025 年 1 月至 6 月写成 2025-01/2025-06。
claim 把查询要求改写成可回答是或否的命题，例如担任项目总负责人。
answer 仅使用 yes、no。明确满足命题为 yes，明确否定命题为 no，未回答命题的背景内容不生成事实。
同一事实的重复转述使用相同的 event、period、claim 和 answer。
每条事实必须有输入 chunk_id 和连续原文 quote，quote 不得改写。
fully_supported 仅表示当前材料直接覆盖当前要求全部要素，缺失、冲突时为 false。
没有缺失信息时 missing_information 必须返回空数组，不能返回空字符串。
原文引用正确不代表事实已经独立核实。不评分、不推荐候选人。"""


def model_extractor(model):
    # 关键事实提取函数
    # 输入单个 requirement 和 对应rows 输出我们规定的 EvidenceExtraction 证据抽取结构化对象
    def extract(requirement, sources):
        payload = {'requirement': requirement['query'], 'sources': [
            {'chunk_id': row['chunk_id'], 'content': row['content']} for row in sources]}
        return model.with_structured_output(EvidenceExtraction).invoke([
            ('system', EXTRACTION_PROMPT),
            ('user', json.dumps(payload, ensure_ascii=False)),
        ])
    return extract


def _validate_fact_sources(facts, sources):
    """Reject model facts that cannot point back to an input Chunk and exact quote."""
    # data.facts部分是LLM抽取的内容，rows是pg数据库读取的chunk文本块
    # 该方法是用于判断LLM抽取引用等信息是否和pg读取的文本块信息相符 (模型可能引用错误。这里做的是 来源校验)
    by_id = {row['chunk_id']: row for row in sources}
    validated = []
    for fact in facts:
        refs = []
        for ref in fact.sources:
            row = by_id.get(ref.chunk_id)
            if row is None or not ref.quote.strip() or ref.quote not in row['content']: #
                raise ValueError('unverifiable_source')
            mapped = {'citation_id': row['citation_id'], 'chunk_id': ref.chunk_id, 'quote': ref.quote}
            if mapped not in refs:
                refs.append(mapped)
        validated.append({**fact.model_dump(exclude={'sources'}), 'sources': refs})
    return validated


def _merge_duplicate_facts(facts):
    """Merge repeated descriptions while preserving every verified source."""
    merged, index_by_key = [], {}
    for fact in facts:
        # fact['event'], fact['period'], fact['claim'], fact['answer']四个参数作为唯一key，判断是否是同一事实，
        # 唯一key未放入chunk就能实现多chunk文本块同一事实的合并
        key = (fact['event'], fact['period'], fact['claim'], fact['answer'])
        if key not in index_by_key:
            index_by_key[key] = len(merged)
            merged.append({**fact, 'sources': []})
        index = index_by_key[key]
        for ref in fact['sources']:
            if ref not in merged[index]['sources']:
                merged[index]['sources'].append(ref)
    return merged


def _find_conflicts(facts):
    """Find yes/no answers about the same event, period and claim."""
    conflicts = []
    for left_index, left in enumerate(facts):
        for right_index in range(left_index + 1, len(facts)):
            right = facts[right_index]
            same_scope = all(left[key] == right[key] and left[key] != 'unknown'
                for key in ('event', 'period', 'claim'))
            opposite_answers = {left['answer'], right['answer']} == {'yes', 'no'}
            if same_scope and opposite_answers:
                conflicts.append([left_index, right_index])
    return conflicts


def _evidence_status(*, facts, conflicts, fully_supported, missing_information):
    if conflicts:
        return 'conflicting' # 1. 【存在冲突】例：conflicts = [[0,1]] 事实有冲突 存在yes and no
    if (any(fact['answer'] == 'yes' for fact in facts) and fully_supported
            and not missing_information):
        return 'sufficient' # 2. 【事实完全满足】材料直接完全支持要求，并且没有已知缺口或冲突
    if facts:
        return 'partial' # 3. 【事实部分满足】表示已经找到相关事实，但材料不完整，或者事实抽取失败
    return 'missing' # 4. 【完全没有事实材料】表示没有命中材料，或者命中内容没有回答当前命题


def build_evidence_packs(*, candidate_ids, requirements, sources, extract):
    packs = []
    for candidate_id in dict.fromkeys(candidate_ids):
        items = []
        for requirement in requirements:
            # 每一个requirement 对应的 rows
            rows = list({row['chunk_id']: row for row in sources
                if row['candidate_id'] == candidate_id
                and requirement['requirement_id'] in row['requirement_ids']}.values())
            item = {**requirement, 'status': 'missing', 'reason': 'no_accessible_hits',
                    'extraction_status': 'not_run', 'facts': [], 'conflicts': [],
                    'missing_information': [], 'citations': rows}
            if rows:
                try:
                    raw = extract(requirement, rows)
                    data = raw if isinstance(raw, EvidenceExtraction) else EvidenceExtraction.model_validate(raw)
                    # 先合并事实
                    facts = _merge_duplicate_facts(_validate_fact_sources(data.facts, rows)) # data.facts部分是LLM抽取的内容，rows是pg数据库读取的chunk文本块
                    # 再找上面合并后事实的 conflicts 冲突部分(主要是双层for循环，'event', 'period', 'claim'相等且'answer'冲突)
                    conflicts = _find_conflicts(facts)
                    status = _evidence_status(
                        facts=facts, # 合并后事实
                        conflicts=conflicts, # 冲突项
                        fully_supported=data.fully_supported,
                        missing_information=data.missing_information
                    )
                    item.update(status=status, reason='evidence_review' if facts else 'no_relevant_evidence',
                        extraction_status='succeeded', facts=facts, conflicts=conflicts,
                        missing_information=data.missing_information)
                except Exception as exc:
                    # Never return provider errors, raw prompts or document contents in errors.
                    logger.warning('evidence_extraction_failed type=%s', type(exc).__name__)
                    item.update(status='partial', reason='extraction_failed', extraction_status='failed')
            items.append(item)
        packs.append({'schema_version': '2.0', 'candidate_id': candidate_id, 'requirements': items})
    return packs
