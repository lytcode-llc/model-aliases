"""resolver: matching, filters, latest selection, alias resolution."""

from __future__ import annotations

from datetime import date

import pytest

from model_aliases.config import AliasSpec
from model_aliases.resolver import (
    Candidate,
    candidate_matches,
    make_candidate,
    natural_key,
    resolve_aliases,
    select_latest,
)

TP = "targetco"


def _targetco(models_dev: dict) -> list[Candidate]:
    return [make_candidate(e) for e in models_dev["targetco"]["models"].values()]


def _aggco(models_dev: dict) -> list[Candidate]:
    return [make_candidate(e) for e in models_dev["aggco"]["models"].values()]


def _spec(**kw) -> AliasSpec:
    kw.setdefault("match", ("tcl-*",))
    return AliasSpec(**kw)


# --- natural_key -----------------------------------------------------------

def test_natural_key_orders_versions():
    assert natural_key("tcl-4.1") > natural_key("tcl-4.0") > natural_key("tcl-3.5") > natural_key("tcl-2.0")


def test_natural_key_never_raises_on_mixed_input():
    # mixed digit/non-digit shapes must stay comparable in both directions
    a, b = natural_key("gpt-4o"), natural_key("gpt-4.0")
    assert a != b and (a > b or a < b)
    assert natural_key("") <= natural_key("0")


# --- candidate_matches -----------------------------------------------------

def test_match_bare_and_prefixed_forms(models_dev):
    c = next(x for x in _targetco(models_dev) if x.id == "tcl-4.1")
    bare = _spec(match=("tcl-4*",))
    prefixed = _spec(match=("targetco/tcl-4*",))
    assert candidate_matches(c, bare, TP)
    assert candidate_matches(c, prefixed, TP)


def test_exclude_wins_over_match(models_dev):
    c = next(x for x in _targetco(models_dev) if x.id == "tcl-4.1-preview")
    keep = _spec(match=("tcl-4*",))
    drop = _spec(match=("tcl-4*",), exclude=("targetco/*-preview",))
    assert candidate_matches(c, keep, TP)
    assert not candidate_matches(c, drop, TP)


def test_empty_match_never_matches(models_dev):
    c = _targetco(models_dev)[0]
    assert not candidate_matches(c, _spec(match=()), TP)


def test_slash_containing_aggregator_ids_match_raw(models_dev):
    c = next(x for x in _aggco(models_dev) if x.id == "vendor/fast-2")
    assert candidate_matches(c, _spec(match=("vendor/fast-*",)), "aggco")


# --- select_latest: ordering ----------------------------------------------

def test_latest_picks_newest_release(models_dev):
    # date-primary: base tcl-4.1 (2026-02-10) beats its earlier preview (2026-02-01)
    assert select_latest(_targetco(models_dev), _spec()) == "tcl-4.1"


def test_tie_broken_by_version_then_id(models_dev):
    # tcl-4 and tcl-04: tcl-04 released later (2025-12-20 > 2025-10-01) wins on date
    pool = [c for c in _targetco(models_dev) if c.id in ("tcl-4", "tcl-04")]
    assert select_latest(pool, _spec(match=("tcl-4*",))) == "tcl-04"
    # same date -> natural version order decides
    same_a = Candidate(id="tcl-9", release_date="2026-01-01")
    same_b = Candidate(id="tcl-10", release_date="2026-01-01")
    assert select_latest([same_a, same_b], _spec()) == "tcl-10"


def test_no_survivors_returns_none(models_dev):
    assert select_latest(_targetco(models_dev), _spec(min_context=500_000)) is None


# --- select_latest: filters ------------------------------------------------

def test_min_context_boundary_inclusive(models_dev):
    # tcl-4.1 context == 400000 -> passes at 400000, fails above
    assert select_latest(_targetco(models_dev), _spec(min_context=400_000)) == "tcl-4.1"
    assert select_latest(_targetco(models_dev), _spec(min_context=400_001)) is None


def test_unknown_context_fails_min_context(models_dev):
    pool = [Candidate(id="mystery")]
    assert select_latest(pool, _spec(min_context=1)) is None


def test_tools_true_filters(models_dev):
    # select_latest does not apply match (resolve_aliases pre-matches); pass a pre-matched pool
    pool = [c for c in _targetco(models_dev) if c.id == "tcl-4.1-preview"]
    assert select_latest(pool, _spec(tools=True)) is None
    assert select_latest(pool, _spec(tools=False)) == "tcl-4.1-preview"


def test_tools_unknown_fails_when_required(models_dev):
    assert select_latest([Candidate(id="mystery", tool_call=None)], _spec(match=("mystery",), tools=True)) is None


def test_modality_requirement(models_dev):
    # tcl-4.1 has image input; tcl-4.0 does not
    assert select_latest(_targetco(models_dev), _spec(input_modalities=("image",))) == "tcl-4.1"
    assert select_latest(_targetco(models_dev), _spec(input_modalities=("video",))) is None
    # unknown modalities fail the requirement
    assert select_latest([Candidate(id="m", input_modalities=None)], _spec(match=("m",), input_modalities=("text",))) is None


def test_min_age_skips_recent_models(models_dev):
    now = date(2026, 3, 1)
    # cutoff = 2026-02-27 (48h before 2026-03-01): a 2026-02-28 release is too new,
    # tcl-4.1 (2026-02-10) passes and wins
    fresh = Candidate(id="tcl-9.9", release_date="2026-02-28")
    pool = _targetco(models_dev) + [fresh]
    assert select_latest(pool, _spec(min_age_hours=48), now=now) == "tcl-4.1"
    # without minAge the brand-new model wins (proves the filter did the work)
    assert select_latest(pool, _spec(), now=now) == "tcl-9.9"


def test_min_age_boundary_inclusive(models_dev):
    now = date(2026, 3, 1)
    on_boundary = Candidate(id="tcl-b", release_date="2026-02-27")  # == cutoff
    assert select_latest([on_boundary], _spec(min_age_hours=48), now=now) == "tcl-b"
    past = Candidate(id="tcl-p", release_date="2026-02-28")  # > cutoff -> too new
    assert select_latest([past], _spec(min_age_hours=48), now=now) is None


def test_min_age_unknown_release_passes(models_dev):
    assert select_latest([Candidate(id="x")], _spec(match=("x",), min_age_hours=48)) == "x"


# --- resolve_aliases -------------------------------------------------------

def test_resolve_multiple_aliases_independent(models_dev):
    cands = _targetco(models_dev)
    out = resolve_aliases(
        {
            "latest": _spec(match=("tcl-*",)),
            "old": _spec(match=("tcl-2*",)),
            "nope": _spec(match=("nothing-*",)),
        },
        cands,
        TP,
    )
    assert out["latest"] == "tcl-4.1"
    assert out["old"] == "tcl-2.0"
    assert out["nope"] is None
    assert list(out) == ["latest", "old", "nope"]


def test_resolve_with_filters_combined(models_dev):
    out = resolve_aliases(
        {"stable": _spec(match=("tcl-*",), exclude=("*-preview",), min_context=100_000, tools=True)},
        _targetco(models_dev),
        TP,
    )
    assert out["stable"] == "tcl-4.1"


# --- make_candidate --------------------------------------------------------

def test_make_candidate_from_fixture_entry(models_dev):
    entry = models_dev["targetco"]["models"]["tcl-4.1"]
    c = make_candidate(entry)
    assert c.id == "tcl-4.1"
    assert c.release_date == "2026-02-10"
    assert c.context == 400000
    assert c.tool_call is True
    assert c.input_modalities == ("text", "image")
    assert c.output_modalities == ("text",)


def test_make_candidate_unknown_fields_stay_none():
    c = make_candidate({"id": "bare"})
    assert (c.release_date, c.context, c.tool_call) == (None, None, None)
    assert c.input_modalities is None


def test_make_candidate_model_id_override():
    assert make_candidate({"id": "ignored"}, model_id="forced").id == "forced"


def test_make_candidate_rejects_bad_entries():
    with pytest.raises(ValueError):
        make_candidate("not-a-dict")
    with pytest.raises(ValueError):
        make_candidate({})
