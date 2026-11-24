from django.conf import settings
from django.db import models


class Enquiry(models.Model):
    """A "Book a demo" or "Contact us" message from the public site. Kept even when the
    email to the helpdesk can't be sent, so nothing is lost."""

    class Kind(models.TextChoices):
        DEMO = "demo", "Book a demo"
        CONTACT = "contact", "Contact us"

    class Delivery(models.TextChoices):
        SENT = "sent", "Emailed to the helpdesk"
        FAILED = "failed", "Email failed"
        NO_HELPDESK = "no_helpdesk", "No helpdesk email set"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CONTACT)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    organisation = models.CharField(max_length=160, blank=True)
    role = models.CharField("Role", max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    use_case = models.CharField(max_length=60, blank=True, help_text="The use-case page they came from, if any")
    message = models.TextField(blank=True)
    delivery = models.CharField(max_length=12, choices=Delivery.choices, blank=True)
    sent_to = models.CharField(max_length=500, blank=True, help_text="The helpdesk address it was emailed to")
    delivery_error = models.CharField(max_length=500, blank=True)
    handled = models.BooleanField(default=False, help_text="Tick once someone has followed it up")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "enquiries"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name} ({self.organisation or self.email})"


class EmailVerification(models.Model):
    """An account created through sign-up waits here (inactive) until its owner clicks the
    link emailed to them. Accounts without a row (staff-made, or from before verification)
    count as verified."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_verification")
    email = models.EmailField(help_text="The address the link was sent to")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True, help_text="When the latest link was sent")
    sends = models.PositiveSmallIntegerField(default=0, help_text="Links sent so far")
    send_error = models.CharField(max_length=500, blank=True, help_text="Why the latest email failed, if it did")
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} ({'confirmed' if self.verified_at else 'waiting'})"

    @property
    def pending(self) -> bool:
        return self.verified_at is None
