"""Root URL configuration for saas_chat.

/                         -> public portal (landing page + workspace finder)
/<prefix>/<slug>/         -> a customer's workspace (prefix = settings.TENANT_URL_PREFIX)
/<prefix>/<slug>/chat/    -> the workspace's document-grounded chat
/<prefix>/<slug>/admin/   -> workspace admin: knowledge-base (RAG) documents
/admin/                   -> platform administration
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

tenant_prefix = f"{settings.TENANT_URL_PREFIX}/<slug:tenant_slug>/"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password/", auth_views.PasswordChangeView.as_view(), name="password_change"),
    path("accounts/password/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path(f"{tenant_prefix}chat/", include("chat.urls")),
    path(f"{tenant_prefix}admin/", include("rag.urls")),
    path(tenant_prefix, include("tenants.urls")),
    path("w/<str:key>/", include("widget.urls")),
    path("", include("portal.urls")),
]
