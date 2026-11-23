"""Django admin: Settings > Platform settings (one page), and per-workspace
overrides shown on each workspace's page (Tenants)."""
from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.conf import settings
from django.utils.text import Truncator

from . import conf
from .models import PlatformSettings, WorkspaceSettings
from .spec import BOOL, LINE, SECRET, SECTIONS, SPECS, TEXT, WORKSPACE_SPECS

SECRETS = [s.name for s in SPECS if s.kind == SECRET]


def _fieldsets(specs, collapse=False, intro=None):
    sets = []
    for section in SECTIONS:
        names = []
        for s in specs:
            if s.section == section:
                names.append(s.name)
                if s.kind == SECRET:
                    names.append(f"{s.name}_clear")
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
    """Secrets are encrypted on save and never shown: leave the field blank to keep the saved
    value, or tick "Clear" to remove it. Each SECRET spec needs a "<name>_clear" field below."""

    smtp_password_clear = forms.BooleanField(required=False, label="Clear", help_text="Remove the saved password.")

    class Meta:
        model = PlatformSettings
        fields = "__all__"
        widgets = _widgets(SPECS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in SECRETS:
            saved = bool(self.instance and getattr(self.instance, name, ""))
            field = self.fields[name]
            field.required = False
            field.widget = forms.PasswordInput(render_value=False, attrs={
                "autocomplete": "new-password", "style": "width: 100%;",
                "placeholder": "Saved. Type a new one to replace it." if saved else ""})
            field.help_text = (("A value is saved. Leave blank to keep it. " if saved else "") + field.help_text)
            if not saved:  # nothing to clear
                self.fields[f"{name}_clear"].widget = forms.HiddenInput()

    def clean(self):
        data = super().clean()
        from llm import crypto

        for name in SECRETS:
            typed = (data.get(name) or "").strip()
            if data.get(f"{name}_clear"):
                data[name] = ""
            elif typed:
                data[name] = crypto.encrypt(typed)
            else:  # blank: keep what is saved (already encrypted)
                data[name] = getattr(self.instance, name, "") if self.instance else ""
        return data


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

    def get_urls(self):
        from django.urls import path

        return [path("test-email/", self.admin_site.admin_view(self.test_email),
                     name="siteconfig_platformsettings_test_email")] + super().get_urls()

    def test_email(self, request):
        """Send a test email with the saved SMTP settings, to the helpdesk (or you), and say how it went."""
        from django.core.mail import EmailMessage
        from django.http import HttpResponseNotAllowed

        from . import mail

        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        back = redirect(reverse("admin:siteconfig_platformsettings_change", args=[1]))
        if not self.has_change_permission(request):
            return back
        conf.invalidate()
        cfg = conf.get()
        to = cfg.helpdesk_email.strip() or request.user.email
        if not to:
            self.message_user(request, "Set the helpdesk email (or an email on your user account) to send a test "
                                       "email to.", messages.ERROR)
            return back
        if not mail.configured(cfg):
            self.message_user(request, "No SMTP server is set, so the test email was only written to the server "
                                       "log. Fill in Email (SMTP) and save first.", messages.WARNING)
        try:
            EmailMessage(f"Test email from {cfg.site_name}",
                         f"This test email was sent from Django admin by {request.user.get_username()}, using the "
                         f"SMTP server {cfg.smtp_host or '(none)'}:{cfg.smtp_port}.\n\nIf you can read it, email "
                         f"is working.", mail.sender(cfg), [to]).send(fail_silently=False)
        except Exception as exc:  # show the server's own answer: wrong password, port, sender ...
            self.message_user(request, f"The test email to {to} failed: {type(exc).__name__}: {exc}", messages.ERROR)
        else:
            if mail.configured(cfg):
                self.message_user(request, f"Test email sent to {to} through {cfg.smtp_host}. Check that inbox "
                                           f"(and its spam folder).", messages.SUCCESS)
        return back


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
