#!/usr/bin/env python3
"""Benchmark the Windows Ollama box from the Mac: network latency, model throughput, and latency percentiles."""
import json
import os
import socket
import statistics
import sys
import time
import urllib.request

HOST = os.environ.get("DC_HOST", "<windows-lan-ip>")
OLLAMA = f"http://{HOST}:11434"
MODELS = sys.argv[1:] or ["qwen2.5:3b", "qwen2.5:3b-32k", "qwen2.5-coder:1.5b-base"]
PROMPTS = {
    "short": "Say hello in one sentence.",
    "code": "Write a Python function that checks whether a string is a palindrome. Include a docstring.",
    "reason": "Explain in about 150 words how an SSH local port forward works.",
}
RUNS = 3


def tcp_rtt(port, n=20):
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        with socket.create_connection((HOST, port), timeout=3):
            pass
        samples.append((time.perf_counter() - t) * 1000)
    return samples


def post(path, body):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def stream_ttft(model, prompt):
    """Time to first token as seen by the client, over the network."""
    req = urllib.request.Request(OLLAMA + "/api/generate",
                                 data=json.dumps({"model": model, "prompt": prompt, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    ttft = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if ttft is None and json.loads(line).get("response"):
                ttft = (time.perf_counter() - t) * 1000
    # Base (completion) models can return whitespace-only or empty output; fall back to total time.
    return ttft if ttft is not None else (time.perf_counter() - t) * 1000


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f = int(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


results = {"network": {}, "models": {}}
for name, port in {"ssh_22": 22, "ollama_11434": 11434}.items():
    s = tcp_rtt(port)
    results["network"][name] = {"p50_ms": round(pct(s, 50), 2), "p95_ms": round(pct(s, 95), 2),
                                "min_ms": round(min(s), 2)}

for model in MODELS:
    # Cold load: unload, then time a 1-token request.
    post("/api/generate", {"model": model, "keep_alive": 0})
    time.sleep(1)
    t = time.perf_counter()
    cold = post("/api/generate", {"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1}})
    cold_ms = (time.perf_counter() - t) * 1000

    per_prompt = {}
    for label, prompt in PROMPTS.items():
        gen_tps, pp_tps, totals, ttfts, tokens = [], [], [], [], []
        for _ in range(RUNS):
            t = time.perf_counter()
            r = post("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                       "options": {"temperature": 0, "num_predict": 256}})
            totals.append((time.perf_counter() - t) * 1000)
            gen_tps.append(r["eval_count"] / (r["eval_duration"] / 1e9))
            if r.get("prompt_eval_duration"):
                pp_tps.append(r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9))
            tokens.append(r["eval_count"])
            ttfts.append(stream_ttft(model, prompt))
        per_prompt[label] = {
            "gen_tok_s": round(statistics.mean(gen_tps), 1),
            "prompt_tok_s": round(statistics.mean(pp_tps), 1) if pp_tps else None,
            "ttft_p50_ms": round(pct(ttfts, 50)),
            "total_p50_ms": round(pct(totals, 50)),
            "total_p95_ms": round(pct(totals, 95)),
            "out_tokens": round(statistics.mean(tokens)),
        }
    ps = json.load(urllib.request.urlopen(OLLAMA + "/api/ps"))
    loaded = next((m for m in ps.get("models", []) if m["name"] == model), {})
    results["models"][model] = {
        "cold_load_ms": round(cold_ms),
        "size_gb": round(loaded.get("size", 0) / 1e9, 2),
        "vram_gb": round(loaded.get("size_vram", 0) / 1e9, 2),
        "prompts": per_prompt,
    }
    print(f"done {model}", file=sys.stderr)

print(json.dumps(results, indent=2))
