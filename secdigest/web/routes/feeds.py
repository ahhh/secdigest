"""Routes: RSS feed management."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    feeds = db.rss_feed_list()
    return templates.TemplateResponse("feeds.html", {
        "request": request,
        "feeds": feeds,
    })


@router.post("/feeds/add")
async def add_feed(request: Request,
                   url: str = Form(...),
                   name: str = Form(""),
                   max_articles: int = Form(5)):
    if not is_authed(request):
        return RedirectResponse("/feeds", status_code=302)
    db.rss_feed_create(url.strip(), name.strip(), max_articles)
    return RedirectResponse("/feeds?msg=Feed+added", status_code=302)


@router.post("/feeds/{feed_id}/toggle")
async def toggle_feed(request: Request, feed_id: int):
    if not is_authed(request):
        return RedirectResponse("/feeds", status_code=302)
    feeds = db.rss_feed_list()
    feed = next((f for f in feeds if f["id"] == feed_id), None)
    if feed:
        db.rss_feed_update(feed_id, active=0 if feed["active"] else 1)
    return RedirectResponse("/feeds", status_code=302)


@router.post("/feeds/{feed_id}/delete")
async def delete_feed(request: Request, feed_id: int):
    if not is_authed(request):
        return RedirectResponse("/feeds", status_code=302)
    db.rss_feed_delete(feed_id)
    return RedirectResponse("/feeds?msg=Feed+removed", status_code=302)
