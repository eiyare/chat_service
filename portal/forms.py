from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import Enquiry


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text="For account notices. Not shared.")
    # Spam trap: hidden from people, filled in by bots.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    pending_email = False  # set when the address belongs to an account still waiting for confirmation

    def clean_email(self):
        from .verification import pending

        email = self.cleaned_data["email"].strip().lower()
        existing = get_user_model().objects.filter(email__iexact=email).first()
        if existing is not None:
            if pending(existing):
                self.pending_email = True
                raise forms.ValidationError("An account with this email is waiting to be confirmed. Check your "
                                            "inbox for the link, or ask for a new one.")
            raise forms.ValidationError("An account with this email already exists. Sign in instead.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class CreateChatbotForm(forms.Form):
    workspace_name = forms.CharField(
        max_length=80, label="Workspace name",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Acme Support", "autofocus": True}),
        help_text="Usually your company, team or project.",
    )
    chatbot_name = forms.CharField(
        max_length=60, initial="Tosin", label="Chatbot name",
        help_text="What your assistant calls itself. You can change it later.",
    )

    def clean_chatbot_name(self):
        name = self.cleaned_data["chatbot_name"].strip()
        if not name:
            raise forms.ValidationError("Give your assistant a name.")
        return name


class ContactForm(forms.ModelForm):
    """Book a demo / Contact us. ``website`` is a spam trap that people never see."""

    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))

    class Meta:
        model = Enquiry
        fields = ("kind", "name", "email", "organisation", "role", "phone", "message", "use_case")
        labels = {"kind": "I'd like to", "organisation": "School or organisation", "role": "Your role",
                  "phone": "Phone (optional)", "message": "Message"}
        widgets = {
            "kind": forms.RadioSelect,
            "use_case": forms.HiddenInput,
            "message": forms.Textarea(attrs={"rows": 5}),
            "name": forms.TextInput(attrs={"autocomplete": "name"}),
            "email": forms.EmailInput(attrs={"autocomplete": "email"}),
            "organisation": forms.TextInput(attrs={"autocomplete": "organization"}),
            "role": forms.TextInput(attrs={"autocomplete": "organization-title"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel", "inputmode": "tel"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = list(Enquiry.Kind.choices)
        self.fields["message"].max_length = 5000
        self.fields["message"].widget.attrs["maxlength"] = 5000

    def clean_use_case(self):
        from .use_cases import BY_SLUG

        slug = self.cleaned_data.get("use_case") or ""
        return slug if slug in BY_SLUG else ""

    def clean(self):
        data = super().clean()
        if data.get("kind") == Enquiry.Kind.CONTACT and not (data.get("message") or "").strip():
            self.add_error("message", "Tell us how we can help.")
        return data


def masked(email: str) -> str:
    """a***@example.com: enough to recognise, not enough to harvest."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "your email address"


class PortalAuthenticationForm(AuthenticationForm):
    """Sign-in that tells someone with an unconfirmed account (right password) to confirm
    their email, instead of "wrong username or password"."""

    unconfirmed_user = None

    def clean(self):
        try:
            return super().clean()
        except forms.ValidationError:
            from .verification import pending

            username, password = self.cleaned_data.get("username"), self.cleaned_data.get("password")
            User = get_user_model()
            user = User.objects.filter(**{f"{User.USERNAME_FIELD}__iexact": username}).first() if username else None
            if user is not None and not user.is_active and pending(user) and user.check_password(password or ""):
                self.unconfirmed_user = user
                raise forms.ValidationError(
                    f"Please confirm your email address first: we sent a link to {masked(user.email)}.",
                    code="unconfirmed")
            raise
