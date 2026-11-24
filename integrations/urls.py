from django.urls import path

from . import api

app_name = "integrations"

urlpatterns = [
    path("", api.info, name="info"),
    path("manifest/", api.manifest, name="manifest"),
    path("scopes/", api.scopes, name="scopes"),
    path("scopes/<str:scope>/items/", api.items, name="items"),
    path("scopes/<str:scope>/items/delete/", api.items_delete, name="items_delete"),
    path("scopes/<str:scope>/delete/", api.scope_delete, name="scope_delete"),
    path("documents/", api.documents, name="documents"),
    path("documents/<int:pk>/chunks/", api.chunks, name="chunks"),
]
