"""Tests for the ontology review/validation workflow (ReviewService).

Covers the orchestration layer that wires reviewer sign-offs and the
quorum gate on top of the version lifecycle, persisting every decision
to the review audit log. Collaborators (registry service, session,
caches) are mocked; the focus is the workflow rules and side effects.
"""

import importlib

import pytest
from unittest.mock import MagicMock, patch

from back.core.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from back.objects.registry.PermissionService import (
    ROLE_ADMIN,
    ROLE_BUILDER,
    ROLE_EDITOR,
    ROLE_VIEWER,
    ROLE_NONE,
)
from back.objects.registry.ReviewService import (
    ACTION_APPROVED,
    ACTION_CHANGES_REQUESTED,
    ACTION_PUBLISHED,
    ACTION_SUBMITTED,
    ReviewService,
)

_mod = importlib.import_module("back.objects.registry.ReviewService")


def _request(email="alice@acme.com"):
    req = MagicMock()
    req.state.user_email = email
    req.headers = {}
    return req


def _make_svc(*, status="DRAFT", last_build="2026-01-01", quorum=1,
              versions=("1", "2"), initial_events=None, configured=True):
    info = {"status": status, "last_build": last_build}
    events = [dict(e) for e in (initial_events or [])]

    svc = MagicMock()
    svc.cfg.is_configured = configured
    svc.list_versions_sorted.return_value = list(versions)
    svc.read_version.return_value = (True, {"info": info}, "")
    svc.store.get_domain_quorum.return_value = quorum

    def _set_status(folder, version, new_status):
        info["status"] = new_status
        return True, "ok"

    svc.set_version_status.side_effect = _set_status

    def _record(folder, version, actor, action, *, from_status="",
                to_status="", comment="", meta=None):
        events.append({
            "folder": folder, "version": version, "actor": actor,
            "action": action, "from_status": from_status,
            "to_status": to_status, "comment": comment,
            "meta": meta or {},
            "created_at": "2026-01-01T00:00:%02d" % len(events),
        })
        return True, ""

    svc.record_review_event.side_effect = _record
    svc.list_review_events.side_effect = lambda folder, version=None: list(events)
    return svc, info, events


def _patch(svc):
    domain = MagicMock()
    domain.domain_folder = "other"
    domain.current_version = "9"
    domain.info = {}
    return patch.multiple(
        _mod,
        get_domain=MagicMock(return_value=domain),
        invalidate_registry_cache=MagicMock(),
    ), patch.object(_mod.RegistryService, "from_context", return_value=svc)


def _call(method, svc, **kwargs):
    p1, p2 = _patch(svc)
    with p1, p2:
        return getattr(ReviewService, method)(
            _request(kwargs.pop("email", "alice@acme.com")),
            MagicMock(),  # session_mgr
            MagicMock(),  # settings
            "acme",
            "2",
            **kwargs,
        )


# ----------------------------------------------------------------------
# Submit
# ----------------------------------------------------------------------


def test_submit_draft_to_review_succeeds():
    svc, info, events = _make_svc(status="DRAFT")
    result = _call("submit", svc, comment="ready",
                   user_role="", user_domain_role=ROLE_BUILDER)
    assert result["success"] is True
    assert result["status"] == "IN-REVIEW"
    assert info["status"] == "IN-REVIEW"
    assert events[-1]["action"] == ACTION_SUBMITTED
    assert events[-1]["from_status"] == "DRAFT"
    assert events[-1]["to_status"] == "IN-REVIEW"


def test_submit_requires_builder():
    svc, _, _ = _make_svc(status="DRAFT")
    with pytest.raises(AuthorizationError):
        _call("submit", svc, comment="", user_role="",
              user_domain_role=ROLE_EDITOR)


def test_submit_requires_build():
    svc, _, _ = _make_svc(status="DRAFT", last_build="")
    with pytest.raises(ValidationError):
        _call("submit", svc, comment="", user_role=ROLE_ADMIN,
              user_domain_role=ROLE_NONE)


def test_submit_wrong_status_conflicts():
    svc, _, _ = _make_svc(status="IN-REVIEW")
    with pytest.raises(ConflictError):
        _call("submit", svc, comment="", user_role=ROLE_ADMIN,
              user_domain_role=ROLE_NONE)


# ----------------------------------------------------------------------
# Sign-off
# ----------------------------------------------------------------------


def test_signoff_approve_records_event():
    svc, info, events = _make_svc(status="IN-REVIEW", quorum=2)
    result = _call("signoff", svc, decision="approve", comment="lgtm",
                   user_role="", user_domain_role=ROLE_VIEWER)
    assert result["success"] is True
    assert info["status"] == "IN-REVIEW"  # approve does not change status
    assert events[-1]["action"] == ACTION_APPROVED
    assert result["approvals"] == 1


def test_signoff_approve_twice_conflicts():
    prior = [{
        "folder": "acme", "version": "2", "actor": "alice@acme.com",
        "action": ACTION_SUBMITTED, "from_status": "DRAFT",
        "to_status": "IN-REVIEW", "comment": "", "meta": {},
        "created_at": "2026-01-01T00:00:00",
    }, {
        "folder": "acme", "version": "2", "actor": "alice@acme.com",
        "action": ACTION_APPROVED, "from_status": "", "to_status": "",
        "comment": "", "meta": {}, "created_at": "2026-01-01T00:00:01",
    }]
    svc, _, _ = _make_svc(status="IN-REVIEW", initial_events=prior)
    with pytest.raises(ConflictError):
        _call("signoff", svc, decision="approve", comment="",
              user_role="", user_domain_role=ROLE_VIEWER,
              email="alice@acme.com")


def test_signoff_request_changes_reopens_to_draft():
    svc, info, events = _make_svc(status="IN-REVIEW")
    _call("signoff", svc, decision="request_changes", comment="fix names",
          user_role="", user_domain_role=ROLE_VIEWER)
    assert info["status"] == "DRAFT"
    assert events[-1]["action"] == ACTION_CHANGES_REQUESTED
    assert events[-1]["to_status"] == "DRAFT"


def test_signoff_requires_membership():
    svc, _, _ = _make_svc(status="IN-REVIEW")
    with pytest.raises(AuthorizationError):
        _call("signoff", svc, decision="approve", comment="",
              user_role="", user_domain_role=ROLE_NONE)


def test_signoff_bad_decision_rejected():
    svc, _, _ = _make_svc(status="IN-REVIEW")
    with pytest.raises(ValidationError):
        _call("signoff", svc, decision="maybe", comment="",
              user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)


# ----------------------------------------------------------------------
# Publish
# ----------------------------------------------------------------------


def test_publish_blocked_until_quorum():
    # A non-admin builder is gated by the quorum.
    svc, _, _ = _make_svc(status="IN-REVIEW", quorum=2)
    with pytest.raises(ConflictError):
        _call("publish", svc, comment="", user_role="",
              user_domain_role=ROLE_BUILDER)


def test_publish_admin_overrides_quorum():
    # An admin may publish even when the quorum is not met.
    svc, info, events = _make_svc(status="IN-REVIEW", quorum=3)
    result = _call("publish", svc, comment="override",
                   user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert result["status"] == "PUBLISHED"
    assert info["status"] == "PUBLISHED"
    assert events[-1]["action"] == ACTION_PUBLISHED
    assert events[-1]["meta"]["quorum_override"] is True


def test_publish_domain_admin_overrides_quorum():
    svc, info, _ = _make_svc(status="IN-REVIEW", quorum=2)
    result = _call("publish", svc, comment="override",
                   user_role="", user_domain_role=ROLE_ADMIN)
    assert result["status"] == "PUBLISHED"
    assert info["status"] == "PUBLISHED"


def test_publish_succeeds_when_quorum_met():
    prior = [{
        "folder": "acme", "version": "2", "actor": "bob@acme.com",
        "action": ACTION_SUBMITTED, "from_status": "DRAFT",
        "to_status": "IN-REVIEW", "comment": "", "meta": {},
        "created_at": "2026-01-01T00:00:00",
    }, {
        "folder": "acme", "version": "2", "actor": "carol@acme.com",
        "action": ACTION_APPROVED, "from_status": "", "to_status": "",
        "comment": "", "meta": {}, "created_at": "2026-01-01T00:00:01",
    }]
    svc, info, events = _make_svc(status="IN-REVIEW", quorum=1,
                                  initial_events=prior)
    result = _call("publish", svc, comment="ship it",
                   user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert result["status"] == "PUBLISHED"
    assert info["status"] == "PUBLISHED"
    assert events[-1]["action"] == ACTION_PUBLISHED


def test_publish_requires_builder():
    svc, _, _ = _make_svc(status="IN-REVIEW", quorum=1)
    with pytest.raises(AuthorizationError):
        _call("publish", svc, comment="", user_role="",
              user_domain_role=ROLE_VIEWER)


# ----------------------------------------------------------------------
# Reopen
# ----------------------------------------------------------------------


def test_reopen_admin_only():
    svc, _, _ = _make_svc(status="PUBLISHED")
    with pytest.raises(AuthorizationError):
        _call("reopen", svc, comment="", user_role="",
              user_domain_role=ROLE_BUILDER)


def test_reopen_published_to_draft():
    svc, info, events = _make_svc(status="PUBLISHED")
    _call("reopen", svc, comment="bugfix", user_role=ROLE_ADMIN,
          user_domain_role=ROLE_NONE)
    assert info["status"] == "DRAFT"
    assert events[-1]["action"] == "reopened"


# ----------------------------------------------------------------------
# Lookup failures
# ----------------------------------------------------------------------


def test_unknown_version_raises_not_found():
    svc, _, _ = _make_svc(status="DRAFT", versions=("1",))
    with pytest.raises(NotFoundError):
        _call("submit", svc, comment="", user_role=ROLE_ADMIN,
              user_domain_role=ROLE_NONE)


def test_registry_not_configured_raises():
    svc, _, _ = _make_svc(configured=False)
    with pytest.raises(ValidationError):
        _call("submit", svc, comment="", user_role=ROLE_ADMIN,
              user_domain_role=ROLE_NONE)


# ----------------------------------------------------------------------
# Pure helpers: summary + pending actions
# ----------------------------------------------------------------------


def test_summarize_resets_approvals_on_resubmit():
    events = [
        {"action": ACTION_SUBMITTED, "actor": "b", "created_at": "t1"},
        {"action": ACTION_APPROVED, "actor": "x@a.com", "created_at": "t2"},
        {"action": ACTION_CHANGES_REQUESTED, "actor": "y@a.com", "created_at": "t3"},
        {"action": ACTION_SUBMITTED, "actor": "b", "created_at": "t4"},
        {"action": ACTION_APPROVED, "actor": "z@a.com", "created_at": "t5"},
    ]
    summary = ReviewService._summarize(events)
    assert summary["approvals"] == 1
    assert summary["approvers"] == ["z@a.com"]
    assert summary["last_activity"] == "t5"


def test_summarize_dedups_same_approver():
    events = [
        {"action": ACTION_SUBMITTED, "actor": "b", "created_at": "t1"},
        {"action": ACTION_APPROVED, "actor": "X@a.com", "created_at": "t2"},
        {"action": ACTION_APPROVED, "actor": "x@a.com", "created_at": "t3"},
    ]
    summary = ReviewService._summarize(events)
    assert summary["approvals"] == 1


def test_pending_actions_draft_builder_can_submit():
    actions = ReviewService._pending_actions(
        "DRAFT", ROLE_BUILDER, "a@a.com",
        {"approvers": [], "approvals": 0}, 1, "2026-01-01")
    assert [a["id"] for a in actions] == ["submit"]


def test_pending_actions_draft_without_build_has_none():
    actions = ReviewService._pending_actions(
        "DRAFT", ROLE_BUILDER, "a@a.com",
        {"approvers": [], "approvals": 0}, 1, "")
    assert actions == []


def test_pending_actions_review_member_can_review():
    actions = ReviewService._pending_actions(
        "IN-REVIEW", ROLE_VIEWER, "a@a.com",
        {"approvers": [], "approvals": 0}, 2, "2026-01-01")
    assert [a["id"] for a in actions] == ["review"]


def test_pending_actions_review_builder_publish_when_quorum_met():
    actions = ReviewService._pending_actions(
        "IN-REVIEW", ROLE_BUILDER, "a@a.com",
        {"approvers": ["x@a.com"], "approvals": 1}, 1, "2026-01-01")
    ids = [a["id"] for a in actions]
    assert "publish" in ids


def test_pending_actions_already_approved_member_has_no_review():
    actions = ReviewService._pending_actions(
        "IN-REVIEW", ROLE_VIEWER, "a@a.com",
        {"approvers": ["a@a.com"], "approvals": 1}, 2, "2026-01-01")
    assert actions == []


def test_pending_actions_builder_no_publish_below_quorum():
    actions = ReviewService._pending_actions(
        "IN-REVIEW", ROLE_BUILDER, "a@a.com",
        {"approvers": [], "approvals": 0}, 2, "2026-01-01")
    assert "publish" not in [a["id"] for a in actions]


def test_pending_actions_admin_publish_below_quorum():
    actions = ReviewService._pending_actions(
        "IN-REVIEW", ROLE_ADMIN, "a@a.com",
        {"approvers": [], "approvals": 0}, 3, "2026-01-01")
    assert "publish" in [a["id"] for a in actions]


def test_review_detail_admin_can_publish_below_quorum():
    svc, _, _ = _make_svc(status="IN-REVIEW", quorum=3)
    result = _call("review_detail", svc,
                   user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert result["actions"]["can_publish"] is True
    assert result["publish_override"] is True
    assert result["quorum_met"] is False


# ----------------------------------------------------------------------
# Comment persistence (one comment per status switch -> audit trail)
# ----------------------------------------------------------------------


def test_submit_persists_comment():
    svc, _, events = _make_svc(status="DRAFT")
    _call("submit", svc, comment="please review carefully",
          user_role="", user_domain_role=ROLE_BUILDER)
    assert events[-1]["action"] == ACTION_SUBMITTED
    assert events[-1]["comment"] == "please review carefully"


def test_signoff_approve_persists_comment():
    svc, _, events = _make_svc(status="IN-REVIEW", quorum=2)
    _call("signoff", svc, decision="approve", comment="looks great",
          user_role="", user_domain_role=ROLE_VIEWER)
    assert events[-1]["action"] == ACTION_APPROVED
    assert events[-1]["comment"] == "looks great"


def test_signoff_request_changes_persists_comment():
    svc, _, events = _make_svc(status="IN-REVIEW")
    _call("signoff", svc, decision="request_changes",
          comment="rename Person -> Individual",
          user_role="", user_domain_role=ROLE_VIEWER)
    assert events[-1]["action"] == ACTION_CHANGES_REQUESTED
    assert events[-1]["comment"] == "rename Person -> Individual"


def test_publish_persists_comment():
    prior = [{
        "folder": "acme", "version": "2", "actor": "carol@acme.com",
        "action": ACTION_APPROVED, "from_status": "", "to_status": "",
        "comment": "", "meta": {}, "created_at": "2026-01-01T00:00:01",
    }]
    svc, _, events = _make_svc(status="IN-REVIEW", quorum=1,
                               initial_events=prior)
    _call("publish", svc, comment="shipping v2",
          user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert events[-1]["action"] == ACTION_PUBLISHED
    assert events[-1]["comment"] == "shipping v2"


def test_reopen_persists_comment():
    svc, _, events = _make_svc(status="PUBLISHED")
    _call("reopen", svc, comment="hotfix needed",
          user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert events[-1]["action"] == "reopened"
    assert events[-1]["comment"] == "hotfix needed"


def test_review_detail_surfaces_events_with_comments():
    """The chat-style comment viewer reads ``events`` from review_detail."""
    prior = [{
        "folder": "acme", "version": "2", "actor": "bob@acme.com",
        "action": ACTION_SUBMITTED, "from_status": "DRAFT",
        "to_status": "IN-REVIEW", "comment": "initial submit",
        "meta": {}, "created_at": "2026-01-01T00:00:00",
    }, {
        "folder": "acme", "version": "2", "actor": "carol@acme.com",
        "action": ACTION_APPROVED, "from_status": "", "to_status": "",
        "comment": "lgtm from me", "meta": {},
        "created_at": "2026-01-01T00:00:01",
    }]
    svc, _, _ = _make_svc(status="IN-REVIEW", initial_events=prior)
    result = _call("review_detail", svc,
                   user_role=ROLE_ADMIN, user_domain_role=ROLE_NONE)
    assert result["success"] is True
    comments = [e["comment"] for e in result["events"]]
    assert comments == ["initial submit", "lgtm from me"]
