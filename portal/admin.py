from django.contrib import admin, messages

from .models import EmailVerification, Enquiry


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    """"Book a demo" and "Contact us" messages from the public site."""
    list_display = ("created_at", "kind", "name", "organisation", "email", "use_case", "delivery", "handled")
    list_filter = ("kind", "handled", "delivery", "use_case")
    search_fields = ("name", "email", "organisation", "message")
    date_hierarchy = "created_at"
    list_editable = ("handled",)
    readonly_fields = ("kind", "name", "email", "organisation", "role", "phone", "use_case", "message",
                       "delivery", "sent_to", "delivery_error", "created_at")
    fields = readonly_fields[:8] + ("handled",) + readonly_fields[8:]
    actions = ["mark_handled", "send_again"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark selected enquiries as handled")
    def mark_handled(self, request, queryset):
        self.message_user(request, f"{queryset.update(handled=True)} marked as handled.", messages.SUCCESS)

    @admin.action(description="Email selected enquiries to the helpdesk again")
    def send_again(self, request, queryset):
        from .contact import deliver

        for enquiry in queryset:
            deliver(enquiry, request)
        sent = queryset.filter(delivery=Enquiry.Delivery.SENT).count()
        level = messages.SUCCESS if sent == queryset.count() else messages.WARNING
        self.message_user(request, f"{sent} of {queryset.count()} emailed. See the Delivery column for the others.",
                          level)


@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    """Sign-ups waiting for (or done with) email confirmation."""
    list_display = ("email", "user", "created_at", "sent_at", "sends", "verified_at", "send_error")
    list_filter = (("verified_at", admin.EmptyFieldListFilter), "created_at")
    search_fields = ("email", "user__username")
    readonly_fields = ("user", "email", "created_at", "sent_at", "sends", "send_error", "verified_at")
    actions = ["send_again", "confirm_now"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Email the confirmation link again")
    def send_again(self, request, queryset):
        from .verification import send

        sent = sum(1 for record in queryset.filter(verified_at__isnull=True) if send(record, request))
        self.message_user(request, f"{sent} link(s) sent. Failures show in the Send error column.", messages.INFO)

    @admin.action(description="Confirm selected accounts now (without the email)")
    def confirm_now(self, request, queryset):
        from .verification import confirm

        records = list(queryset.filter(verified_at__isnull=True).select_related("user"))
        for record in records:
            confirm(record.user)
        self.message_user(request, f"{len(records)} account(s) confirmed and activated.", messages.SUCCESS)
