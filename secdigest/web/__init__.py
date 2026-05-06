from pathlib import Path
from fastapi.templating import Jinja2Templates

# Shared templates instance — imported by all route modules
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

from secdigest.web.csrf import csrf_input, csrf_token_value
templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["csrf_token_value"] = csrf_token_value
