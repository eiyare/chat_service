from django.contrib import admin, messages

from .models import Enquiry


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
