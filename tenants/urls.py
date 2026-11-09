from django.urls import path

from . import views

app_name = "tenants"

urlpatterns = [
    path("", views.home, name="home"),
]
