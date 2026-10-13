import json,sys
from collections import Counter,defaultdict
from pathlib import Path
p=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent
rows=json.loads((p/'results.json').read_text(encoding='utf-8'))
checklists={c['case_id']:c['checks'] for c in json.loads((p/'review-checklist.json').read_text(encoding='utf-8'))['cases']}
decisions=json.loads((p/'manual-decisions.json').read_text(encoding='utf-8'))
reviews=[]
for r in rows:
 cid,strategy,attempt=r['case_id'],r['strategy'],str(r['attempt']);d=r['result'].get('display')
 decision=decisions.get(cid,{}).get(strategy,{}).get(attempt)
 verdicts=[]
 for check in checklists[cid]:
  short=check['id'].rsplit('-',1)[-1]
  reason=decision.get('fail',{}).get(short) if decision else None
  verdict='not_evaluated' if not d else 'pending_review' if not decision else 'fail' if reason else 'pass'
  verdicts.append({**check,'verdict':verdict,
    'review_note':reason or ('人工原文对照，含义在展示中保留；允许同义压缩及跨合理字段保留' if verdict=='pass' else None),
    'output_evidence_file':f"{cid}/attempt-{attempt}/{strategy}/result.json"})
 additional=decision.get('additional',[]) if decision else []
 failed=sum(v['verdict']=='fail' for v in verdicts)
 quality='not_evaluated' if not d else 'pending_review' if not decision else 'fail' if failed or any(a['severity']!='minor' for a in additional) else 'pass_targeted_review'
 reviews.append({'case_id':cid,'strategy':strategy,'attempt':int(attempt),'quality_grade':quality,
   'review_scope':'71项清单及人工发现的补充问题；专项通过不代表原文事实已核实或穷尽所有语义风险',
   'checks':verdicts,'additional_issues':additional})
grouped=defaultdict(list)
for row in reviews:grouped[(row['case_id'],row['strategy'])].append(row)
summary=[]
for (cid,strategy),rr in grouped.items():
 summary.append({'case_id':cid,'strategy':strategy,'samples':len(rr),'targeted_pass':sum(r['quality_grade']=='pass_targeted_review' for r in rr),
    'failed_samples':sum(r['quality_grade']=='fail' for r in rr),'pending_samples':sum(r['quality_grade']=='pending_review' for r in rr),
    'not_evaluated':sum(r['quality_grade']=='not_evaluated' for r in rr),
    'failed_facts':sum(sum(c['verdict']=='fail' for c in r['checks']) for r in rr),
    'additional_issues':sum(len(r['additional_issues']) for r in rr)})
result={'decision':'do_not_enable_split_in_production','reason':'Known omissions/unsupported statements remain; additional SDK request and latency do not meet acceptance.',
 'checklist_facts':sum(len(v) for v in checklists.values()),'samples':len(reviews),'summary':summary,'reviews':reviews}
(p/'semantic-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 人工原文对照结论','','当前独立全局提取候选不接入生产。以下仅为指定信息与补充问题的专项评审，不能代替旅行原文质量和扩展数据集验收。','',
 '| 场景 | 方案 | 专项通过/样本 | 未评审 | 清单遗漏或改义项 | 补充问题 |','| --- | --- | ---: | ---: | ---: | ---: |']
for r in summary:lines.append(f"| {r['case_id']} | {r['strategy']} | {r['targeted_pass']}/{r['samples']} | {r['pending_samples']} | {r['failed_facts']} | {r['additional_issues']} |")
lines+=['','## 逐样本问题','']
for r in reviews:
 problems=[c['id']+'：'+c['review_note'] for c in r['checks'] if c['verdict']=='fail']
 problems += [a['field']+'：'+a['finding'] for a in r['additional_issues']]
 if problems:lines+= [f"- {r['case_id']} {r['strategy']} 第{r['attempt']}次："+'；'.join(problems)]
(p/'semantic-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
