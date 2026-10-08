# Runbooks in Git

A starter repository for Katam runbooks managed with GitOps. Copy this folder into a new Git
repository (GitHub, GitLab, Gitea or Forgejo).

1. **Point Katam at the repository** in `/etc/katam/config.yaml` on every controller, then
   `sudo systemctl restart katam`:

   ```yaml
   runbooks:
     git:
       url: git@github.com:your-org/katam-runbooks.git   # or https://...
       branch: main
       path: runbooks
       ssh_key: /var/lib/katam/.ssh/runbooks_deploy        # read-only deploy key
       # token_file: /etc/katam/git-token                  # instead, for private HTTPS repos
       webhook_secret: <openssl rand -hex 24>
       interval_s: 300                                     # pull at least this often
   ```

   Create the deploy key with `sudo -u katam ssh-keygen -t ed25519 -N '' -f /var/lib/katam/.ssh/runbooks_deploy`
   and add the `.pub` file to the repository as a read-only deploy key.

2. **Add a webhook** so pushes sync at once: payload URL `https://<katam>/api/v1/hooks/git`,
   content type `application/json`, secret = `webhook_secret`, event = push. GitHub, GitLab
   (secret token), Gitea and Forgejo are supported.

3. **Use the CI pipeline** (`.github/workflows/runbooks.yml` or `.gitlab-ci.yml`): pull requests
   are linted offline and validated against the live cluster; merges to `main` sync Katam.

Rules Katam applies on every sync:

- Each `*.yaml` / `*.yml` under `path` is one runbook; its `name:` is the runbook's name.
- One invalid file fails the whole sync and nothing changes, so a typo never half-applies.
- Runbooks from Git are read-only in the UI and API. Change them with a commit.
- A runbook deleted from the repository is deleted from Katam (`prune: false` keeps it).
- Runbooks created in the UI are left alone.
- Every run of a Git runbook records the file and commit it came from.

Lint locally before pushing:

```bash
pip install "git+https://github.com/RaymondKatam/katam#subdirectory=controller"
katamctl runbook lint runbooks/
```
