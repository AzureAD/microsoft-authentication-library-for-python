# Prunes unwanted benchmark series from data.js
import json, sys


if len(sys.argv) < 3:
    sys.exit("Usage: python prune.py <path_to_data.js> <name_1_to_be_removed> <name_2_to_be_removed> ...")
    sys.exit(1)
js = open(sys.argv[1]).read()
header = "window.BENCHMARK_DATA = "
assert js.startswith(header)
data = json.loads(js[len(header):])
benchmark = []
for commit in data["entries"]["Benchmark"]:
    commit["benches"] = [
        bench for bench in commit["benches"]
        if bench["name"] not in sys.argv[2:]]
    benchmark.append(commit)
data["entries"]["Benchmark"] = benchmark
print(header, end="")
print(json.dumps(data, indent=2))  # It is great that modern Python keeps the order of keys
