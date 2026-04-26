#!/usr/bin/env python3
"""Run the trading bot backend server in development mode (auto-reload enabled).

DO NOT use this for production. Uvicorn's --reload watches the project root and
restarts the worker on any file change, which wipes in-process caches and causes
APScheduler to miss ticks. For production use run.py.
"""
import os
import uvicorn
from backend.models.database import init_db

if __name__ == "__main__":
    print("Initializing database...")
    init_db()

    port = int(os.environ.get("PORT", 8000))
    print("Starting in DEVELOPMENT mode (auto-reload enabled — DO NOT use for production)")
    print(f"Starting server on http://0.0.0.0:{port}")
    print(f"API docs available at http://localhost:{port}/docs")

    uvicorn.run(
        "backend.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
