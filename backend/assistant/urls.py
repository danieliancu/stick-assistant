from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("health/", views.health, name="health"),
    path("chat/", views.chat, name="chat"),
    path("agenda/today/", views.agenda_today, name="agenda-today"),
    path("voice/", views.voice, name="voice"),
    path("voice/agenda/", views.voice_agenda, name="voice-agenda"),
    path("voice/requests/<str:request_id>/", views.voice_request_status, name="voice-status"),
    path("voice/audio/<str:request_id>/", views.voice_audio, name="voice-audio"),
]
