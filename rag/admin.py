"""Django admin for the knowledge base.

Documents are what you manage: rename, delete, rebuild. Chunks are generated
from documents, so they are read-only here; to change them, rebuild their
document (re-split and re-embed). That keeps counts and the index consistent.
"""
from asgiref.sync import async_to_sync
from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html

from llm import get_config

from . import services
from .models import FAQ, Chunk, Document, embed_signature


def _rebuild(modeladmin, request, documents):
    ok = failed = 0
    for document in documents:
        result = async_to_sync(services.process)(document, trigger="rebuild")
        if result.status == Document.Status.READY:
            ok += 1
        else:
            failed += 1
            modeladmin.message_user(request, f"{document.name}: {result.error}", messages.ERROR)
    modeladmin.message_user(
        request, f"Rebuilt chunks for {ok} document{'' if ok == 1 else 's'}; {failed} failed.",
        messages.SUCCESS if not failed else messages.WARNING,
    )


class IndexStateFilter(admin.SimpleListFilter):
    title = "index"
    parameter_name = "index"

    def lookups(self, request, model_admin):
        return [("current", "Current"), ("rebuild", "Needs rebuild")]

    def queryset(self, request, queryset):
        signature = embed_signature(get_config())
        if self.value() == "current":
            return queryset.filter(status=Document.Status.READY, embed_signature=signature)
        if self.value() == "rebuild":
            return queryset.exclude(status=Document.Status.READY, embed_signature=signature)
        return queryset


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "status", "index_state", "chunks_link", "size_display", "created_at")
    list_filter = ("tenant", "status", IndexStateFilter, "extension")
    search_fields = ("name", "tenant__name")
    list_select_related = ("tenant",)
    actions = ["rebuild_chunks", "delete_selected"]
    fields = ("name", "tenant", "status", "error", "index_state", "chunks_link", "size_display", "extension",
              "embed_model", "file", "sha256", "created_at", "updated_at")
    readonly_fields = ("tenant", "status", "error", "index_state", "chunks_link", "size_display", "extension",
                       "embed_model", "file", "sha256", "created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_chunks=Count("chunks"))

    def has_add_permission(self, request):
        return False  # uploads go through the workspace's Admin page, which validates and indexes them

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            self.message_user(
                request,
                "Upload documents from each workspace's Admin page. Here you can rename, delete, or rebuild a "
                "document's chunks; deleting a document also deletes its chunks and stored file.",
                messages.INFO,
            )
        return super().changelist_view(request, extra_context)

    @admin.display(description="Chunks", ordering="_chunks")
    def chunks_link(self, obj):
        count = getattr(obj, "_chunks", None)
        if count is None:
            count = obj.chunks.count()
        url = reverse("admin:rag_chunk_changelist") + f"?document__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, count)

    @admin.display(description="Index")
    def index_state(self, obj):
        if obj.status == Document.Status.FAILED:
            return "Failed - rebuild to retry"
        if obj.status == Document.Status.PROCESSING:
            return "Processing"
        if obj.embed_signature != embed_signature(get_config()):
            return "Needs rebuild (embedding settings changed)"
        return "Current"

    @admin.display(description="Size", ordering="size")
    def size_display(self, obj):
        from django.template.defaultfilters import filesizeformat
        return filesizeformat(obj.size)

    @admin.action(description="Rebuild chunks (re-split and re-embed)")
    def rebuild_chunks(self, request, queryset):
        _rebuild(self, request, queryset)

    def get_deleted_objects(self, objs, request):
        # Chunks can't be deleted on their own, but deleting their document removes them.
        deleted, counts, perms_needed, protected = super().get_deleted_objects(objs, request)
        perms_needed.discard(Chunk._meta.verbose_name)
        return deleted, counts, perms_needed, protected

    def delete_queryset(self, request, queryset):
        # Row-by-row so every document's file is removed too (rag/signals.py).
        for document in queryset:
            document.delete()


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    """Read-only: chunks are generated from documents."""
    list_display = ("document", "index", "tenant", "characters", "preview")
    list_filter = ("tenant", ("document", admin.RelatedOnlyFieldListFilter))
    search_fields = ("text", "document__name")
    list_select_related = ("document", "tenant")
    actions = ["rebuild_documents"]
    fields = ("document", "tenant", "index", "characters", "dimensions", "text")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # deleting a chunk by hand would leave its document partially indexed

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            self.message_user(
                request,
                "Chunks are generated from documents and can't be edited or deleted here. To change them, "
                "rebuild the document (select chunks below > 'Rebuild their documents', or use Documents).",
                messages.INFO,
            )
        return super().changelist_view(request, extra_context)

    @admin.display(description="Characters")
    def characters(self, obj):
        return len(obj.text)

    @admin.display(description="Text")
    def preview(self, obj):
        return obj.text[:90] + ("..." if len(obj.text) > 90 else "")

    @admin.action(description="Rebuild their documents (re-split and re-embed)")
    def rebuild_documents(self, request, queryset):
        ids = set(queryset.values_list("document_id", flat=True))
        _rebuild(self, request, Document.objects.filter(pk__in=ids))

    def get_actions(self, request):
        # Read-only models lose "delete selected" anyway; make sure our action stays available.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


def _index_faqs(modeladmin, request, faqs, quiet_success=False):
    from llm import LLMError

    from . import faq as faq_lib

    config = get_config()
    if not config.embed_model:
        modeladmin.message_user(
            request, "No embedding model is enabled, so FAQs can only match questions asked word for word.",
            messages.WARNING,
        )
        return
    try:
        indexed = async_to_sync(faq_lib.index)(list(faqs), config)
    except LLMError as exc:
        modeladmin.message_user(
            request, f"Couldn't index FAQs ({exc}); they'll be indexed automatically on first use.",
            messages.WARNING,
        )
        return
    if not quiet_success:
        modeladmin.message_user(request, f"Indexed {len(indexed)} FAQ{'' if len(indexed) == 1 else 's'}.",
                                messages.SUCCESS)


@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ("question", "tenant", "enabled", "phrasing_count", "hit_count", "last_hit_at", "index_state")
    list_editable = ("enabled",)
    list_filter = ("tenant", "enabled")
    search_fields = ("question", "alternate_questions", "answer", "tenant__name")
    list_select_related = ("tenant",)
    actions = ["reindex"]
    fields = ("tenant", "question", "alternate_questions", "answer", "enabled", "hit_count", "last_hit_at")
    readonly_fields = ("hit_count", "last_hit_at")

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            self.message_user(
                request,
                "A chat question matching an FAQ is answered with its answer directly, without calling the model. "
                "It matches when asked word for word, or when it uses no key words outside the FAQ's wording "
                "(add common rewordings under 'Other phrasings'). Close FAQs are also given to the model as context.",
                messages.INFO,
            )
        return super().changelist_view(request, extra_context)

    @admin.display(description="Phrasings")
    def phrasing_count(self, obj):
        return len(obj.phrasings())

    @admin.display(description="Index")
    def index_state(self, obj):
        from . import faq as faq_lib

        config = get_config()
        if not config.embed_model:
            return "Exact match only"
        return "Current" if obj.embed_signature == faq_lib.signature(obj, config) else "Pending (indexed on first use)"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.enabled:
            _index_faqs(self, request, [obj], quiet_success=True)

    @admin.action(description="Re-index selected FAQs")
    def reindex(self, request, queryset):
        _index_faqs(self, request, queryset)
