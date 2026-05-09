"""Admin-side web package init.

Two responsibilities:
1. Construct the single ``Jinja2Templates`` instance every admin route
   module imports. Sharing one instance means template caches stay warm
   across requests and we don't reconfigure globals in N places.
2. Register the CSRF helpers as Jinja globals so every template can call
   ``{{ csrf_input() }}`` without each route having to thread them
   through context.
"""
from pathlib import Path
from fastapi.templating import Jinja2Templates

# Shared templates instance — imported by all route modules
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Late import to avoid a circular: csrf.py reads ``config`` and ``db``,
# which themselves can be imported indirectly through this package's
# routes during module init.
from secdigest.web.csrf import csrf_input, csrf_token_value
templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["csrf_token_value"] = csrf_token_value
