"""Website chat widget: the embeddable chat page, its public config and API, and
the workspace Admin page's Embed tab."""
import json
import re

from asgiref.sync import sync_to_async

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from chat.models import Conversation, Message
from chat.views import respond
from integrations import identity
from integrations import tools as integration_tools
from rag.models import Document
from tenants.decorators import tenant_required

from . import ratelimit
from .forms import WidgetForm
from .models import Widget

_VISITOR = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ITEM_KEY = re.compile(r"^[a-z]+(:[A-Za-z0-9_-]+){0,3}$")


async def _active_widget(key) -> Widget:
    try:
        widget = await Widget.objects.select_related("tenant").aget(public_key=key)
    except Widget.DoesNotExist:
        raise Http404("Widget not found")
    if not widget.enabled or not widget.tenant.is_active:
        raise Http404("Widget not found")
    return widget


def _same_origin(request) -> bool:
    """Browsers send Origin on cross-site POSTs; only our own iframe may call the API."""
    origin = request.headers.get("Origin")
    return origin is None or origin == f"{request.scheme}://{request.get_host()}"


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


async def _visitor_request(request, key):
    """Common checks for widget API calls. Returns (widget, body, visitor_id, visitor) or an
    error response. ``visitor`` is the user a connected app signed in (integrations.identity),
    with the scope and page they're in, else None."""
    widget = await _active_widget(key)
    if not _same_origin(request):
        return _error("Cross-origin requests are not allowed", 403)
    if request.content_type != "application/json":
        return _error("Expected application/json", 415)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _error("Invalid JSON body", 400)
    visitor_id = str(body.get("visitor_id") or "") if isinstance(body, dict) else ""
    if not _VISITOR.match(visitor_id) or visitor_id.startswith(identity.RESERVED_VISITOR_PREFIX):
        return _error("Invalid visitor", 400)
    visitor = None
    if body.get("user_token"):
        visitor = await sync_to_async(identity.verify)(widget.tenant, body["user_token"])
        if visitor is not None:  # signed in by a connected app: same conversations on every device
            visitor_id = visitor.visitor_id
    return widget, body, visitor_id, visitor


@login_not_required  # public: shown to visitors of the customer's website
@require_GET
@xframe_options_exempt
async def page(request, key):
    """The chat UI loaded inside the iframe on the customer's website."""
    widget = await _active_widget(key)
    config = {
        "greeting": widget.greeting,
        "storageKey": f"sc:{widget.public_key}",
        "urls": {
            "send": reverse("widget:send", args=[key]),
            "history": reverse("widget:history", args=[key]),
        },
    }
    response = render(request, "widget/widget.html", {"widget": widget, "tenant": widget.tenant, "config": config})
    # Browsers refuse to show this page inside any site not listed here.
    response["Content-Security-Policy"] = f"frame-ancestors {widget.frame_ancestors()}"
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@login_not_required
@require_GET
async def config(request, key):
    """Public, non-sensitive launcher settings, read by embed.js on the customer's site."""
    try:
        widget = await _active_widget(key)
        data = {"enabled": True, "title": widget.title, "color": widget.accent_color,
                "textColor": widget.text_color, "position": widget.position}
    except Http404:
        data = {"enabled": False}
    response = JsonResponse(data)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=60"
    return response


@login_not_required
@csrf_exempt  # no cookies are used: the public key and visitor ID identify the caller
@require_POST
async def send(request, key):
    checked = await _visitor_request(request, key)
    if not isinstance(checked, tuple):
        return checked
    widget, _, visitor_id, visitor = checked
    from siteconfig import conf

    await conf.aget(widget.tenant)  # load settings for sync access during the reply
    if not await ratelimit.allow(widget, request):
        return _error("You're sending messages too quickly. Please wait a moment.", 429)
    toolset = await sync_to_async(integration_tools.for_widget)(widget, visitor)
    return await respond(request, widget.tenant, Conversation.Source.WIDGET, visitor_id, visitor=visitor,
                         toolset=toolset)


@login_not_required
@csrf_exempt
@require_POST
async def history(request, key):
    checked = await _visitor_request(request, key)
    if not isinstance(checked, tuple):
        return checked
    widget, body, visitor_id, *_ = checked
    conversations = Conversation.objects.filter(
        tenant=widget.tenant, source=Conversation.Source.WIDGET, visitor_id=visitor_id,
    )
    try:
        if body.get("conversation_id") is None:  # latest, e.g. a student on a new device
            conversation = await conversations.alatest("pk")
        else:
            conversation = await conversations.aget(pk=body.get("conversation_id"))
    except (Conversation.DoesNotExist, ValueError, TypeError):
        return _error("Conversation not found", 404)
    msgs = [m.as_dict() async for m in conversation.messages.visible()]
    return JsonResponse({"conversation_id": conversation.pk, "messages": msgs})


# --- The Moodle plugin's endpoints -------------------------------------------------------
# The plugin calls /w/<widget key>/moodle/... with "Authorization: Bearer <shared code>". The
# shared code is the secret of the workspace's Moodle integration, and these endpoints are the
# generic integration API (integrations.api) with the course as scope "course:<id>".


def _moodle_integration(request, key, need=None):
    from integrations.api import Denied, _check
    from integrations.models import Integration

    widget = Widget.objects.select_related("tenant").filter(public_key=key).first()
    integration = None
    if widget is not None:
        integration = Integration.objects.select_related("tenant").filter(
            tenant=widget.tenant, kind=Integration.Kind.MOODLE).first()
    try:
        _check(integration, request, need)
    except Denied as exc:
        return exc.response
    return integration


_amoodle_integration = sync_to_async(_moodle_integration)


def _course(course) -> str:
    return f"course:{course}"


@login_not_required  # server-to-server: the Moodle plugin authenticates with the shared code
@require_GET
def moodle_documents(request, key):
    """The workspace's ready documents and a version that changes whenever their passages do."""
    from integrations import api

    integration = _moodle_integration(request, key, "share_documents")
    return integration if isinstance(integration, JsonResponse) else api.list_documents(integration)


@login_not_required
@require_GET
def moodle_chunks(request, key, pk):
    """One document's passages, in order."""
    from integrations import api

    integration = _moodle_integration(request, key, "share_documents")
    return integration if isinstance(integration, JsonResponse) else api.document_chunks(integration, pk)


@login_not_required
@require_GET
async def moodle_courses(request, key):
    """IDs of the courses whose content saaschat holds."""
    integration = await _amoodle_integration(request, key, "content")
    if isinstance(integration, JsonResponse):
        return integration
    scopes = [s async for s in Document.objects.filter(integration=integration, scope__startswith="course:")
              .order_by().values_list("scope", flat=True).distinct()]
    return JsonResponse({"courses": sorted(int(s.split(":", 1)[1]) for s in scopes if s.split(":", 1)[1].isdigit())})


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
async def moodle_course_items(request, key, course):
    """GET: the course's items {key: version}, and the file types and size accepted.
    POST (multipart: key, version, name, file): add or replace one item."""
    from integrations import api

    integration = await _amoodle_integration(request, key, "content")
    if isinstance(integration, JsonResponse):
        return integration
    if request.method == "GET":
        return await api.list_items(integration, _course(course))
    if not _ITEM_KEY.match(request.POST.get("key", "")) or len(request.POST.get("key", "")) > 100:
        return _error("Invalid key", 400)
    return await api.put_item(integration, _course(course), request)


@login_not_required
@csrf_exempt
@require_POST
async def moodle_course_items_delete(request, key, course):
    """Remove items from a course. JSON body: {"keys": [...]}."""
    from integrations import api

    integration = await _amoodle_integration(request, key, "content")
    if isinstance(integration, JsonResponse):
        return integration
    return await api.delete_items(integration, _course(course), request)


@login_not_required
@csrf_exempt
@require_POST
async def moodle_course_delete(request, key, course):
    """Remove all of a course's content (it is no longer sent from Moodle)."""
    from integrations import api

    integration = await _amoodle_integration(request, key, "content")
    if isinstance(integration, JsonResponse):
        return integration
    return await api.delete_scope(integration, _course(course))


@require_http_methods(["GET", "POST"])
@tenant_required(admin=True)
def embed_settings(request):
    """Workspace Admin > Embed: configure the widget and copy the snippet."""
    tenant = request.tenant
    widget, _ = Widget.objects.get_or_create(tenant=tenant, defaults={"title": f"Ask {tenant.chatbot_name}"[:60]})
    if request.method == "POST" and request.POST.get("action") == "regenerate":
        widget.regenerate_key()
        widget.save(update_fields=["public_key", "updated_at"])
        messages.success(request, "New key generated. Update the snippet on your website; the old one stopped working.")
        return redirect("rag:embed", tenant_slug=tenant.slug)
    form = WidgetForm(request.POST or None, instance=widget)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Widget settings saved.")
            return redirect("rag:embed", tenant_slug=tenant.slug)
        messages.error(request, "Please fix the highlighted fields.")
    script_url = request.build_absolute_uri(static("widget/embed.js"))
    snippet = f'<script src="{script_url}" data-key="{widget.public_key}" async></script>'
    widget_questions = Message.objects.filter(
        role=Message.Role.ASSISTANT, conversation__tenant=tenant, conversation__source=Conversation.Source.WIDGET,
    ).count()
    return render(request, "widget/admin_embed.html", {
        "tenant": tenant, "widget": widget, "form": form, "snippet": snippet, "script_url": script_url,
        "widget_questions": widget_questions,
    })
