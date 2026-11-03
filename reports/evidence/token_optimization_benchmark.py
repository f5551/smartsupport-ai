"""Compare captured pre-change FAQ payloads with the same offline scenario now.
No OpenAI client or network; local input estimates are not billed token counts.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent import Agent
from config import AgentConfig
from fake_openai import FakeOpenAI,fake_response,tool_call
from mcp_transport import TrustedSession
from tests.test_agent import FakeMCP
from token_limits import estimate_input,compact_result

HERE=Path(__file__).resolve().parent


def metrics(requests):
    requests=deepcopy(requests)
    # Remove random nonce tokenization as a confounder, without changing evidence.
    for request in requests:
        for item in request['input']:
            if 'call_id' in item:item['call_id']='benchmark_call_1'
    counts=[estimate_input(r['instructions'],r['input'],r['tools']) for r in requests]
    chars=sum(len(json.dumps({k:r[k] for k in ('instructions','input','tools')},ensure_ascii=False,separators=(',',':'))) for r in requests)
    envelope=(Decimal(sum(counts))*Decimal('.75')+Decimal(len(requests)*requests[0]['max_output_tokens'])*Decimal('4.50'))/1000000
    return {'api_calls_simulated':len(requests),'local_input_estimate_per_request':counts,
            'local_input_estimate_total':sum(counts),'request_context_chars_total':chars,
            'output_token_cap_per_request':requests[0]['max_output_tokens'],
            'estimated_envelope_usd_at_gpt54mini_rates':str(envelope)}


async def sample():
    client=FakeOpenAI([fake_response(calls=[tool_call('search_faq',{'query':'مدة الشحن'},'benchmark_call_1')]),fake_response('مدة الشحن 2 إلى 5 أيام عمل')])
    config=AgentConfig(model='fake-test-model',input_price=Decimal('.1'),output_price=Decimal('.2'))
    mcp=FakeMCP(payload={'ok':True,'data':[{'id':'FAQ-001','question':'ما مدة الشحن؟','answer':'مدة الشحن من 2 إلى 5 أيام عمل.'}],'error':None})
    start=perf_counter()
    result=await Agent(client,mcp,TrustedSession('demo_customer_001','optimization-benchmark'),config).run('كم تستغرق مدة الشحن؟')
    assert result.stop_reason=='final_answer' and result.tools_used==['search_faq']
    return client.requests,round((perf_counter()-start)*1000,3)


if __name__=='__main__':
    baseline=json.loads((HERE/'token_optimization_before.json').read_text(encoding='utf-8'))
    requests,elapsed=asyncio.run(sample())
    before,after=metrics(baseline['faq_requests']),metrics(requests)
    large={'ok':True,'data':{'content':'محتوى محفوظ طويل '*1000,'status':'saved'},'error':None}
    legacy=json.dumps({'trust':'untrusted_store_data','result':large},ensure_ascii=False)
    result={'scenario':'Same Arabic FAQ and deterministic FAQ data, FakeOpenAI/FakeMCP; no paid API',
            'sample_size':1,'token_estimation_method':'o200k JSON +256 framing +8/item +16/tool; normalized call IDs; same method both sides',
            'before':before,'after':after,
            'local_input_estimate_reduction_percent':str((Decimal(before['local_input_estimate_total'])-Decimal(after['local_input_estimate_total']))/Decimal(before['local_input_estimate_total'])*100),
            'after_local_elapsed_ms':elapsed,'before_local_elapsed_ms':None,'real_api_cost_after':None,
            'simulated_usage_is_fixed_not_a_savings_measure':True,
            'long_memory_result_chars_before':len(legacy),'long_memory_result_chars_after':len(compact_result(large,2000)),
            'original_trace_hashes_preserved':all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in baseline['trace_hashes'].items())}
    (HERE/'token_optimization_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
