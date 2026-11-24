"""Workspace Admin > Integrations: connect apps (Moodle, a website, a custom app)."""
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from rag.models import Document
from siteconfig import conf
from tenants.decorators import tenant_required

from .forms import IntegrationForm
from .models import Integration


def _api_base(request, integration) -> str:
    base = (conf.get().site_url or "").rstrip("/")
    path = f"/i/{integration.key}/"
    return base + path if base else request.build_absolute_uri(path)


@require_http_methods(["GET", "POST"])
@tenant_required(admin=True)
def index(request):
    tenant = request.tenant
    form = IntegrationForm(request.POST or None, tenant=tenant,
                           initial={"kind": Integration.Kind.APP, "signed_users": True})
    if request.method == "POST":
        if form.is_valid():
            integration = form.save()
            messages.success(request, f"{integration.name} connected. Give the app its key and secret below.")
            return redirect("integrations_admin:detail", tenant_slug=tenant.slug, pk=integration.pk)
        messages.error(request, "Please fix the highlighted fields.")
    rows = (Integration.objects.filter(tenant=tenant)
            .annotate(items=Count("documents"), scopes=Count("documents__scope", distinct=True)).order_by("name"))
    return render(request, "integrations/index.html", {"tenant": tenant, "rows": rows, "form": form})


@require_http_methods(["GET", "POST"])
@tenant_required(admin=True)
def detail(request, pk):
    tenant = request.tenant
    integration = get_object_or_404(Integration, pk=pk, tenant=tenant)
    action = request.POST.get("action") if request.method == "POST" else None
    if action == "regenerate":
        integration.regenerate_secret()
        integration.save(update_fields=["secret_encrypted", "updated_at"])
        messages.success(request, "New secret generated. Update it in the app: the old one has stopped working.")
        return redirect("integrations_admin:detail", tenant_slug=tenant.slug, pk=pk)
    if action == "delete":
        if request.POST.get("confirm") == integration.name:
            name = integration.name
            integration.delete()  # its content goes with it (rag.signals removes the files)
            messages.success(request, f"{name} disconnected and its content removed.")
            return redirect("integrations_admin:index", tenant_slug=tenant.slug)
        messages.error(request, "Type the integration's name exactly to disconnect it.")
        return redirect("integrations_admin:detail", tenant_slug=tenant.slug, pk=pk)
    form = IntegrationForm(request.POST if action == "save" else None, instance=integration, tenant=tenant)
    if action == "save":
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect("integrations_admin:detail", tenant_slug=tenant.slug, pk=pk)
        messages.error(request, "Please fix the highlighted fields.")
    scopes = list(Document.objects.filter(integration=integration).values("scope").annotate(
        items=Count("id"), ready=Count("id", filter=Q(status=Document.Status.READY)),
        failed=Count("id", filter=Q(status=Document.Status.FAILED))).order_by("scope"))
    widget = getattr(tenant, "widget", None)
    return render(request, "integrations/detail.html", {
        "tenant": tenant, "integration": integration, "form": form, "scopes": scopes,
        "api_base": _api_base(request, integration), "widget": widget,
        "tools": integration.manifest_tools,
    })
