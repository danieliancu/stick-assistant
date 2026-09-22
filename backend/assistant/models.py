import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone


class AgendaItemQuerySet(models.QuerySet):
    def with_effective_at(self):
        """Annotate the effective datetime: starts_at for appointments, due_at for tasks."""
        return self.annotate(effective_at=Coalesce("starts_at", "due_at"))

    def active(self):
        return self.filter(status=AgendaItem.Status.PENDING)

    def in_range(self, start: datetime, end: datetime):
        """Items whose effective datetime falls in [start, end)."""
        return self.with_effective_at().filter(effective_at__gte=start, effective_at__lt=end)

    def undated(self):
        return self.filter(starts_at__isnull=True, due_at__isnull=True)


class AgendaItem(models.Model):
    class Type(models.TextChoices):
        TASK = "TASK", "Task"
        APPOINTMENT = "APPOINTMENT", "Appointment"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    # Allowed status transitions (from -> set of targets).
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.COMPLETED, Status.CANCELLED},
        Status.COMPLETED: {Status.PENDING},
        Status.CANCELLED: {Status.PENDING},
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.TASK)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    starts_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    # False when a task only has a due *date* (stored at local midnight) and no time.
    due_has_time = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AgendaItemQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at", "due_at", "created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["starts_at"]),
            models.Index(fields=["due_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()}: {self.title}"

    @property
    def when(self):
        return self.starts_at or self.due_at

    def clean(self):
        errors = {}

        self.title = (self.title or "").strip()
        if not self.title:
            errors["title"] = "Title cannot be empty."

        for field in ("starts_at", "due_at"):
            value = getattr(self, field)
            if value is None:
                continue
            if not isinstance(value, datetime):
                errors[field] = "Must be a datetime."
            elif timezone.is_naive(value):
                errors[field] = "Datetime must be timezone-aware."

        if self.type == self.Type.APPOINTMENT:
            if self.starts_at is None:
                errors["starts_at"] = "Appointments require a start date and time."
            if self.due_at is not None:
                errors["due_at"] = "Appointments use starts_at, not due_at."
        elif self.type == self.Type.TASK and self.starts_at is not None:
            errors["starts_at"] = "Tasks use due_at, not starts_at."

        if errors:
            raise ValidationError(errors)

    def _validate_status_transition(self):
        if self._state.adding:
            return
        previous = (
            type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
        )
        if previous is None or previous == self.status:
            return
        if self.status not in self.ALLOWED_TRANSITIONS.get(previous, set()):
            raise ValidationError(
                {"status": f"Cannot change status from {previous} to {self.status}."}
            )

    def save(self, *args, **kwargs):
        if self.status not in self.Status.values:
            raise ValidationError({"status": f"Invalid status: {self.status}."})
        self._validate_status_transition()
        super().save(*args, **kwargs)


class VoiceRequest(models.Model):
    """
    Execution record for one device voice request, keyed by the device-generated
    request_id. Makes retries idempotent: the agenda operation for a request runs
    at most once, and its outcome is persisted before any speech is generated.
    Raw recordings are never stored, only their SHA-256.
    """

    class Kind(models.TextChoices):
        VOICE = "VOICE", "Voice command"
        AGENDA_SUMMARY = "AGENDA_SUMMARY", "Today's agenda summary"

    class Status(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class Stage(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"            # nothing executed yet
        TRANSCRIBED = "TRANSCRIBED", "Transcribed"   # nothing executed yet
        EXECUTING = "EXECUTING", "Executing"         # agenda may be changing
        EXECUTED = "EXECUTED", "Executed"            # outcome persisted

    # Stages in which no agenda operation can have started: safe to run again.
    SAFE_TO_RETRY_STAGES = {Stage.RECEIVED, Stage.TRANSCRIBED}

    id = models.UUIDField(primary_key=True, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.VOICE)
    payload_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSING)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.RECEIVED)
    outcome = models.CharField(max_length=20, blank=True, default="")
    error_code = models.CharField(max_length=50, blank=True, default="")
    transcript = models.TextField(blank=True, default="")
    reply_text = models.TextField(blank=True, default="")
    language = models.CharField(max_length=5, blank=True, default="")
    actions = models.JSONField(default=list, blank=True)
    audio = models.BinaryField(null=True, blank=True, editable=False)
    audio_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at"]), models.Index(fields=["audio_expires_at"])]

    def __str__(self) -> str:
        return f"{self.kind} {self.id} {self.status}/{self.stage}"

    @property
    def is_safe_to_retry(self) -> bool:
        return self.stage in self.SAFE_TO_RETRY_STAGES

    def has_audio(self, now=None) -> bool:
        now = now or timezone.now()
        return bool(self.audio) and (self.audio_expires_at is None or self.audio_expires_at > now)
