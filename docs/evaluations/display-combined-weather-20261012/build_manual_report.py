import json, sys
from collections import defaultdict
from pathlib import Path

p = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent
rows = json.loads((p/'results.json').read_text(encoding='utf-8'))
main = {c['case_id']: c['checks'] for c in json.loads((p/'review-checklist.json').read_text(encoding='utf-8'))['cases']}
weather = {c['case_id']: c['checks'] for c in json.loads((p/'weather-review-checklist.json').read_text(encoding='utf-8'))['cases']}
decisions = json.loads((p/'manual-decisions.json').read_text(encoding='utf-8'))
reviews = []
for row in rows:
    cid, attempt = row['case_id'], str(row['attempt'])
    decision = decisions[cid][attempt]
    result_checks = []
    for kind, checklist in [('main', main[cid]), ('weather', weather[cid])]:
        for check in checklist:
            if check['source_quote'] not in row['source_text']:
                raise ValueError('Missing source quote: '+check['id'])
            short = check['id'].rsplit('-', 1)[-1]
            failure = decision['fail' if kind=='main' else 'weather_fail'].get(short)
            ambiguous = decision.get('ambiguous', {}).get(short) if kind=='main' else None
            verdict = 'not_evaluated' if not row['result'].get('display') else 'fail' if failure else 'ambiguous' if ambiguous else 'pass'
            result_checks.append({**check, 'verdict': verdict,
                'review_note': failure or ambiguous or '人工原文对照，信息含义保留；允许合理跨字段、去重与同义压缩',
                'output_evidence_file': f"{cid}/attempt-{attempt}/combined/result.json"})
    additional = decision['additional']
    failed = any(c['verdict']=='fail' for c in result_checks) or any(a['severity'] in ['major', 'critical'] for a in additional)
    ambiguous = any(c['verdict']=='ambiguous' for c in result_checks)
    grade = 'not_evaluated' if not row['result'].get('display') else 'fail' if failed else 'ambiguous' if ambiguous else 'pass_targeted_review'
    reviews.append({'case_id': cid, 'attempt': int(attempt), 'quality_grade': grade,
        'scope': '71项主清单+18项天气清单与补充观察，不证明源行程事实已核实或穷尽语义正确',
        'checks': result_checks, 'additional_issues': additional})
groups = defaultdict(list)
for row in reviews:
    groups[row['case_id']].append(row)
summary = [{'case_id': cid, 'samples': len(rr),
    'targeted_pass': sum(r['quality_grade']=='pass_targeted_review' for r in rr),
    'fail': sum(r['quality_grade']=='fail' for r in rr),
    'ambiguous': sum(r['quality_grade']=='ambiguous' for r in rr),
    'failed_checks': sum(sum(c['verdict']=='fail' for c in r['checks']) for r in rr),
    'weather_failed_checks': sum(sum(c['verdict']=='fail' and '-W' in c['id'] for c in r['checks']) for r in rr)
    } for cid,rr in groups.items()]
result = {'decision': 'continue_single_request_candidate_no_production_switch',
    'main_facts': 71, 'weather_facts': 18, 'reviewed_observations': sum(len(r['checks']) for r in reviews),
    'summary': summary, 'reviews': reviews,
    'note': 'Three-section coverage improved on fixed sources; temporal inference, qualifier scope and other issues remain.'}
(p/'semantic-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
lines = ['# 合并单次提取人工评审', '',
    '当前候选值得继续验证，但尚未切换生产。三类信息覆盖和整体语义分别判断；专项通过不证明全部内容正确。', '',
    '| 场景 | 专项通过/样本 | 失败 | 歧义 | 清单失败项 | 天气事实失败项 |',
    '| --- | ---: | ---: | ---: | ---: | ---: |']
for r in summary:
    lines.append(f"| {r['case_id']} | {r['targeted_pass']}/{r['samples']} | {r['fail']} | {r['ambiguous']} | {r['failed_checks']} | {r['weather_failed_checks']} |")
lines += ['', '## 逐样本问题', '']
for r in reviews:
    problems = [c['id']+'：'+c['review_note'] for c in r['checks'] if c['verdict'] in ['fail','ambiguous']]
    problems += [a['field']+'：'+a['finding'] for a in r['additional_issues']]
    if problems:
        lines.append(f"- {r['case_id']} 第{r['attempt']}次："+'；'.join(problems))
(p/'semantic-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'samples':len(rows),'reviewed_observations':result['reviewed_observations'],'summary':summary},ensure_ascii=False))
