"""Routes: subscriber list management."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login

router = APIRouter()


@router.get("/subscribers", response_class=HTMLResponse)
async def subscribers_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("subscribers.html", {
        "request": request,
        "subscribers": db.subscriber_list(),
    })


@router.post("/subscribers")
async def add_subscriber(request: Request, email: str = Form(...), name: str = Form("")):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    result = db.subscriber_create(email.strip().lower(), name.strip())
    msg = "Added" if result else "Already+exists"
    return RedirectResponse(f"/subscribers?msg={msg}", status_code=302)


@router.post("/subscribers/{sub_id}/toggle")
async def toggle_subscriber(request: Request, sub_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    subs = db.subscriber_list()
    match = next((s for s in subs if s["id"] == sub_id), None)
    if match:
        db.subscriber_update(sub_id, active=0 if match["active"] else 1)
    return RedirectResponse("/subscribers", status_code=302)


@router.post("/subscribers/{sub_id}/delete")
async def delete_subscriber(request: Request, sub_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.subscriber_delete(sub_id)
    return RedirectResponse("/subscribers", status_code=302)
