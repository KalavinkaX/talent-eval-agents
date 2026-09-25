from app.evidence_pack import build_evidence_packs

# 输入顺序很重要：它决定合并后事实索引；本实验不提前排序。
rows = [
    ('x0', '星河', '2025', '总负责人', 'yes', '星河项目2025年C901担任总负责人。'),
    ('x1', '远航', '2025', '技术负责人', 'yes', '远航项目2025年C901担任技术负责人。'),
    ('x2', '星河', '2025', '总负责人', 'no', '星河项目2025年C901未担任总负责人。'),
    ('x3', '远航', '2024', '技术负责人', 'no', '远航项目2024年C901未担任技术负责人。'),
    ('x4', '星河', '2025', '总负责人', 'yes', '新项目复盘：星河项目2025年C901担任总负责人。'),
]

sources = [
    {'chunk_id': cid, 'candidate_id': 'C901', 'citation_id': cid,
     'content': text, 'requirement_ids': ['S1']}
    for cid, event, period, claim, answer, text in rows
]
# 代表从五个 Chunk 分别得到的结构化抽取结果；用固定结果锁定后处理行为。
facts = [
    {'event': event, 'period': period, 'claim': claim, 'answer': answer,
     'sources': [{'chunk_id': cid, 'quote': text}]}
    for cid, event, period, claim, answer, text in rows
]
pack = build_evidence_packs(
    candidate_ids=['C901'],
    requirements=[{'requirement_id': 'S1', 'query': '核对项目负责人记录'}],
    sources=sources,
    extract=lambda requirement, input_chunks: {
        'facts': facts, 'fully_supported': False, 'missing_information': []
    },
)[0]['requirements'][0]

for i, f in enumerate(pack['facts']):
    print(i, f['event'], f['period'], f['claim'], f['answer'],
          [src['chunk_id'] for src in f['sources']])
print('conflicts=', pack['conflicts'], 'status=', pack['status'])
assert pack['extraction_status'] == 'succeeded'
assert len(pack['facts']) == 4
assert [s['chunk_id'] for s in pack['facts'][0]['sources']] == ['x0', 'x4']
assert pack['conflicts'] == [[0, 2]]
assert pack['status'] == 'conflicting'