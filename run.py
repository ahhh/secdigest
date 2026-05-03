#!/usr/bin/env python3
"""Development entry point. For production use uvicorn directly (see README)."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "secdigest.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        reload_dirs=["secdigest"],
    )
