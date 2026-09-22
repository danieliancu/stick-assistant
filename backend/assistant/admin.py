from django.contrib import admin

from .models import AgendaItem


@admin.register(AgendaItem)
class AgendaItemAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "status", "starts_at", "due_at", "updated_at")
    list_filter = ("type", "status", "starts_at", "due_at", "created_at")
    search_fields = ("title", "description")
    date_hierarchy = "created_at"
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)
