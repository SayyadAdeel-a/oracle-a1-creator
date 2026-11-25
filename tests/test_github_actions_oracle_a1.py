import unittest
from types import SimpleNamespace
from unittest.mock import patch

import github_actions_oracle_a1 as creator


def settings():
    return creator.Settings(
        user_ocid="user",
        tenancy_ocid="tenancy",
        fingerprint="fingerprint",
        private_key="private-key-content",
        compartment_id="compartment",
        availability_domain="abc:AP-MUMBAI-1-AD-1",
        subnet_id="subnet",
        image_id="image",
        ssh_public_key="ssh-ed25519 public-key",
    )


class Model:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class CreatorTests(unittest.TestCase):
    def test_build_config_uses_mumbai_and_secret_key_content(self):
        config = creator.build_oci_config(settings())

        self.assertEqual(config["region"], "ap-mumbai-1")
        self.assertEqual(config["key_content"], "private-key-content")

    def test_error_reporting_redacts_private_key_and_classifies_auth_errors(self):
        error = SimpleNamespace(
            status=401,
            code="NotAuthenticated",
            message="Invalid key: private-key-content",
        )

        details = creator.safe_error_details(error, "private-key-content")

        self.assertNotIn("private-key-content", details)
        self.assertIn("[REDACTED]", details)
        self.assertEqual(creator.error_exit_code(error), 3)

    def test_load_settings_rejects_non_mumbai_availability_domain(self):
        values = {
            "OCI_USER_OCID": "user",
            "OCI_TENANCY_OCID": "tenancy",
            "OCI_FINGERPRINT": "fingerprint",
            "OCI_PRIVATE_KEY": "private-key",
            "OCI_COMPARTMENT_ID": "compartment",
            "OCI_AVAILABILITY_DOMAIN": "abc:AP-HYDERABAD-1-AD-1",
            "OCI_SUBNET_ID": "subnet",
            "OCI_IMAGE_ID": "image",
            "SSH_PUBLIC_KEY": "ssh-ed25519 public-key",
        }

        with self.assertRaises(creator.ConfigurationError):
            creator.load_settings(values)

    def test_image_validation_requires_ubuntu_2404_and_a1_compatibility(self):
        compute = SimpleNamespace(
            get_image=lambda image_id: SimpleNamespace(
                data=SimpleNamespace(
                    operating_system="Canonical Ubuntu",
                    operating_system_version="24.04",
                )
            ),
            list_shapes=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(shape=creator.SHAPE)]
            ),
        )

        creator.validate_image(compute, settings())

        incompatible = SimpleNamespace(
            get_image=lambda image_id: SimpleNamespace(
                data=SimpleNamespace(
                    operating_system="Canonical Ubuntu",
                    operating_system_version="22.04",
                )
            ),
            list_shapes=compute.list_shapes,
        )
        with self.assertRaises(creator.ConfigurationError):
            creator.validate_image(incompatible, settings())

    def test_instance_request_has_requested_storage_network_and_encryption(self):
        models = SimpleNamespace(
            LaunchInstanceDetails=Model,
            LaunchInstanceShapeConfigDetails=Model,
            InstanceSourceViaImageDetails=Model,
            CreateVnicDetails=Model,
        )
        oci = SimpleNamespace(core=SimpleNamespace(models=models))
        instance = SimpleNamespace(id="instance-id")

        # Capture the actual model instance through a small explicit client below.
        class CapturingCompute:
            request = None

            def launch_instance(self, request):
                self.request = request
                return SimpleNamespace(data=instance)

        compute = CapturingCompute()
        self.assertIs(creator.create_instance(compute, settings(), oci), instance)
        request = compute.request.kwargs
        self.assertEqual(request["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(request["shape_config"].kwargs, {"ocpus": 2, "memory_in_gbs": 12})
        self.assertEqual(
            request["source_details"].kwargs,
            {
                "image_id": "image",
                "boot_volume_vpus_per_gb": 10,
                "boot_volume_size_in_gbs": 100,
            },
        )
        self.assertTrue(request["create_vnic_details"].kwargs["assign_public_ip"])
        self.assertTrue(request["is_pv_encryption_in_transit_enabled"])

    def test_existing_instance_lookup_follows_pagination(self):
        existing = SimpleNamespace(
            display_name=creator.DISPLAY_NAME,
            lifecycle_state="TERMINATED",
        )

        class Compute:
            calls = 0

            def list_instances(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        data=[],
                        headers={"opc-next-page": "next-page"},
                    )
                self.assert_page(kwargs)
                return SimpleNamespace(data=[existing], headers={})

            @staticmethod
            def assert_page(kwargs):
                if kwargs.get("page") != "next-page":
                    raise AssertionError("next page token was not used")

        self.assertIs(creator.find_existing_instance(Compute(), settings()), existing)

    def test_capacity_errors_retry_with_backoff_and_existing_instance_skips_launch(self):
        class ServiceError(Exception):
            def __init__(self):
                self.status = 500
                self.code = "InternalError"
                self.message = "Out of host capacity"

        oci = SimpleNamespace(
            exceptions=SimpleNamespace(ServiceError=ServiceError)
        )
        launched = SimpleNamespace(id="new-instance")
        with (
            patch.object(creator, "find_existing_instance", return_value=None),
            patch.object(
                creator,
                "create_instance",
                side_effect=[ServiceError(), ServiceError(), launched],
            ) as create,
            patch.object(creator.time, "sleep") as sleep,
        ):
            result, created = creator.launch_with_retries(
                object(),
                settings(),
                oci,
            )

        self.assertIs(result, launched)
        self.assertTrue(created)
        self.assertEqual(create.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])

        existing = SimpleNamespace(id="existing-instance")
        with (
            patch.object(creator, "find_existing_instance", return_value=existing),
            patch.object(creator, "create_instance") as create,
        ):
            result, created = creator.launch_with_retries(object(), settings(), oci)

        self.assertIs(result, existing)
        self.assertFalse(created)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
