"""Fixed small real-model A/B comparison on the current harness."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config.settings import Settings
from app.schemas.chat import ChatRequest
from app.services.container import build_container


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["A", "B"], required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    import os

    from pydantic import SecretStr

    key = SecretStr(os.environ["AGNES"])
    # Same credential, endpoint, model, neural retrieval and corpus for every variant.
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///./data/local/compare-{args.variant}.db",
        deepseek_api_key=key,
        deepseek_base_url="https://apihub.agnes-ai.com/v1",
        deepseek_model="agnes-3.0-flash",
    )
    settings = settings.model_copy(
        update={
            "agent_delegation_enabled": args.variant == "B",
            "agent_delegation_policy": "auto" if args.variant == "B" else "off",
        }
    )
    c = await build_container(settings, seed=True)
    rows = []
    try:
        for query in ["你好", "七天无理由退货规则是什么？", "查询订单 ORD10002 状态"]:
            start = time.perf_counter()
            r = await c.chat_service.chat(
                ChatRequest(message=query, user_id="u_demo"), trace_id="compare"
            )
            rows.append(
                {
                    "query": query,
                    **r.model_dump(mode="json"),
                    "wall_seconds": time.perf_counter() - start,
                }
            )
            print(query, rows[-1]["answer"][:120], flush=True)
    finally:
        await c.aclose()
    Path(args.output).write_text(
        json.dumps(
            {
                "variant": args.variant,
                "model": "agnes-3.0-flash",
                "settings": "BAAI/bge-small-zh-v1.5, BAAI/bge-reranker-base; original corpus/chunking/RRF/top-k",
                "cases": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
