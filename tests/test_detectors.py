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


def test_run_hands_a_detector_its_configuration():
    files = {"app/r.py": "from lib import store\ndef f(conn):\n    k = store.put(1)\n"
                         "    conn.execute(\"INSERT INTO refs (k) VALUES (?)\", (k,))\n"}
    index = {"c": {"detector": "commit_then_link"}}
    trees = detectors.parse(files)
    assert detectors.run(trees, index) == {"c": []}
    got = detectors.run(trees, index, config={"commit_then_link": {"committers": ("put",)}})
    assert [h.site for h in got["c"]] == ["app/r.py:f"]
