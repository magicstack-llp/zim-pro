import argparse
import asyncio
import logging


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zim-pro", description="ZIM library server, updater and MCP server."
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the web server (default)")
    commands.add_parser("mcp", help="run the MCP server over stdio, for desktop LLM apps")
    args = parser.parse_args()

    from .config import Config

    config = Config()
    if args.command == "mcp":
        # stdout carries the protocol, so logs must stay on stderr.
        logging.basicConfig(level=logging.WARNING)
        from .main import build_context
        from .mcp_server import build_mcp

        ctx = build_context(config)
        ctx.scanner.scan()
        asyncio.run(build_mcp(ctx).run_stdio_async())
        return

    import uvicorn

    from .main import create_app

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")
