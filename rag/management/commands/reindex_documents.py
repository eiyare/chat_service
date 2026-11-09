from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from rag import services
from rag.models import Document, current_embed_signature
from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Re-extract, re-chunk and re-embed documents from their stored files. "
        "By default only documents embedded with outdated settings (or failed ones) are processed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this workspace slug")
        parser.add_argument("--all", action="store_true", help="Reindex every document")

    def handle(self, *args, tenant=None, all=False, **options):
        qs = Document.objects.select_related("tenant")
        if tenant:
            if not Tenant.objects.filter(slug=tenant).exists():
                raise CommandError(f"No workspace with slug {tenant!r}")
            qs = qs.filter(tenant__slug=tenant)
        if not all:
            qs = qs.exclude(status=Document.Status.READY, embed_signature=current_embed_signature())
        documents = list(qs)
        if not documents:
            self.stdout.write("Nothing to reindex.")
            return
        # async_to_sync keeps ORM calls on this thread (and its DB connection).
        async_to_sync(self._run)(documents)

    async def _run(self, documents):
        ok = failed = 0
        for doc in documents:
            result = await services.process(doc, trigger="reindex")
            if result.status == Document.Status.READY:
                ok += 1
                self.stdout.write(f"  ok      {doc.tenant.slug}/{doc.name}: {result.chunk_count} chunks")
            else:
                failed += 1
                self.stdout.write(self.style.WARNING(f"  failed  {doc.tenant.slug}/{doc.name}: {result.error}"))
        self.stdout.write(self.style.SUCCESS(f"Reindexed {ok} document(s), {failed} failed."))
