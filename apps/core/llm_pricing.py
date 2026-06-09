"""
Static pricing map (per 1M text tokens) from OpenAI pricing page.
Unknown models will show no cost.
"""

PRICING_PER_1M = {
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "gpt-5.1": {"input": 1.25, "output": 10.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "gpt-5.2-chat-latest": {"input": 1.75, "output": 14.00},
    "gpt-5.1-chat-latest": {"input": 1.25, "output": 10.00},
    "gpt-5-chat-latest": {"input": 1.25, "output": 10.00},
    "gpt-5.2-codex": {"input": 1.75, "output": 14.00},
    "gpt-5.1-codex-max": {"input": 1.25, "output": 10.00},
    "gpt-5.1-codex": {"input": 1.25, "output": 10.00},
    "gpt-5-codex": {"input": 1.25, "output": 10.00},
    "gpt-5.2-pro": {"input": 21.00, "output": 168.00},
    "gpt-5-pro": {"input": 15.00, "output": 120.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-05-13": {"input": 5.00, "output": 15.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-realtime": {"input": 4.00, "output": 16.00},
    "gpt-realtime-mini": {"input": 0.60, "output": 2.40},
    "gpt-4o-realtime-preview": {"input": 5.00, "output": 20.00},
    "gpt-4o-mini-realtime-preview": {"input": 0.60, "output": 2.40},

    # ── DeepSeek (direct api.deepseek.com) — approx, per 1M tokens ──
    "deepseek-chat": {"input": 0.27, "output": 1.10},          # V3
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},      # R1

    # ── OpenRouter model ids (provider/model) — approx ──
    "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "deepseek/deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek/deepseek-r1": {"input": 0.55, "output": 2.19},
    "qwen/qwen-2.5-72b-instruct": {"input": 0.35, "output": 0.40},
    "meta-llama/llama-3.1-70b-instruct": {"input": 0.30, "output": 0.30},

    # ── Together AI ──
    "Qwen/Qwen2.5-72B-Instruct-Turbo": {"input": 0.88, "output": 0.88},
    "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo": {"input": 0.88, "output": 0.88},
}
