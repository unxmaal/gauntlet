"""Tier-2 trigger detectors: each fires on its positive fixture and stays quiet on its negative one."""

from gauntlet import detectors, registry


def sites(fn, files):
    return sorted({h.site for h in fn(detectors.parse(files))})


def quiet_then_fires(fn, innocent, bad):
    # Quiet on the innocent files, and firing once the bad file joins them: quiet for the right reason.
    assert sites(fn, innocent) == []
    assert sites(fn, {**innocent, **bad}) != []


def test_a_closed_table_with_a_fallback_lookup_is_found():
    src = ("FAMILY = {\"qwen\": 1, \"llama\": 2, \"gemma\": 3}\n"
           "def size(name):\n    return FAMILY.get(name, 0)\n")
    other = "from app import t\ndef f(k):\n    return t.SPEC.get(k)\n"
    table = "SPEC = {\"a\": 1, \"b\": 2, \"c\": 3}\n"
    got = sites(detectors.closed_tables, {"app/t.py": src + table, "app/u.py": other})
    assert got == ["app/t.py:FAMILY", "app/t.py:SPEC"]


def test_a_closed_table_looked_up_by_a_constant_or_refusing_is_left_alone():
    innocent = {"app/t.py": ("FAMILY = {\"qwen\": 1, \"llama\": 2, \"gemma\": 3}\n"
                                 "SMALL = {\"a\": 1}\nlower = {\"a\": 1, \"b\": 2, \"c\": 3}\n"
                                 "def f(k):\n    return FAMILY[k], FAMILY.get(\"qwen\", 0), SMALL.get(k, 0)\n"
                                 "def g(k, d):\n    return d.get(k, 0), lower.get(k)\n")}
    bad = {"app/u.py": "from app import t\ndef h(k):\n    return t.FAMILY.get(k, 9)\n"}
    quiet_then_fires(detectors.closed_tables, innocent, bad)


_TABLES = "LANES = {\"text\": 1, \"image\": 2, \"tts\": 3}\n"


def test_a_membership_test_that_drops_or_defaults_an_unlisted_key_is_found():
    # `[x for x in lanes if x in LANE_QUERIES]` is a lookup no .get scan sees.
    for body in ("    return [x for x in xs if x in LANES]\n",
                 "    for x in xs:\n        if x not in LANES:\n            continue\n",
                 "    return 1 if xs in LANES else 0\n",
                 "    return 0 if xs not in LANES else 1\n",
                 "    if xs in LANES:\n        return 1\n    return 0\n"):
        got = sites(detectors.closed_tables, {"app/t.py": _TABLES + "def f(xs):\n" + body})
        assert got == ["app/t.py:LANES"], body
    other = "from app import t\ndef g(k):\n    return sum(1 for x in k if x in t.LANES)\n"
    assert sites(detectors.closed_tables, {"app/t.py": _TABLES, "app/u.py": other}) == ["app/t.py:LANES"]


def test_a_membership_test_that_refuses_or_keeps_an_unlisted_key_is_left_alone():
    innocent = {"app/t.py": _TABLES + (
        "def f(k, xs, ok):\n"
        "    if k not in LANES:\n        raise ValueError(k)\n"
        "    if ok and k not in LANES:\n        raise ValueError(k)\n"
        "    if k in LANES:\n        pass\n    else:\n        raise ValueError(k)\n"
        "    unknown = [x for x in xs if x not in LANES]\n"
        "    return \"text\" in LANES, unknown, k not in LANES\n"),
                "app/s.py": "LANES = (\"text\",)\n",
                "app/v.py": "from app import s\ndef h(k):\n    return k in s.LANES\n"}
    bad = {"app/u.py": "from app import t\ndef g(k):\n    if k not in t.LANES:\n        return None\n"}
    quiet_then_fires(detectors.closed_tables, innocent, bad)


def test_state_read_from_the_newest_row_is_found():
    src = ("def latest(conn, n):\n"
           "    return conn.execute(\"SELECT v FROM verdicts WHERE n = ? \"\n"
           "                        \"ORDER BY id DESC LIMIT 1\", (n,)).fetchone()\n"
           "class S:\n    def last(self, c):\n"
           "        return c.execute(f\"SELECT * FROM {T} ORDER BY at DESC, id DESC\\n LIMIT 1\")\n")
    assert sites(detectors.latest_row_state, {"app/s.py": src}) == ["app/s.py:S.last", "app/s.py:latest"]


def test_a_newest_row_query_that_is_not_state_is_left_alone():
    innocent = {"app/s.py": ("Q = \"SELECT v FROM t ORDER BY id DESC LIMIT 10\"\n"
                                 "R = \"SELECT v FROM t ORDER BY id ASC LIMIT 1\"\n"
                                 "# ORDER BY id DESC LIMIT 1 in a comment\n")}
    bad = {"app/x.py": "def f(c):\n    c.execute(\"select v from t order by id desc limit 1\")\n"}
    quiet_then_fires(detectors.latest_row_state, innocent, bad)


def test_an_unguarded_state_write_and_a_second_writer_module_are_found():
    # jobs.state set WHERE id = ? alone, and from more than one module.
    files = {"app/a.py": ("def claim(c, i):\n"
                              "    c.execute(\"UPDATE jobs SET note = ?, \"\n"
                              "              \"state = ? WHERE id = ?\", (\"\", \"running\", i))\n"),
             "app/b.py": ("class W:\n    def close(self, c, i):\n"
                              "        c.execute(f\"UPDATE jobs SET state = 'done' WHERE id = {i} AND state = 'running'\")\n")}
    assert sites(detectors.unguarded_transition, files) == ["app/a.py:claim", "table:jobs.state"]


def test_a_guarded_state_write_from_one_module_is_left_alone():
    innocent = {"app/q.py": ("def claim(c, i):\n"
                                 "    c.execute(\"UPDATE jobs SET state = ? WHERE id = ? AND state='pending'\", (i,))\n"
                                 "def move(c, i, v):\n"
                                 "    c.execute(\"UPDATE proposals SET state = ?, state_verdict_id = ? \"\n"
                                 "              \"WHERE id = ? AND COALESCE(state_verdict_id, 0) = ?\", (i, v))\n"
                                 "def note(c, i):\n    c.execute(\"UPDATE jobs SET note = ? WHERE id = ? AND state = ?\")\n"
                                 "S = \"SELECT state FROM jobs WHERE status = 1\"\n"),
                "app/memory_store/migrations/v9.py": "Q = \"UPDATE jobs SET state = 'done' WHERE id = 1\"\n"}
    assert sites(detectors.unguarded_transition, innocent) == []
    second = {"app/r.py": "def f(c):\n    c.execute(\"update jobs set state = 'x' where id = 1 and state = 'y'\")\n"}
    assert sites(detectors.unguarded_transition, {**innocent, **second}) == ["table:jobs.state"]
    bad = {"app/q.py": innocent["app/q.py"] + "def g(c):\n    c.execute(\"UPDATE jobs SET status = 1\")\n"}
    quiet_then_fires(detectors.unguarded_transition, innocent, bad)


def test_a_guard_only_tests_call_is_found():
    files = {"app/m.py": "def check_model(r):\n    return r\ndef ensure_dir(p):\n    return p\n",
             "app/n.py": "from app import m\nm.ensure_dir(1)\n",
             "tests/test_m.py": "from app import m\ndef test_x():\n    m.check_model(1)\n"}
    assert sites(detectors.unreached_guards, files) == ["app/m.py:check_model"]


def test_a_guard_production_calls_is_left_alone():
    innocent = {"app/m.py": ("def check_model(r):\n    return r\n"
                                 "class C:\n    def guard_lane(self):\n        return 1\n"
                                 "def checker(x):\n    return x\n"),
                "app/n.py": "from app.m import check_model, C\ncheck_model(1)\nC().guard_lane()\n"}
    bad = {"app/o.py": "def refuse_live(p):\n    return p\n"}
    quiet_then_fires(detectors.unreached_guards, innocent, bad)


def test_an_option_nothing_reads_is_found():
    files = {"app/cli.py": ("def build(sub):\n    p = sub.add_parser(\"x\")\n"
                                "    p.add_argument(\"--dry-run\", action=\"store_true\")\n"
                                "    p.add_argument(\"--top\", type=int)\n"
                                "    p.add_argument(\"--seed\", dest=\"rng_seed\")\n"
                                "    p.add_argument(\"path\")\n"),
             "app/commands/x.py": "def cmd_x(a):\n    return a.top\n"}
    assert sites(detectors.ignored_options, files) == [
        "app/cli.py:--dry-run", "app/cli.py:--seed", "app/cli.py:path"]


def test_an_option_that_is_read_is_left_alone():
    innocent = {"app/cli.py": ("def build(p):\n    p.add_argument(\"--dry-run\")\n"
                                   "    p.add_argument(\"-n\", \"--top\")\n    p.add_argument(\"--seed\", dest=\"rng\")\n"
                                   "    p.add_argument(\"names\", nargs=\"*\")\n"),
                "app/commands/x.py": ("def cmd_x(a):\n    return a.dry_run, getattr(a, \"top\", 3), a.rng, "
                                          "a.names\n")}
    bad = {"app/other.py": "def b(p):\n    p.add_argument(\"--never-read\")\n"}
    quiet_then_fires(detectors.ignored_options, innocent, bad)


def test_one_default_for_every_member_is_found():
    src = ("CTX = 8192\nTIMEOUT_S = 600\n"
           "def serve(model, ctx=CTX):\n    return ctx\n"
           "def run(model, *, wait=TIMEOUT_S):\n    return wait\n"
           "def load(repo, ceiling=ins.MEMORY_CEILING):\n    return ceiling\n")
    assert sites(detectors.member_defaults, {"app/s.py": src}) == [
        "app/s.py:load(ceiling)", "app/s.py:run(wait)", "app/s.py:serve(ctx)"]


def test_a_default_that_is_not_per_member_is_left_alone():
    innocent = {"app/s.py": ("LIMIT = 10\nPORT = 8080\n"
                                 "def page(rows, limit=LIMIT, port=PORT, ctx=None, timeout=30):\n    return rows\n")}
    bad = {"app/t.py": "BUDGET_GB = 22\ndef fits(m, budget=BUDGET_GB):\n    return m\n"}
    quiet_then_fires(detectors.member_defaults, innocent, bad)


def test_a_harness_failure_written_as_a_verdict_is_found():
    src = ("def screen(proc):\n    if proc.returncode != 0:\n        return Verdict(\"broken\", \"exited\")\n"
           "def inspect(repo):\n    try:\n        go(repo)\n    except OSError:\n"
           "        decide(repo, \"declined\")\n"
           "def verify(p):\n    return \"ok\" if p.returncode == 0 else \"broken\"\n")
    assert sites(detectors.harness_verdicts, {"app/s.py": src}) == [
        "app/s.py:inspect", "app/s.py:screen", "app/s.py:verify"]


def test_a_verdict_from_the_subject_s_own_output_is_left_alone():
    innocent = {"app/s.py": ("def verdict(summary):\n    if not summary:\n        return \"broken\"\n"
                                 "    return \"works\"\n"
                                 "def run(p):\n    try:\n        go()\n    except OSError:\n"
                                 "        return \"queued\"\n"
                                 "REJECTIONS = (\"broken\", \"declined\")\n")}
    bad = {"app/t.py": ("def run(p):\n    try:\n        go()\n    except TimeoutError:\n"
                            "        return \"broken\"\n")}
    quiet_then_fires(detectors.harness_verdicts, innocent, bad)


def test_one_list_written_in_two_modules_is_found():
    files = {"app/a.py": "TERMINAL = (\"measured\", \"broken\", \"declined\")\n",
             "app/b.py": "DONE = {\"declined\", \"broken\", \"measured\"}\n",
             "app/c.py": "OTHER = (\"measured\", \"broken\")\n"}
    assert sites(detectors.duplicated_literals, files) == ["app/a.py:TERMINAL", "app/b.py:DONE"]


def test_one_list_written_once_is_left_alone():
    innocent = {"app/a.py": "TERMINAL = (\"measured\", \"broken\", \"declined\")\nONE = (\"x\",)\n",
                "app/b.py": ("from app.a import TERMINAL\nDONE = TERMINAL\nONE = (\"x\",)\n"
                                 "def f():\n    local = (\"measured\", \"broken\", \"declined\")\n    return local\n"),
                "tests/test_a.py": "WANT = (\"measured\", \"broken\", \"declined\")\n"}
    bad = {"evals/c.py": "STATES = [\"declined\", \"measured\", \"broken\"]\n"}
    quiet_then_fires(detectors.duplicated_literals, innocent, bad)


def test_an_environment_variable_read_is_a_seam():
    files = {"app/a.py": ("import os\nH = os.environ.get(\"LOCALHARNESS_HOME\")\n"
                              "K = os.environ[\"SOHOT_KEY\"]\nP = os.getenv(\"HOME\")\n"),
             "app/b.py": "import os\nH = os.environ.get(\"LOCALHARNESS_HOME\", \"\")\n"}
    assert sites(detectors.env_seams, files) == ["env:LOCALHARNESS_HOME", "env:SOHOT_KEY"]


def test_a_common_or_written_variable_is_not_a_seam():
    innocent = {"app/a.py": ("import os\nos.environ[\"X_SET\"] = \"1\"\nP = os.getenv(\"PATH\")\n"
                                 "U = os.environ.get(\"USER\")\nT = os.environ.get(name)\n"),
                "tests/test_a.py": "import os\nZ = os.environ.get(\"ONLY_TESTS\")\n"}
    bad = {"app/b.py": "import os\nV = os.environ.get(\"SOHOT_NEW\")\n"}
    quiet_then_fires(detectors.env_seams, innocent, bad)


def test_a_marker_binds_a_test_to_a_class_and_a_site():
    src = ("import pytest\n"
           "@pytest.mark.gauntlet(\"a-closed-table-fronting-an-open-set\", site=\"app/t.py:FAMILY\")\n"
           "def test_a():\n    pass\n"
           "@pytest.mark.slow\n@pytest.mark.gauntlet(\"one-question-two-answers\", site=\"app/a.py:X\")\n"
           "def test_b():\n    pass\n"
           "class TestC:\n    @pytest.mark.gauntlet(\"x\", site=\"app/c.py:Y\")\n    def test_c(self):\n        pass\n")
    got = detectors.bindings({"tests/test_t.py": src})
    assert got == {("a-closed-table-fronting-an-open-set", "app/t.py:FAMILY"): ["tests/test_t.py::test_a"],
                   ("one-question-two-answers", "app/a.py:X"): ["tests/test_t.py::test_b"],
                   ("x", "app/c.py:Y"): ["tests/test_t.py::TestC::test_c"]}


def test_a_marker_with_no_site_or_outside_tests_binds_nothing():
    src = ("import pytest\n@pytest.mark.gauntlet(\"x\")\ndef test_a():\n    pass\n"
           "@pytest.mark.gauntlet(\"x\", site=name)\ndef test_b():\n    pass\n")
    assert detectors.bindings({"tests/test_t.py": src}) == {}
    good = "import pytest\n@pytest.mark.gauntlet(\"x\", site=\"a:b\")\ndef test_a():\n    pass\n"
    assert detectors.bindings({"app/t.py": good}) == {}
    assert detectors.bindings({"tests/test_t.py": good}) != {}


def test_the_ledger_splits_hits_into_bound_waived_and_unbound():
    hits = {"c": [detectors.Hit("a:X", 1, ""), detectors.Hit("a:Y", 2, ""), detectors.Hit("a:Z", 3, "")]}
    index = {"c": {"detector": "d", "waivers": {"a:Y": "reason"}}}
    led = detectors.ledger(hits, {("c", "a:X"): ["t::x"], ("c", "a:gone"): ["t::y"]}, index)
    assert led["c"]["bound"] == ["a:X"]
    assert led["c"]["waived"] == ["a:Y"]
    assert led["c"]["unbound"] == ["a:Z"]
    assert led["c"]["stale_bindings"] == ["a:gone"]
    assert led["c"]["stale_waivers"] == []




def test_the_registry_shape_of_waivers_and_review_is_checked():
    index = {"c": {"instances": [{"issue": 1}], "scanners": [], "waivers": {"a:b": " "},
                   "review": "([unclosed"}}
    snap = [{"id": "c", "tier": 2}]
    got = registry.problems(index, {}, {}, snap, {1: {"title": "", "created": ""}}, ".")
    assert any("waiver a:b has no reason" in p for p in got)
    assert any("review heuristic does not compile" in p for p in got)










def test_a_marker_may_name_its_class_through_a_module_constant():
    src = ("import pytest\nCLOSED = \"a-closed-table-fronting-an-open-set\"\n"
           "@pytest.mark.gauntlet(CLOSED, site=\"app/t.py:FAMILY\")\ndef test_a():\n    pass\n"
           "@pytest.mark.gauntlet(UNKNOWN, site=\"app/t.py:X\")\ndef test_b():\n    pass\n")
    assert detectors.bindings({"tests/test_t.py": src}) == {
        ("a-closed-table-fronting-an-open-set", "app/t.py:FAMILY"): ["tests/test_t.py::test_a"]}


def test_an_address_compared_to_a_loopback_constant_is_found():
    files = {"app/s.py": ("URL = \"http://127.0.0.1:8082\"\n"
                              "def engine(base):\n    if base == URL:\n        return 1\n"),
             "app/t.py": ("from app import s\nclass R:\n    def f(self, b):\n"
                              "        return b in (s.URL, \"x\")\n")}
    assert sites(detectors.loopback_equality, files) == ["app/s.py:engine", "app/t.py:R.f"]


def test_a_loopback_constant_used_without_comparing_is_left_alone():
    innocent = {"app/s.py": ("URL = \"http://127.0.0.1:8082\"\nOTHER = \"http://gw.test\"\n"
                                 "def get(path):\n    return URL + path\n"
                                 "def other(b):\n    return b == OTHER\n"),
                "tests/test_s.py": "from app.s import URL\ndef test_x():\n    assert URL == \"x\"\n"}
    bad = {"app/u.py": "def f(b):\n    return b != \"http://localhost:4000\"\n"}
    quiet_then_fires(detectors.loopback_equality, innocent, bad)


def test_a_key_built_from_a_row_position_is_found():
    # A verdict keyed f"{case_id}#{i}" over enumerate(rows) pairs two runs only if their row order matches.
    innocent = {"app/p.py": ("def f(rows):\n    out = {}\n    for i, r in enumerate(rows):\n"
                                 "        out[r['case_id']] = i\n        print(f\"{i}\")\n    return out\n")}
    bad = {"app/q.py": ("def g(rows):\n    out = {}\n    for i, r in enumerate(rows):\n"
                            "        out[f\"{r['case_id']}#{i}\"] = r['passed']\n    return out\n")}
    quiet_then_fires(detectors.row_index_keys, innocent, bad)
    assert sites(detectors.row_index_keys, bad) == ["app/q.py:g"]

def test_a_declared_move_table_is_found():
    innocent = {"app/m.py": 'STATES = ("a", "b")\nFAMILY = {"a": 1}\nREMOVES = {("a", "b")}\n'}
    bad = {"app/n.py": 'A = "a"\nMOVES = frozenset({(None, A), (A, "b")})\n'}
    quiet_then_fires(detectors.move_tables, innocent, bad)
    assert sites(detectors.move_tables, {**innocent, **bad}) == ["app/n.py:MOVES"]


def test_every_detector_has_a_fixture_here():
    text = open(__file__, encoding="utf-8").read()
    assert [name for name in detectors.DETECTORS if f"detectors.{name}" not in text] == []


def test_production_is_a_seam_the_consumer_sets():
    files = {"app/a.py": "import os\nA = os.environ.get(\"ONE\")\n",
             "cases/b.py": "import os\nB = os.environ.get(\"TWO\")\n"}
    trees = detectors.parse(files)
    assert sorted(h.site for h in detectors.env_seams(trees)) == ["env:ONE", "env:TWO"]
    assert [h.site for h in detectors.env_seams(trees, lambda rel: not rel.startswith("cases/"))] == ["env:ONE"]
    index = {"c": {"detector": "env_seams"}}
    assert [h.site for h in detectors.run(trees, index, lambda rel: rel.startswith("cases/"))["c"]] == ["env:TWO"]


def test_a_consumer_may_add_its_own_detector():
    def mine(trees, production):
        return [detectors.Hit(f"{rel}:x", 1, "mine") for rel in trees if production(rel)]
    got = detectors.run(detectors.parse({"app/a.py": "x = 1\n"}), {"c": {"detector": "mine"}},
                        detectors={"mine": mine})
    assert [h.site for h in got["c"]] == ["app/a.py:x"]


def test_a_class_naming_a_detector_that_does_not_exist_is_left_to_the_ratchet_not_a_crash():
    trees = detectors.parse({"app/a.py": "x = 1\n"})
    assert detectors.run(trees, {"c": {"detector": "no_such"}, "d": {"detector": "env_seams"}}) == {"d": []}
