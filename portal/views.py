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
        "verify": d.require_email_verification,
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


PENDING_KEY = "pending_verification"  # session: the account that just signed up, for "send it again"
RESENDS_PER_IP_PER_HOUR = 5


def _resend_rate_ok(request) -> bool:
    key = f"verify-rl:{client_ip(request)}:{int(time.time() // 3600)}"
    if cache.add(key, 1, timeout=3700):
        return True
    try:
        return cache.incr(key) <= RESENDS_PER_IP_PER_HOUR
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
            if conf.get().require_email_verification:
                from . import verification

                verification.purge_unconfirmed()
                verification.start(user, request)
                request.session[PENDING_KEY] = user.pk
                return redirect("portal:check_email")
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


@login_not_required
@require_http_methods(["GET", "POST"])
def check_email(request):
    """After sign-up: "check your inbox", with a button to send the link again."""
    from django.contrib.auth import get_user_model

    from . import verification
    from .forms import masked

    user = get_user_model().objects.filter(pk=request.session.get(PENDING_KEY)).first()
    record = verification.pending(user) if user else None
    if record is None:
        return redirect("portal:verify_resend")
    if request.method == "POST":
        if not verification.can_resend(record) or not _resend_rate_ok(request):
            messages.info(request, "We've just sent a link. Please wait a minute before asking for another.")
        elif verification.send(record, request):
            messages.success(request, "We've sent a new link.")
        else:
            messages.error(request, "We couldn't send the email just now. Please try again in a few minutes.")
        return redirect("portal:check_email")
    return render(request, "portal/check_email.html", {
        "email": masked(record.email), "failed": bool(record.send_error), "plan": _plan(),
        "hours": conf.get().verification_link_hours,
    })


@login_not_required
def verify_email(request, token):
    """The link in the email: confirms the address, activates the account and signs in."""
    from . import verification

    user, problem = verification.read_token(token, conf.get().verification_link_hours)
    record = verification.pending(user) if user else None
    if user is not None and record is None and user.is_active:
        messages.info(request, "Your email address is already confirmed.")
        return redirect("portal:create" if request.user.is_authenticated else "portal:home")
    if record is None or problem:
        return render(request, "portal/verify_failed.html", {
            "expired": problem == "expired" and record is not None, "plan": _plan(),
            "email": record.email if (record and problem == "expired") else "",
        }, status=400)
    verification.confirm(user)
    request.session.pop(PENDING_KEY, None)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Thanks, your email address is confirmed. Now create your free chatbot.")
    return redirect("portal:create")


@login_not_required
@require_http_methods(["GET", "POST"])
def verify_resend(request):
    """Ask for a new link by email address. The answer is the same whether or not an account
    is waiting for that address, so it can't be used to find out who has an account."""
    from django.contrib.auth import get_user_model

    from . import verification

    email = (request.POST.get("email") or request.GET.get("email") or "").strip()
    if request.method == "POST":
        if not _resend_rate_ok(request):
            messages.error(request, "Too many requests from your network. Please try again later.")
            return redirect("portal:verify_resend")
        user = get_user_model().objects.filter(email__iexact=email, is_active=False).first() if email else None
        record = verification.pending(user) if user else None
        if record is not None and verification.can_resend(record):
            verification.send(record, request)
        return render(request, "portal/verify_resent.html", {"plan": _plan()})
    return render(request, "portal/verify_resend.html", {"email": email, "plan": _plan()})
