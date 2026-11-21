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
from rag import jobs, services
from rag.models import Document, IndexingRun
from tenants.decorators import tenant_required

from . import moodle, ratelimit
from .student_tools import StudentTools
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
    """Common checks for widget API calls. Returns (widget, body, visitor_id, course, activity,
    student) or an error response. ``student`` is the verified Moodle student with their
    signed token (for student data tools), else None. ``course`` is the Moodle course a verified student is on (when the
    workspace takes course content), else None; ``activity`` the course module they're viewing."""
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
    if not _VISITOR.match(visitor_id) or visitor_id.startswith("moodle-"):  # reserved for verified students
        return _error("Invalid visitor", 400)
    course = activity = student = None
    if widget.moodle_enabled and body.get("user_token"):
        user = moodle.verify_user(widget.moodle_shared_code, body["user_token"])
        if user:  # signed by the Moodle plugin: same conversations on every device
            if not widget.moodle_courses:
                user["course"] = None
            course = user["course"]
            activity = user["activity"] if course is not None else None
            student = {**user, "token": body["user_token"]}
            visitor_id = moodle.visitor_id(user)
    return widget, body, visitor_id, course, activity, student


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
    widget, _, visitor_id, course, activity, student = checked
    from siteconfig import conf

    await conf.aget(widget.tenant)  # load settings for sync access during the reply
    if not await ratelimit.allow(widget, request):
        return _error("You're sending messages too quickly. Please wait a moment.", 429)
    tools = StudentTools(widget, student, student["token"]) if StudentTools.available(widget, student) else None
    return await respond(request, widget.tenant, Conversation.Source.WIDGET, visitor_id, course=course,
                         activity=activity, student_tools=tools)


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


def _moodle_copy_widget(request, key):
    """The widget whose passages the Moodle plugin may copy, or an error response."""
    try:
        widget = Widget.objects.select_related("tenant").get(public_key=key, moodle_enabled=True,
                                                             tenant__is_active=True)
    except Widget.DoesNotExist:
        raise Http404("Not found")
    if not moodle.check_shared_code(widget, request):
        return _error("Wrong shared code", 403)
    if widget.chunk_storage != Widget.ChunkStorage.MIRROR:
        return _error("Copying passages to Moodle is turned off for this workspace", 409)
    return widget


def _ready_documents(widget):
    return Document.objects.filter(tenant=widget.tenant, status=Document.Status.READY,
                                   moodle_course_id__isnull=True)


@login_not_required  # server-to-server: the Moodle plugin authenticates with the shared code
@require_GET
def moodle_documents(request, key):
    """The workspace's ready documents and a version that changes whenever their passages do."""
    widget = _moodle_copy_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    docs = [{"id": d.pk, "name": d.name, "version": f"{d.sha256[:16]}-{int(d.updated_at.timestamp())}"}
            for d in _ready_documents(widget).order_by("pk")]
    return JsonResponse({"documents": docs})


@login_not_required
@require_GET
def moodle_chunks(request, key, pk):
    """One document's passages, in order."""
    widget = _moodle_copy_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    try:
        doc = _ready_documents(widget).get(pk=pk)
    except Document.DoesNotExist:
        raise Http404("Document not found")
    chunks = [{"index": c.index, "section": c.section, "text": c.text}
              for c in doc.chunks.order_by("index").only("index", "section", "text")]
    return JsonResponse({"id": doc.pk, "name": doc.name, "chunks": chunks})


# --- Course content from the Moodle plugin ------------------------------------------
# The plugin's hourly task sends each course's activities, files, forum posts and dates.
# Every item becomes a document tagged with its course; students on that course's pages
# are answered from those documents only (chat.views.respond).


async def _moodle_course_widget(request, key):
    """The widget taking course content from the plugin, or an error response."""
    try:
        widget = await Widget.objects.select_related("tenant").aget(public_key=key, moodle_enabled=True,
                                                                    tenant__is_active=True)
    except Widget.DoesNotExist:
        raise Http404("Not found")
    if not moodle.check_shared_code(widget, request):
        return _error("Wrong shared code", 403)
    if not widget.moodle_courses:
        return _error("Course content is turned off for this workspace", 409)
    return widget


def _course_documents(widget, course=None):
    documents = Document.objects.filter(tenant=widget.tenant, moodle_course_id__isnull=False)
    return documents if course is None else documents.filter(moodle_course_id=course)


@login_not_required  # server-to-server: the Moodle plugin authenticates with the shared code
@require_GET
async def moodle_courses(request, key):
    """IDs of the courses whose content saaschat holds."""
    widget = await _moodle_course_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    ids = [c async for c in _course_documents(widget).order_by().values_list("moodle_course_id", flat=True).distinct()]
    return JsonResponse({"courses": sorted(ids)})


@login_not_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
async def moodle_course_items(request, key, course):
    """GET: the course's items {key: version}, and the file types and size accepted.
    POST (multipart: key, version, name, file): add or replace one item; it is processed
    in the background, and searchable once ready."""
    widget = await _moodle_course_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    policy = widget.tenant.upload_policy
    if request.method == "GET":
        items = {k: v async for k, v in _course_documents(widget, course).values_list("moodle_key", "moodle_version")}
        return JsonResponse({"items": items, "extensions": list(services.course_item_extensions(policy)),
                             "max_file_bytes": policy.max_file_bytes})
    item_key = request.POST.get("key", "")
    upload = request.FILES.get("file")
    if not _ITEM_KEY.match(item_key) or len(item_key) > 100:
        return _error("Invalid key", 400)
    if upload is None:
        return _error("No file", 400)
    if upload.size > policy.max_file_bytes:
        return _error(f"{upload.name}: larger than the {policy.max_file_mb} MB limit", 413)
    try:
        document = await services.store_course_item(
            widget.tenant, course, item_key, request.POST.get("version", ""), request.POST.get("name", "").strip(),
            upload.name, upload.read(),
        )
    except services.UploadRejected as exc:
        return _error(str(exc), 400)
    await jobs.aenqueue(document, IndexingRun.Trigger.UPLOAD)
    return JsonResponse({"id": document.pk, "status": document.status}, status=202)


@login_not_required
@csrf_exempt
@require_POST
async def moodle_course_items_delete(request, key, course):
    """Remove items from a course. JSON body: {"keys": [...]}."""
    widget = await _moodle_course_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    try:
        keys = json.loads(request.body or b"{}").get("keys")
    except (ValueError, AttributeError):
        keys = None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return _error("Expected {\"keys\": [...]}", 400)
    deleted = 0
    async for document in _course_documents(widget, course).filter(moodle_key__in=keys):
        await services.delete_document(document)
        deleted += 1
    return JsonResponse({"deleted": deleted})


@login_not_required
@csrf_exempt
@require_POST
async def moodle_course_delete(request, key, course):
    """Remove all of a course's content (it is no longer sent from Moodle)."""
    widget = await _moodle_course_widget(request, key)
    if not isinstance(widget, Widget):
        return widget
    deleted = 0
    async for document in _course_documents(widget, course):
        await services.delete_document(document)
        deleted += 1
    return JsonResponse({"deleted": deleted})


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
