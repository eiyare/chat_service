"""Keep stored files in step with Document rows, however they are deleted
(workspace page, Django admin, bulk delete, deleting a whole workspace)."""
from pathlib import Path

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Document


@receiver(post_delete, sender=Document, dispatch_uid="rag-delete-document-file")
def delete_document_file(sender, instance, **kwargs):
    if not instance.file:
        return
    storage, name = instance.file.storage, instance.file.name
    try:
        folder = Path(instance.file.path).parent
    except NotImplementedError:  # non-filesystem storage
        folder = None

    def cleanup():
        from .services import _remove_empty_dirs

        storage.delete(name)
        if folder is not None:
            _remove_empty_dirs(folder)

    # Only once the deletion is committed, so a rolled-back delete keeps its file.
    transaction.on_commit(cleanup)
