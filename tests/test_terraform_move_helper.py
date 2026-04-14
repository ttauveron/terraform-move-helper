import importlib.util
import json
import shlex
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "terraform-move-helper.py"

spec = importlib.util.spec_from_file_location("terraform_move_helper", MODULE_PATH)
terraform_move_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terraform_move_helper)


def resource_change(address, resource_type, actions, before=None, after=None):
    return {
        "address": address,
        "type": resource_type,
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
        },
    }


def write_plan(tmp_path, resource_changes):
    plan_path = tmp_path / "tfplan.json"
    plan_path.write_text(json.dumps({"resource_changes": resource_changes}))
    return plan_path


def output_commands(output_path):
    return output_path.read_text().splitlines()


def parsed_command_set(output_path):
    return {
        tuple(shlex.split(command)) for command in output_commands(output_path)
    }


def test_flatten_dict_keeps_nested_keys_and_stringifies_values():
    result = terraform_move_helper.flatten_dict(
        {
            "id": "bucket-1",
            "tags": {
                "env": "prod",
                "enabled": True,
            },
        }
    )

    assert set(result) == {
        ("id", "bucket-1"),
        ("tags.env", "prod"),
        ("tags.enabled", "True"),
    }


def test_filter_resources_by_action_requires_exact_single_action():
    changes = [
        resource_change("aws_s3_bucket.deleted", "aws_s3_bucket", ["delete"]),
        resource_change("aws_s3_bucket.replaced", "aws_s3_bucket", ["delete", "create"]),
        resource_change("aws_s3_bucket.created", "aws_s3_bucket", ["create"]),
    ]

    result = terraform_move_helper.filter_resources_by_action(changes, "delete")

    assert [resource["address"] for resource in result] == ["aws_s3_bucket.deleted"]


def test_calculate_match_scores_prefers_matching_resource_state():
    destroyed = [
        resource_change(
            "aws_s3_bucket.old",
            "aws_s3_bucket",
            ["delete"],
            before={"bucket": "prod-assets", "acl": "private"},
        )
    ]
    created = [
        resource_change(
            "module.storage.aws_s3_bucket.assets",
            "aws_s3_bucket",
            ["create"],
            after={"bucket": "prod-assets", "acl": "private"},
        ),
        resource_change(
            "module.storage.aws_s3_bucket.logs",
            "aws_s3_bucket",
            ["create"],
            after={"bucket": "prod-logs", "acl": "private"},
        ),
    ]

    scores = terraform_move_helper.calculate_match_scores(destroyed, created)

    assets_score = scores["aws_s3_bucket.old"][
        "module.storage.aws_s3_bucket.assets"
    ]["aggregated"]
    logs_score = scores["aws_s3_bucket.old"][
        "module.storage.aws_s3_bucket.logs"
    ]["aggregated"]

    assert assets_score > logs_score


def test_main_writes_terraform_state_mv_command(tmp_path, capsys):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                "aws_s3_bucket.old",
                "aws_s3_bucket",
                ["delete"],
                before={"bucket": "prod-assets", "acl": "private"},
            ),
            resource_change(
                "module.storage.aws_s3_bucket.assets",
                "aws_s3_bucket",
                ["create"],
                after={"bucket": "prod-assets", "acl": "private"},
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert shlex.split(output_path.read_text()) == [
        "terraform",
        "state",
        "mv",
        "aws_s3_bucket.old",
        "module.storage.aws_s3_bucket.assets",
    ]
    assert "Terraform move commands have been written" in capsys.readouterr().out


def test_main_rejects_destroyed_resource_without_created_match(tmp_path, capsys):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                "aws_s3_bucket.old",
                "aws_s3_bucket",
                ["delete"],
                before={"bucket": "prod-assets"},
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    with pytest.raises(SystemExit) as exc_info:
        terraform_move_helper.main(str(plan_path), str(output_path))

    assert exc_info.value.code == 1
    assert not output_path.exists()
    output = capsys.readouterr().out
    assert "Error: Mismatch for resource type 'aws_s3_bucket'" in output
    assert "  Destroyed: 1 resource(s)" in output
    assert "  Created: 0 resource(s)" in output
    assert "Cannot proceed with matching because the numbers don't match." in output


def test_main_rejects_mismatch_when_destroyed_count_exceeds_created_count(
    tmp_path,
    capsys,
):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                "aws_s3_bucket.assets_old",
                "aws_s3_bucket",
                ["delete"],
                before={"bucket": "prod-assets"},
            ),
            resource_change(
                "aws_s3_bucket.logs_old",
                "aws_s3_bucket",
                ["delete"],
                before={"bucket": "prod-logs"},
            ),
            resource_change(
                "module.storage.aws_s3_bucket.assets",
                "aws_s3_bucket",
                ["create"],
                after={"bucket": "prod-assets"},
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    with pytest.raises(SystemExit) as exc_info:
        terraform_move_helper.main(str(plan_path), str(output_path))

    assert exc_info.value.code == 1
    assert not output_path.exists()
    output = capsys.readouterr().out
    assert "Error: Mismatch for resource type 'aws_s3_bucket'" in output
    assert "  Destroyed: 2 resource(s)" in output
    assert "  Created: 1 resource(s)" in output
    assert "Cannot proceed with matching because the numbers don't match." in output


def test_main_rejects_created_resource_type_without_destroyed_counterpart(
    tmp_path,
    capsys,
):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                "aws_s3_bucket.old",
                "aws_s3_bucket",
                ["delete"],
                before={"bucket": "prod-assets"},
            ),
            resource_change(
                "module.storage.aws_s3_bucket.assets",
                "aws_s3_bucket",
                ["create"],
                after={"bucket": "prod-assets"},
            ),
            resource_change(
                "aws_iam_role.new",
                "aws_iam_role",
                ["create"],
                after={"name": "new-role"},
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    with pytest.raises(SystemExit) as exc_info:
        terraform_move_helper.main(str(plan_path), str(output_path))

    assert exc_info.value.code == 1
    assert not output_path.exists()
    output = capsys.readouterr().out
    assert "Error: Mismatch for resource type 'aws_iam_role'" in output
    assert "  Destroyed: 0 resource(s)" in output
    assert "  Created: 1 resource(s)" in output
    assert "Cannot proceed with matching because the numbers don't match." in output


def test_main_rejects_multiple_resource_type_mismatches(tmp_path, capsys):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                'module.csm_secret["old-a"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={"secret_id": "old-a"},
            ),
            resource_change(
                'module.csm_secret["old-b"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={"secret_id": "old-b"},
            ),
            resource_change(
                'module.csm_secret["new-a"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["create"],
                after={"secret_id": "old-a"},
            ),
            resource_change(
                'module.gcs["old-bucket"].google_storage_bucket_iam_binding.readers',
                "google_storage_bucket_iam_binding",
                ["delete"],
                before={"bucket": "old-bucket", "role": "roles/storage.objectViewer"},
            ),
            resource_change(
                'module.gcs["new-bucket"].google_storage_bucket_iam_binding.readers',
                "google_storage_bucket_iam_binding",
                ["create"],
                after={"bucket": "old-bucket", "role": "roles/storage.objectViewer"},
            ),
            resource_change(
                'module.gcs["new-bucket"].google_storage_bucket_iam_binding.writers',
                "google_storage_bucket_iam_binding",
                ["create"],
                after={"bucket": "old-bucket", "role": "roles/storage.objectCreator"},
            ),
            resource_change(
                'module.gcs["old-bucket"].random_id.name_suffix[0]',
                "random_id",
                ["delete"],
                before={"byte_length": 4, "keepers": {"name": "old-bucket"}},
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    with pytest.raises(SystemExit) as exc_info:
        terraform_move_helper.main(str(plan_path), str(output_path))

    assert exc_info.value.code == 1
    assert not output_path.exists()
    output = capsys.readouterr().out
    assert "Error: Mismatch for resource type 'google_secret_manager_secret'" in output
    assert "  Destroyed: 2 resource(s)" in output
    assert "  Created: 1 resource(s)" in output
    assert (
        "Error: Mismatch for resource type 'google_storage_bucket_iam_binding'"
        in output
    )
    assert "  Destroyed: 1 resource(s)" in output
    assert "  Created: 2 resource(s)" in output
    assert "Error: Mismatch for resource type 'random_id'" in output
    assert "  Destroyed: 1 resource(s)" in output
    assert "  Created: 0 resource(s)" in output
    assert "Cannot proceed with matching because the numbers don't match." in output


def test_main_matches_similar_for_each_addresses_with_square_brackets(tmp_path):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                'module.files["test1"].local_file.default',
                "local_file",
                ["delete"],
                before={
                    "filename": "/tmp/test1.txt",
                    "content": "content-for-test-1",
                    "file_permission": "0644",
                },
            ),
            resource_change(
                'module.files["test2"].local_file.default',
                "local_file",
                ["delete"],
                before={
                    "filename": "/tmp/test2.txt",
                    "content": "content-for-test-2",
                    "file_permission": "0600",
                },
            ),
            resource_change(
                'module.files["test1-aaa"].local_file.default',
                "local_file",
                ["create"],
                after={
                    "filename": "/tmp/test1.txt",
                    "content": "content-for-test-1",
                    "file_permission": "0644",
                },
            ),
            resource_change(
                'module.files["test3"].local_file.default',
                "local_file",
                ["create"],
                after={
                    "filename": "/tmp/test2.txt",
                    "content": "content-for-test-2",
                    "file_permission": "0600",
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert parsed_command_set(output_path) == {
        (
            "terraform",
            "state",
            "mv",
            'module.files["test1"].local_file.default',
            'module.files["test1-aaa"].local_file.default',
        ),
        (
            "terraform",
            "state",
            "mv",
            'module.files["test2"].local_file.default',
            'module.files["test3"].local_file.default',
        ),
    }


def test_main_matches_secrets_when_csm_app_prefix_is_added(tmp_path):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                'module.csm_secret["auth-tokensecret"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={
                    "secret_id": "auth-tokensecret",
                    "replication": {"auto": True},
                    "labels": {"owner": "csm"},
                },
            ),
            resource_change(
                'module.csm_secret["cv-fapi-mysql-user"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={
                    "secret_id": "cv-fapi-mysql-user",
                    "replication": {"auto": True},
                    "labels": {"owner": "csm"},
                },
            ),
            resource_change(
                'module.csm_secret["csm-app-auth-tokensecret"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["create"],
                after={
                    "secret_id": "auth-tokensecret",
                    "replication": {"auto": True},
                    "labels": {"owner": "csm"},
                },
            ),
            resource_change(
                'module.csm_secret["csm-app-cv-fapi-mysql-user"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["create"],
                after={
                    "secret_id": "cv-fapi-mysql-user",
                    "replication": {"auto": True},
                    "labels": {"owner": "csm"},
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert parsed_command_set(output_path) == {
        (
            "terraform",
            "state",
            "mv",
            'module.csm_secret["auth-tokensecret"].google_secret_manager_secret.secret',
            'module.csm_secret["csm-app-auth-tokensecret"].google_secret_manager_secret.secret',
        ),
        (
            "terraform",
            "state",
            "mv",
            'module.csm_secret["cv-fapi-mysql-user"].google_secret_manager_secret.secret',
            'module.csm_secret["csm-app-cv-fapi-mysql-user"].google_secret_manager_secret.secret',
        ),
    }


def test_main_distinguishes_contentful_tokens_with_near_identical_names(tmp_path):
    old_names = [
        "contentful-access-token-cda",
        "contentful-access-token-cma",
        "contentful-access-token-preview",
    ]
    token_kinds = ["cda", "cma", "preview"]
    changes = []

    for old_name, token_kind in zip(old_names, token_kinds):
        changes.append(
            resource_change(
                f'module.csm_secret["{old_name}"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={
                    "secret_id": old_name,
                    "labels": {"token_kind": token_kind},
                },
            )
        )

    for old_name, token_kind in zip(old_names, token_kinds):
        changes.append(
            resource_change(
                (
                    f'module.csm_secret["csm-app-{old_name}"].'
                    "google_secret_manager_secret.secret"
                ),
                "google_secret_manager_secret",
                ["create"],
                after={
                    "secret_id": old_name,
                    "labels": {"token_kind": token_kind},
                },
            )
        )

    plan_path = write_plan(tmp_path, changes)
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert parsed_command_set(output_path) == {
        (
            "terraform",
            "state",
            "mv",
            f'module.csm_secret["{old_name}"].google_secret_manager_secret.secret',
            (
                f'module.csm_secret["csm-app-{old_name}"].'
                "google_secret_manager_secret.secret"
            ),
        )
        for old_name in old_names
    }


def test_main_distinguishes_tmp_suffix_resources(tmp_path):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                'module.csm_secret["webportal-db-password"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={
                    "secret_id": "webportal-db-password",
                    "labels": {"rotation": "stable"},
                },
            ),
            resource_change(
                'module.csm_secret["webportal-db-password-tmp"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["delete"],
                before={
                    "secret_id": "webportal-db-password-tmp",
                    "labels": {"rotation": "temporary"},
                },
            ),
            resource_change(
                'module.csm_secret["csm-app-webportal-db-password-tmp"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["create"],
                after={
                    "secret_id": "webportal-db-password-tmp",
                    "labels": {"rotation": "temporary"},
                },
            ),
            resource_change(
                'module.csm_secret["csm-app-webportal-db-password"].google_secret_manager_secret.secret',
                "google_secret_manager_secret",
                ["create"],
                after={
                    "secret_id": "webportal-db-password",
                    "labels": {"rotation": "stable"},
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert parsed_command_set(output_path) == {
        (
            "terraform",
            "state",
            "mv",
            'module.csm_secret["webportal-db-password"].google_secret_manager_secret.secret',
            'module.csm_secret["csm-app-webportal-db-password"].google_secret_manager_secret.secret',
        ),
        (
            "terraform",
            "state",
            "mv",
            'module.csm_secret["webportal-db-password-tmp"].google_secret_manager_secret.secret',
            'module.csm_secret["csm-app-webportal-db-password-tmp"].google_secret_manager_secret.secret',
        ),
    }


def test_main_handles_iam_role_keys_with_nested_square_brackets(tmp_path):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                (
                    'module.svc_account["argocd-dev-operation"].'
                    'google_project_iam_member.svc_accounts_prj_role["roles/container.clusterViewer"]'
                ),
                "google_project_iam_member",
                ["delete"],
                before={
                    "project": "adr-vde",
                    "member": "serviceAccount:argocd-dev@adr-vde.iam.gserviceaccount.com",
                    "role": "roles/container.clusterViewer",
                },
            ),
            resource_change(
                (
                    'module.svc_account["driftctl-dev-operation"].'
                    'google_project_iam_member.svc_accounts_prj_role["roles/storage.admin"]'
                ),
                "google_project_iam_member",
                ["delete"],
                before={
                    "project": "adr-vde",
                    "member": "serviceAccount:driftctl-dev@adr-vde.iam.gserviceaccount.com",
                    "role": "roles/storage.admin",
                },
            ),
            resource_change(
                (
                    'module.svc_account["csm-app-argocd-dev-operation"].'
                    'google_project_iam_member.svc_accounts_prj_role["roles/container.clusterViewer"]'
                ),
                "google_project_iam_member",
                ["create"],
                after={
                    "project": "adr-vde",
                    "member": "serviceAccount:argocd-dev@adr-vde.iam.gserviceaccount.com",
                    "role": "roles/container.clusterViewer",
                },
            ),
            resource_change(
                (
                    'module.svc_account["csm-app-driftctl-dev-operation"].'
                    'google_project_iam_member.svc_accounts_prj_role["roles/storage.admin"]'
                ),
                "google_project_iam_member",
                ["create"],
                after={
                    "project": "adr-vde",
                    "member": "serviceAccount:driftctl-dev@adr-vde.iam.gserviceaccount.com",
                    "role": "roles/storage.admin",
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert parsed_command_set(output_path) == {
        (
            "terraform",
            "state",
            "mv",
            (
                'module.svc_account["argocd-dev-operation"].'
                'google_project_iam_member.svc_accounts_prj_role["roles/container.clusterViewer"]'
            ),
            (
                'module.svc_account["csm-app-argocd-dev-operation"].'
                'google_project_iam_member.svc_accounts_prj_role["roles/container.clusterViewer"]'
            ),
        ),
        (
            "terraform",
            "state",
            "mv",
            (
                'module.svc_account["driftctl-dev-operation"].'
                'google_project_iam_member.svc_accounts_prj_role["roles/storage.admin"]'
            ),
            (
                'module.svc_account["csm-app-driftctl-dev-operation"].'
                'google_project_iam_member.svc_accounts_prj_role["roles/storage.admin"]'
            ),
        ),
    }


def test_main_reports_ambiguous_matches_without_generating_commands(
    tmp_path,
    capsys,
):
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                'module.files["old-a"].local_file.default',
                "local_file",
                ["delete"],
                before={
                    "file_permission": "0644",
                    "directory_permission": "0755",
                    "content": "same-template",
                },
            ),
            resource_change(
                'module.files["old-b"].local_file.default',
                "local_file",
                ["delete"],
                before={
                    "file_permission": "0644",
                    "directory_permission": "0755",
                    "content": "same-template",
                },
            ),
            resource_change(
                'module.files["new-a"].local_file.default',
                "local_file",
                ["create"],
                after={
                    "file_permission": "0644",
                    "directory_permission": "0755",
                    "content": "same-template",
                },
            ),
            resource_change(
                'module.files["new-b"].local_file.default',
                "local_file",
                ["create"],
                after={
                    "file_permission": "0644",
                    "directory_permission": "0755",
                    "content": "same-template",
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert output_commands(output_path) == []
    output = capsys.readouterr().out
    assert "Ambiguous Matches:" in output
    assert 'destroyed: module.files["old-a"].local_file.default' in output
    assert 'module.files["new-a"].local_file.default' in output
    assert 'module.files["new-b"].local_file.default' in output


def test_main_handles_nested_modules_and_punctuation_in_for_each_keys(tmp_path):
    source_address = (
        'module.env["prod.eu-west-1"].'
        'module.files["config/app.v1.json"].local_file.default'
    )
    destination_address = (
        'module.env["prod.eu-west-1"].'
        'module.files["config/app-v2.json"].local_file.default'
    )
    plan_path = write_plan(
        tmp_path,
        [
            resource_change(
                source_address,
                "local_file",
                ["delete"],
                before={
                    "filename": "/etc/my-app/config/app.json",
                    "content_sha256": "abc123",
                    "directory_permission": "0755",
                },
            ),
            resource_change(
                destination_address,
                "local_file",
                ["create"],
                after={
                    "filename": "/etc/my-app/config/app.json",
                    "content_sha256": "abc123",
                    "directory_permission": "0755",
                },
            ),
        ],
    )
    output_path = tmp_path / "move_commands.sh"

    terraform_move_helper.main(str(plan_path), str(output_path))

    assert [shlex.split(command) for command in output_commands(output_path)] == [
        ["terraform", "state", "mv", source_address, destination_address]
    ]


def test_build_state_mv_command_shell_quotes_single_quotes_in_addresses():
    source_address = 'module.files["team\'s-file"].local_file.default'
    destination_address = 'module.files["teams-file"].local_file.default'

    command = terraform_move_helper.build_state_mv_command(
        source_address,
        destination_address,
    )

    assert shlex.split(command) == [
        "terraform",
        "state",
        "mv",
        source_address,
        destination_address,
    ]
