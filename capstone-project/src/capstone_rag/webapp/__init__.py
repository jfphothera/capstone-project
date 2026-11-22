"""The local web application: a FastAPI layer over the existing modules.

Nothing in here contains business logic. Every route calls the same
`capstone_rag` functions the Streamlit pages and the demonstration scripts
call, so there is one implementation of each rule and one place it is
enforced. A UI button is a request to this API; the API decides.
"""

from .app import create_app, main

__all__ = ["create_app", "main"]
