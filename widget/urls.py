from django.urls import path

from . import views

app_name = "widget"

urlpatterns = [
    path("", views.page, name="page"),
    path("config.json", views.config, name="config"),
    path("api/send/", views.send, name="send"),
    path("api/history/", views.history, name="history"),
]
