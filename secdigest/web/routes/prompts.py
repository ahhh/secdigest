"""Routes: curation and summary prompt management.

Operators can author additional instructions that get prepended to the
LLM calls (curation = scoring, summary = per-article writeup). This page
is just a CRUD view over the ``prompts`` table — fetcher.py / summarizer.py
read ``active=1`` rows on every run and concatenate them into the user
message. Same auth + CSRF posture as the rest of the admin app.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("prompts.html", {
        "request": request,
        "prompts": db.prompt_list(),
    })


@router.post("/prompts")
async def create_prompt(request: Request, name: str = Form(...),
                        type_: str = Form(..., alias="type"),
                        content: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_create(name, type_, content)
    return RedirectResponse("/prompts", status_code=302)


@router.post("/prompts/{prompt_id}/update")
async def update_prompt(request: Request, prompt_id: int,
                        name: str = Form(...), content: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_update(prompt_id, name=name, content=content)
    return RedirectResponse("/prompts", status_code=302)


@router.post("/prompts/{prompt_id}/toggle")
async def toggle_prompt(request: Request, prompt_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    p = db.prompt_list()
    match = next((x for x in p if x["id"] == prompt_id), None)
    if match:
        db.prompt_update(prompt_id, active=0 if match["active"] else 1)
    return RedirectResponse("/prompts", status_code=302)


@router.post("/prompts/{prompt_id}/delete")
async def delete_prompt(request: Request, prompt_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_delete(prompt_id)
    return RedirectResponse("/prompts", status_code=302)
