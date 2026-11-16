"""
ASGI config for saas_chat project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saas_chat.settings")

application = get_asgi_application()

# Document processing worker (rag/jobs.py): also resumes jobs a restart interrupted.
from rag import jobs  # noqa: E402  (needs Django set up first)

jobs.start()
