"""Entrypoint for the ambient-expense-agent app."""

from app.expense_agent.agent import root_agent, app

__all__ = ["root_agent", "app"]
