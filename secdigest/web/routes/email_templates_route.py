"""Routes: email template management."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login

router = APIRouter()


@router.get("/email-templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("email_templates.html", {
        "request": request,
        "email_templates": db.email_template_list(),
    })


@router.get("/email-templates/{template_id}/json")
async def template_json(request: Request, template_id: int):
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
    return RedirectResponse(f"/email-templates?msg=Saved", status_code=302)


@router.post("/email-templates/{template_id}/delete")
async def delete_template(request: Request, template_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    tmpl = db.email_template_get(template_id)
    if tmpl and tmpl["is_builtin"]:
        return RedirectResponse("/email-templates?msg=Cannot+delete+built-in+templates&status=error", status_code=302)
    db.email_template_delete(template_id)
    return RedirectResponse("/email-templates?msg=Deleted", status_code=302)
