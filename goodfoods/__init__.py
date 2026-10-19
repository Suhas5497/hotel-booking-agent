"""GoodFoods reservation agent."""
from __future__ import annotations

__version__ = "1.0.0"


def build_stack(db_path=None, llm_settings=None):
    """Wire database -> service -> MCP server -> MCP client -> agent. Returns (agent, service)."""
    from .agent import ReservationAgent
    from .config import DB_PATH, load_llm_settings
    from .llm import LLMClient
    from .mcp_client import InProcessTransport, MCPClient
    from .mcp_server import MCPServer
    from .seed import build_database
    from .services import ReservationService

    db = build_database(db_path or DB_PATH)
    service = ReservationService(db)
    mcp = MCPClient(InProcessTransport(MCPServer(service))).initialize()
    agent = ReservationAgent(LLMClient(llm_settings or load_llm_settings()), mcp, n_outlets=db.restaurant_count())
    return agent, service
