"""Operator command-line helpers for open-harness-router.

Each module runs from the repository root as
``PYTHONPATH=src .venv/bin/python -m cli.<name>`` and reuses the router's
own code paths (settings, provider factory, TLS trust store) instead of
re-implementing them, so a verdict printed here predicts what the running
service will do. The modules back the ``add-provider`` Claude Code skill
(``.claude/skills/add-provider``).
"""
