"""Task 08 test-only admission. No network or persistence at import time."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from pathlib import Path
import sys

# Support loading the archived guard directly, independently of the caller CWD.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from cost_tracker import CostTracker
from ticket_budget import BudgetBlocked, COUNT_FIELDS, GENERATE_FIELDS, MODEL, generation_bound


class RunBudget:
    def __init__(self, daily, isolated, *, total_limit=Decimal('1'), daily_limit=Decimal('.10')):
        self.daily, self.isolated = daily, isolated
        self.total_limit, self.daily_limit = total_limit, daily_limit
        self.halted = False

    def reserve(self, key, upper):
        if self.halted or not self.isolated.reserve(key, upper, self.total_limit):
            raise BudgetBlocked('Run budget refused')
        try:
            if not self.daily.reserve(key, upper, self.daily_limit):
                raise BudgetBlocked('Daily budget refused')
        except BaseException:
            self.isolated.cancel_unsent(key)
            self.halted = True
            raise

    def settle(self, key, actual):
        try:
            self.daily.settle(key, actual)
            self.isolated.settle(key, actual)
        finally:
            if actual is None:
                self.halted = True


class CountedClient:
    usage_source = 'api'

    def __init__(self, client, price, budget, *, task_limit=Decimal('.005')):
        self.bound = generation_bound(price)
        if self.bound > task_limit:
            raise BudgetBlocked('Task envelope refused')
        self.client = client.with_options(max_retries=0)
        self.price, self.budget, self.task_limit = price, budget, task_limit
        self.responses = self
        self.tracker = CostTracker(price, source='api')
        self.calls = self.count_calls = 0
        self.halted = False
        self.phase = None
        self.errors, self.evidence = [], []
        self.prefix = uuid4().hex
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.client.__aexit__(*args)

    def with_options(self, **options):
        if options.get('max_retries', 0) != 0:
            raise BudgetBlocked('Retries prohibited')
        self.client = self.client.with_options(**{**options, 'max_retries': 0})
        return self

    async def create(self, **kwargs):
        async with self.lock:
            if self.halted or self.budget.halted or self.calls >= 2:
                raise BudgetBlocked('Closed task or turn limit')
            key, sent = None, False
            try:
                payload = deepcopy(kwargs)
                payload.update(reasoning={'effort': 'none'}, truncation='disabled')
                if (set(payload) - GENERATE_FIELDS or payload.get('model') != MODEL
                        or payload.get('service_tier') != 'default' or payload.get('store') is not False
                        or payload.get('parallel_tool_calls') is not False
                        or type(payload.get('max_output_tokens')) is not int
                        or not 16 <= payload['max_output_tokens'] <= 96
                        or not isinstance(payload.get('tools'), list)
                        or any(t.get('type') != 'function' for t in payload['tools'])):
                    raise BudgetBlocked('Unsupported test request')
                counted = {k: deepcopy(v) for k,v in payload.items() if k in COUNT_FIELDS}
                self.phase = 'input_tokens_count'
                self.count_calls += 1
                count = await self.client.responses.input_tokens.count(**counted)
                tokens = getattr(count, 'input_tokens', None)
                if type(tokens) is not int or not 0 <= tokens <= 2500:
                    raise BudgetBlocked('Unverifiable input count')
                upper = self.tracker.estimate(tokens, payload['max_output_tokens'])
                if self.tracker.committed + upper > self.task_limit:
                    raise BudgetBlocked('Task budget refused')
                key = self.prefix + ':' + str(self.calls + 1)
                self.budget.reserve(key, upper)
                self.tracker.reserve(key, upper)
                self.phase = 'responses_create'
                self.calls += 1
                sent = True
                response = await self.client.responses.create(**payload)
                self.phase = 'response_usage'
                record = self.tracker.record(key, getattr(response, 'usage', None),
                    model=getattr(response, 'model', None), service_tier=getattr(response, 'service_tier', None))
                actual = Decimal(record['cost_usd']) if record['cost_usd'] is not None else None
                self.budget.settle(key, actual)
                self.evidence.append({'model':getattr(response,'model',None),
                    'service_tier':getattr(response,'service_tier',None),'server_count':tokens,
                    'input_tokens':record['input_tokens'],'output_tokens':record['output_tokens'],
                    'cached_tokens':record['cached_tokens'],'cost_usd':record['cost_usd'],
                    'reserved_upper_usd':str(upper)})
                if (actual is None or getattr(response,'model',None) != MODEL
                        or getattr(response,'service_tier',None) != 'default'
                        or record['input_tokens'] != tokens
                        or record['output_tokens'] > payload['max_output_tokens'] or actual > upper):
                    raise BudgetBlocked('Usage contract mismatch')
                return response
            except BaseException as exc:
                self.halted = self.budget.halted = True
                self.errors.append({'type':type(exc).__name__, 'phase':self.phase, 'generation_sent':sent})
                if key is not None and key in self.tracker.reservations:
                    self.tracker.record(key, None)
                    self.budget.settle(key, None)
                raise


class ScopedMCP:
    def __init__(self, session, allowed, exact_write=None):
        self.session, self.allowed, self.exact_write = session, set(allowed), exact_write
        self.calls = []

    async def list_tools(self):
        return await self.session.list_tools()

    async def call_tool(self, name, arguments):
        if name not in self.allowed:
            raise ValueError('Unexpected tool for acceptance scenario')
        if name in {'create_ticket','save_memory'}:
            body = {k:v for k,v in arguments.items() if k != 'approval_id'}
            if body != self.exact_write or not arguments.get('approval_id') or any(c == name for c in self.calls):
                raise ValueError('Unapproved or duplicate synthetic write')
        self.calls.append(name)
        return await self.session.call_tool(name, arguments=arguments)
