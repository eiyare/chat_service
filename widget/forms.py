from django import forms

from .models import Widget


class WidgetForm(forms.ModelForm):
    class Meta:
        model = Widget
        fields = ["enabled", "title", "greeting", "accent_color", "position", "allowed_domains",
                  "rate_limit_per_minute"]
        widgets = {
            "accent_color": forms.TextInput(attrs={"type": "color"}),
            "allowed_domains": forms.Textarea(attrs={"rows": 3, "placeholder": "example.com\nwww.example.com"}),
            "greeting": forms.TextInput(),
        }
        labels = {
            "enabled": "Widget enabled",
            "allowed_domains": "Websites allowed to embed it",
            "rate_limit_per_minute": "Messages per minute per visitor",
        }
