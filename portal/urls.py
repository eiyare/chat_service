from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("create/", views.create, name="create"),
    path("use-cases/", views.use_cases, name="use_cases"),
    path("use-cases/<slug:slug>/", views.use_case, name="use_case"),
]
