from django.utils.functional import SimpleLazyObject

from siteconfig import conf


def site(request):
    """Expose non-secret site settings to every template."""
    def plan():
        from portal.views import _plan
        return _plan()

    return {"SITE_NAME": conf.get().site_name, "plan": SimpleLazyObject(plan)}
