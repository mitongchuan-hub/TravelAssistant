"""Review fixed display outputs. Checks are targeted observations, not semantic grades."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/datasets/display_semantics_v1/cases.json'


def compact(value):
    return re.sub(r'\s+', '', value or '')


def inspect_display(case, result):
    display = result.get('display')
    if not display:
        return {'available': False, 'observations': [], 'quality_grade': 'not_evaluated'}
    observations = []
    def observe(name, passed, detail):
        observations.append({'check': name, 'pass': bool(passed), 'detail': detail})
    clothing = compact(json.dumps(display['preparation']['clothing'], ensure_ascii=False))
    for alternatives in case['clothing_groups']:
        observe('clothing:' + '/'.join(alternatives), any(word in clothing for word in alternatives),
                '原文装备应出现在衣物准备字段；词语检查只覆盖指定表达。')
    observe('overview_complete', len(compact(display.get('overview'))) >= 12, '检查已知单字残句，不证明概览全面。')
    all_text = compact(json.dumps(display, ensure_ascii=False))
    observe('budget_amount', case['budget_amount'] in all_text, '检查总预算数字是否仍出现，不证明预算口径完整。')
    observe('no_technical_notes', 'options_detail' not in all_text, '检查上轮已知技术标签。')
    items = [item for day in display['days'] for item in day['items']]
    for clock in case['approximate_times']:
        matching = [i for i in items if clock in compact(i.get('time_text'))]
        observe('approximate_time:' + clock, bool(matching) and all('左右' in i['time_text'] for i in matching),
                '此固定原文对应时刻明确带左右，不应压成精确时间。')
    for item in items:
        title = item['title']
        # Mixed check-out/breakfast and activity/rest items require manual review.
        expected = None
        if any(word in title for word in ('午餐', '晚餐', '早餐')) and not any(word in title for word in ('退房', '散步')):
            expected = 'meal'
        elif '湖滨公园散步' in title and not any(word in title for word in ('休息', '休整')):
            expected = 'activity'
        if expected:
            observe('category:' + title, item['category'] == expected, f'固定样本明确主目的预期为{expected}。')
        for phrase in case['forbidden_duration_phrases']:
            if phrase in compact(item.get('duration_text')):
                observe('invented_duration:' + title, False, '原文未给出该时长：' + phrase)
    return {'available': True, 'observations': observations,
            'flag_count': sum(not o['pass'] for o in observations), 'quality_grade': 'pending_manual_review'}


def build_report(before, after, output, fixtures=FIXTURES):
    cases = {c['id']: c for c in json.loads(Path(fixtures).read_text(encoding='utf-8'))['cases']}
    records = []
    for label, path in [('before', before), ('after', after)]:
        if path is None:
            continue
        for row in json.loads(Path(path).read_text(encoding='utf-8')):
            case = cases[row['case_id']]
            if row['source_text'] != case['source_text']:
                raise ValueError('Source changed: ' + case['id'])
            records.append({'phase': label, 'case_id': case['id'], 'attempt': row['attempt'],
                'seconds': row['seconds'], 'display_status': row['result']['display_status'],
                'error_type': row['result'].get('error_type'), **inspect_display(case, row['result'])})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/'checks.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    grouped = defaultdict(list)
    for row in records:
        grouped[(row['phase'], row['case_id'])].append(row)
    lines = ['# 固定原文语义专项对照', '',
        '指定词语和字段检查仅用于发现已知问题；通过不代表语义全部通过。失败请求不计为内容通过或内容失败。', '',
        '| 阶段 | 场景 | 样本 | 有效卡片 | 专项问题数 | 耗时中位数 |', '| --- | --- | ---: | ---: | ---: | ---: |']
    for (phase, cid), rows in grouped.items():
        lines.append(f"| {phase} | {cid} | {len(rows)} | {sum(r['available'] for r in rows)} | "
                     f"{sum(r.get('flag_count',0) for r in rows)} | {statistics.median(r['seconds'] for r in rows):.2f}s |")
    lines += ['', '## 需核对的输出', '']
    for row in records:
        failures = [o for o in row['observations'] if not o['pass']]
        if failures or not row['available']:
            lines += [f"- {row['phase']} {row['case_id']} 第{row['attempt']}次：" +
                      ('；'.join(o['check'] for o in failures) if row['available'] else '无有效卡片：' + str(row['error_type']))]
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'samples': len(records), 'report': str(output/'report.md')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--after', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build_report(args.before, args.after, args.output)
