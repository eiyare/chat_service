"""Delete request logs older than the admin setting "Keep request logs for (days)".

This also happens automatically once an hour while the site gets requests; use this command
to run it now, or from cron on a quiet site.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from siteconfig import conf
from traffic.models import RequestLog


class Command(BaseCommand):
    help = "Delete request logs older than the retention period set in Django admin."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Keep this many days instead of the admin setting")

    def handle(self, *args, days=None, **options):
        days = days or conf.get().traffic_retention_days
        deleted, _ = RequestLog.objects.filter(created_at__lt=timezone.now() - timezone.timedelta(days=days)).delete()
        self.stdout.write(f"Deleted {deleted} request logs older than {days} days.")
