from django.urls import path

from widget import views as widget_views

from . import views

app_name = "rag"

urlpatterns = [
    path("", views.admin_home, name="home"),
    path("performance/", views.performance, name="performance"),
    path("embed/", widget_views.embed_settings, name="embed"),
    path("documents/upload/", views.upload, name="upload"),
    path("documents/<int:pk>/delete/", views.delete, name="delete"),
    path("documents/<int:pk>/rebuild/", views.rebuild, name="rebuild"),
]
