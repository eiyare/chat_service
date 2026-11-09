from django.shortcuts import redirect

from .decorators import tenant_required


@tenant_required
def home(request):
    """Workspace entry point: the chat app is the workspace's main app."""
    return redirect("chat:home", tenant_slug=request.tenant.slug)
