# Publishing Helm Jetson Alpine

Publishing source and distributing a flashable image have different release
requirements. Keep the repository private until the source checks and owner
decisions below are complete.

## Source publication

1. Choose and add the license for Diode-owned code. Review
   [component licensing](../THIRD_PARTY.md), including the existing GPL overlay,
   loader patch, and retained NVIDIA version metadata.
2. Run CI and the local source/secret checks. Inspect any findings rather than
   globally disabling a detector. Never add signing keys, device dumps,
   authorized-key files, firmware archives, or local credentials to Git.
3. Review all refs and history, not only the latest checkout. Private
   infrastructure references removed from current files may remain in older
   commits. If those references must stay private, sanitize a separate clone,
   review the result, and coordinate replacement of published refs with every
   collaborator. Rewriting history changes commit IDs; a normal cleanup commit
   does not remove old content.
4. Inspect existing Actions logs, release assets, issues, and other repository
   content before changing visibility. GitHub makes Actions history and logs
   public with the repository. Remove historical content only after reviewing
   exactly what will be removed and retaining any needed private records.
5. Protect `main` with required CI, reviewed pull requests, stale-review
   dismissal, and disabled force pushes/deletion. Keep the Actions token
   read-only and disable Actions approval of pull requests.
6. Verify GitHub secret scanning and push protection are enabled. Public
   repositories can use these features for free; enabling them on private
   organization repositories may require a paid GitHub Secret Protection
   entitlement. The CI secret scan can run before publication.
7. Enable private vulnerability reporting when GitHub makes it available for
   the public repository, and verify the reporting route in
   [SECURITY.md](../SECURITY.md). Require approval for Actions runs from outside
   contributors before executing their workflow changes.
8. Keep the experimental warning and private build prerequisites visible.
   Source access alone does not provide the internal native loader or seed.

GitHub documents the effects of
[changing visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
and [securing a repository](https://docs.github.com/en/code-security/getting-started/quickstart-for-securing-your-repository).

Run the same source and secret checks locally before committing:

```sh
brew install gitleaks
python3 tools/check-public-source.py --self-test
python3 tools/check-public-source.py
gitleaks git --config .gitleaks.toml --log-opts="--all --full-history" \
  --redact=100 --no-banner --no-color --ignore-gitleaks-allow .
```

The privacy check reads current tracked files, so stage new publication files
explicitly before running it. The Gitleaks command scans committed history;
CI runs after the proposed source changes are committed. Review staged diffs
as well as scanner results.

### Historical copies on GitHub

A force-push replaces branch history but does not guarantee removal of old
commit URLs, cached views, forks, or collaborators' clones. GitHub documents
these limits in
[removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
Rewriting signed commits also removes their original valid signatures.

For a repository that has always been private, publishing a new repository
containing only the verified clean history while keeping the original private
avoids carrying that original repository's cached history into the public
release. Push only the intended cleaned branch, and verify the new repository
before making it public. Existing collaborators still need to use the clean
history so they do not reintroduce old commits.

## Firmware and public browser rollout

Do not describe the current preview as a production-ready five-SKU release.
Before distributing flashable images or opening a public flasher:

- Complete the exact-component redistribution review in
  [THIRD_PARTY.md](../THIRD_PARTY.md) and package the required notices/source.
- Provide an authorized, accessible acquisition or generation path for every
  build input, or explain the remaining build limitations accurately.
- Verify the BSP archive against a trusted supplier digest. Record all resolved
  package versions and artifact hashes; the current Alpine resolution floats.
- Qualify each advertised SKU and host/browser combination on hardware. Record
  RAM boot, NVMe installation, QSPI backup/write/readback, cold boot, and
  interruption/recovery behavior. Synthetic tests do not qualify hardware.
- Decide the production physical-access policy and remove debug root bypasses
  where that policy requires it. Use the fresh recovery environment, keep the
  one-board requirement, and ensure users back up needed data before flashing.
- Use a dedicated HTTPS origin with restricted deployment credentials and
  reviewed immutable artifacts. The frontend and same-origin catalog are
  trusted code/data that determine which firmware is installed.

Rebuild recovery bundles after installer or rootfs changes; an existing
generated bundle still contains the old code until it is rebuilt.
