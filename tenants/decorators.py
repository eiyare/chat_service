from functools import wraps
from inspect import iscoroutinefunction

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import Membership, Tenant

ADMIN_ROLES = ("superuser", Membership.Role.ADMIN)


def _role(user, tenant):
    if user.is_superuser:
        return "superuser"
    m = Membership.objects.filter(user=user, tenant=tenant).values_list("role", flat=True).first()
    return m


async def _arole(user, tenant):
    if user.is_superuser:
        return "superuser"
    return await Membership.objects.filter(user=user, tenant=tenant).values_list("role", flat=True).afirst()


def _check(request, tenant, role, admin):
    if role is None:
        raise Http404("Workspace not found")  # not a member: don't reveal that it exists
    if admin and role not in ADMIN_ROLES:
        raise PermissionDenied("Workspace admins only")
    request.tenant = tenant
    request.workspace_role = role
    request.is_workspace_admin = role in ADMIN_ROLES


def tenant_required(view_func=None, *, admin=False):
    """Resolve the workspace from the URL, check the user may use it, and attach
    ``request.tenant``, ``request.workspace_role`` and ``request.is_workspace_admin``.

    Use as ``@tenant_required`` (members and workspace admins) or
    ``@tenant_required(admin=True)`` (workspace admins only). Works for sync and
    async views. Unknown, inactive or not-a-member workspaces return 404; members
    on admin-only pages get 403. Login itself is enforced by LoginRequiredMiddleware.
    """
    if view_func is None:
        return lambda f: tenant_required(f, admin=admin)

    if iscoroutinefunction(view_func):

        @wraps(view_func)
        async def _async_wrapped(request, tenant_slug, *args, **kwargs):
            user = await request.auser()
            request.user = user  # templates read request.user; avoid a sync lookup in async code
            if not user.is_authenticated:
                raise PermissionDenied("Login required")
            try:
                tenant = await Tenant.objects.aget(slug=tenant_slug, is_active=True)
            except Tenant.DoesNotExist:
                raise Http404("Workspace not found")
            _check(request, tenant, await _arole(user, tenant), admin)
            from siteconfig import conf

            await conf.aget(tenant)  # load this workspace's settings for sync access in the view
            return await view_func(request, *args, **kwargs)

        return _async_wrapped

    @wraps(view_func)
    def _wrapped(request, tenant_slug, *args, **kwargs):
        if not request.user.is_authenticated:
            raise PermissionDenied("Login required")
        try:
            tenant = Tenant.objects.get(slug=tenant_slug, is_active=True)
        except Tenant.DoesNotExist:
            raise Http404("Workspace not found")
        _check(request, tenant, _role(request.user, tenant), admin)
        return view_func(request, *args, **kwargs)

    return _wrapped
