from django.conf import settings
from django.utils.functional import SimpleLazyObject


def site(request):
    """Expose non-secret site settings to every template."""
    def plan():
        from portal.views import _plan
        return _plan()

    return {"SITE_NAME": settings.SITE_NAME, "plan": SimpleLazyObject(plan)}
