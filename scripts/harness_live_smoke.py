"""Explicit opt-in, serial real AGNES demo. Never saves secret values."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config.settings import Settings
from app.schemas.chat import ChatRequest
from app.services.container import build_container


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--specialists", action="store_true")
    parser.add_argument("--specialist-probe", action="store_true")
    parser.add_argument("--output", default="eval/reports/harness_live.json")
    args = parser.parse_args()
    settings = Settings(
        database_url="sqlite+aiosqlite:///./data/local/harness-live.db",
        agent_delegation_enabled=args.specialists,
        agent_deadline_seconds=150,
        max_tool_calls=16,
    )
    if not settings.agnes.get_secret_value():
        raise SystemExit("AGNES binding missing")
    c = await build_container(settings, seed=True)
    records = []
    try:
        session = None
        queries = [
            "定制马克杯超过七天但有质量问题还能退吗？",
            "查询订单 ORD10002 的商品和状态",
            "这笔订单有质量问题，我想退货",
            "原因是收到商品有裂纹，需要提交退货申请",
        ]
        if args.specialist_probe:
            queries = [
                "请用delegate_task分别委派知识专家核对定制商品的质量问题退货例外、订单专家查询ORD10002的状态和商品；两个调查独立可并行。汇总证据和订单事实，不执行任何写操作。"
            ]
        for text in queries:
            response = await c.chat_service.chat(
                ChatRequest(message=text, user_id="u_demo", session_id=session),
                trace_id="live_smoke",
            )
            session = response.session_id
            records.append({"query": text, **response.model_dump(mode="json")})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
            if response.pending_action:
                confirmation = await c.chat_service.chat(
                    ChatRequest(
                        message="确认执行",
                        user_id="u_demo",
                        session_id=session,
                        confirm_action_id=response.pending_action.action_id,
                    ),
                    trace_id="live_confirm",
                )
                records.append({"query": "确认执行", **confirmation.model_dump(mode="json")})
                print(json.dumps(records[-1], ensure_ascii=False), flush=True)
                break
    finally:
        await c.aclose()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {"model": settings.agnes_model, "specialists": args.specialists, "cases": records},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
