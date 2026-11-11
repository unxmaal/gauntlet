"""The HCL front end: each detector fires on its positive fixture and stays quiet on the negative beside it."""

import json
import subprocess

import pytest

from gauntlet import cli, hcl

pytestmark = pytest.mark.skipif(not hcl.available(), reason="python-hcl2 is not installed: gauntlet[hcl]")

NET = "10.20" + ".0.0/16"
BACKEND_EMPTY = 'terraform {\n  backend "s3" {}\n}\nvariable "env" {}\n'
TIED = ('resource "terraform_data" "env" {\n  lifecycle {\n    precondition {\n'
        '      condition     = var.env == terraform.workspace\n      error_message = "wrong state"\n    }\n  }\n}\n')
ENVS = {"infra/staging.tfvars": 'env = "staging"\n', "infra/production.tfvars": 'env = "production"\n'}


def scan(tmp_path, files, rule=None, git=False, **config):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    fe = hcl.HclFrontEnd()
    if git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        rels = fe.discover(tmp_path)
    else:
        rels = sorted(files)
    index = {rule: {"detector": rule}} if rule else None
    cfg = {rule: config} if rule and config else None
    return [f for f in fe.scan(tmp_path, rels, index, cfg) if rule is None or f.rule == rule]


def sites(found):
    return sorted(f.site for f in found)


def test_duplicated_literal_fires_on_a_cidr_and_a_region_written_twice(tmp_path):
    found = scan(tmp_path, {
        "net/main.tf": f'resource "aws_vpc" "main" {{\n  cidr_block = "{NET}"\n}}\n',
        "app/main.tf": f'locals {{\n  vpc_cidr = "{NET}"\n  account = "123456789012"\n}}\n',
        "app/variables.tf": 'variable "region" {\n  default = "us-west-2"\n}\n',
        "app/staging.tfvars": 'region = "us-west-2"\naccount = "123456789012"\n',
    }, "hcl_duplicated_literal")
    assert sites(found) == sorted([f"net/main.tf:{NET}", f"app/main.tf:{NET}", "app/variables.tf:us-west-2",
                                   "app/staging.tfvars:us-west-2", "app/main.tf:123456789012",
                                   "app/staging.tfvars:123456789012"])
    assert next(f for f in found if f.site == f"net/main.tf:{NET}").line == 2


def test_duplicated_literal_ignores_one_home_open_world_and_service_principals(tmp_path):
    found = scan(tmp_path, {
        "net/main.tf": (f'resource "aws_vpc" "main" {{\n  cidr_block = "{NET}"\n}}\n'
                        'locals {\n  any = "0.0.0.0/0"\n  svc = "ec2.amazonaws.com"\n  page = "index.html"\n}\n'),
        "app/main.tf": ('locals {\n  any = "0.0.0.0/0"\n  svc = "ec2.amazonaws.com"\n  page = "index.html"\n'
                        '  region = var.region\n}\n'),
    }, "hcl_duplicated_literal")
    assert found == []


def test_env_two_inputs_fires_on_a_partial_backend_with_two_var_files(tmp_path):
    found = scan(tmp_path, {"infra/main.tf": BACKEND_EMPTY, **ENVS}, "hcl_env_two_inputs")
    assert sites(found) == ["infra/main.tf:backend.s3"] and found[0].line == 2
    assert "staging.tfvars" in found[0].why


def test_env_two_inputs_is_quiet_when_a_precondition_ties_them_or_there_is_one_env(tmp_path):
    assert scan(tmp_path, {"infra/main.tf": BACKEND_EMPTY + TIED, **ENVS}, "hcl_env_two_inputs") == []
    assert scan(tmp_path / "one", {"infra/main.tf": BACKEND_EMPTY, "infra/staging.tfvars": 'env = "s"\n'},
                "hcl_env_two_inputs") == []


FULL = 'terraform {{\n  backend "s3" {{\n    bucket = "acme-state"\n    key = "{key}"\n  }}\n}}\n'


def test_shared_backend_fires_on_two_roots_with_one_state_key(tmp_path):
    found = scan(tmp_path, {"a/main.tf": FULL.format(key="app.tfstate"), "b/main.tf": FULL.format(key="app.tfstate")},
                 "hcl_shared_backend")
    assert sites(found) == ["a/main.tf:backend.s3", "b/main.tf:backend.s3"]


def test_shared_backend_is_quiet_on_distinct_keys(tmp_path):
    assert scan(tmp_path, {"a/main.tf": FULL.format(key="a.tfstate"), "b/main.tf": FULL.format(key="b.tfstate")},
                "hcl_shared_backend") == []


SHARED_FILE = {"a/main.tf": BACKEND_EMPTY, "b/main.tf": BACKEND_EMPTY,
               "backend.tfbackend": 'bucket = "acme-state"\nregion = "us-east-1"\n'}


def test_shared_backend_fires_on_one_config_file_split_only_by_a_cli_key(tmp_path):
    found = scan(tmp_path, {**SHARED_FILE, "scripts/init.sh": (
        "terraform -chdir=a init -backend-config=../backend.tfbackend \\\n  -backend-config=\"key=a.tfstate\"\n"
        "terraform -chdir=b init -backend-config=../backend.tfbackend -backend-config=key=b.tfstate\n")},
        "hcl_shared_backend")
    assert sites(found) == ["backend.tfbackend:backend-config"]
    assert "scripts/init.sh:1" in found[0].why and "names no key" in found[0].why


def test_shared_backend_is_quiet_on_one_config_file_per_root(tmp_path):
    assert scan(tmp_path, {**SHARED_FILE, "a/a.tfbackend": 'key = "a"\n', "b/b.tfbackend": 'key = "b"\n',
                           "scripts/init.sh": ("terraform -chdir=a init -backend-config=a.tfbackend\n"
                                               "terraform -chdir=b init -backend-config=b.tfbackend\n")},
                "hcl_shared_backend") == []


DB = ('resource "aws_db_instance" "main" {{\n  identifier = "acme-db"\n  skip_final_snapshot = {skip}\n{life}}}\n'
      'variable "skip" {{\n  default = false\n}}\n')
LIFE = "  lifecycle {\n    prevent_destroy = true\n  }\n"


def test_stateful_unprotected_fires_on_no_guard_and_a_snapshot_skip_a_var_file_can_set(tmp_path):
    found = scan(tmp_path, {"main.tf": DB.format(skip="var.skip", life=""), "prod.tfvars": "skip = true\n",
                            "vault.tf": 'resource "acme_vault" "v" {\n  size = 1\n}\n'},
                 "hcl_stateful_unprotected", stateful=("acme_vault",))
    assert sites(found) == ["main.tf:aws_db_instance.main", "main.tf:aws_db_instance.main.skip_final_snapshot",
                            "vault.tf:acme_vault.v"]


def test_stateful_unprotected_is_quiet_with_prevent_destroy_and_no_reachable_skip(tmp_path):
    assert scan(tmp_path, {"main.tf": DB.format(skip="var.skip", life=LIFE),
                           "other.tf": 'resource "aws_instance" "web" {\n  ami = "ami-1"\n}\n'},
                "hcl_stateful_unprotected") == []
    found = scan(tmp_path / "lit", {"main.tf": DB.format(skip="true", life=LIFE)}, "hcl_stateful_unprotected")
    assert sites(found) == ["main.tf:aws_db_instance.main.skip_final_snapshot"]


def test_count_by_position_fires_on_count_index_into_a_data_list(tmp_path):
    found = scan(tmp_path, {"main.tf": (
        'resource "aws_route_table_association" "a" {\n  count = length(data.aws_subnets.all.ids)\n'
        '  subnet_id = data.aws_subnets.all.ids[count.index]\n}\n'
        'resource "aws_eip" "b" {\n  count = 2\n  instance = element(data.aws_instances.web.ids, count.index)\n}\n')},
        "hcl_count_by_position")
    assert sites(found) == ["main.tf:aws_eip.b", "main.tf:aws_route_table_association.a"]
    assert found[1].line == 1


def test_count_by_position_is_quiet_on_for_each_and_plain_counts(tmp_path):
    assert scan(tmp_path, {"main.tf": (
        'resource "aws_route_table_association" "a" {\n  for_each = toset(data.aws_subnets.all.ids)\n'
        '  subnet_id = each.value\n}\n'
        'resource "aws_eip" "b" {\n  count = var.n\n  tags = { Name = "eip-${count.index}" }\n}\n')},
        "hcl_count_by_position") == []


ROOT = 'terraform {\n  backend "s3" {}\n}\n'


def test_lock_missing_fires_on_a_missing_and_a_git_ignored_lock(tmp_path):
    found = scan(tmp_path, {"infra/main.tf": ROOT, "edge/main.tf": ROOT, "edge/.terraform.lock.hcl": "# lock\n",
                            ".gitignore": ".terraform.lock.hcl\n"}, "hcl_lock_missing", git=True)
    assert sites(found) == ["edge/main.tf:.terraform.lock.hcl", "infra/main.tf:.terraform.lock.hcl"]
    assert "git-ignored" in found[0].why and "missing" in found[1].why


def test_lock_missing_is_quiet_on_a_tracked_lock_and_a_child_module(tmp_path):
    assert scan(tmp_path, {"infra/main.tf": ROOT + 'module "db" {\n  source = "../modules/db"\n}\n',
                           "infra/.terraform.lock.hcl": "# lock\n", "modules/db/main.tf": 'variable "x" {}\n',
                           "modules/db/example.tfvars": 'x = 1\n'}, "hcl_lock_missing", git=True) == []


def test_constant_assert_fires_when_operands_are_only_constants_or_variables(tmp_path):
    found = scan(tmp_path, {"main.tf": 'variable "env" {}\n', "tests/a.tftest.hcl": (
        'run "plan" {\n  variables {\n    env = "staging"\n  }\n'
        '  assert {\n    condition = var.env == "staging"\n    error_message = "x"\n  }\n'
        '  assert {\n    condition = length(["a"]) == 1\n    error_message = "x"\n  }\n'
        '  assert {\n    condition = var.env != "named in data.aws_x.y"\n    error_message = "x"\n  }\n}\n')},
        "hcl_constant_assert")
    assert sites(found) == ["tests/a.tftest.hcl:run.plan.assert[0]", "tests/a.tftest.hcl:run.plan.assert[1]",
                            "tests/a.tftest.hcl:run.plan.assert[2]"]


def test_constant_assert_is_quiet_when_an_operand_is_planned(tmp_path):
    assert scan(tmp_path, {"main.tf": 'variable "env" {}\n', "tests/a.tftest.hcl": (
        'run "plan" {\n  assert {\n    condition = aws_s3_bucket.b.bucket == "acme-${var.env}"\n'
        '    error_message = "x"\n  }\n'
        '  assert {\n    condition = output.name == var.env\n    error_message = "x"\n  }\n}\n')},
        "hcl_constant_assert") == []


TEST_STAGING = ('run "staging" {\n  variables {\n    env = "staging"\n  }\n'
                '  assert {\n    condition = aws_s3_bucket.b.bucket != ""\n    error_message = "x"\n  }\n}\n')


def test_test_skips_tfvars_fires_on_a_named_env_its_file_never_feeds(tmp_path):
    found = scan(tmp_path, {"infra/main.tf": BACKEND_EMPTY, **ENVS, "infra/tests/env.tftest.hcl": TEST_STAGING},
                 "hcl_test_skips_tfvars")
    assert sites(found) == ["infra/tests/env.tftest.hcl:staging.tfvars"]


def test_test_skips_tfvars_is_quiet_when_a_driver_runs_the_test_with_the_file(tmp_path):
    assert scan(tmp_path, {"infra/main.tf": BACKEND_EMPTY, **ENVS, "infra/tests/env.tftest.hcl": TEST_STAGING,
                           "Makefile": "test:\n\tterraform -chdir=infra test -var-file=staging.tfvars\n"},
                "hcl_test_skips_tfvars") == []


ZONE = 'resource "aws_route53_zone" "main" {\n  name = "example.com"\n}\n'
NS_OUT = 'output "name_servers" {\n  value = aws_route53_zone.main.name_servers\n}\n'
NS_CHECK = ('check "delegation" {\n  data "dns_ns_record_set" "d" {\n    host = "example.com"\n  }\n'
            '  assert {\n    condition = sort(data.dns_ns_record_set.d.nameservers) == '
            'sort(aws_route53_zone.main.name_servers)\n    error_message = "delegation drifted"\n  }\n}\n')


def test_unchecked_external_fires_on_name_servers_output_with_no_check(tmp_path):
    found = scan(tmp_path, {"dns.tf": ZONE + NS_OUT}, "hcl_unchecked_external")
    assert sites(found) == ["dns.tf:output.name_servers"] and found[0].line == 4


def test_unchecked_external_is_quiet_when_a_check_compares_the_copy(tmp_path):
    assert scan(tmp_path, {"dns.tf": ZONE + NS_OUT + NS_CHECK}, "hcl_unchecked_external") == []


def test_order_in_prose_fires_on_a_description_and_a_comment(tmp_path):
    found = scan(tmp_path, {"dns.tf": (
        'output "ns" {\n  value = 1\n  description = "Copy these to the registrar after the first apply"\n}\n'
        '# Run the bootstrap module before applying this one.\nresource "aws_s3_bucket" "logs" {\n}\n')},
        "hcl_order_in_prose")
    assert sites(found) == ["dns.tf:comment.aws_s3_bucket.logs", "dns.tf:output.ns.description"]


def test_order_in_prose_is_quiet_on_plain_descriptions(tmp_path):
    assert scan(tmp_path, {"dns.tf": (
        'output "ns" {\n  value = 1\n  description = "The zone\'s name servers"\n}\n'
        '# Creates the log bucket.\nresource "aws_s3_bucket" "logs" {\n}\n')}, "hcl_order_in_prose") == []


def test_an_unparseable_file_is_a_finding_not_a_silence(tmp_path):
    found = scan(tmp_path, {"bad.tf": 'resource "a" "b" {\n', "ok.tf": ZONE})
    assert [f.site for f in found if f.rule == "hcl_unparsed"] == ["bad.tf:parse"]


def test_the_index_names_the_class_and_defaults_apply_without_one(tmp_path):
    files = {"dns.tf": ZONE + NS_OUT}
    (tmp_path / "dns.tf").write_text(files["dns.tf"], encoding="utf-8")
    fe = hcl.HclFrontEnd()
    got = fe.scan(tmp_path, ["dns.tf"], {"mine": {"detector": "hcl_unchecked_external"}})
    assert [(f.cls, f.rule) for f in got] == [("mine", "hcl_unchecked_external")]
    default = {f.cls for f in fe.scan(tmp_path, ["dns.tf"]) if f.rule == "hcl_unchecked_external"}
    assert default == {"external-state-with-no-assertion-on-it"}


def test_every_default_class_is_in_the_corpus():
    from gauntlet import skill
    ids = {c["id"] for c in skill.read_skill()}
    assert {cls for _, cls in hcl.DETECTORS.values()} - ids == {"hcl-unparsed"}


def test_cli_scan_prints_sarif_for_the_hcl_front_end(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "dns.tf").write_text(ZONE + NS_OUT, encoding="utf-8")
    assert cli.main(["scan", str(tmp_path), "--frontend", "hcl", "--format", "sarif"]) == 0
    doc = json.loads(capsys.readouterr().out)
    rules = {r["ruleId"] for r in doc["runs"][0]["results"]}
    assert "hcl_unchecked_external" in rules and "hcl_stateful_unprotected" in rules
    assert cli.main(["scan", str(tmp_path), "--frontend", "hcl", "--fail"]) == 1


def test_without_python_hcl2_the_front_end_says_how_to_install_it(monkeypatch, tmp_path):
    monkeypatch.setattr(hcl, "available", lambda: False)
    (tmp_path / "a.tf").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"gauntlet\[hcl\]"):
        hcl.Repo(tmp_path, ["a.tf"])
