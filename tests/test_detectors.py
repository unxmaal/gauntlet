"""Tier-2 trigger detectors: each fires on its positive fixture and stays quiet on its negative one."""

import re
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


def test_a_configured_lifecycle_column_is_checked_and_the_default_is_unchanged():
    guarded = {"app/o.py": "def settle(c, i):\n    c.execute(\"UPDATE runs SET outcome = ? WHERE id = ? AND outcome IS NULL\")\n"}
    bad = {"app/p.py": "def settle(c, i):\n    c.execute(\"UPDATE trials SET outcome = ? WHERE id = ?\", (i,))\n"}
    files = detectors.parse({**guarded, **bad})
    assert sorted(h.site for h in detectors.unguarded_transition(files)) == []
    got = detectors.unguarded_transition(files, columns=("state", "status", "outcome"))
    assert sorted(h.site for h in got) == ["app/p.py:settle"]
    assert detectors.unguarded_transition(detectors.parse(guarded), columns=("outcome",)) == []
    via = detectors.run(files, {"c": {"detector": "unguarded_transition"}},
                        config={"unguarded_transition": {"columns": ("outcome",)}})
    assert [h.site for h in via["c"]] == ["app/p.py:settle"]


def test_a_where_built_by_substitution_is_not_read_as_unguarded():
    innocent = {"app/f.py": ("def move(c, cond):\n    c.execute(f\"UPDATE jobs SET state = ? WHERE {cond}\")\n"
                             "def other(c, i):\n    c.execute(f\"UPDATE runs SET state = ? WHERE id = ? AND {i}\")\n")}
    bad = {"app/g.py": "def move(c, t):\n    c.execute(f\"UPDATE {t} SET state = ? WHERE id = ?\")\n"}
    quiet_then_fires(detectors.unguarded_transition, innocent, bad)


def test_an_unguarded_state_attribute_and_a_second_writer_module_are_found():
    files = {"app/jobs.py": ("class Job:\n    def __init__(self):\n        self.state = \"pending\"\n"
                             "    def start(self):\n        self.state = \"running\"\n"
                             "    def finish(self):\n        if self.state != \"running\":\n            raise ValueError\n"
                             "        self.state = \"done\"\n"),
             "app/worker.py": ("from app.jobs import Job\n"
                               "def cancel(job: Job):\n    job.state = State.CANCELLED\n"
                               "def ok(job):\n    match job.state:\n        case \"running\":\n"
                               "            job.state = \"paused\"\n")}
    got = sites(detectors.unguarded_transition, files)
    assert got == ["app/jobs.py:Job.start", "app/jobs.py:Job.state", "app/worker.py:cancel"]


def test_a_guarded_constructed_or_declared_mover_state_attribute_is_left_alone():
    innocent = {"app/jobs.py": ("MOVES = {(\"pending\", \"running\"), (\"running\", \"done\")}\n"
                                "class Job:\n    def __init__(self):\n        self.state = \"pending\"\n"
                                "    def move(self, to):\n        if (self.state, to) not in MOVES:\n"
                                "            raise ValueError(to)\n        self.state = to\n"
                                "    def go(self):\n        self.state = RUNNING\n"
                                "def make():\n    job = Job()\n    job.state = \"pending\"\n    return job\n"
                                "def copy(a, b):\n    a.state = b.state\n"
                                "def code(r):\n    r.status = 200\n"),
                "app/http.py": "def reply(r):\n    r.status = \"ok\"\n"}
    got = detectors.unguarded_transition(detectors.parse(innocent), movers=("go",))
    assert [h.site for h in got] == []
    bad = {"app/other.py": "def stop(job):\n    if job.ready:\n        job.state = \"stopped\"\n"}
    got = detectors.unguarded_transition(detectors.parse({**innocent, **bad}), movers=("go",))
    assert [h.site for h in got] == ["app/other.py:stop", "app/jobs.py:Job.state"]
    via = detectors.run(detectors.parse(innocent), {"c": {"detector": "unguarded_transition"}})
    assert [h.site for h in via["c"]] == ["app/jobs.py:Job.go"]


def test_a_stage_inferred_from_columns_written_apart_is_found():
    files = {"app/dl.py": ("def begin(c, i):\n    c.execute(\"UPDATE downloads SET started_at = ? WHERE id = ?\", (i,))\n"
                           "def end(c, i):\n    c.execute(\"UPDATE downloads SET finished_at = ?, note = ? WHERE id = ?\")\n"
                           "def add(c):\n    c.execute(\"INSERT INTO downloads (url, created_at) VALUES (?, ?)\")\n"),
             "app/view.py": ("def running(c):\n    return c.execute(\"SELECT id FROM downloads d WHERE \"\n"
                             "                     \"d.started_at IS NOT NULL AND d.finished_at IS NULL\")\n")}
    assert sites(detectors.inferred_stage, files) == ["table:downloads"]
    py = {"app/dl.py": files["app/dl.py"],
          "app/view.py": "def running(row):\n    return row[\"started_at\"] and not row[\"finished_at\"]\n"}
    assert sites(detectors.inferred_stage, py) == ["table:downloads"]


def test_a_stage_inferred_from_attributes_written_apart_is_found():
    files = {"app/order.py": ("class Order:\n    def __init__(self):\n"
                              "        self.paid_at = None\n        self.shipped_at = None\n        self.cancelled = False\n"
                              "    def pay(self, now):\n        self.paid_at = now\n"
                              "    def ship(self, now):\n        self.shipped_at = now\n"
                              "    def open(self):\n        return self.paid_at is not None and self.shipped_at is None\n"),
             "app/admin.py": "def cancel(order):\n    order.cancelled = True\n"}
    assert sites(detectors.inferred_stage, files) == ["app/order.py:Order"]
    got = detectors.inferred_stage(detectors.parse(files))[0]
    assert "paid_at" in got.why and "shipped_at" in got.why and "app/order.py:" in got.why


def test_a_stage_with_a_declared_state_or_no_joint_read_is_left_alone():
    innocent = {"app/dl.py": ("def begin(c, i):\n    c.execute(\"UPDATE downloads SET started_at = ? WHERE id = ?\")\n"
                              "def end(c, i):\n    c.execute(\"UPDATE downloads SET finished_at = ? WHERE id = ?\")\n"
                              "def both(c):\n    c.execute(\"UPDATE runs SET started_at = ?, finished_at = ?\")\n"
                              "def seen(c):\n    c.execute(\"UPDATE posts SET created_at = ?\")\n"
                              "    c.execute(\"UPDATE posts SET updated_at = ?\")\n"
                              "    c.execute(\"SELECT 1 FROM posts WHERE created_at IS NULL AND updated_at IS NULL\")\n"
                              "    c.execute(\"SELECT 1 FROM runs WHERE started_at IS NULL AND finished_at IS NULL\")\n"
                              "def go(c):\n    c.execute(\"UPDATE jobs SET started_at = ?, state = 'running' WHERE state = 'new'\")\n"
                              "    c.execute(\"UPDATE jobs SET finished_at = ?\")\n"
                              "    c.execute(\"SELECT 1 FROM jobs WHERE started_at IS NOT NULL AND finished_at IS NULL\")\n"
                              "def one(c):\n    c.execute(\"SELECT 1 FROM downloads WHERE finished_at IS NULL\")\n"),
                "app/t.py": ("class T:\n    def __init__(self):\n        self.state = \"new\"\n"
                             "    def a(self, n):\n        self.started_at = n\n"
                             "    def b(self, n):\n        self.finished_at = n\n"
                             "    def live(self):\n        return self.started_at and not self.finished_at\n")}
    bad = {"app/view.py": "def running(c):\n    c.execute(\"SELECT 1 FROM downloads WHERE started_at IS NOT NULL AND finished_at IS NULL\")\n"}
    quiet_then_fires(detectors.inferred_stage, innocent, bad)
    marked = detectors.inferred_stage(detectors.parse({**innocent, **bad}), markers=("_done",))
    assert marked == []
    ignored = detectors.inferred_stage(detectors.parse({**innocent, **bad}), ignore=("finished_at",))
    assert ignored == []
    via = detectors.run(detectors.parse({**innocent, **bad}), {"c": {"detector": ["unguarded_transition", "inferred_stage"]}},
                        config={"inferred_stage": {"ignore": ("started_at",)}})
    assert via["c"] == []


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


_ADD = ("def add(conn, argv):\n    cur = conn.execute(\"INSERT INTO jobs (argv) VALUES (?)\", (argv,))\n"
        "    conn.commit()\n    return cur.lastrowid\n")


def test_a_committed_id_linked_by_a_second_write_is_found():
    bad = {"app/q.py": _ADD, "app/r.py": (
        "from app import q\n"
        "def resume(conn, row, add=None):\n    add = add or q.add\n    jid = add(conn, row.argv)\n"
        "    conn.execute(\"UPDATE runs SET job_id = ? WHERE id = ?\", (jid, row.id))\n    conn.commit()\n"
        "def sweep(conn, values):\n    ids = []\n    for v in values:\n        ids.append(q.add(conn, v))\n"
        "    conn.execute(\"INSERT INTO sweeps (jobs) VALUES (?)\", (str(ids),))\n")}
    assert sites(detectors.commit_then_link, bad) == ["app/r.py:resume", "app/r.py:sweep"]


def test_a_link_in_the_same_transaction_or_keyed_for_dedup_is_left_alone():
    innocent = {"app/q.py": _ADD, "app/r.py": (
        "from app import q\n"
        "def one(conn, row):\n    jid = q.add(conn, row.argv, commit=False)\n"
        "    conn.execute(\"UPDATE runs SET job_id = ? WHERE id = ?\", (jid, row.id))\n    conn.commit()\n"
        "def keyed(conn, row):\n    jid = q.add(conn, row.argv)\n"
        "    conn.execute(\"INSERT INTO runs (job_id) VALUES (?) ON CONFLICT DO NOTHING\", (jid,))\n"
        "def unlinked(conn, row):\n    jid = q.add(conn, row.argv)\n    print(jid)\n")}
    bad = {"app/s.py": ("from app import q\ndef late(conn, row):\n    jid = q.add(conn, row.argv)\n"
                        "    conn.execute(\"UPDATE runs SET job_id = ?\", (jid,))\n")}
    quiet_then_fires(detectors.commit_then_link, innocent, bad)


def test_a_consumer_names_a_committer_the_scan_cannot_see():
    files = {"app/r.py": "from lib import store\ndef f(conn):\n    k = store.put(1)\n"
                         "    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n"}
    assert sites(detectors.commit_then_link, files) == []
    got = detectors.commit_then_link(detectors.parse(files), committers=("put",))
    assert [h.site for h in got] == ["app/r.py:f"]


_MOVE_COMMITS = ("def _move(conn, i):\n    cur = conn.execute(\"INSERT INTO moves (i) VALUES (?)\", (i,))\n"
                 "    conn.commit()\n    return cur.lastrowid\n")
_MOVE_PLAIN = ("def _move(conn, i):\n    cur = conn.execute(\"INSERT INTO moves (i) VALUES (?)\", (i,))\n"
               "    return cur.lastrowid\n")


def test_a_committer_is_a_call_that_resolves_to_a_committing_definition_not_a_shared_name():
    innocent = {"app/a.py": _MOVE_COMMITS, "app/b.py": _MOVE_PLAIN + (
        "from app import a\n"
        "def close(conn, i):\n    m = _move(conn, i)\n"
        "    conn.execute(\"INSERT INTO log (m) VALUES (?)\", (m,))\n"
        "def other(conn, i, x):\n    m = x._move(conn, i)\n"
        "    conn.execute(\"INSERT INTO log (m) VALUES (?)\", (m,))\n"),
        "app/c.py": ("from app import b\nfrom app.b import _move as mv\n"
                     "def f(conn, i):\n    m = b._move(conn, i)\n    n = mv(conn, i)\n"
                     "    conn.execute(\"INSERT INTO log (m, n) VALUES (?, ?)\", (m, n))\n")}
    bad = {"app/d.py": ("from app import a\nfrom app.a import _move as mv\n"
                        "def g(conn, i):\n    m = a._move(conn, i)\n"
                        "    conn.execute(\"INSERT INTO log (m) VALUES (?)\", (m,))\n"
                        "def h(conn, i):\n    n = mv(conn, i)\n"
                        "    conn.execute(\"INSERT INTO log (n) VALUES (?)\", (n,))\n")}
    quiet_then_fires(detectors.commit_then_link, innocent, bad)
    assert sites(detectors.commit_then_link, {**innocent, **bad}) == ["app/d.py:g", "app/d.py:h"]
    same = {"app/a.py": _MOVE_COMMITS + ("def k(conn, i):\n    m = _move(conn, i)\n"
                                         "    conn.execute(\"INSERT INTO log (m) VALUES (?)\", (m,))\n"
                                         "class S:\n    def j(self, conn, i):\n        m = self._move(conn, i)\n"
                                         "        conn.execute(\"INSERT INTO log (m) VALUES (?)\", (m,))\n")}
    assert sites(detectors.commit_then_link, same) == ["app/a.py:S.j", "app/a.py:k"]
    unique = {"app/s.py": ("class Store:\n    def put_row(self, conn, v):\n"
                           "        cur = conn.execute(\"INSERT INTO rows (v) VALUES (?)\", (v,))\n"
                           "        conn.commit()\n        return cur.lastrowid\n"),
              "app/u.py": ("def f(conn, store, v):\n    k = store.put_row(conn, v)\n"
                           "    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n")}
    assert sites(detectors.commit_then_link, {**innocent, **unique}) == ["app/u.py:f"]
    got = detectors.commit_then_link(detectors.parse(innocent), committers=("_move",))
    assert sorted(h.site for h in got) == ["app/b.py:close", "app/b.py:other", "app/c.py:f"]


_LINKS = ("def f(conn, x):\n    v = ms.decide(conn, x)\n"
          "    conn.execute(\"UPDATE runs SET v = ? WHERE id = ?\", (v, x))\n"
          "def g(conn, x):\n    k = store_put(conn, x)\n    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n")


def test_a_committer_re_exported_by_a_package_init_is_followed_to_its_definition():
    plain = _ADD.replace("    conn.commit()\n", "")
    init = {"app/ms/__init__.py": "from .tx import add as decide\nfrom app.ms.tx import add as put\n",
            "app/c.py": "from app import ms\nfrom app.ms import put as store_put\n" + _LINKS}
    quiet_then_fires(detectors.commit_then_link, {**init, "app/ms/tx.py": plain}, {"app/ms/tx.py": _ADD})
    assert sites(detectors.commit_then_link, {**init, "app/ms/tx.py": _ADD}) == ["app/c.py:f", "app/c.py:g"]


def test_a_re_export_cycle_between_package_inits_terminates_and_resolves_nothing():
    files = {"app/p/__init__.py": "from app.q import decide\n", "app/q/__init__.py": "from app.p import decide\n",
             "app/c.py": "from app import p\nfrom app.q import decide as store_put\nms = p\n" + _LINKS}
    assert sites(detectors.commit_then_link, files) == []


_DECIDE_PLAIN = ("def decide(conn, x):\n    cur = conn.execute(\"INSERT INTO verdicts (x) VALUES (?)\", (x,))\n"
                 "    return cur.lastrowid\n")
_CALLS = ("def f(conn, x):\n    v = {}.decide(conn, x)\n"
          "    conn.execute(\"INSERT INTO log (v) VALUES (?)\", (v,))\n")


def test_a_qualified_committer_matches_only_calls_that_resolve_to_that_definition():
    files = {"app/x.py": _DECIDE_PLAIN, "app/y.py": _DECIDE_PLAIN,
             "app/pkg/__init__.py": "from app.x import decide\n",
             "app/cx.py": "from app import x\n" + _CALLS.format("x"),
             "app/cy.py": "from app import y\n" + _CALLS.format("y"),
             "app/co.py": _CALLS.format("obj").replace("conn, x)", "conn, x, obj)", 1)}
    trees = detectors.parse(files)

    def fired(*committers):
        return sorted(h.site for h in detectors.commit_then_link(trees, committers=committers))
    assert fired() == []
    assert fired("decide") == ["app/co.py:f", "app/cx.py:f", "app/cy.py:f"]
    assert fired("app.x:decide") == ["app/cx.py:f"]
    assert fired("app/x.py:decide") == ["app/cx.py:f"]
    assert fired("x.py:decide") == ["app/cx.py:f"]
    assert fired("app.pkg:decide") == ["app/cx.py:f"]
    assert fired("app.x:decide", "app.y:decide") == ["app/co.py:f", "app/cx.py:f", "app/cy.py:f"]
    try:
        fired("app.z:decide")
    except ValueError as e:
        assert "app.z:decide" in str(e)
    else:
        raise AssertionError("a qualified committer naming nothing was accepted")


_HELPERS = ("from app import q\n"
            "def settle(conn, jid):\n    conn.execute(\"UPDATE runs SET job_id = ?\", (jid,))\n"
            "def peek(conn, jid):\n    return conn.execute(\"SELECT * FROM runs WHERE job_id = ?\", (jid,)).fetchone()\n"
            "def keyed(conn, jid):\n    conn.execute(\"INSERT INTO runs (job_id) VALUES (?) ON CONFLICT DO NOTHING\", (jid,))\n"
            "def via(conn, jid):\n    settle(conn, jid)\n"
            "def h2(conn, jid):\n    via(conn, jid)\n"
            "def h3(conn, jid):\n    h2(conn, jid)\n")


def test_a_committed_id_passed_to_a_helper_that_writes_is_a_second_write():
    innocent = {"app/q.py": _ADD, "app/h.py": _HELPERS, "app/r.py": (
        "from app import h, q\n"
        "def reads(conn, v):\n    jid = q.add(conn, v)\n    return h.peek(conn, jid)\n"
        "def deduped(conn, v):\n    jid = q.add(conn, v)\n    h.keyed(conn, jid)\n"
        "def too_deep(conn, v):\n    jid = q.add(conn, v)\n    h.h3(conn, jid)\n"
        "def other(conn, v, w):\n    jid = q.add(conn, v)\n    h.settle(conn, w)\n    return jid\n"
        "def unbound(conn, v, log):\n    jid = q.add(conn, v)\n    log.settle(conn, jid)\n")}
    bad = {"app/s.py": ("from app import q\nfrom app.h import settle, h2\n"
                        "def once(conn, v):\n    jid = q.add(conn, v)\n    settle(conn, jid)\n"
                        "def deep(conn, v):\n    jid = q.add(conn, v)\n    h2(conn, jid=jid)\n")}
    quiet_then_fires(detectors.commit_then_link, innocent, bad)
    assert sites(detectors.commit_then_link, {**innocent, **bad}) == ["app/s.py:deep", "app/s.py:once"]


_STATE = ("def move(conn, rid, to):\n    sql = f\"UPDATE downloads SET state = ? WHERE id = ?\"\n"
          "    return conn.execute(sql, (to, rid)).rowcount == 1\n"
          "def look(conn, rid):\n    sql = \"SELECT state FROM downloads WHERE id = ?\"\n"
          "    return conn.execute(sql, (rid,)).fetchone()\n")
_DL = ("from app import state as ds\n"
       "def start(conn, repo):\n    cur = conn.execute(\"INSERT INTO downloads (repo) VALUES (?)\", (repo,))\n"
       "    conn.commit()\n    return cur.lastrowid\n"
       "def finish(conn, rid):\n    conn.execute(\"UPDATE downloads SET complete = 1 WHERE id = ?\", (rid,))\n"
       "    conn.commit()\n    return {\"id\": rid}\n"
       "def record(conn, repo):\n    rid = start(conn, repo, commit=False)\n    return finish(conn, rid)\n"
       "def peek(conn):\n    row = conn.execute(\"SELECT v FROM meta\").fetchone()\n    conn.commit()\n    return row\n"
       "def schema(conn):\n    return peek(conn)\n")


def test_a_value_a_wrapper_returns_from_a_commit_then_settled_through_a_helper_is_found():
    innocent = {"app/state.py": _STATE, "app/dl.py": _DL, "app/b.py": (
        "from app import state as ds\nfrom app.dl import record, schema\n"
        "def backfill(conn, repos):\n"
        "    def check(rid):\n        ds.look(conn, rid)\n"
        "    def once(repo):\n        got = record(conn, repo)\n        check(got[\"id\"])\n        return got\n"
        "    def read_only(repo):\n        v = schema(conn)\n        ds.move(conn, v, \"seen\")\n")}
    bad = {"app/c.py": ("from app import state as ds\nfrom app.dl import record\n"
                        "def backfill(conn, repos):\n"
                        "    def settle(rid):\n        ds.move(conn, rid, \"removed\")\n"
                        "    def once(repo):\n        got = record(conn, repo)\n        settle(got[\"id\"])\n"
                        "        return got\n")}
    quiet_then_fires(detectors.commit_then_link, innocent, bad)
    assert sites(detectors.commit_then_link, {**innocent, **bad}) == ["app/c.py:backfill.once"]
    wrapped = {"app/w.py": ("from lib import store\ndef wrap(conn):\n    return store.put(conn)\n"
                            "def f(conn):\n    k = wrap(conn)\n    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n")}
    got = detectors.commit_then_link(detectors.parse(wrapped), committers=("put",))
    assert [h.site for h in got] == []


def test_a_statement_that_assigns_a_committed_value_does_not_also_write_it():
    innocent = {"app/q.py": _ADD, "app/h.py": _HELPERS, "app/r.py": (
        "from app import h, q\n"
        "def lease(conn, v):\n    own = h.peek(conn, v)\n    own = own and q.add(conn, own)\n    return own\n"
        "def keyed(conn, r):\n    r[\"recorded\"] = q.add(conn, r)\n    return r\n")}
    bad = {"app/s.py": ("from app import h, q\n"
                        "def once(conn, v):\n    x = q.add(conn, v)\n    h.settle(conn, x)\n"
                        "def again(conn, v):\n    x = q.add(conn, v)\n    x = x and h.settle(conn, x)\n"
                        "def chain(conn, vs):\n    jid = None\n    for v in vs:\n        jid = q.add(conn, v, jid)\n"
                        "def twice(conn, v):\n    x = q.add(conn, v)\n    x = q.add(conn, x)\n")}
    quiet_then_fires(detectors.commit_then_link, innocent, bad)
    assert sites(detectors.commit_then_link, {**innocent, **bad}) == ["app/s.py:again", "app/s.py:chain",
                                                                      "app/s.py:once", "app/s.py:twice"]


_READ_STOPS = ("import json\n"
               "def read(path):\n    rows = []\n    for line in open(path):\n        try:\n"
               "            rows.append(json.loads(line))\n        except ValueError:\n            break\n"
               "    return rows\n")


def test_an_append_after_a_reader_that_stops_on_a_torn_line_is_found():
    bad = {"app/c.py": _READ_STOPS + ("def add(path, row):\n    with open(path, \"a\") as f:\n"
                                      "        f.write(json.dumps(row) + \"\\n\")\n")}
    assert sites(detectors.torn_append, bad) == ["app/c.py:add"]


def test_an_append_that_repairs_the_tail_or_a_reader_that_skips_is_left_alone():
    skip = _READ_STOPS.replace("break", "continue")
    innocent = {
        "app/c.py": _READ_STOPS + ("def add(path, row):\n    with open(path, \"rb\") as f:\n        f.seek(-1, 2)\n"
                                   "        torn = f.read(1) != b\"\\n\"\n"
                                   "    with open(path, \"a\") as f:\n"
                                   "        f.write((\"\\n\" if torn else \"\") + json.dumps(row))\n"
                                   "def rw(path, row):\n    with open(path, \"a+\") as f:\n        f.write(row)\n"
                                   "def fix(path, row):\n    text = open(path).read()\n"
                                   "    if text and not text.endswith(\"\\n\"):\n        text += \"\\n\"\n"
                                   "    with open(path, \"a\") as f:\n        f.write(json.dumps(row))\n"),
        "app/d.py": skip + "def add(path, row):\n    with open(path, mode=\"a\") as f:\n        f.write(row)\n"}
    bad = {"app/e.py": _READ_STOPS + "def add(p, row):\n    with p.open(\"a\") as f:\n        f.write(row)\n"}
    quiet_then_fires(detectors.torn_append, innocent, bad)


_SERVED = ("import subprocess, contextlib, os, signal\n"
           "@contextlib.contextmanager\ndef served(argv):\n"
           "    proc = subprocess.Popen(argv, start_new_session=True)\n"
           "    try:\n        yield proc\n    finally:\n        os.killpg(proc.pid, signal.SIGTERM)\n")


def test_a_new_session_child_stopped_only_on_the_way_out_is_found():
    bad = {"app/serve.py": _SERVED,
           "app/run.py": "from app import serve\ndef main():\n    with serve.served([\"x\"]):\n        pass\n"
                         "if __name__ == \"__main__\":\n    main()\n",
           "app/needle.py": ("import subprocess, atexit, os\nclass S:\n    def __init__(self):\n"
                             "        self.proc = subprocess.Popen([\"s\"], preexec_fn=os.setsid)\n"
                             "        atexit.register(self.close)\n"
                             "    def close(self):\n        self.proc.terminate()\n")}
    assert sites(detectors.orphanable_child, bad) == ["app/needle.py:S.__init__", "app/serve.py:served"]


def test_a_child_whose_owner_handles_sigterm_or_stops_it_in_line_is_left_alone():
    handled = ("import signal\nfrom app import serve\n"
               "def main():\n    signal.signal(signal.SIGTERM, lambda *a: None)\n"
               "    with serve.served([\"x\"]):\n        pass\n"
               "if __name__ == \"__main__\":\n    main()\n")
    innocent = {
        "app/serve.py": _SERVED, "app/run.py": handled,
        "app/m.py": ("import subprocess, signal, contextlib\n"
                     "@contextlib.contextmanager\ndef term_raises():\n"
                     "    old = signal.signal(signal.SIGTERM, lambda *a: None)\n    yield\n"
                     "def group(argv):\n    with term_raises():\n"
                     "        p = subprocess.Popen(argv, start_new_session=True)\n"
                     "        try:\n            p.wait()\n        finally:\n            p.kill()\n"
                     "def inline(argv):\n    p = subprocess.Popen(argv, start_new_session=True)\n"
                     "    p.wait(5)\n    p.terminate()\n"
                     "def same_group(argv):\n    p = subprocess.Popen(argv)\n    try:\n        p.wait()\n"
                     "    finally:\n        p.kill()\n"
                     "def detached(argv):\n    subprocess.Popen(argv, start_new_session=True)\n")}
    bad = {"app/cli.py": "from app import serve\nif __name__ == \"__main__\":\n    serve.served([\"y\"])\n"}
    quiet_then_fires(detectors.orphanable_child, innocent, bad)


def test_a_marker_file_with_no_owner_lifted_on_one_path_is_found():
    bad = {"app/q.py": ("import time\nclass Q:\n"
                        "    def hold(self):\n        (self.root / \"held\").write_text(str(time.time()))\n"
                        "    def release(self):\n        (self.root / \"held\").unlink()\n")}
    assert sites(detectors.orphanable_child, bad) == ["app/q.py:held"]


def test_a_marker_with_a_pid_or_several_lifters_or_an_atomic_rename_is_left_alone():
    innocent = {"app/q.py": (
        "import os\nclass Q:\n"
        "    def hold(self):\n        self.lock.write_text(str(os.getpid()))\n"
        "    def release(self):\n        self.lock.unlink()\n"
        "    def pause(self):\n        self.paused.touch()\n"
        "    def resume(self):\n        self.paused.unlink()\n"
        "    def settle(self):\n        self.paused.unlink(missing_ok=True)\n"
        "    def save(self, text):\n        self.tmp.write_text(text)\n        self.tmp.replace(self.dest)\n"
        "    def drop(self):\n        self.tmp.unlink()\n")}
    bad = {"app/r.py": "def mark(d):\n    (d / \"busy\").touch()\ndef clear(d):\n    (d / \"busy\").unlink()\n"}
    quiet_then_fires(detectors.orphanable_child, innocent, bad)


def _marker(move):
    return {"app/atomic_file.py": "import os\ndef replace(src, dst):\n    os.replace(src, dst)\n",
            "app/m.py": ("import os\nfrom app import atomic_file\n"
                         "def hold(d):\n    (d / \"held\").write_text(\"x\")\n" + move +
                         "def drop(d):\n    (d / \"held\").unlink()\n")}


def test_a_marker_moved_through_a_module_or_a_configured_writer_is_left_alone():
    for move in ("    os.replace(d / \"held\", d / \"done\")\n", "    atomic_file.replace(d / \"held\", d / \"done\")\n"):
        assert sites(detectors.orphanable_child, _marker(move)) == []
    swap = _marker("    atomic_file.swap(d / \"held\", d / \"done\")\n")
    assert sites(detectors.orphanable_child, swap) == ["app/m.py:held"]
    assert detectors.orphanable_child(detectors.parse(swap), writers=("app.atomic_file:swap",)) == []


def test_a_str_replace_on_a_name_like_the_marker_does_not_hide_it():
    got = sites(detectors.orphanable_child, _marker("    held = \"held\"\n    held.replace(\"h\", \"H\")\n"))
    assert got == ["app/m.py:held"]


def test_a_wait_with_no_deadline_or_cap_is_found():
    bad = {"app/w.py": ("import time\n"
                        "def quiesce(is_running):\n    while is_running():\n        time.sleep(5)\n"
                        "def retry(attempt):\n    tries = 0\n    while True:\n        tries += 1\n"
                        "        row = attempt()\n        if row:\n            return row\n        time.sleep(1)\n"
                        "def drain(queue):\n    while True:\n        item = queue.get()\n        if item is None:\n"
                        "            break\n")}
    assert sites(detectors.undeadlined_wait, bad) == ["app/w.py:drain", "app/w.py:quiesce", "app/w.py:retry"]


def test_a_wait_with_a_deadline_or_a_capped_counter_is_left_alone():
    innocent = {"app/w.py": (
        "import time\n"
        "def until(pred, timeout):\n    end = time.monotonic() + timeout\n    while pred():\n"
        "        if time.monotonic() > end:\n            raise TimeoutError\n        time.sleep(1)\n"
        "def capped(pred):\n    n = 0\n    while pred():\n        n += 1\n        if n > 10:\n            break\n"
        "        time.sleep(1)\n"
        "def param(pred, deadline):\n    while pred() and clock() < deadline:\n        time.sleep(1)\n"
        "def spin(xs):\n    while xs:\n        xs.pop()\n"
        "def bounded(pred):\n    for _ in range(10):\n        time.sleep(1)\n")}
    bad = {"app/v.py": "import time\ndef f(p):\n    while not p.done:\n        p.event.wait(1)\n"}
    quiet_then_fires(detectors.undeadlined_wait, innocent, bad)


_STATES = ("PENDING = \"pending\"\nRUNNING = \"running\"\nPAUSED = \"paused\"\nDONE = \"done\"\n"
           "MOVES = {PENDING: {RUNNING}, RUNNING: {PAUSED, DONE}, PAUSED: set(), DONE: set()}\n")
_WRITER = ("from app import js\n"
           "def pause(conn, i):\n    conn.execute(\"UPDATE jobs SET state = 'paused' WHERE id = ?\", (i,))\n"
           "def finish(conn, i):\n    conn.execute(\"UPDATE jobs SET state = ? WHERE id = ?\", (js.DONE, i))\n")
_REPORT = "from app import js\ndef done(rows):\n    return [r for r in rows if r.state == js.DONE]\n"


def test_a_terminal_state_no_one_outside_its_writers_reads_is_found():
    files = {"app/js.py": _STATES, "app/wq.py": _WRITER, "app/report.py": _REPORT}
    assert sites(detectors.terminal_without_reader, files) == ["app/js.py:MOVES:paused"]


def test_a_terminal_state_read_elsewhere_is_left_alone_and_registered_tables_are_used():
    innocent = {"app/js.py": _STATES, "app/wq.py": _WRITER, "app/report.py": _REPORT,
                "app/sweep.py": ("def unstick(conn):\n"
                                 "    conn.execute(\"UPDATE jobs SET state = 'pending' WHERE state = 'paused'\")\n")}
    quiet_then_fires(detectors.terminal_without_reader, innocent, {"app/report.py": "def done(rows):\n    return rows\n"})
    from gauntlet.moves import MoveTable
    table = MoveTable("app/js.py:MOVES", {"pending": {"running"}, "running": {"paused", "stuck"}}, initial=["pending"])
    got = detectors.terminal_without_reader(detectors.parse(innocent), tables=[table])
    assert [h.site for h in got] == ["app/js.py:MOVES:stuck"]


_BOOKING = ("OFFERED = \"offered\"\nCONFIRMED = \"confirmed\"\nLOCKED = \"locked\"\nLAPSED = \"lapsed\"\n"
            "MOVES = {\"accept\": ((OFFERED,), CONFIRMED), \"lock\": ((CONFIRMED, OFFERED), LOCKED),\n"
            "         \"expire\": ((OFFERED,), \"lapsed\")}\n")
_BOOKING_REPORT = "from app import booking\ndef locked(rows):\n    return [r for r in rows if r.state == booking.LOCKED]\n"


def test_an_action_to_from_and_to_states_table_does_not_crash_the_walker():
    trees = detectors.parse({"app/booking.py": _BOOKING})
    detectors.terminal_without_reader(trees)
    assert sites(detectors.move_tables, {"app/booking.py": _BOOKING}) == ["app/booking.py:MOVES"]


def test_an_action_table_is_read_as_moves_from_each_from_state_to_the_to_state():
    tree = detectors.parse({"app/booking.py": _BOOKING})["app/booking.py"]
    (rel, name, line, table, names), = detectors._found_tables({"app/booking.py": tree})
    assert table == {"offered": {"confirmed", "locked", "lapsed"}, "confirmed": {"locked"}, "locked": set(),
                     "lapsed": set()}
    assert names["offered"] == {"OFFERED"} and names["lapsed"] == set()
    got = sites(detectors.terminal_without_reader, {"app/booking.py": _BOOKING, "app/report.py": _BOOKING_REPORT})
    assert got == ["app/booking.py:MOVES:lapsed"]


def test_a_move_table_node_the_walker_does_not_know_is_skipped():
    src = "MOVES = {\"a\": {\"b\", f(x)}, \"b\": {(\"c\", \"d\")}, \"c\": set()}\n"
    tree = detectors.parse({"app/m.py": src})["app/m.py"]
    (_, _, _, table, _), = detectors._found_tables({"app/m.py": tree})
    assert table == {"a": {"b"}, "b": set(), "c": set()}


def test_run_hands_a_detector_its_configuration():
    files = {"app/r.py": "from lib import store\ndef f(conn):\n    k = store.put(1)\n"
                         "    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n"}
    index = {"c": {"detector": "commit_then_link"}}
    trees = detectors.parse(files)
    assert detectors.run(trees, index) == {"c": []}
    got = detectors.run(trees, index, config={"commit_then_link": {"committers": ("put",)}})
    assert [h.site for h in got["c"]] == ["app/r.py:f"]


_SCHEMA = ("SCHEMA = \"\"\"CREATE TABLE jobs (id INTEGER PRIMARY KEY, machine_id INTEGER, argv TEXT);\n"
           "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\"\"\"\n")
_SCOPE = {"columns": ("machine_id",), "helpers": ("_here",), "null_helper": "app/scope.py:here"}


def scoped_sites(files, **config):
    return sorted({h.site for h in detectors.unscoped_query(detectors.parse(files), **{**_SCOPE, **config})})


def test_a_query_on_a_scoped_table_with_no_scope_and_a_stray_null_meaning_are_found():
    bad = {"app/schema.py": _SCHEMA,
           "app/q.py": ("def pending(conn):\n    return conn.execute(\"SELECT id FROM jobs WHERE argv = ?\").fetchall()\n"
                        "def claim(conn, where):\n    conn.execute(f\"UPDATE jobs SET argv = '' WHERE {where}\")\n"
                        "def mine(conn, m):\n    return conn.execute(\"SELECT id FROM leases WHERE machine_id = ? \"\n"
                        "                        \"OR machine_id IS NULL\", (m,)).fetchall()\n")}
    assert scoped_sites(bad) == ["app/q.py:claim:jobs", "app/q.py:mine:null-scope", "app/q.py:pending:jobs"]
    assert scoped_sites(bad, tables=("leases",)) == scoped_sites(bad)
    assert "app/q.py:pending:jobs" in scoped_sites({"app/q.py": bad["app/q.py"]}, tables=("jobs",))
    assert scoped_sites({"app/q.py": bad["app/q.py"]}) == ["app/q.py:mine:null-scope"]


def test_a_scoped_by_id_or_unshared_query_and_the_one_null_helper_are_left_alone():
    innocent = {
        "app/schema.py": _SCHEMA,
        "app/scope.py": "def here(m):\n    return f\"(machine_id = {m} OR machine_id IS NULL)\"\n",
        "app/q.py": ("from app.scope import here as _here\n"
                     "def a(conn, m):\n    return conn.execute(\"SELECT id FROM jobs WHERE machine_id = ?\", (m,))\n"
                     "def b(conn, m):\n    return conn.execute(f\"SELECT id FROM jobs WHERE {_here(m)}\")\n"
                     "def c(conn, i):\n    return conn.execute(\"SELECT argv FROM jobs WHERE id = ?\", (i,))\n"
                     "def d(conn):\n    return conn.execute(\"SELECT body FROM notes\")\n"
                     "def e(conn, m):\n    conn.execute(\"INSERT INTO jobs (machine_id, argv) VALUES (?, '')\", (m,))\n"
                     "def f(conn, m):\n    return conn.execute(\"SELECT id FROM jobs WHERE \" + _here(m))\n"
                     "def g():\n    return \"select a job from jobs\"\n")}
    bad = {"app/r.py": "def h(conn):\n    return conn.execute(\"DELETE FROM jobs WHERE argv = ''\")\n"}
    assert scoped_sites(innocent) == []
    assert scoped_sites({**innocent, **bad}) == ["app/r.py:h:jobs"]
    assert detectors.unscoped_query(detectors.parse({**innocent, **bad})) == []
    moved = {**innocent, "app/r.py": "def h(m):\n    return f\"machine_id = {m} OR machine_id IS NULL\"\n"}
    assert scoped_sites(moved) == ["app/r.py:h:null-scope"]


_ALIVE = "import os\ndef alive(pid):\n    try:\n        os.kill(pid, 0)\n    except OSError:\n        return False\n    return True\n"


def test_a_stored_pid_checked_only_for_liveness_is_found():
    bad = {"app/lease.py": ("import os, json\n"
                            "def holder_alive(path):\n    rec = json.loads(path.read_text())\n    try:\n"
                            "        os.kill(rec[\"pid\"], 0)\n    except ProcessLookupError:\n        return False\n"
                            "    return True\n"),
           "app/wq.py": _ALIVE + ("def settle(conn):\n    for row in conn.execute(\"SELECT pid FROM leases\"):\n"
                                  "        if not alive(row[0]):\n            conn.execute(\"DELETE FROM leases\")\n"),
           "app/d.py": ("from lib import wq\ndef switching(p):\n    pid = int(p.read_text())\n"
                        "    return wq.running(pid)\n"),
           "app/deploy.py": ("import json\nfrom app import wq\n"
                             "def _read(p):\n    return json.loads(p.read_text())\n"
                             "def settle(p, alive=wq.alive):\n    rec = _read(p)\n"
                             "    if rec.get(\"pid\") and alive(int(rec[\"pid\"])):\n        return None\n")}
    assert sites(detectors.stored_pid_liveness, bad) == ["app/deploy.py:settle", "app/lease.py:holder_alive",
                                                          "app/wq.py:settle"]
    got = detectors.stored_pid_liveness(detectors.parse(bad), alive=("running",))
    assert sorted(h.site for h in got) == ["app/d.py:switching", "app/deploy.py:settle", "app/lease.py:holder_alive",
                                           "app/wq.py:settle"]


def test_an_own_or_child_pid_or_one_checked_against_a_second_identity_is_left_alone():
    innocent = {"app/wq.py": _ALIVE + (
        "import subprocess, json\n"
        "def me():\n    return alive(os.getpid())\n"
        "def child(argv):\n    proc = subprocess.Popen(argv)\n    return alive(proc.pid)\n"
        "def leads(path):\n    rec = json.loads(path.read_text())\n"
        "    return alive(rec[\"pid\"]) and os.getpgid(rec[\"pid\"]) == rec[\"pgid\"]\n"
        "def same(path, started):\n    rec = json.loads(path.read_text())\n"
        "    return alive(rec[\"pid\"]) and rec[\"start_time\"] == started\n"
        "def _leads(pid):\n    return os.getpgid(pid) == pid\n"
        "def _pid_of(path):\n    pid = int(path.read_text())\n    return pid if alive(pid) and _leads(pid) else None\n"
        "def get(path):\n    return json.loads(path.read_text())\n"
        "def ended(holder):\n    pid = holder.get(\"pid\")\n    return not alive(int(pid))\n")}
    bad = {"app/x.py": "import os\ndef f(p):\n    os.kill(int(open(p).read()), 0)\n"}
    quiet_then_fires(detectors.stored_pid_liveness, innocent, bad)


def test_an_unkeyed_file_cache_an_unlocked_json_cache_and_a_persisted_process_cache_are_found():
    bad = {"app/m.py": (
        "import functools, json, subprocess\n"
        "@functools.lru_cache(maxsize=4)\ndef cases(root):\n    return (root / \"cases.txt\").read_text().split()\n"
        "@functools.cache\ndef versions():\n"
        "    return subprocess.run([\"llama-server\", \"--version\"], capture_output=True).stdout\n"
        "def remember(conn, m):\n    v = versions()\n"
        "    conn.execute(\"UPDATE machines SET versions = ? WHERE id = ?\", (v, m))\n"
        "def digests(home):\n    path = home / \"cache\" / \"digests.json\"\n"
        "    got = json.loads(path.read_text())\n    got[\"x\"] = 1\n    path.write_text(json.dumps(got))\n")}
    assert sites(detectors.uncached_key, bad) == ["app/m.py:cases", "app/m.py:digests", "app/m.py:remember",
                                                  "app/m.py:versions"]


def test_a_stamped_cache_a_locked_json_cache_and_an_unpersisted_cache_are_left_alone():
    innocent = {"app/m.py": (
        "import functools, json, fcntl\n"
        "@functools.lru_cache\ndef cases(root, mtime):\n    return (root / \"cases.txt\").read_text().split()\n"
        "@functools.cache\ndef table():\n    return {\"a\": 1}\n"
        "def show():\n    print(table())\n"
        "_PATH = \"cache.json\"\n"
        "def load():\n    with open(_PATH) as f:\n        return json.load(f)\n"
        "def save(d):\n    with open(_PATH, \"w\") as f:\n        fcntl.flock(f, fcntl.LOCK_EX)\n"
        "        json.dump(d, f)\n"
        "def once(p, d):\n    p.write_text(json.dumps(d))\n"
        "_THIS = None\n"
        "def this():\n    global _THIS\n    if _THIS is None:\n        _THIS = probe()\n    return _THIS\n"
        "def report():\n    print(this())\n")}
    bad = {"app/n.py": "from app.m import this\ndef keep(path):\n    path.write_text(this())\n"}
    quiet_then_fires(detectors.uncached_key, innocent, bad)
    torn = {"app/o.py": ("import json, os\n"
                         "def load():\n    with open(\"c.json\") as f:\n        return json.load(f)\n"
                         "def save(d):\n    with open(\"c.json.tmp\", \"w\") as f:\n        json.dump(d, f)\n"
                         "    os.replace(\"c.json.tmp\", \"c.json\")\n")}
    assert sites(detectors.uncached_key, {**innocent, **torn}) == ["app/o.py:save"]


_PATHS = {"app/paths.py": "from pathlib import Path\nSTATE = Path(\"state.json\")\n"}
_READER = {"app/r.py": "import json\nfrom app import paths\ndef load():\n    return json.loads(paths.STATE.read_text())\n"}


def test_a_json_writer_and_reader_in_two_modules_sharing_a_path_constant_are_found():
    writer = {"app/w.py": ("import json\nfrom app.paths import STATE\n"
                           "def save(d):\n    STATE.write_text(json.dumps(d))\n")}
    assert sites(detectors.uncached_key, {**_PATHS, **_READER, **writer}) == ["app/w.py:save"]
    same = {"app/v.py": ("import json\nfrom pathlib import Path\n"
                         "def save(d):\n    Path(\"cache.json\").write_text(json.dumps(d))\n"),
            "app/u.py": ("import json\nfrom pathlib import Path\n"
                         "def load():\n    return json.loads(Path(\"cache.json\").read_text())\n")}
    assert sites(detectors.uncached_key, same) == ["app/v.py:save"]


def test_a_locked_cross_module_pair_and_unrelated_local_paths_are_left_alone():
    innocent = {**_PATHS, **_READER,
                "app/w.py": ("import json\nfrom filelock import FileLock\nfrom app.paths import STATE\n"
                             "def save(d):\n    with FileLock(\"state.lock\"):\n"
                             "        STATE.write_text(json.dumps(d))\n"),
                "app/a.py": "import json\ndef keep(path, d):\n    path.write_text(json.dumps(d))\n",
                "app/b.py": "import json\ndef get(path):\n    return json.loads(path.read_text())\n"}
    bad = {"app/x.py": ("import json\nfrom app import paths\n"
                        "def bump(d):\n    paths.STATE.write_text(json.dumps(d))\n")}
    quiet_then_fires(detectors.uncached_key, innocent, bad)
    assert sites(detectors.uncached_key, {**innocent, **bad}) == ["app/x.py:bump"]


_ATOMIC = {"app/atomic_file.py": ("import os, time\n"
                                  "def replace(src, dst):\n    for _ in range(3):\n        try:\n"
                                  "            return os.replace(src, dst)\n        except PermissionError:\n"
                                  "            time.sleep(0.1)\n"
                                  "def swap(src, dst):\n    return replace(src, dst)\n")}


def _store(move, head="from app import atomic_file\n"):
    return {"app/store.py": ("import json\nfrom pathlib import Path\n" + head + "STATE = Path(\"state.json\")\n"
                             "def load():\n    return json.loads(STATE.read_text())\n"
                             "def save(d):\n    tmp = STATE.with_suffix(\".tmp\")\n"
                             "    tmp.write_text(json.dumps(d))\n" + move)}


def test_a_replace_through_a_module_qualified_wrapper_is_followed():
    got = sites(detectors.uncached_key, {**_ATOMIC, **_store("    atomic_file.replace(tmp, STATE)\n")})
    assert got == ["app/store.py:save"]


def test_a_configured_writer_is_followed_through_its_module_or_an_imported_name():
    via_module = {**_ATOMIC, **_store("    atomic_file.swap(tmp, STATE)\n")}
    via_name = {**_ATOMIC, **_store("    move(tmp, STATE)\n", "from app.atomic_file import swap as move\n")}
    for files in (via_module, via_name):
        assert sites(detectors.uncached_key, files) == []
        got = detectors.uncached_key(detectors.parse(files), writers=("app.atomic_file:swap",))
        assert sorted(h.site for h in got) == ["app/store.py:save"]
    other = detectors.uncached_key(detectors.parse(via_module), writers=("app.other:swap",))
    assert other == []


def test_a_two_argument_str_replace_is_not_a_move():
    for move in ("    name = str(tmp)\n    name.replace(tmp, STATE)\n", "    \"state\".replace(tmp, STATE)\n"):
        assert sites(detectors.uncached_key, {**_ATOMIC, **_store(move)}) == []


_PROSE = ("note", "detail", "reason", "why", "outcome", "evidence")


def prose_sites(files, fields=_PROSE):
    return sorted({h.site for h in detectors.prose_match(detectors.parse(files), fields=fields)})


def test_a_decision_matched_out_of_stored_prose_is_found():
    bad = {"app/gate.py": (
        "import re\nPASSED = re.compile(r\"passed over\")\nAT_STOP_TIME = \"stop time\"\n"
        "def verdict(conn):\n"
        "    return conn.execute(\"SELECT id FROM verdicts WHERE v.detail LIKE 'repo:%' AND x = 1\").fetchone()\n"
        "def overrun(conn, out):\n    return conn.execute(\"SELECT 1 FROM acts WHERE instr(evidence, ?) > 0\", (out,))\n"
        "def superseded(job):\n    return job[\"note\"].startswith(\"superseded by \")\n"
        "def held(row):\n    return \"notified:\" in (row.outcome or \"\")\n"
        "def looks(progress):\n    return str(progress.get(\"why\") or \"\").startswith(AT_STOP_TIME)\n"
        "def aged(rows):\n    return [r for r in rows if PASSED.search(r[\"detail\"])]\n"
        "def refused(reason):\n    return re.match(r\"too-big\", reason.strip())\n")}
    assert prose_sites(bad) == ["app/gate.py:aged:detail", "app/gate.py:held:outcome", "app/gate.py:looks:why",
                                "app/gate.py:overrun:evidence", "app/gate.py:refused:reason",
                                "app/gate.py:superseded:note", "app/gate.py:verdict:detail"]
    assert prose_sites(bad, fields=()) == []


def test_prose_compared_whole_external_text_and_its_blind_spots_are_left_alone():
    innocent = {"app/gate.py": (
        "def by_column(conn):\n    return conn.execute(\"SELECT id FROM verdicts WHERE reason = ? AND lane = 'repo'\")\n"
        "def external(proc):\n    return proc.stderr.startswith(\"error\")\n"
        "def named(row):\n    return row[\"name\"].startswith(\"Qwen\")\n"
        "def show(detail):\n    print(detail)\n"
        "def whole(job):\n    return job[\"note\"] == \"superseded\"\n"
        "def variable(job, prefix):\n    return job[\"note\"].startswith(prefix)\n"
        "def label(conn):\n    return conn.execute(\"SELECT 1 FROM t WHERE title LIKE 'x%'\")\n"
        "def message():\n    return \"refused: the detail GLOB matched nothing\"\n")}
    bad = {"app/r.py": "def f(conn):\n    return conn.execute(\"SELECT 1 FROM t WHERE note GLOB 'x*'\")\n"}
    assert prose_sites(innocent) == []
    assert prose_sites({**innocent, **bad}) == ["app/r.py:f:note"]
    lower = {"app/l.py": "def g(conn):\n    return conn.execute(\"select id from verdicts where detail like 'repo:%'\")\n"}
    assert prose_sites({**innocent, **lower}) == ["app/l.py:g:detail"]


_LEDGER = ("import json, sqlite3\nfrom app import store, deploy\n"
           "def gateway_rows(conn):\n    try:\n        return conn.execute(\"SELECT * FROM g\").fetchall()\n"
           "    except sqlite3.OperationalError:\n        return []\n"
           "def passed_over(conn):\n    try:\n        got = {r[0]: r[1] for r in conn.execute(\"SELECT n, d FROM v\")}\n"
           "    except sqlite3.Error:\n        got = {}\n    return got\n"
           "def settle():\n    try:\n        return deploy.settle()\n    except (OSError, ValueError):\n        return\n"
           "def maybe(conn, strict):\n    try:\n        return conn.execute(\"SELECT 1\").fetchall()\n"
           "    except sqlite3.Error:\n        if strict:\n            raise\n        return []\n"
           "def show(conn):\n    rows = gateway_rows(conn)\n    if not rows:\n"
           "        print(\"no gateway requests in the window\")\n"
           "def report(path):\n    try:\n        rows = json.loads(path.read_text())\n    except OSError:\n"
           "        rows = None\n    if rows is None:\n        print(f\"nothing recorded in {path}\")\n"
           "def gather():\n    return store.connect() if store.backend() == \"postgres\" else None\n")
_CHOOSER = ("from app import store\n"
            "def _read_conn():\n    if store.backend() == \"postgres\":\n        return store.connect()\n"
            "    return None\n")
_STORE = "def backend():\n    return \"sqlite\"\ndef connect():\n    return None\ndef ok():\n    return backend()\n"


def empty_sites(files, **config):
    return sorted({h.site for h in detectors.empty_on_failure(detectors.parse(files), **config)})


def test_a_failed_read_returned_as_nothing_a_none_message_over_it_and_copied_store_choosers_are_found():
    bad = {"app/ledger.py": _LEDGER, "app/context.py": _CHOOSER, "app/store.py": _STORE}
    found = ["app/ledger.py:gateway_rows:except", "app/ledger.py:maybe:except", "app/ledger.py:passed_over:except",
             "app/ledger.py:report:none-message", "app/ledger.py:settle:except", "app/ledger.py:show:none-message"]
    assert empty_sites(bad) == found
    assert empty_sites(bad, choosers=("backend",)) == sorted(
        found + ["app/context.py:_read_conn:chooser", "app/ledger.py:gather:chooser"])


def test_a_read_that_raises_or_says_it_failed_and_a_single_store_chooser_are_left_alone():
    innocent = {"app/ledger.py": (
        "import json, sqlite3\nDEFAULTS = {\"a\": 1}\n"
        "class StoreDown(Exception):\n    pass\n"
        "def strict(conn):\n    try:\n        return conn.execute(\"SELECT * FROM g\").fetchall()\n"
        "    except sqlite3.Error as exc:\n        raise StoreDown(\"the store could not be read\") from exc\n"
        "def logged(conn):\n    try:\n        rows = conn.execute(\"SELECT * FROM g\").fetchall()\n"
        "    except sqlite3.Error:\n        rows = []\n        raise\n    return rows\n"
        "def show(conn):\n    rows = strict(conn)\n    if not rows:\n        print(\"no gateway requests\")\n"
        "def number(text):\n    try:\n        return int(text)\n    except ValueError:\n        return 0\n"
        "def config(path):\n    try:\n        return json.loads(path.read_text())\n    except OSError:\n"
        "        return DEFAULTS\n"
        "def report(path):\n    try:\n        rows = json.loads(path.read_text())\n    except OSError:\n"
        "        rows = None\n    if rows is None:\n        print(\"could not read the ledger: no file\")\n"),
        "app/common.py": _CHOOSER, "app/store.py": _STORE}
    bad = {"app/x.py": "def f(p):\n    try:\n        return open(p).read()\n    except OSError:\n        return ''\n"}
    quiet_then_fires(detectors.empty_on_failure, innocent, bad)
    assert empty_sites(innocent, choosers=("backend",)) == []


_DDL = ("SCHEMA = \"\"\"\nCREATE TABLE IF NOT EXISTS jobs (\n    id INTEGER PRIMARY KEY,\n"
        "    argv TEXT NOT NULL DEFAULT '',  -- the command, as run\n    note TEXT,\n"
        "    night_gate INTEGER DEFAULT 0,\n    machine_id INTEGER REFERENCES machines(id),\n"
        "    UNIQUE (argv, note)\n);\n"
        "CREATE TABLE day_clearances (id INTEGER PRIMARY KEY, cleared_at REAL, until REAL);\n\"\"\"\n")
_MIGRATION = ("def up(c):\n    c.execute(\"ALTER TABLE jobs ADD COLUMN shown_first TEXT\")\n"
              "    c.execute(\"UPDATE jobs SET night_gate = 1\")\n"
              "    c.execute(\"SELECT note, night_gate, cleared_at FROM jobs, day_clearances\")\n")
_WR = {"migrations": ("app/schema.py", "app/migrations/")}


def column_sites(files, **config):
    return sorted({h.site for h in detectors.unpaired_column(detectors.parse(files), **{**_WR, **config})})


def test_a_column_nothing_writes_or_nothing_reads_is_found():
    bad = {"app/schema.py": _DDL, "app/migrations/v2.py": _MIGRATION, "app/q.py": (
        "def add(conn, argv, m):\n"
        "    conn.execute(\"INSERT INTO jobs (argv, machine_id, shown_first) VALUES (?, ?, '')\", (argv, m))\n"
        "def mine(conn, m):\n    return conn.execute(\"SELECT id, argv FROM jobs WHERE machine_id = ?\", (m,))\n"
        "def annotate(conn, i, n):\n    conn.execute(\"UPDATE jobs SET note = ? WHERE id = ?\", (n, i))\n"
        "def clearances(conn):\n    return conn.execute(\"SELECT cleared_at, until FROM day_clearances\")\n")}
    assert column_sites(bad) == [
        "app/migrations/v2.py:jobs.shown_first:no-reader", "app/schema.py:day_clearances.cleared_at:no-writer",
        "app/schema.py:day_clearances.until:no-writer", "app/schema.py:jobs.night_gate:no-reader",
        "app/schema.py:jobs.night_gate:no-writer", "app/schema.py:jobs.note:no-reader"]
    assert column_sites(bad, migrations=("app/migrations/",), schema=("app/schema.py",)) == [
        s for s in column_sites(bad) if "shown_first" not in s]
    assert column_sites(bad, migrations=()) == []


def test_columns_paired_through_upserts_dynamic_inserts_and_selected_rows_are_left_alone():
    innocent = {"app/schema.py": _DDL, "app/migrations/v2.py": _MIGRATION, "app/q.py": (
        "def add(conn, argv, m):\n"
        "    conn.execute(\"INSERT INTO jobs (argv, machine_id, shown_first) VALUES (?, ?, '') \"\n"
        "                 \"ON CONFLICT (argv) DO UPDATE SET night_gate = excluded.night_gate\", (argv, m))\n"
        "def put(conn, **cols):\n"
        "    conn.execute(\"INSERT INTO day_clearances (\" + \", \".join(cols) + \") VALUES (?, ?)\", tuple(cols))\n"
        "def clear(conn):\n    put(conn, cleared_at=1.0, until=2.0)\n"
        "def rows(conn):\n    return [dict(r) for r in conn.execute(\"SELECT * FROM jobs WHERE machine_id = ?\", (1,))]\n"
        "def gated(conn):\n    return conn.execute(\"SELECT cleared_at, until FROM day_clearances\").fetchall()\n"
        "def annotate(conn, i, n):\n    conn.execute(\"UPDATE jobs SET note = ? WHERE id = ?\", (n, i))\n"),
        "app/show.py": ("from app.q import rows\n"
                        "def show(conn):\n    for r in rows(conn):\n"
                        "        print(r[\"argv\"], r[\"note\"], r.get(\"night_gate\"), r[\"shown_first\"])\n")}
    assert column_sites(innocent) == []
    bad = {"app/migrations/v3.py": "SQL = \"ALTER TABLE jobs ADD COLUMN said TEXT\"\n"}
    assert column_sites({**innocent, **bad}) == ["app/migrations/v3.py:jobs.said:no-reader",
                                                 "app/migrations/v3.py:jobs.said:no-writer"]


_SKEW = {"versions": ("SCHEMA_VERSION", "stored_schema"), "phases": ("smoke",), "markers": ("SMOKE_PHASE",)}


def skew_sites(files, **config):
    return sorted({h.site for h in detectors.one_sided_schema_check(detectors.parse(files), **config)})


def test_a_one_sided_schema_check_a_dropped_mismatch_and_a_check_blind_to_its_phase_are_found():
    bad = {"app/deploy.py": (
        "import sys\nfrom app import store\nSCHEMA_VERSION = 5\n"
        "def stale(conn, cwd):\n    code = schema_of(cwd)\n    have = store.stored_schema(conn)\n"
        "    if code >= have:\n        return \"\"\n    return \"behind\"\n"
        "def newer(conn):\n    if SCHEMA_VERSION < store.stored_schema(conn):\n        raise SystemExit(1)\n"
        "def note(conn, row):\n    have = store.stored_schema(conn)\n    if have != SCHEMA_VERSION:\n"
        "        print(\"skipped\", file=sys.stderr)\n        return None\n"
        "    conn.execute(\"INSERT INTO d VALUES (?)\", row)\n"
        "def smoke(argv):\n    for name in argv:\n        probe(name)\n"
        "def probe(name):\n    return name\n"
        "def live(conn):\n    if store.stored_schema(conn) != SCHEMA_VERSION:\n        conn.close()\n"
        "        return _skip(\"another schema\")\n    return conn\n"
        "def run(conn, release):\n    have, want = store.stored_schema(conn), plan(release)\n"
        "    return want > have\n")}
    assert skew_sites(bad, **_SKEW) == ["app/deploy.py:newer:one-sided", "app/deploy.py:note:drops",
                                        "app/deploy.py:run:one-sided", "app/deploy.py:smoke:both-phases",
                                        "app/deploy.py:stale:one-sided"]
    assert skew_sites(bad, **_SKEW, skips=("_skip",)) == sorted(skew_sites(bad, **_SKEW) + ["app/deploy.py:live:drops"])
    assert skew_sites(bad) == []


def test_a_two_sided_or_raising_version_check_and_a_phase_aware_check_are_left_alone():
    innocent = {"app/deploy.py": (
        "import os\nfrom app import store\nSCHEMA_VERSION = 5\nLIMIT = 3\n"
        "def migrate(conn):\n    have = store.stored_schema(conn)\n    if have > SCHEMA_VERSION:\n"
        "        raise RuntimeError(\"newer store\")\n    if SCHEMA_VERSION > have:\n        upgrade(conn)\n"
        "def exact(conn):\n    have = store.stored_schema(conn)\n    if have != SCHEMA_VERSION:\n"
        "        raise RuntimeError(\"skew\")\n    return have < SCHEMA_VERSION\n"
        "def strict(conn, hard):\n    if store.stored_schema(conn) != SCHEMA_VERSION:\n        if hard:\n"
        "            raise SkewError\n        return None\n"
        "def spooled(conn, row):\n    if store.stored_schema(conn) != SCHEMA_VERSION:\n        spool(row)\n"
        "        return None\n"
        "def size(xs, version):\n    return len(xs) < LIMIT or version < 3\n"
        "def pair(conn, xs):\n    n, have = len(xs), store.stored_schema(conn)\n    return n < LIMIT and have\n"
        "def smoke(argv):\n    for name in argv:\n        probe(name)\n"
        "def probe(name):\n    if os.environ.get(\"SMOKE_PHASE\") == \"before\":\n        return None\n    return name\n"
        "def smoke_main():\n    return probe(\"x\")\n")}
    bad = {"app/q.py": "from app.deploy import SCHEMA_VERSION\ndef f(v):\n    return v <= SCHEMA_VERSION\n"}
    assert skew_sites(innocent, **_SKEW) == []
    assert skew_sites({**innocent, **bad}, **_SKEW) == ["app/q.py:f:one-sided"]


_EFFECTS = {"effects": ("desktop", "post"), "live": ("DEFAULT_HOME", "live_home", "PYTEST_CURRENT_TEST"),
            "guarded": ("osascript", "ssh")}
_MONITOR = ("from urllib.request import urlopen\n"
            "def post(url):\n    if not live_home():\n        return\n    urlopen(url)\n")


def effect_sites(files, **config):
    return sorted({h.site for h in detectors.effect_reachable(detectors.parse(files), **config)})


def test_an_effect_with_no_live_gate_one_bound_as_a_default_and_an_unguarded_binary_are_found():
    bad = {"app/monitor.py": _MONITOR, "app/notify.py": (
        "import subprocess\nfrom app import monitor\nGH = \"gh\"\n"
        "def desktop(msg):\n    subprocess.run([\"/usr/bin/osascript\", \"-e\", msg])\n"
        "def alert(msg, home):\n    if home != DEFAULT_HOME:\n        return\n    desktop(msg)\n"
        "def morning(msg):\n    desktop(msg)\n"
        "def ci(gh=monitor.post):\n    return gh(\"x\")\n"
        "def issues():\n    argv = [GH, \"issue\", \"create\"]\n    subprocess.run(argv)\n"
        "def launch():\n    subprocess.Popen(\"launchctl bootstrap gui/1 x\", shell=True)\n")}
    assert effect_sites(bad, **_EFFECTS) == ["app/notify.py:ci:default:post", "app/notify.py:issues:binary:gh",
                                             "app/notify.py:launch:binary:launchctl", "app/notify.py:morning:desktop"]
    assert effect_sites(bad) == ["app/notify.py:desktop:binary:osascript", "app/notify.py:issues:binary:gh",
                                 "app/notify.py:launch:binary:launchctl"]


def test_a_gated_effect_a_hook_resolved_at_call_time_and_guarded_binaries_are_left_alone():
    innocent = {"app/monitor.py": _MONITOR, "app/notify.py": (
        "import os, subprocess\nfrom app import monitor\n"
        "def desktop(msg):\n    \"\"\"Not the DEFAULT_HOME gate, only its name.\"\"\"\n"
        "    if os.environ.get(\"PYTEST_CURRENT_TEST\"):\n        return\n"
        "    subprocess.run([\"osascript\", \"-e\", msg])\n"
        "def morning(msg):\n    desktop(msg)\n"
        "def ci(gh=None):\n    return (gh or monitor.post)(\"x\")\n"
        "def status():\n    subprocess.run([\"git\", \"status\"])\n"
        "def probe(pid):\n    os.kill(pid, 0)\n"
        "def remote():\n    subprocess.run([\"ssh\", \"host\", \"true\"])\n")}
    assert effect_sites(innocent, **_EFFECTS) == []
    second = {"app/u.py": "def desktop(msg):\n    print(msg)\n"}
    assert effect_sites({**innocent, **second}, **_EFFECTS) == ["app/notify.py:morning:desktop"]
    bare = {"app/notify.py": innocent["app/notify.py"].replace("PYTEST_CURRENT_TEST", "VERBOSE")}
    assert effect_sites({**innocent, **bare}, **_EFFECTS) == ["app/notify.py:morning:desktop"]


_ORDER = {"effects": ("desktop",), "reads": ("hf_home",), "entries": ("main", "__main__"), "guard": ("env.guard",),
          "needs": ("models_dir", "huggingface_hub"), "pairs": {"LOCALHARNESS_HOME": ("LOCALHARNESS_STORE", "pinned")}}


def order_sites(files, **config):
    return sorted({h.site for h in detectors.unchecked_order(detectors.parse(files), **config)})


def test_a_default_bound_hook_an_import_time_read_an_unguarded_entry_and_a_home_without_its_backend_are_found():
    bad = {"app/run.py": (
        "import os\nfrom pathlib import Path\nfrom app import monitor, env\n"
        "MODEL = os.environ.get(\"WHISPER_MODEL\", \"base\")\nBIN = Path" ".home() / \"bin\"\n"
        "class Conf:\n    ROOT = env.hf_home()\n"
        "def alert(msg, notify=monitor.desktop):\n    notify(msg)\n"
        "def main():\n    print(models_dir())\n"
        "def scratch(path):\n    for k, v in {\"LOCALHARNESS_HOME\": path, \"X\": \"1\"}.items():\n"
        "        os.environ[k] = v\n"
        "def fixture(monkeypatch, p):\n    monkeypatch.setenv(\"LOCALHARNESS_HOME\", p)\n"
        "def direct(p):\n    os.environ[\"LOCALHARNESS_HOME\"] = p\n"
        "def merged(p):\n    os.environ.update(LOCALHARNESS_HOME=p)\n"
        "SCRATCH = {\"LOCALHARNESS_HOME\": \"\", \"HF_HOME\": \"hf\"}\n"
        "def chaos(p):\n    for var, sub in SCRATCH.items():\n        os.environ[var] = p + sub\n"
        "if __name__ == \"__main__\":\n    main()\n"),
        "app/vlm.py": ("def label():\n    from huggingface_hub import hf_hub_download\n    return hf_hub_download(\"x\")\n"
                       "if __name__ == \"__main__\":\n    label()\n")}
    assert order_sites(bad, **_ORDER) == [
        "app/run.py:<module>:import:WHISPER_MODEL", "app/run.py:<module>:import:hf_home",
        "app/run.py:<module>:import:home()", "app/run.py:__main__:no-guard", "app/run.py:alert:default:desktop",
        "app/run.py:chaos:LOCALHARNESS_HOME:no-backend",
        "app/run.py:direct:LOCALHARNESS_HOME:no-backend", "app/run.py:fixture:LOCALHARNESS_HOME:no-backend",
        "app/run.py:main:no-guard", "app/run.py:merged:LOCALHARNESS_HOME:no-backend",
        "app/run.py:scratch:LOCALHARNESS_HOME:no-backend", "app/vlm.py:__main__:no-guard"]
    assert order_sites(bad) == ["app/run.py:<module>:import:WHISPER_MODEL", "app/run.py:<module>:import:home()"]


def test_reads_at_call_time_a_guarded_entry_and_a_home_set_with_its_backend_are_left_alone():
    innocent = {"app/run.py": (
        "import os\nfrom app import monitor, env, store\nLIMIT = 3\n"
        "def model():\n    return os.environ.get(\"WHISPER_MODEL\", \"base\")\n"
        "def alert(msg, notify=None):\n    (notify or monitor.desktop)(msg)\n"
        "def main():\n    setup()\n    print(models_dir())\n"
        "def setup():\n    app.env.guard()\n"
        "def scratch(path):\n    os.environ[\"LOCALHARNESS_HOME\"] = path\n"
        "    os.environ[\"LOCALHARNESS_STORE\"] = \"sqlite\"\n"
        "def both(path):\n    os.environ.update(LOCALHARNESS_HOME=path, LOCALHARNESS_STORE=\"sqlite\")\n"
        "def held(monkeypatch, path):\n    monkeypatch.setenv(\"LOCALHARNESS_HOME\", path)\n    store.pinned(path)\n"
        "if __name__ == \"__main__\":\n    print(os.environ.get(\"DEBUG\"))\n    main()\n"),
        "app/plain.py": "def main():\n    print(\"hi\")\nif __name__ == \"__main__\":\n    main()\n"}
    bad = {"app/hf.py": "def main():\n    return models_dir()\n"}
    assert order_sites(innocent, **_ORDER) == []
    assert order_sites({**innocent, **bad}, **_ORDER) == ["app/hf.py:main:no-guard"]


_SQL_WORDS = ("SELECT|INSERT|UPDATE|DELETE|REPLACE|INTO|FROM|WHERE|SET|VALUES|JOIN|ON|CONFLICT|DO|AND|OR|IS|NOT|NULL|"
              "LIKE|GLOB|CREATE|TABLE|IF|EXISTS|ALTER|ADD|COLUMN|DROP|PRIMARY|KEY|UNIQUE|REFERENCES|DEFAULT|INTEGER|"
              "TEXT|REAL|DISTINCT")


def lowered(files):
    return {k: re.sub(rf"\b(?:{_SQL_WORDS})\b", lambda m: m.group(0).lower(), v) for k, v in files.items()}


def test_lowercase_sql_is_read_like_uppercase_sql():
    scope = {"app/schema.py": _SCHEMA,
             "app/q.py": ("def pending(conn):\n    return conn.execute(\"SELECT id FROM jobs WHERE argv = ?\").fetchall()\n"
                          "def mine(conn, m):\n    return conn.execute(\"SELECT id FROM leases WHERE machine_id = ? \"\n"
                          "                        \"OR machine_id IS NULL\", (m,)).fetchall()\n"
                          "def g():\n    return \"select a job from jobs\"\n")}
    assert "update" in lowered({"x": "UPDATE"})["x"]
    assert scoped_sites(lowered(scope)) == scoped_sites(scope) == ["app/q.py:mine:null-scope", "app/q.py:pending:jobs"]
    prose = {"app/gate.py": (
        "def verdict(conn):\n"
        "    return conn.execute(\"SELECT id FROM verdicts WHERE v.detail LIKE 'repo:%' AND x = 1\").fetchone()\n")}
    assert prose_sites(lowered(prose)) == prose_sites(prose) == ["app/gate.py:verdict:detail"]
    cols = {"app/schema.py": _DDL, "app/migrations/v2.py": _MIGRATION, "app/q.py": (
        "def add(conn, argv, m):\n"
        "    conn.execute(\"INSERT INTO jobs (argv, machine_id, shown_first) VALUES (?, ?, '') \"\n"
        "                 \"ON CONFLICT (argv) DO UPDATE SET night_gate = excluded.night_gate\", (argv, m))\n"
        "def mine(conn, m):\n    return conn.execute(\"SELECT id, argv FROM jobs WHERE machine_id = ?\", (m,))\n"
        "def annotate(conn, i, n):\n    conn.execute(\"UPDATE jobs SET note = ? WHERE id = ?\", (n, i))\n"
        "def clearances(conn):\n    return conn.execute(\"SELECT cleared_at, until FROM day_clearances\")\n"),
        "app/migrations/v3.py": "SQL = \"ALTER TABLE jobs ADD COLUMN said TEXT\"\n"}
    assert column_sites(lowered(cols)) == column_sites(cols)
    assert "app/migrations/v3.py:jobs.said:no-writer" in column_sites(cols)


def test_a_class_with_two_detectors_runs_both():
    trees = detectors.parse({"app/r.py": "def f(store, k):\n    store.put(k)\n    store.link(k)\n"})
    both = lambda trees, production, tag="": [detectors.Hit(f"app/r.py:{tag}a", 1, "a")]
    other = lambda trees, production: [detectors.Hit("app/r.py:b", 2, "b")]
    extra = {"one": both, "two": other}
    got = detectors.run(trees, {"c": {"detector": ("one", "two")}}, detectors=extra, config={"one": {"tag": "x"}})
    assert [h.site for h in got["c"]] == ["app/r.py:b", "app/r.py:xa"]
    assert detectors.names({"detector": "one"}) == ("one",)
    assert detectors.names({"detector": ["one", "two"]}) == ("one", "two")
    assert detectors.names({}) == ()


def test_a_dotted_effect_finds_its_own_definition_and_its_gate():
    files = {"pkg/monitor.py": "def alert(kind, home=None):\n    if live_home(home):\n        send(kind)\n",
             "pkg/deploy.py": "from pkg import monitor\ndef _alert_here(k):\n    monitor.alert(k)\n"}
    assert effect_sites(files, effects=("monitor.alert",), live=("live_home",)) == []
    assert effect_sites(files, effects=("alert",), live=("live_home",)) == []
    ungated = {**files, "pkg/monitor.py": "def alert(kind, home=None):\n    send(kind)\n"}
    assert effect_sites(ungated, effects=("monitor.alert",), live=("live_home",)) == [
        "pkg/deploy.py:_alert_here:monitor.alert"]
    elsewhere = {**files, "pkg/monitor.py": "from pkg.vendor import alert\n",
                 "pkg/other.py": "def alert(kind):\n    if live_home():\n        send(kind)\n"}
    assert effect_sites(elsewhere, effects=("monitor.alert",), live=("live_home",)) == [
        "pkg/deploy.py:_alert_here:monitor.alert"]


def test_prose_lifted_by_a_migration_is_left_alone():
    files = {"app/migrations/v9.py": "def up(row):\n    return row[\"note\"].startswith(\"superseded by \")\n",
             "app/gate.py": "def held(job):\n    return job[\"note\"].startswith(\"superseded by \")\n"}
    assert prose_sites(files) == ["app/gate.py:held:note", "app/migrations/v9.py:up:note"]
    got = detectors.prose_match(detectors.parse(files), fields=_PROSE, migrations=("app/migrations/",))
    assert sorted(h.site for h in got) == ["app/gate.py:held:note"]


_LANES = ("OPEN = (\"queued\", \"held\")\n"
          "MOVES = {\"hold\": (OPEN, \"held\"), \"start\": ((\"queued\",), \"running\"), \"drop\": (OPEN, \"dropped\")}\n")


def test_an_action_table_whose_from_states_is_a_constant_reads_the_constant_s_states():
    tree = detectors.parse({"app/lanes.py": _LANES})["app/lanes.py"]
    (_, _, _, table, _), = detectors._found_tables({"app/lanes.py": tree})
    assert table == {"queued": {"held", "running", "dropped"}, "held": {"held", "dropped"}, "running": set(),
                     "dropped": set()}
    report = "def live(rows):\n    return [r for r in rows if r.state == \"running\"]\n"
    got = sites(detectors.terminal_without_reader, {"app/lanes.py": _LANES, "app/report.py": report})
    assert got == ["app/lanes.py:MOVES:dropped"]


def test_effect_reachable_without_a_live_predicate_finds_no_ungated_call():
    files = {"app/notify.py": ("import subprocess\ndef desktop(msg):\n    print(msg)\n"
                               "def morning(msg):\n    desktop(msg)\n"
                               "def issues():\n    subprocess.run([\"gh\", \"issue\"])\n")}
    assert effect_sites(files, effects=("desktop",)) == ["app/notify.py:issues:binary:gh"]
    assert "app/notify.py:morning:desktop" in effect_sites(files, effects=("desktop",), live=("LIVE",))


_MODELS = ("from sqlalchemy import Column, ForeignKey, Integer, String, Table\n"
           "from sqlalchemy.orm import Mapped, mapped_column, relationship\n"
           "class Job(Base):\n    __tablename__ = \"job\"\n"
           "    id: Mapped[int] = mapped_column(primary_key=True)\n"
           "    shop_id: Mapped[int] = mapped_column(ForeignKey(\"shop.id\"))\n"
           "    argv: Mapped[str]\n    note = Column(String)\n    shop: Mapped[\"Shop\"] = relationship()\n"
           "class Shop(Base):\n    __tablename__ = \"shop\"\n    id = Column(Integer, primary_key=True)\n"
           "    name: Mapped[str] = mapped_column(String)\n"
           "    def label(self):\n        return self.name.title()\n"
           "tags = Table(\"tag\", Base.metadata, Column(\"id\", Integer, primary_key=True), Column(\"word\", String))\n")
_ALEMBIC = ("import sqlalchemy as sa\nfrom alembic import op\n"
            "def upgrade():\n"
            "    op.create_table(\"lap\", sa.Column(\"id\", sa.Integer, primary_key=True), sa.Column(\"secs\", sa.Float))\n"
            "    op.add_column(\"job\", sa.Column(\"seen\", sa.Boolean))\n"
            "    op.add_column(\"job\", sa.Column(\"gone\", sa.Boolean))\n"
            "    op.drop_column(\"job\", \"gone\")\n"
            "    op.execute(\"UPDATE job SET seen = false\")\n")
_ORM_USE = ("from sqlalchemy import select, update\nfrom app.models import Job, Shop\n"
            "def add(s, shop, argv):\n    s.add(Job(shop_id=shop, argv=argv))\n"
            "def mine(s, shop):\n    return s.scalars(select(Job).where(Job.shop_id == shop)).all()\n"
            "def show(job):\n    return job.argv, job.seen\n"
            "def rename(s, i, n):\n    s.execute(update(Shop).where(Shop.id == i).values(name=n))\n"
            "def mark(job):\n    job.seen = True\n"
            "def time(s, secs):\n    s.execute(\"INSERT INTO lap (secs) VALUES (?)\", (secs,))\n"
            "def laps(s):\n    return s.execute(\"SELECT secs FROM lap\").all()\n"
            "def words(s):\n    return s.execute(\"SELECT word FROM tag\").all()\n")


def test_a_sqlalchemy_and_alembic_schema_is_read_and_its_orm_use_pairs_columns():
    files = {"app/models.py": _MODELS, "app/migrations/v1.py": _ALEMBIC, "app/q.py": _ORM_USE}
    cfg = {"schema": ("app/models.py", "app/migrations/"), "migrations": ("app/migrations/",)}
    assert column_sites(files, **cfg) == ["app/models.py:job.note:no-reader", "app/models.py:job.note:no-writer",
                                          "app/models.py:tag.word:no-writer"]
    blind = {**files, "app/q.py": "def show(job):\n    return job.argv\n"}
    assert "app/migrations/v1.py:job.seen:no-reader" in column_sites(blind, **cfg)


def test_a_read_in_a_schema_file_counts_and_a_migration_still_does_not():
    files = {"app/models.py": _MODELS, "app/q.py": _ORM_USE.replace("def rename", "def _rename")}
    cfg = {"schema": ("app/models.py",)}
    assert "app/models.py:shop.name:no-reader" not in column_sites(files, **cfg)
    assert "app/models.py:shop.name:no-reader" in column_sites(files, schema=(), migrations=("app/models.py",))
    ddl = {"app/schema.py": ("SCHEMA = \"CREATE TABLE v (id INTEGER PRIMARY KEY, n INTEGER)\"\n"
                             "def version(c):\n    return c.execute(\"SELECT n FROM v\").fetchone()\n"),
           "app/w.py": "def bump(c):\n    c.execute(\"UPDATE v SET n = n + 1\")\n"}
    assert column_sites(ddl, schema=("app/schema.py",), migrations=()) == []
    assert column_sites(ddl, migrations=("app/schema.py",)) == ["app/schema.py:v.n:no-reader"]


def test_an_orm_query_on_a_scoped_model_with_no_scope_is_found():
    use = ("from sqlalchemy import select, delete\nfrom app.models import Job, Shop\n"
           "def mine(s, shop):\n    return s.scalars(select(Job).where(Job.shop_id == shop)).all()\n"
           "def one(s, i):\n    return s.scalars(select(Job).where(Job.id == i)).one()\n"
           "def every(s):\n    return s.scalars(select(Job.argv)).all()\n"
           "def purge(s):\n    s.execute(delete(Job).where(Job.argv == \"\"))\n"
           "def legacy(s):\n    return s.query(Job).filter_by(argv=\"x\").all()\n"
           "def shops(s):\n    return s.scalars(select(Shop)).all()\n")
    got = sorted({h.site for h in detectors.unscoped_query(detectors.parse({"app/models.py": _MODELS, "app/q.py": use}),
                                                           columns=("shop_id",))})
    assert got == ["app/q.py:every:job", "app/q.py:legacy:job", "app/q.py:purge:job"]


_TEAMS = ("from sqlalchemy import ForeignKey, func\nfrom sqlalchemy.orm import Mapped, mapped_column, relationship\n"
          "class Club(Base):\n    __tablename__ = \"club\"\n    id: Mapped[int] = mapped_column(primary_key=True)\n"
          "    rosters: Mapped[list[\"Roster\"]] = relationship(back_populates=\"club\", order_by=\"Roster.rank\")\n"
          "class Roster(Base):\n    __tablename__ = \"roster\"\n    id: Mapped[int] = mapped_column(primary_key=True)\n"
          "    club_id: Mapped[int] = mapped_column(ForeignKey(\"club.id\"))\n"
          "    code: Mapped[str] = mapped_column(unique=True)\n    label: Mapped[str]\n    rank: Mapped[int]\n"
          "    made: Mapped[str] = mapped_column(server_default=func.now())\n"
          "    club: Mapped[Club] = relationship(back_populates=\"rosters\")\n")
_TEAM_USE = ("from sqlalchemy import select\nfrom app.models import Club, Roster\n"
             "def add(s, club, **cols):\n    club.rosters.append(Roster(**cols))\n"
             "def seed(s, club):\n    add(s, club, code=\"x\", label=\"a\", rank=1)\n"
             "def show(r):\n    return r.label, r.club.id, r.made\n"
             "def by_code(s, code):\n    return s.scalars(select(Roster).where(Roster.code == code)).one()\n"
             "def of(s, club):\n    return s.scalars(select(Roster).where(Roster.club == club)).all()\n"
             "def by_label(s, label):\n    return s.scalars(select(Roster).where(Roster.label == label)).all()\n")


def test_relationships_defaults_kwargs_and_order_by_strings_pair_orm_columns():
    files = {"app/models.py": _TEAMS, "app/q.py": _TEAM_USE}
    assert column_sites(files, schema=("app/models.py",), migrations=()) == []
    unread = {**files, "app/models.py": _TEAMS.replace(", order_by=\"Roster.rank\"", "")}
    assert column_sites(unread, schema=("app/models.py",), migrations=()) == ["app/models.py:roster.rank:no-reader"]


def test_an_orm_lookup_by_a_unique_column_or_through_the_scope_relationship_is_scoped():
    got = sorted({h.site for h in detectors.unscoped_query(
        detectors.parse({"app/models.py": _TEAMS, "app/q.py": _TEAM_USE}), columns=("club_id",))})
    assert got == ["app/q.py:by_label:roster"]


def test_tree_helpers_are_built_once_per_node_however_many_detectors_ask():
    files = {"app/js.py": _STATES, "app/wq.py": _WRITER, "app/report.py": _REPORT, "app/models.py": _MODELS,
             "app/q.py": _ORM_USE, "app/booking.py": _BOOKING}
    trees = detectors.parse(files)
    index = {name: {"detector": name} for name in detectors.DETECTORS}
    first = detectors.run(trees, index)
    built = dict(detectors.BUILDS)
    assert {"_walk", "_scopes", "_own", "_innermost"} <= {k for k, v in built.items() if v}
    assert detectors.run(trees, index) == first
    assert dict(detectors.BUILDS) == built


def _calls(fn, *args, **kw):
    import sys
    count = [0]

    def tally(frame, event, arg):
        if event == "call":
            count[0] += 1
    sys.setprofile(tally)
    try:
        got = fn(*args, **kw)
    finally:
        sys.setprofile(None)
    return got, count[0]


def _lanes(n):
    # n modules, each with its own move table, a writer of its terminal state and a reader of its neighbour's.
    files = {}
    for i in range(n):
        files[f"app/m{i}.py"] = (
            f"RUN = \"{n}\"\nOPEN{i} = \"open{i}\"\nDONE{i} = \"done{i}\"\n"
            f"MOVES = {{OPEN{i}: {{DONE{i}}}, DONE{i}: set()}}\n"
            f"def finish(conn, k):\n    conn.execute(\"UPDATE t SET s = 'done{i}' WHERE id = ?\", (k,))\n"
            f"    return [r for r in conn.rows() if r.s == 'done{i + 1}' or r.kind in ('a', 'b')]\n")
    return detectors.parse(files)


def test_terminal_without_reader_costs_calls_linear_in_the_tree_however_many_tables_it_holds():
    small, big = _lanes(30), _lanes(60)
    hits_small, cost_small = _calls(detectors.terminal_without_reader, small)
    hits_big, cost_big = _calls(detectors.terminal_without_reader, big)
    assert [h.site for h in hits_small] == [h.site for h in hits_big] == ["app/m0.py:MOVES:done0"]
    assert cost_big <= 2.5 * cost_small, (cost_small, cost_big)
