"""Website chat widget: the embeddable chat page, its public config and API, and
the workspace Admin page's Embed tab."""
import json
import re

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
from tenants.decorators import tenant_required

from . import ratelimit
from .forms import WidgetForm
from .models import Widget

_VISITOR = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


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
    """Common checks for widget API calls. Returns (widget, body, visitor_id) or an error response."""
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
    if not _VISITOR.match(visitor_id):
        return _error("Invalid visitor", 400)
    return widget, body, visitor_id


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
    widget, _, visitor_id = checked
    from siteconfig import conf

    await conf.aget(widget.tenant)  # load settings for sync access during the reply
    if not await ratelimit.allow(widget, request):
        return _error("You're sending messages too quickly. Please wait a moment.", 429)
    return await respond(request, widget.tenant, Conversation.Source.WIDGET, visitor_id)


@login_not_required
@csrf_exempt
@require_POST
async def history(request, key):
    checked = await _visitor_request(request, key)
    if not isinstance(checked, tuple):
        return checked
    widget, body, visitor_id = checked
    try:
        conversation = await Conversation.objects.aget(
            pk=body.get("conversation_id"), tenant=widget.tenant,
            source=Conversation.Source.WIDGET, visitor_id=visitor_id,
        )
    except (Conversation.DoesNotExist, ValueError, TypeError):
        return _error("Conversation not found", 404)
    msgs = [m.as_dict() async for m in conversation.messages.all()]
    return JsonResponse({"conversation_id": conversation.pk, "messages": msgs})


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
