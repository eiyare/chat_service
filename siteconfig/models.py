"""Admin-managed behaviour settings: one platform-wide row, plus optional
per-workspace overrides (blank = use the platform value)."""
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from tenants.models import TOKEN_PERIODS, Tenant

from .spec import BOOL, FLOAT, GROUNDING, GROUNDING_MODES, INT, LINE, PERIOD, SPECS, TEXT, WORKSPACE_SPECS


def _field(spec, override: bool):
    validators = []
    if spec.min is not None:
        validators.append(MinValueValidator(spec.min))
    if spec.max is not None:
        validators.append(MaxValueValidator(spec.max))
    common = {"verbose_name": spec.label, "help_text": spec.help}
    blank = {"null": True, "blank": True} if override else {}
    if spec.kind == TEXT:
        return models.TextField(blank=override, **common)
    if spec.kind == LINE:
        return models.CharField(max_length=500, blank=override, **common)
    if spec.kind == INT:
        return models.IntegerField(validators=validators, **blank, **common)
    if spec.kind == FLOAT:
        return models.FloatField(validators=validators, **blank, **common)
    if spec.kind == BOOL:
        return models.BooleanField(null=True, blank=True, **common) if override else models.BooleanField(**common)
    if spec.kind == PERIOD:
        return models.CharField(max_length=8, choices=TOKEN_PERIODS, blank=override, **common)
    if spec.kind == GROUNDING:
        return models.CharField(max_length=10, choices=GROUNDING_MODES, blank=override, **common)
    raise ValueError(spec.kind)


class PlatformSettings(models.Model):
    """The single row of platform-wide settings (pk=1)."""

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "platform settings"
        verbose_name_plural = "platform settings"

    def __str__(self):
        return "Platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.chunk_overlap is not None and self.chunk_size is not None and self.chunk_overlap >= self.chunk_size:
            errors["chunk_overlap"] = "Must be smaller than the chunk size."
        from rag.extract import SUPPORTED_EXTENSIONS

        types = [t.strip().lower().lstrip(".") for t in (self.default_allowed_file_types or "").split(",") if t.strip()]
        bad = [t for t in types if t not in SUPPORTED_EXTENSIONS]
        if not types or bad:
            errors["default_allowed_file_types"] = f"Choose from {', '.join(SUPPORTED_EXTENSIONS)}."
        else:
            self.default_allowed_file_types = ",".join(dict.fromkeys(types))
        if errors:
            raise ValidationError(errors)


class WorkspaceSettings(models.Model):
    """Per-workspace overrides; any blank field uses the platform setting."""

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="settings_override",
                                  verbose_name="workspace")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "workspace settings"
        verbose_name_plural = "workspace settings"

    def __str__(self):
        return f"Settings for {self.tenant}"


for _spec in SPECS:
    PlatformSettings.add_to_class(_spec.name, _field(_spec, override=False))
for _spec in WORKSPACE_SPECS:
    WorkspaceSettings.add_to_class(_spec.name, _field(_spec, override=True))
