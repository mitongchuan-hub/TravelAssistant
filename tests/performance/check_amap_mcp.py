"""Read-only AMap MCP smoke test: discovery, adapters and trusted-POI route cache."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from app.tools import amap_mcp
    from app.tools.registry import run_tool
    from app.places import collect_places
    output=args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be empty; previous failures are preserved')
    output.mkdir(parents=True,exist_ok=True)
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ['app/tools/amap_mcp.py','app/tools/places.py','app/tools/food.py','app/tools/routes.py','requirements.txt']}
    (output/'manifest.json').write_text(json.dumps({'started_at':datetime.now().isoformat(),
        'transport':'streamable_http_mcp','timeout_seconds':amap_mcp.timeout_seconds(),
        'source_hashes':hashes,'scope':'direct_readonly_tools_no_llm_no_store','quality_grade':'not_evaluated'},indent=2),encoding='utf-8')
    rows,requests,schemas,phases=[],[],[],[]
    original_call=amap_mcp.MCPTools.call
    def call(client,name,arguments):
        started=monotonic()
        row={'tool':name}
        requests.append(row)
        try:
            result=original_call(client,name,arguments)
            row['status']='returned'
            return result
        except Exception as exc:
            row.update(status='failed',error_type=type(exc).__name__,
                       stage=getattr(exc,'stage',None),provider_error_type=getattr(exc,'error_type',None))
            raise
        finally:
            row['seconds']=monotonic()-started
    def save():
        value={'rows':rows,'mcp_tools_calls':requests,'discovered_tools':schemas,'mcp_phases':phases}
        (output/'results.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    def execute(name,arguments,**kwargs):
        started=monotonic()
        with amap_mcp.observe_phases(phases.append):
            result=run_tool(name,arguments,**kwargs)
        rows.append({'tool':name,'arguments':arguments,'seconds':monotonic()-started,'result':result})
        save()
        print(json.dumps({'tool':name,'status':result['status'],'seconds':round(rows[-1]['seconds'],3)},ensure_ascii=False),flush=True)
        return result
    amap_mcp.MCPTools.call=call
    try:
        with amap_mcp.observe_phases(phases.append), amap_mcp.session() as tools:
            schemas.extend({'name':name,'inputSchema':schema} for name,schema in tools.specs.items())
        save()
        a=execute('search_places',{'city':'杭州','keywords':'杭州东站'})
        b=execute('search_places',{'city':'杭州','keywords':'西湖湖滨公园'})
        if a['status']=='unavailable' and b['status']=='unavailable':
            status={'status':'blocked','reason':'two_place_queries_unavailable','completed_steps':len(rows)}
        else:
            execute('search_food',{'city':'杭州','keywords':'杭帮菜','max_results':3})
            if a.get('candidates') and b.get('candidates'):
                catalog=collect_places([{'role':'tool','name':'search_places','content':json.dumps(result,ensure_ascii=False)} for result in (a,b)])
                origin,destination=a['candidates'][0]['poi_id'],b['candidates'][0]['poi_id']
                if origin not in catalog or destination not in catalog:
                    raise ValueError('MCP response failed trusted-POI binding')
                cache={}
                for mode in ('walking','transit','driving'):
                    execute('query_route',{'origin_poi_id':origin,'destination_poi_id':destination,'mode':mode},place_catalog=catalog,route_cache=cache)
                before=len(requests)
                execute('query_route',{'origin_poi_id':origin,'destination_poi_id':destination,'mode':'transit'},place_catalog=catalog,route_cache=cache)
                if len(requests)!=before:
                    raise ValueError('Cache reuse unexpectedly sent another MCP request')
            status={'status':'completed' if len(rows)==7 and all(r['result']['status']=='available' for r in rows) else 'incomplete',
                    'completed_steps':len(rows),'available_steps':sum(r['result']['status']=='available' for r in rows)}
    except Exception as exc:
        save()
        status={'status':'blocked','error_type':type(exc).__name__,'stage':getattr(exc,'stage',None),
                'provider_error_type':getattr(exc,'error_type',None),'completed_steps':len(rows)}
    finally:
        amap_mcp.MCPTools.call=original_call
    status['mcp_tools_calls']=len(requests)
    (output/'status.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
    print(json.dumps(status),flush=True)
    return 0 if status['status']=='completed' else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(run(parser.parse_args()))
