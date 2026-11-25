"""Clawback: a small, security-first desktop assistant.

Design invariant: the model never touches the host. It can only ask for
typed tools, and every tool executes inside a Docker container whose mounts
are the entire filesystem it can see.
"""

__version__ = "0.1.0"
