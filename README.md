# Oracle Cloud A1.Flex Auto-Creator

This repository contains a GitHub Actions workflow that attempts to create one
Oracle Cloud Infrastructure (OCI) `VM.Standard.A1.Flex` instance. It retries
capacity and rate-limit errors, checks for an existing instance before
launching, and disables its own workflow after an instance is found or
successfully launched.

OCI capacity is not guaranteed. Charges may apply if your tenancy, region,
resource usage, or boot-volume settings are outside current Always Free
eligibility. Review OCI pricing and your tenancy's usage before enabling this
workflow.

## Provisioned configuration

The target values are set in `github_actions_oracle_a1.py`:

| Setting | Value |
|---|---|
| Region | `ap-mumbai-1` (Mumbai) |
| Availability domain | AD-1 in Mumbai |
| Shape | `VM.Standard.A1.Flex` |
| OCPUs | 2 |
| Memory | 12 GB |
| Image | Ubuntu 24.04, ARM64 (verified before launch) |
| Boot volume | 100 GB, 10 VPU/GB |
| In-transit encryption | Enabled |
| Public IPv4 | Enabled on the instance VNIC |

The selected subnet must be public and have a route to an Internet Gateway for
the public IP to be reachable. Configure subnet security rules separately; for
SSH, allow TCP port 22 only from trusted source addresses.

## Setup

### 1. Prepare OCI

1. In the Mumbai home region, create or select a compartment, VCN, public
   subnet, SSH key pair, and an Ubuntu 24.04 ARM64 image.
2. Copy the full availability-domain **name** for Mumbai AD-1. It is usually
   formatted like `<tenancy-prefix>:AP-MUMBAI-1-AD-1`; it is not an OCID.
3. Create an OCI API signing key for the OCI user and record its fingerprint.
   Keep the private key private.
4. Ensure the OCI user belongs to a group with the following permissions in
   the target compartment (replace the placeholders):

   ```text
   Allow group <group-name> to manage instance-family in compartment <compartment-name>
   Allow group <group-name> to use virtual-network-family in compartment <compartment-name>
   ```

   `manage instance-family` permits listing instances, checking images and
   compatible shapes, and launching an instance. `use virtual-network-family`
   permits using the subnet and looking up the instance VNIC.

### 2. Add GitHub Actions Secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and
add the required secrets:

| Secret | Value |
|---|---|
| `OCI_USER_OCID` | OCI API-signing user OCID |
| `OCI_TENANCY_OCID` | Tenancy OCID |
| `OCI_FINGERPRINT` | Fingerprint of that user's API signing key |
| `OCI_PRIVATE_KEY` | Full contents of the OCI API signing private-key PEM |
| `OCI_COMPARTMENT_ID` | OCID of the target compartment |
| `OCI_AVAILABILITY_DOMAIN` | Full Mumbai AD-1 name, e.g. `<prefix>:AP-MUMBAI-1-AD-1` |
| `OCI_SUBNET_ID` | OCID of the public subnet |
| `OCI_IMAGE_ID` | Mumbai-region OCID of an Ubuntu 24.04 ARM64 image |
| `SSH_PUBLIC_KEY` | Public SSH key to install for the `ubuntu` user |

The region is fixed to `ap-mumbai-1` in the script, so no `OCI_REGION` secret
is used. The script checks the selected image's Ubuntu version and whether it
supports `VM.Standard.A1.Flex` before attempting a launch.

Optional success/failure notifications:

| Secret | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord webhook URL |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_USER_ID` | Telegram chat/user ID |

Set both Telegram secrets to enable Telegram notifications. Do not put an OCI
private key or SSH private key in source code, workflow YAML, logs, or the
repository. GitHub Secrets inject the OCI API private key only at runtime. The
SSH private key stays with you; only its public key is supplied as
`SSH_PUBLIC_KEY`.

### 3. Enable the workflow

Enable **Actions** for the repository, then run **Oracle A1.Flex Instance
Creator** manually once from the Actions tab. Scheduled runs use the default
branch and occur every 10 minutes (`*/10 * * * *`); GitHub may delay scheduled
events during high load. The workflow has a repository-wide concurrency group
to prevent overlapping runs.

The Actions workflow needs these repository permissions:

- `contents: read` to check out the source.
- `actions: write` so it can disable itself after an instance is found or
  created.

Once an instance is found or launched, the workflow disables itself. If GitHub
cannot disable it, the disable step fails visibly; disable the workflow
manually in the Actions tab. The script also checks for the named instance
before every launch attempt, so subsequent runs do not create another instance
while that OCI resource is present.

## Retry and result behavior

- Capacity and HTTP 429 errors get up to three launch attempts per run, with
  30-second then 60-second delays. If they persist, the run fails clearly and
  the next scheduled run can try again.
- Every attempt for the same launch configuration uses a deterministic OCI
  `opc_retry_token`. After a network or timeout error, the script checks OCI
  for the instance before retrying; if it is not visible, any retry reuses that
  token so OCI can deduplicate an accepted request whose response was lost.
- Other OCI errors are reported without retrying, to avoid repeating invalid
  requests or authentication failures.
- Existing resources with the configured name in the configured compartment
  and AD are treated as success, including resources that are terminating or
  terminated. This prevents a second launch if the workflow is manually
  re-enabled.
- A successful launch is monitored for up to five minutes. If OCI has not
  assigned a public IP yet, the success message directs you to the OCI Console.
- Success/failure is written to the Actions log and job summary. Discord and
  Telegram notifications are optional.

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | Instance launched or matching instance already exists |
| `1` | Capacity retries exhausted |
| `2` | Rate-limit retries exhausted |
| `3` | Missing/invalid configuration, OCI SDK, or authentication setup |
| `4` | Other OCI failure |

## Validate locally

The focused unit tests use mocks and do not require OCI credentials or the OCI
SDK:

```bash
python -m unittest discover -s tests -v
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Missing secret / configuration error | Confirm every required secret is present and non-empty. Check that the AD name ends in `:AP-MUMBAI-1-AD-1`. |
| `401 NotAuthenticated` | Verify user OCID, tenancy OCID, fingerprint, and that `OCI_PRIVATE_KEY` matches the API key registered for that user. |
| `NotAuthorizedOrNotFound` / 404 | Verify compartment, subnet, image OCIDs and the OCI policy grants above. The OCI region is fixed to Mumbai. |
| Image validation fails | Choose a Mumbai image reporting Ubuntu `24.04` (or `24.04.x`) and compatible with `VM.Standard.A1.Flex` (ARM64). |
| Capacity or 429 failure | Expected in busy regions; review the run log. The script retries twice after the initial attempt and scheduled runs continue every 10 minutes. |
| Instance has no public IP | Confirm the subnet is public, public IPv4 assignment is allowed, and its route table uses an Internet Gateway. |
| SSH connection fails | Confirm the instance is `RUNNING`, the subnet security list/NSG allows TCP 22 from your IP, and use the matching local SSH private key. |
| Workflow did not stop after success | Check the “Disable workflow” step and repository Actions permissions. If needed, disable it manually; the instance lookup remains a duplicate guard. |

## License

MIT License — see [LICENSE](LICENSE).
