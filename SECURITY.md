# Security policy

## Reporting a vulnerability

Please do not open a public issue containing credentials, private deployment URLs, or exploit details.

Report security concerns privately through the maintainer's GitHub profile. Include the affected version, reproduction steps, impact, and any suggested mitigation. You should receive an acknowledgement within seven days.

## Credential exposure

If a Bright Data API key or zone credential is exposed:

1. Revoke or rotate it immediately in Bright Data account settings.
2. Replace it in every local and hosted environment.
3. Remove it from Git history before publishing or distributing the repository.
4. Review Bright Data usage for unexpected requests.

Deleting a credential only from the latest commit is not sufficient because previous Git objects remain accessible.

## Deployment boundary

This server authenticates outbound Bright Data API calls. It does not provide inbound client authentication. Internet-facing deployments must be protected by an authenticated gateway, reverse proxy, or platform access control.
