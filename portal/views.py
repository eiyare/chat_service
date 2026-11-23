"""The public home page, the signed-in dashboard, sign-up and free chatbot creation."""
import time

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods

from siteconfig import conf
from tenants.models import Membership, Tenant
from widget.ratelimit import client_ip

from .forms import CreateChatbotForm, SignupForm

SIGNUPS_PER_IP_PER_HOUR = 5


def free_slots(user) -> int | None:
    """Workspaces this account may still create itself (None = unlimited)."""
    if user.is_superuser:
        return None
    allowed = conf.get().free_chatbots_per_account
    return max(0, allowed - Tenant.objects.filter(created_by=user).count())


def _plan():
    d = conf.get()
    return {
        "documents": d.default_max_documents,
        "file_mb": d.default_max_upload_mb,
        "types": d.default_allowed_file_types.upper().replace(",", ", "),
        "chatbots": d.free_chatbots_per_account,
        "signup": d.allow_signup,
    }


@login_not_required
def home(request):
    if not request.user.is_authenticated:
        from .use_cases import USE_CASES

        return render(request, "portal/landing.html", {"plan": _plan(), "login_form": AuthenticationForm(),
                                                       "use_cases": USE_CASES})
    return render(request, "portal/dashboard.html", {
        "workspaces": _workspaces(request.user), "slots": free_slots(request.user), "plan": _plan(),
    })


def _workspaces(user):
    qs = Tenant.objects.filter(is_active=True).annotate(document_count=Count("documents", distinct=True))
    if user.is_superuser:
        return [{"tenant": t, "role": "Superuser", "admin": True} for t in qs.order_by("name")]
    roles = dict(Membership.objects.filter(user=user).values_list("tenant_id", "role"))
    return [
        {"tenant": t, "role": Membership.Role(roles[t.pk]).label.split(" (")[0], "admin": roles[t.pk] == "admin"}
        for t in qs.filter(pk__in=roles).order_by("name")
    ]


def _signup_rate_ok(request) -> bool:
    key = f"signup-rl:{client_ip(request)}:{int(time.time() // 3600)}"
    if cache.add(key, 1, timeout=3700):
        return True
    try:
        return cache.incr(key) <= SIGNUPS_PER_IP_PER_HOUR
    except ValueError:
        return True


@login_not_required
@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("portal:create")
    if not conf.get().allow_signup:
        messages.info(request, "Sign-up is currently closed. Ask an administrator for an account.")
        return redirect("portal:home")
    form = SignupForm(request.POST or None)
    if request.method == "POST":
        if form.data.get("website"):  # spam trap filled in
            return redirect("portal:home")
        if not _signup_rate_ok(request):
            form.add_error(None, "Too many sign-ups from your network. Please try again later.")
        elif form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Welcome! Your account is ready. Now create your free chatbot.")
            return redirect("portal:create")
    return render(request, "portal/signup.html", {"form": form, "plan": _plan()})


def _unique_slug(name: str) -> str:
    base = slugify(name)[:50].strip("-") or "workspace"
    slug, n = base, 2
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


@require_http_methods(["GET", "POST"])
def create(request):
    slots = free_slots(request.user)
    if slots == 0:
        messages.info(request, "You've already created your free chatbot. Open it below.")
        return redirect("portal:home")
    form = CreateChatbotForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if free_slots(request.user) == 0:  # re-check inside the transaction
                return redirect("portal:home")
            tenant = Tenant.objects.create(
                name=form.cleaned_data["workspace_name"], slug=_unique_slug(form.cleaned_data["workspace_name"]),
                chatbot_name=form.cleaned_data["chatbot_name"], created_by=request.user,
            )
            Membership.objects.create(user=request.user, tenant=tenant, role=Membership.Role.ADMIN)
        messages.success(request, f"{tenant.chatbot_name} is ready. Upload the documents it should answer from.")
        return redirect(reverse("rag:home", kwargs={"tenant_slug": tenant.slug}))
    return render(request, "portal/create.html", {"form": form, "plan": _plan(), "slots": slots})


@login_not_required
def use_cases(request):
    """Every use case, for visitors choosing where they fit."""
    from .use_cases import USE_CASES

    return render(request, "portal/use_cases/index.html", {"use_cases": USE_CASES, "plan": _plan()})


@login_not_required
def use_case(request, slug):
    from django.http import Http404

    from .use_cases import BY_SLUG, USE_CASES

    case = BY_SLUG.get(slug)
    if case is None:
        raise Http404("No such use case")
    others = [u for u in USE_CASES if u.slug != slug]
    return render(request, case.template, {"case": case, "others": others, "plan": _plan()})


ENQUIRIES_PER_IP_PER_HOUR = 5


def _enquiry_rate_ok(request) -> bool:
    key = f"enquiry-rl:{client_ip(request)}:{int(time.time() // 3600)}"
    if cache.add(key, 1, timeout=3700):
        return True
    try:
        return cache.incr(key) <= ENQUIRIES_PER_IP_PER_HOUR
    except ValueError:
        return True


@login_not_required
@require_http_methods(["GET", "POST"])
def contact(request):
    """Book a demo / Contact us: saved, then emailed to the helpdesk (portal/contact.py).
    ?topic=demo picks "Book a demo"; ?use_case=<slug> records the page they came from."""
    from . import contact as contact_lib
    from .forms import ContactForm
    from .models import Enquiry
    from .use_cases import BY_SLUG

    topic = request.GET.get("topic")
    use_case = BY_SLUG.get(request.GET.get("use_case", ""))
    initial = {"kind": Enquiry.Kind.DEMO if topic == "demo" else Enquiry.Kind.CONTACT,
               "use_case": use_case.slug if use_case else ""}
    if request.user.is_authenticated:
        initial.update(name=request.user.get_full_name(), email=request.user.email)
    form = ContactForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.data.get("website"):  # spam trap filled in: pretend it worked
            return redirect("portal:contact_thanks")
        if not _enquiry_rate_ok(request):
            form.add_error(None, "Too many messages from your network. Please try again in an hour.")
        elif form.is_valid():
            enquiry = form.save()
            contact_lib.deliver(enquiry, request)
            request.session["enquiry_kind"] = enquiry.kind
            return redirect("portal:contact_thanks")
    cfg = conf.get()
    case = BY_SLUG.get(form["use_case"].value() or "")
    return render(request, "portal/contact.html", {
        "form": form, "plan": _plan(), "case": case,
        "helpdesk": cfg.helpdesk_email if cfg.show_helpdesk_email else "",
    })


@login_not_required
def contact_thanks(request):
    from .models import Enquiry

    demo = request.session.pop("enquiry_kind", "") == Enquiry.Kind.DEMO
    return render(request, "portal/contact_thanks.html", {"demo": demo, "plan": _plan()})
