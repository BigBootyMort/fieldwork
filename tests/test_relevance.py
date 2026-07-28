"""Relevance gate — pure-local unit tests (no stack, no network).

Guards the fix for the observed failure where CourtListener returned unrelated
cases for a person and the synthesis LLM reported them as the target's own cases.
"""
import os
import sys

# The relevance module is pure-stdlib; import it directly from the backend.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "app"))

from relevance import apply_relevance, name_tokens, is_relevant  # noqa: E402


def test_demotes_off_target_court_cases():
    results = {"Court Records": {"found": True, "total": 3, "cases": [
        {"case_name": "Christopher Harborne v. Dow Jones & Company, Inc.", "snippet": ""},
        {"case_name": "NRSC v. FEC", "snippet": ""},
        {"case_name": "United States v. Samuel Bankman-Fried", "snippet": "fraud FTX"},
    ]}}
    apply_relevance("Sam Bankman-Fried", results)
    cr = results["Court Records"]

    kept = [c["case_name"] for c in cr["cases"]]
    weak = [c["case_name"] for c in cr["weak_matches"]]
    assert kept == ["United States v. Samuel Bankman-Fried"]
    assert set(weak) == {
        "Christopher Harborne v. Dow Jones & Company, Inc.", "NRSC v. FEC"}
    # headline stays honest
    assert cr["total"] == 1
    assert cr["found"] is True
    assert cr["relevance"]["filtered_out"] == 2


def test_all_relevant_leaves_result_untouched():
    results = {"Adverse Media": {"found": True, "adverse_count": 1, "articles": [
        {"title": "Samuel Bankman-Fried sentenced to 25 years", "snippet": "FTX fraud"},
    ]}}
    apply_relevance("Sam Bankman-Fried", results)
    am = results["Adverse Media"]
    assert "weak_matches" not in am          # nothing demoted
    assert am["adverse_count"] == 1
    assert am["found"] is True


def test_all_off_target_flips_found_false():
    results = {"Court Records": {"found": True, "total": 2, "cases": [
        {"case_name": "Acme Corp v. Widget LLC", "snippet": ""},
        {"case_name": "Doe v. Roe", "snippet": ""},
    ]}}
    apply_relevance("Sam Bankman-Fried", results)
    cr = results["Court Records"]
    assert cr["cases"] == []
    assert cr["total"] == 0
    assert cr["found"] is False              # honest "no findings", not laundered


def test_surname_rule_and_tokenizer():
    toks = name_tokens("Acme Holdings Ltd")
    assert toks == ["acme"]                  # stopwords dropped
    assert is_relevant(["sam", "bankman", "fried"], "fried", {"jane", "fried"})
    assert not is_relevant(["sam", "bankman", "fried"], "fried", {"john", "smith"})
