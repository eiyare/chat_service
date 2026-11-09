from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text="For account notices. Not shared.")
    # Spam trap: hidden from people, filled in by bots.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
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
