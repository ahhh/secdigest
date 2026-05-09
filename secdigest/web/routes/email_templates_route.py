"""Routes: email template management.

Backs the /email-templates page, where the operator can list, edit,
fork, and delete the HTML templates used by the mailer. The six
``is_builtin=1`` templates can be edited but not deleted — they're
re-seeded on startup, so deletion would just be a confusing UX.

The global header markup also lives on this page for convenience —
it's rendered into every issue whose 'Include header' toggle is on.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/email-templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("email_templates.html", {
        "request": request,
        "email_templates": db.email_template_list(),
        "header_html": db.cfg_get("header_html") or "",
    })


@router.post("/email-templates/header")
async def save_global_header(request: Request, header_html: str = Form("")):
    """Save the single global newsletter header. The toggle that controls
    whether it renders in a given issue lives on the per-newsletter builder;
    this route only stores the markup itself."""
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.cfg_set("header_html", header_html or "")
    return RedirectResponse("/email-templates?msg=Header+saved", status_code=302)


@router.get("/email-templates/{template_id}/json")
async def template_json(request: Request, template_id: int):
    """Used by the in-page editor's "switch template" dropdown — fetches
    the raw fields so the textareas can be repopulated without a full
    page reload."""
    if not is_authed(request):
        return JSONResponse({}, status_code=401)
    tmpl = db.email_template_get(template_id)
    if not tmpl:
        return JSONResponse({}, status_code=404)
    return JSONResponse(dict(tmpl))


@router.post("/email-templates/new")
async def create_template(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    subject: str = Form("SecDigest — {date}"),
    html: str = Form(...),
    article_html: str = Form(...),
):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.email_template_create(name, description, subject, html, article_html)
    return RedirectResponse("/email-templates?msg=Template+created", status_code=302)


@router.post("/email-templates/{template_id}/save")
async def save_template(
    request: Request,
    template_id: int,
    name: str = Form(...),
    description: str = Form(""),
    subject: str = Form("SecDigest — {date}"),
    html: str = Form(...),
    article_html: str = Form(...),
):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.email_template_update(template_id, name=name, description=description,
                              subject=subject, html=html, article_html=article_html)
    return RedirectResponse("/email-templates?msg=Saved", status_code=302)


@router.post("/email-templates/{template_id}/delete")
async def delete_template(request: Request, template_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    tmpl = db.email_template_get(template_id)
    if tmpl and tmpl["is_builtin"]:
        return RedirectResponse("/email-templates?msg=Cannot+delete+built-in+templates&status=error", status_code=302)
    db.email_template_delete(template_id)
    return RedirectResponse("/email-templates?msg=Deleted", status_code=302)
