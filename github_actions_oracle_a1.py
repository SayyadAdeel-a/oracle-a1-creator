#!/usr/bin/env python3
"""Create one Oracle Cloud A1.Flex instance from a scheduled GitHub Actions run."""

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

REGION = "ap-mumbai-1"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_IN_GBS = 12
BOOT_VOLUME_SIZE_GBS = 100
BOOT_VOLUME_VPUS_PER_GB = 10
DISPLAY_NAME = "free-tier-arm-a1"
MAX_LAUNCH_ATTEMPTS = 3
RETRY_BASE_SECONDS = 30


class ConfigurationError(Exception):
    """Raised when required environment configuration is missing or invalid."""


class RetryExhausted(Exception):
    def __init__(self, error, attempts):
        super().__init__("Retryable launch error after all attempts")
        self.error = error
        self.attempts = attempts


@dataclass(frozen=True)
class Settings:
    user_ocid: str
    tenancy_ocid: str
    fingerprint: str
    private_key: str
    compartment_id: str
    availability_domain: str
    subnet_id: str
    image_id: str
    ssh_public_key: str


def log(message):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {message}", flush=True)


def load_settings(environ=None):
    environ = os.environ if environ is None else environ
    required = (
        "OCI_USER_OCID",
        "OCI_TENANCY_OCID",
        "OCI_FINGERPRINT",
        "OCI_PRIVATE_KEY",
        "OCI_COMPARTMENT_ID",
        "OCI_AVAILABILITY_DOMAIN",
        "OCI_SUBNET_ID",
        "OCI_IMAGE_ID",
        "SSH_PUBLIC_KEY",
    )
    values = {name: environ.get(name, "").strip() for name in required}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigurationError(
            "Missing required GitHub Actions secret(s): " + ", ".join(missing)
        )

    availability_domain = values["OCI_AVAILABILITY_DOMAIN"]
    if not availability_domain.upper().endswith(":AP-MUMBAI-1-AD-1"):
        raise ConfigurationError(
            "OCI_AVAILABILITY_DOMAIN must be the full AD-1 name for ap-mumbai-1 "
            "(for example, <tenancy-prefix>:AP-MUMBAI-1-AD-1)."
        )

    return Settings(
        user_ocid=values["OCI_USER_OCID"],
        tenancy_ocid=values["OCI_TENANCY_OCID"],
        fingerprint=values["OCI_FINGERPRINT"],
        private_key=values["OCI_PRIVATE_KEY"],
        compartment_id=values["OCI_COMPARTMENT_ID"],
        availability_domain=availability_domain,
        subnet_id=values["OCI_SUBNET_ID"],
        image_id=values["OCI_IMAGE_ID"],
        ssh_public_key=values["SSH_PUBLIC_KEY"],
    )


def build_oci_config(settings):
    return {
        "user": settings.user_ocid,
        "tenancy": settings.tenancy_ocid,
        "fingerprint": settings.fingerprint,
        "key_content": settings.private_key,
        "region": REGION,
    }


def set_action_output(name, value):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as output_file:
            output_file.write(f"{name}={value}\n")
    except OSError as error:
        log(f"ERROR: Could not write GitHub Actions output ({error.__class__.__name__}).")


def write_action_summary(message):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write(f"## Oracle A1.Flex creator\n\n{message}\n")
    except OSError as error:
        log(f"ERROR: Could not write GitHub Actions summary ({error.__class__.__name__}).")


def _post_json(url, payload, service_name, expect_json=False):
    try:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            response_body = response.read()
        if expect_json:
            result = json.loads(response_body)
            if not isinstance(result, dict) or not result.get("ok"):
                log(f"{service_name} notification was rejected by the service.")
    except (URLError, TimeoutError, OSError, ValueError) as error:
        log(f"{service_name} notification failed ({error.__class__.__name__}).")


def send_notifications(message):
    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if discord_webhook:
        _post_json(
            discord_webhook,
            {"content": message},
            "Discord",
        )

    telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    telegram_user_id = os.environ.get("TELEGRAM_USER_ID", "").strip()
    if telegram_token and telegram_user_id:
        _post_json(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            {"chat_id": telegram_user_id, "text": message},
            "Telegram",
            expect_json=True,
        )
    elif telegram_token or telegram_user_id:
        log("Telegram notification skipped: both TELEGRAM_TOKEN and TELEGRAM_USER_ID are required.")


def finish(message, exit_code, instance_found=False):
    log(message)
    if instance_found:
        set_action_output("instance_found", "true")
    write_action_summary(message)
    send_notifications(message)
    return exit_code


def find_existing_instance(compute_client, settings):
    page = None
    while True:
        params = {
            "compartment_id": settings.compartment_id,
            "availability_domain": settings.availability_domain,
            "display_name": DISPLAY_NAME,
        }
        if page:
            params["page"] = page
        response = compute_client.list_instances(**params)
        for instance in response.data:
            if instance.display_name == DISPLAY_NAME:
                return instance
        page = response.headers.get("opc-next-page")
        if not page:
            return None


def validate_image(compute_client, settings):
    image = compute_client.get_image(settings.image_id).data
    operating_system = (image.operating_system or "").strip().casefold()
    version = (image.operating_system_version or "").strip()
    if operating_system not in {"canonical ubuntu", "ubuntu"} or not re.fullmatch(
        r"24\.04(?:\.\d+)?", version
    ):
        raise ConfigurationError(
            "OCI_IMAGE_ID must identify an Ubuntu 24.04 image; "
            f"the selected image reports {image.operating_system!r} {version!r}."
        )

    shapes = compute_client.list_shapes(
        compartment_id=settings.compartment_id,
        image_id=settings.image_id,
    ).data
    if not any(shape.shape == SHAPE for shape in shapes):
        raise ConfigurationError(
            f"OCI_IMAGE_ID is not compatible with {SHAPE}; select an Ubuntu 24.04 ARM64 image."
        )


def create_instance(compute_client, settings, oci, opc_retry_token):
    instance_details = oci.core.models.LaunchInstanceDetails(
        availability_domain=settings.availability_domain,
        compartment_id=settings.compartment_id,
        shape=SHAPE,
        display_name=DISPLAY_NAME,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS,
            memory_in_gbs=MEMORY_IN_GBS,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=settings.image_id,
            boot_volume_vpus_per_gb=BOOT_VOLUME_VPUS_PER_GB,
            boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_GBS,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=settings.subnet_id,
            assign_public_ip=True,
            display_name=f"{DISPLAY_NAME}-vnic",
        ),
        metadata={"ssh_authorized_keys": settings.ssh_public_key},
        is_pv_encryption_in_transit_enabled=True,
    )
    response = compute_client.launch_instance(
        instance_details,
        opc_retry_token=opc_retry_token,
    )
    return response.data


def build_launch_retry_token(settings):
    request_identity = {
        "region": REGION,
        "availability_domain": settings.availability_domain,
        "compartment_id": settings.compartment_id,
        "display_name": DISPLAY_NAME,
        "shape": SHAPE,
        "ocpus": OCPUS,
        "memory_in_gbs": MEMORY_IN_GBS,
        "image_id": settings.image_id,
        "boot_volume_size_in_gbs": BOOT_VOLUME_SIZE_GBS,
        "boot_volume_vpus_per_gb": BOOT_VOLUME_VPUS_PER_GB,
        "subnet_id": settings.subnet_id,
        "assign_public_ip": True,
        "is_pv_encryption_in_transit_enabled": True,
        "ssh_public_key_sha256": hashlib.sha256(
            settings.ssh_public_key.encode("utf-8")
        ).hexdigest(),
    }
    canonical_identity = json.dumps(
        request_identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_identity).hexdigest()


def is_capacity_error(error):
    message = " ".join(
        (
            str(getattr(error, "code", "")),
            str(getattr(error, "message", "")),
        )
    ).casefold()
    return any(
        phrase in message
        for phrase in (
            "out of capacity",
            "out of host capacity",
            "no available host",
            "capacity is unavailable",
        )
    )


def is_rate_limit_error(error):
    return getattr(error, "status", None) == 429 or "too many requests" in str(
        getattr(error, "message", "")
    ).casefold()


def is_ambiguous_network_error(error, oci):
    exception_types = (TimeoutError, ConnectionError, URLError)
    oci_exceptions = getattr(oci, "exceptions", None)
    if oci_exceptions:
        exception_types += tuple(
            exception_type
            for name in ("RequestException", "ConnectTimeout")
            if isinstance(
                (exception_type := getattr(oci_exceptions, name, None)),
                type,
            )
        )
    return isinstance(error, exception_types)


def launch_with_retries(compute_client, settings, oci):
    retry_token = build_launch_retry_token(settings)
    for attempt in range(1, MAX_LAUNCH_ATTEMPTS + 1):
        existing = find_existing_instance(compute_client, settings)
        if existing:
            return existing, False

        try:
            return create_instance(
                compute_client,
                settings,
                oci,
                opc_retry_token=retry_token,
            ), True
        except Exception as error:
            is_service_error = isinstance(error, oci.exceptions.ServiceError)
            if is_service_error:
                retryable = is_capacity_error(error) or is_rate_limit_error(error)
                category = "capacity" if is_capacity_error(error) else "rate limit"
            elif is_ambiguous_network_error(error, oci):
                log(
                    "Launch response was ambiguous; checking OCI for the instance "
                    "before considering another attempt."
                )
                existing = find_existing_instance(compute_client, settings)
                if existing:
                    return existing, False
                retryable = True
                category = "network/timeout"
            else:
                raise

            if not retryable:
                raise
            if attempt == MAX_LAUNCH_ATTEMPTS:
                raise RetryExhausted(error, attempt) from error

            delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            log(
                f"Launch attempt {attempt}/{MAX_LAUNCH_ATTEMPTS} hit a {category} "
                f"error; retrying in {delay} seconds."
            )
            time.sleep(delay)

    raise AssertionError("Launch retry loop exited unexpectedly.")


def wait_for_running(compute_client, network_client, settings, instance_id):
    log("Waiting up to five minutes for the instance to reach RUNNING.")
    deadline = time.monotonic() + 300
    state = "PROVISIONING"
    while time.monotonic() < deadline:
        instance = compute_client.get_instance(instance_id).data
        state = instance.lifecycle_state
        log(f"Instance state: {state}")
        if state == "RUNNING":
            break
        if state in {"TERMINATED", "TERMINATING"}:
            return state, None
        time.sleep(min(10, max(0, deadline - time.monotonic())))

    if state != "RUNNING":
        return state, None

    attachments = compute_client.list_vnic_attachments(
        compartment_id=settings.compartment_id,
        instance_id=instance_id,
    ).data
    for attachment in attachments:
        vnic = network_client.get_vnic(attachment.vnic_id).data
        if vnic.public_ip:
            return state, vnic.public_ip
    return state, None


def safe_error_details(error, private_key=""):
    status = getattr(error, "status", None)
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if message:
        details = f"{message}"
    else:
        details = error.__class__.__name__
    if status is not None or code:
        details = f"OCI status={status or 'unknown'}, code={code or 'unknown'}: {details}"
    if private_key:
        details = details.replace(private_key, "[REDACTED]")
    return details


def error_exit_code(error):
    if getattr(error, "status", None) in {400, 401, 403, 404}:
        return 3
    return 4


def main():
    try:
        settings = load_settings()
    except ConfigurationError as error:
        return finish(f"FAILURE: {error}", 3)

    try:
        import oci
    except ImportError:
        return finish("FAILURE: OCI Python SDK is not installed.", 3)

    try:
        config = build_oci_config(settings)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
    except Exception as error:
        return finish(
            f"FAILURE: Could not initialize OCI clients ({error.__class__.__name__}).",
            3,
        )

    log(f"Target: {SHAPE}, {OCPUS} OCPU / {MEMORY_IN_GBS} GB RAM, {REGION}, AD-1")

    try:
        existing = find_existing_instance(compute_client, settings)
        if existing:
            message = (
                "SUCCESS: An instance with the configured name already exists; "
                f"no creation attempted. ID: {existing.id}, state: {existing.lifecycle_state}."
            )
            return finish(message, 0, instance_found=True)

        validate_image(compute_client, settings)
        instance, created = launch_with_retries(compute_client, settings, oci)
    except RetryExhausted as error:
        capacity = is_capacity_error(error.error)
        rate_limited = is_rate_limit_error(error.error)
        label = (
            "capacity"
            if capacity
            else "rate-limit"
            if rate_limited
            else "network/timeout"
        )
        details = safe_error_details(error.error, settings.private_key)
        return finish(
            f"FAILURE: OCI {label} error after {error.attempts} launch attempts. "
            f"{details}. Will retry on the next scheduled run.",
            1 if capacity else 2 if rate_limited else 4,
        )
    except ConfigurationError as error:
        return finish(f"FAILURE: {error}", 3)
    except Exception as error:
        return finish(
            f"FAILURE: OCI request failed. {safe_error_details(error, settings.private_key)}",
            error_exit_code(error),
        )

    if not created:
        message = (
            "SUCCESS: An instance with the configured name already exists; "
            f"no creation attempted. ID: {instance.id}, state: {instance.lifecycle_state}."
        )
        return finish(message, 0, instance_found=True)

    try:
        state, public_ip = wait_for_running(
            compute_client,
            network_client,
            settings,
            instance.id,
        )
        if state in {"TERMINATED", "TERMINATING"}:
            message = (
                f"FAILURE: Instance {instance.id} was created but is now {state}. "
                "No further creation attempts will be made automatically."
            )
            return finish(message, 4, instance_found=True)
    except Exception as error:
        state = "unknown"
        public_ip = None
        log(
            "WARNING: Instance launch succeeded, but its final state could not be "
            f"verified ({safe_error_details(error, settings.private_key)})."
        )

    details = [
        "SUCCESS: Oracle A1.Flex instance created.",
        f"Instance ID: {instance.id}",
        f"Name: {instance.display_name}",
        f"Availability domain: {instance.availability_domain}",
        f"Shape: {SHAPE} ({OCPUS} OCPU, {MEMORY_IN_GBS} GB RAM)",
        f"Boot volume: {BOOT_VOLUME_SIZE_GBS} GB, {BOOT_VOLUME_VPUS_PER_GB} VPU/GB",
        f"Lifecycle state: {state}",
    ]
    if public_ip:
        details.extend(
            (
                f"Public IPv4: {public_ip}",
                f"SSH: ssh -i <private-key-file> ubuntu@{public_ip}",
            )
        )
    else:
        details.append("Public IPv4: not available yet; check the OCI Console.")
    details.append("The GitHub Actions workflow will be disabled automatically.")
    return finish("\n".join(details), 0, instance_found=True)


if __name__ == "__main__":
    sys.exit(main())
