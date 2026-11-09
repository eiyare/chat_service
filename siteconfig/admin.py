"""Django admin: Settings > Platform settings (one page), and per-workspace
overrides shown on each workspace's page (Tenants)."""
from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import Truncator

from . import conf
from .models import PlatformSettings, WorkspaceSettings
from .spec import BOOL, LINE, SECTIONS, SPECS, TEXT, WORKSPACE_SPECS


def _fieldsets(specs, collapse=False, intro=None):
    sets = []
    for section in SECTIONS:
        names = [s.name for s in specs if s.section == section]
        if names:
            options = {"fields": names}
            if collapse:
                options["classes"] = ("collapse",)
            sets.append((section, options))
    if intro:
        sets[0][1]["description"] = intro
    return sets


def _widgets(specs):
    widgets = {}
    for s in specs:
        if s.kind == TEXT:
            widgets[s.name] = forms.Textarea(attrs={"rows": 14, "style": "width: 100%; font-family: monospace;"})
        elif s.kind == LINE:
            widgets[s.name] = forms.TextInput(attrs={"style": "width: 100%;"})
    return widgets


class PlatformSettingsForm(forms.ModelForm):
    class Meta:
        model = PlatformSettings
        fields = "__all__"
        widgets = _widgets(SPECS)


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    """Single page of platform-wide settings."""
    form = PlatformSettingsForm
    fieldsets = _fieldsets(
        SPECS,
        intro="These apply to every workspace. Settings marked on a workspace's page (Tenants > workspace > "
              "Settings overrides) can be changed for that workspace only. Changes apply within a few seconds; "
              "no restart needed.",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj, _ = PlatformSettings.objects.get_or_create(pk=1, defaults=conf.defaults())
        return redirect(reverse("admin:siteconfig_platformsettings_change", args=[obj.pk]))

    def response_change(self, request, obj):
        self.message_user(request, "Platform settings saved.", messages.SUCCESS)
        return redirect(reverse("admin:siteconfig_platformsettings_change", args=[obj.pk]))


class WorkspaceSettingsForm(forms.ModelForm):
    class Meta:
        model = WorkspaceSettings
        fields = "__all__"
        widgets = _widgets(WORKSPACE_SPECS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        platform = conf.get()
        for s in WORKSPACE_SPECS:
            field = self.fields[s.name]
            value = getattr(platform, s.name)
            if s.kind == BOOL:
                field.widget = forms.Select(choices=[
                    ("unknown", f"Platform setting ({'yes' if value else 'no'})"), ("true", "Yes"), ("false", "No"),
                ])
                field.help_text = s.help
                continue
            shown = Truncator(str(value).replace("\n", " ")).chars(160)
            field.help_text = f"Blank = platform setting: \u201c{shown}\u201d" + (f" {s.help}" if s.help else "")


class WorkspaceSettingsInline(admin.StackedInline):
    """Per-workspace overrides, on the workspace's page in Django admin."""
    model = WorkspaceSettings
    form = WorkspaceSettingsForm
    can_delete = False
    extra = 1
    max_num = 1
    # With max_num=1 Django titles the section with the singular name.
    verbose_name = "Settings overrides (blank = use the platform settings)"
    verbose_name_plural = "Settings overrides (blank = use the platform settings)"
    fieldsets = _fieldsets(WORKSPACE_SPECS, collapse=True)
