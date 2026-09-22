from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("health/", views.health, name="health"),
    path("chat/", views.chat, name="chat"),
    path("agenda/today/", views.agenda_today, name="agenda-today"),
]
