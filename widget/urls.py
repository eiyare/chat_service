from django.urls import path

from . import views

app_name = "widget"

urlpatterns = [
    path("", views.page, name="page"),
    path("config.json", views.config, name="config"),
    path("api/send/", views.send, name="send"),
    path("api/history/", views.history, name="history"),
    path("moodle/documents/", views.moodle_documents, name="moodle_documents"),
    path("moodle/documents/<int:pk>/chunks/", views.moodle_chunks, name="moodle_chunks"),
    path("moodle/courses/", views.moodle_courses, name="moodle_courses"),
    path("moodle/courses/<int:course>/items/", views.moodle_course_items, name="moodle_course_items"),
    path("moodle/courses/<int:course>/items/delete/", views.moodle_course_items_delete,
         name="moodle_course_items_delete"),
    path("moodle/courses/<int:course>/delete/", views.moodle_course_delete, name="moodle_course_delete"),
]
