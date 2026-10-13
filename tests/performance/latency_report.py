"""Readable latency tables, flat CSV and an offline interactive timeline report."""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import statistics

LABELS = {
    'model_main': '意图识别模型', 'model_chat': '闲聊模型', 'model_research': '查询模型',
    'model_planning': '规划模型', 'model_revision': '修改模型', 'model_display': '展示模型',
    'model_overhead': '模型请求组织/兼容处理', 'external_io': '真实外部服务获取/解析',
    'controlled_io': '合成服务获取/解析', 'external_tool_overhead': '外部工具分发/整理',
    'internal_tool': '内部工具自身', 'context': '上下文准备/读取', 'validation': '程序校验',
    'commit': '保存边界/计划替换', 'serialization': '快照序列化', 'persistence': 'SQLite保存',
    'logging': '日志组织/写入', 'submission': '请求接受/入队', 'server_response': 'JSON响应',
    'render': '页面HTML响应', 'display_overhead': '展示后处理自身', 'orchestration': '调度/其余处理',
    'injected_wait': '受控故障等待', 'observer_wait': '测试程序等待（非用户等待）',
}


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    index = (len(values)-1)*q
    low = int(index)
    high = min(low+1, len(values)-1)
    return values[low] + (values[high]-values[low])*(index-low)


def statistics_row(values):
    return {'count': len(values), 'min': min(values) if values else None,
            'median': statistics.median(values) if values else None,
            'p90': percentile(values, .9), 'max': max(values) if values else None}


def aggregate(records):
    grouped = defaultdict(list)
    for row in records:
        grouped[(row['case_id'], row['step'])].append(row)
    result = []
    for (case, step), rows in grouped.items():
        categories = sorted({k for r in rows for k in r['timeline']['exclusive_seconds_by_category'] if k != 'observer_wait'})
        breakdown = {k: statistics.mean(r['timeline']['exclusive_seconds_by_category'].get(k, 0) for r in rows) for k in categories}
        success = [r for r in rows if r['outcome'] in {'succeeded', 'ready', 'diagnostic_completed'}]
        failed = [r for r in rows if r['outcome'] not in {'succeeded', 'ready', 'diagnostic_completed', 'needs_input'}]
        saved_gaps = [r['readiness']['prose_to_reply_store_seconds'] for r in rows if r['readiness']['prose_to_reply_store_seconds'] is not None]
        result.append({'case_id': case, 'step': step, 'title': rows[0]['title'], 'group': rows[0]['group'],
            'entry': rows[0]['entry'], 'samples': len(rows), 'outcomes': dict(Counter(r['outcome'] for r in rows)),
            'primary_wait_seconds': statistics_row([r['metrics']['primary_seconds'] for r in rows if r['metrics'].get('primary_seconds') is not None]),
            'all_wait_seconds': statistics_row([r['metrics']['seconds'] for r in rows]),
            'successful_wait_seconds': statistics_row([r['metrics']['seconds'] for r in success]),
            'failed_wait_seconds': [r['metrics']['seconds'] for r in failed],
            'prose_to_store_seconds': statistics_row(saved_gaps),
            'expectation_failures': sum(not r['expectation_checks_pass'] for r in rows),
            'display_fallbacks': sum((r['display_conversion'] or r['after']['plan']).get('display_status') == 'fallback'
                and (r['display_conversion'] is not None or r['metrics']['display_seconds'] > 0) for r in rows),
            'mean_exclusive_seconds': breakdown,
            'largest_category': max(breakdown, key=breakdown.get) if breakdown else None,
            'sdk_requests': statistics_row([r['metrics']['sdk_requests'] for r in rows]),
            'tool_calls': statistics_row([r['metrics']['tool_calls'] for r in rows])})
    return sorted(result, key=lambda r: (r['case_id'], r['step']))


def write_reports(output, metadata, records, failures):
    summary = {'metadata': metadata, 'samples': len(records), 'process_errors': failures,
        'quality_grade': 'not_evaluated', 'accounting_valid': all(r['timeline']['accounting_valid'] for r in records),
        'outcomes': dict(Counter(r['outcome'] for r in records)), 'cases': aggregate(records),
        'notes': ['Exclusive durations add within each lane, not across concurrent lanes.',
                  'Primary text readiness and full background completion are reported separately. Observer wait is not user latency.',
                  'Failed/fallback waits retained. needs_input is its own outcome.',
                  'P90 from small samples is descriptive, not a latency SLA.',
                  'SDK time cannot isolate provider queue, prefill, reasoning or decode time.',
                  'Browser-visible and first-token time not measured.']}
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output/'spans.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        fields = ['case_id', 'attempt', 'step', 'entry', 'outcome', 'model_mode', 'tools_mode',
                  'id', 'parent_id', 'lane', 'name', 'category', 'start_seconds', 'end_seconds', 'seconds', 'self_seconds', 'status', 'error_type', 'metadata']
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in records:
            for span in row['timeline']['spans']:
                writer.writerow({**{k: row[k] for k in fields if k in row}, **span,
                    'metadata': json.dumps(span['metadata'], ensure_ascii=False)})
    lines = ['# Agent 场景耗时报告', '',
        f"模型模式：{metadata['model_mode']}；工具模式：{metadata['tools_mode']}；模型：{metadata.get('model')}。",
        f"记录{len(records)}个处理步骤；进程错误{len(failures)}个。业务数据快照未变化：{metadata.get('business_db_unchanged')}。", '',
        '文字生成完成与保存/服务器响应就绪分别记录；这里没有测量实际浏览器可见时间和首token。', '',
        '| 场景 / 步骤 | 样本 | 文字首次响应中位数 | 含后台完成中位数 | P90 | 最大 | 状态 | 主要耗时类别 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |']
    for case in summary['cases']:
        stats = case['all_wait_seconds']
        category = case['largest_category']
        primary = case['primary_wait_seconds']['median']
        primary_text = f'{primary:.3f}s' if primary is not None else '不适用'
        lines.append(f"| {case['case_id']} / {case['step']} {case['title']} | {case['samples']} | {primary_text} | {stats['median']:.3f}s | {stats['p90']:.3f}s | {stats['max']:.3f}s | {json.dumps(case['outcomes'],ensure_ascii=False)} | {LABELS.get(category, category)} {case['mean_exclusive_seconds'].get(category,0):.3f}s |")
    lines += ['', '## 逐场景调用', '']
    for row in records:
        lines += [f"### {row['case_id']} 第{row['attempt']}次 / 步骤{row['step']}：{row['title']}", '',
            f"状态 `{row['outcome']}`，总耗时 {row['metrics']['seconds']:.3f}s；SDK请求 {row['metrics']['sdk_requests']} 次，工具 {row['metrics']['tool_calls']} 次，后端获取 {row['metrics']['backend_calls']} 次，缓存命中 {row['metrics']['cache_hits']} 次。", '',
            '| 开始 | 步骤 | 总耗时（含子调用） | 自身耗时 | 状态 |', '| ---: | --- | ---: | ---: | --- |']
        for span in row['timeline']['spans']:
            if span['name'].startswith(('SDK请求：', '工具：', '服务获取', 'SQLite', '消息JSON', '聊天片段', '行程页')):
                lines.append(f"| {span['start_seconds']:.3f}s | {span['name']} | {span['seconds']:.3f}s | {span['self_seconds']:.3f}s | {span['status']} |")
        lines += ['', '就绪时刻：`' + json.dumps(row['readiness'], ensure_ascii=False) + '`。', '']
    (output/'report.md').write_text('\n'.join(lines), encoding='utf-8')
    compact = [{k: r[k] for k in ('case_id','title','group','attempt','step','entry','input','model_mode','tools_mode',
        'outcome','expectation_checks','expectation_checks_pass','metrics','readiness','timeline','fault_hits')} for r in records]
    # Hide full workflow snapshots in the HTML payload; raw evidence retains them.
    for row in compact:
        row['timeline'] = {**row['timeline'], 'events': [
            {k: v for k, v in e.items() if k != 'value'} for e in row['timeline']['events']]}
    payload = json.dumps({'metadata': metadata, 'summary': summary, 'samples': compact, 'labels': LABELS}, ensure_ascii=False, default=str).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template = (Path(__file__).with_name('latency_report_template.html')).read_text(encoding='utf-8')
    (output/'report.html').write_text(template.replace('/*__LATENCY_DATA__*/', payload), encoding='utf-8')
    return summary
