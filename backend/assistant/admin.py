from django.contrib import admin

from .models import AgendaItem, VoiceRequest


@admin.register(AgendaItem)
class AgendaItemAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "status", "starts_at", "due_at", "updated_at")
    list_filter = ("type", "status", "starts_at", "due_at", "created_at")
    search_fields = ("title", "description")
    date_hierarchy = "created_at"
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(VoiceRequest)
class VoiceRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "stage", "outcome", "created_at")
    list_filter = ("kind", "status", "stage", "outcome", "created_at")
    search_fields = ("id", "transcript", "reply_text")
    exclude = ("audio",)
    readonly_fields = [f.name for f in VoiceRequest._meta.fields if f.name != "audio"]

    def has_add_permission(self, request):
        return False
