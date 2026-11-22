"""Agent Arena Event Service (AES).

Application-neutral ingestion, state and distribution of agent activity for
the Agent Arena desktops. Standard library only; it never imports the Capstone
application, so its lifecycle is independent of any one producer.
"""
__version__ = "2.5.1"
CONTRACT = "arena.v2"
SUPPORTED_CONTRACTS = (CONTRACT,)
