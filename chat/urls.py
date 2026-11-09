from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="home"),
    path("c/<int:pk>/", views.conversation_detail, name="conversation"),
    path("api/send/", views.api_send, name="api_send"),
    path("api/c/<int:pk>/delete/", views.api_delete, name="api_delete"),
]
