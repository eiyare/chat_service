from django import forms

from .models import Integration


class IntegrationForm(forms.ModelForm):
    class Meta:
        model = Integration
        fields = ("name", "kind", "site_url", "enabled", "signed_users", "content", "tools", "share_documents")
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Company website (Odoo)"}),
                   "site_url": forms.URLInput(attrs={"placeholder": "https://www.example.com"})}

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        if self.instance.pk:
            self.fields.pop("kind")  # fixed once created

    def clean(self):
        data = super().clean()
        kind = data.get("kind", self.instance.kind)
        if kind == Integration.Kind.MOODLE and not self.instance.pk and Integration.objects.filter(
                tenant=self.tenant, kind=Integration.Kind.MOODLE).exists():
            self.add_error("kind", "This workspace already has a Moodle connection.")
        if kind == Integration.Kind.APP and data.get("tools") and not data.get("site_url"):
            self.add_error("site_url", "Tools are called on the app's address: enter it.")
        return data

    def save(self, commit=True):
        integration = super().save(commit=False)
        integration.tenant = self.tenant
        if commit:
            integration.save()
        return integration
