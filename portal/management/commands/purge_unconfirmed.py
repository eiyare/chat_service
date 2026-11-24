"""Delete sign-up accounts never confirmed within the admin setting's days.

This also happens at most hourly when someone signs up; run it now, or from cron.
"""
from django.core.management.base import BaseCommand

from portal.verification import purge_unconfirmed


class Command(BaseCommand):
    help = "Delete accounts whose email was never confirmed within the admin setting's days."

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted {purge_unconfirmed(force=True)} unconfirmed account(s).")
