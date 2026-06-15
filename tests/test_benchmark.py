import os
from pathlib import Path

from tests.simulator import ClientCredentialGrantSimulator as CcaTester
from perf_baseline import Baseline


_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_DIR = _REPO_ROOT / ".perf-baseline"
os.makedirs(_BASELINE_DIR, exist_ok=True)
baseline = Baseline(str(_BASELINE_DIR / "data"), threshold=1.5)  # Up to 1.5x slower than baseline

# Here come benchmark test cases, powered by pytest-benchmark
# Func names will become diag names.
def test_cca_1_tenant_with_10_tokens_per_tenant_and_cache_hit(benchmark):
    tester = CcaTester(tokens_per_tenant=10, cache_hit=True)
    baseline.set_or_compare(tester.run)
    benchmark(tester.run)

def test_cca_many_tenants_with_10_tokens_per_tenant_and_cache_hit(benchmark):
    tester = CcaTester(number_of_tenants=1000, tokens_per_tenant=10, cache_hit=True)
    baseline.set_or_compare(tester.run)
    benchmark(tester.run)

def test_cca_1_tenant_with_10_tokens_per_tenant_and_cache_miss(benchmark):
    tester = CcaTester(tokens_per_tenant=10, cache_hit=False)
    baseline.set_or_compare(tester.run)
    benchmark(tester.run)

def test_cca_many_tenants_with_10_tokens_per_tenant_and_cache_miss(benchmark):
    tester = CcaTester(number_of_tenants=1000, tokens_per_tenant=10, cache_hit=False)
    baseline.set_or_compare(tester.run)
    benchmark(tester.run)

