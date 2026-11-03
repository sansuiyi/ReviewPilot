# Security Policy

ReviewPilot is an open-source tool to help efficiently review and handle pull requests.

This document describes the security policy of the ReviewPilot project.

## Self-Hosted Solutions

When using ReviewPilot with your OpenAI (or other LLM provider) API key, the security relationship is directly between you and the provider. ReviewPilot does not send your code to any servers operated by the project.

Types of self-hosted solutions:

- Locally
- GitHub integration
- GitLab integration
- BitBucket integration
- Azure DevOps integration

## Supported Versions

This section outlines which versions of ReviewPilot are currently supported with security updates.

Security-sensitive dependencies are declared with bounded version ranges in `pyproject.toml`
(`>=patched,<next-major`) so pip consumers can pull security patches on their own cadence;
the exact versions used by this repository's own builds (Docker, CI, uv sync) come from `uv.lock`.

## Reporting a Vulnerability

We take the security of ReviewPilot seriously. If you discover a security vulnerability, please report it privately through GitHub's private vulnerability reporting, which is enabled on this repository:

[**Report a vulnerability**](https://github.com/sansuiyi/ReviewPilot/security/advisories/new)

Please include a description of the vulnerability, steps to reproduce, and the affected ReviewPilot version.

Do not open a public issue for a security report — a public issue discloses the vulnerability before a fix is available.
