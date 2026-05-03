from pathlib import Path
from fastapi.templating import Jinja2Templates

# Shared templates instance — imported by all route modules
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
