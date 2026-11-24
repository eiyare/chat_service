from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("signup/check-email/", views.check_email, name="check_email"),
    path("verify/resend/", views.verify_resend, name="verify_resend"),
    path("verify/<str:token>/", views.verify_email, name="verify_email"),
    path("create/", views.create, name="create"),
    path("contact/", views.contact, name="contact"),
    path("contact/thanks/", views.contact_thanks, name="contact_thanks"),
    path("use-cases/", views.use_cases, name="use_cases"),
    path("use-cases/<slug:slug>/", views.use_case, name="use_case"),
]
