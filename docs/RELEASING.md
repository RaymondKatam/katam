# Releasing Katam (maintainer notes)

Releases are built by GitHub Actions (`.github/workflows/release.yml`). You push a tag; it builds
the packages and ISO, publishes a GitHub Release, and updates the signed apt repository and the
`releases.json` update feed on GitHub Pages.

## One-time setup

1. **Create a public GitHub repository** (e.g. `katam`) and push this folder to it.
2. **Create a signing key** on your own machine, kept offline and backed up:
   ```bash
   gpg --quick-gen-key "Katam Release Signing <you@example.com>" ed25519 sign 3y
   gpg --armor --export-secret-keys you@example.com > katam-signing-private.asc   # keep safe
   ```
3. **Add repository secrets** (Settings > Secrets and variables > Actions):
   - `KATAM_GPG_PRIVATE_KEY`: contents of `katam-signing-private.asc`
   - `KATAM_GPG_KEY_ID`: `you@example.com`
4. **Enable Pages:** Settings > Pages > Deploy from a branch > `gh-pages` / root (the first release creates the branch).
5. **Turn on automatic releases:** under Settings > Secrets and variables > Actions > Variables, add `KATAM_AUTO_RELEASE` = `true`. Until you do, pushing a tag builds nothing, so a hand-published release is never overwritten.
6. **Edit the placeholders** `RaymondKatam` in `website/download.html`, and set the feed URL in `config.example.yaml` (`updates.feed_url: https://RaymondKatam.github.io/katam/releases.json`).

## Cutting a release

```bash
# update release/notes.md with what changed, commit, then:
git tag v0.5.1
git push origin v0.5.1
```

The workflow runs for about 30-60 minutes. Tags with a suffix (`v0.4.0-beta1`) publish to the
**beta** channel and are marked pre-release; plain tags publish to **stable**.

Results:
- `https://github.com/RaymondKatam/katam/releases`: ISO, `.sha256`, package manifest, `.deb` files
- `https://RaymondKatam.github.io/katam/apt`: signed apt repository (servers update from here)
- `https://RaymondKatam.github.io/katam/releases.json`: update feed (controllers and your blog read it)
- `https://RaymondKatam.github.io/katam/`: copy of the download page

## Your blog

Either link to the GitHub Pages download page, or paste `website/download.html` into your blog.
It reads `releases.json`, so the version, download link and checksum update by themselves.
Don't host the ISO on your blog server: GitHub Releases serves it for free (files up to 2 GB).

## Breaking changes

If a release can't be upgraded to directly, pass `--min-upgrade-from X.Y.Z` to
`release/make-feed.py` in the workflow; controllers on older versions will say which version to
upgrade to first and hide the Upgrade button.

## Before the first public release

- The project is licensed Apache-2.0 (`LICENSE`, `NOTICE`). To change it, replace both files before others start contributing.
- The ISO is a modified Ubuntu system. Canonical's intellectual property policy governs use of
  the Ubuntu name and logos in derivatives; read it at ubuntu.com/legal and adjust branding if needed.
- The ISO contains GPL software from Ubuntu. Each release includes a package manifest; the matching
  sources are in the Ubuntu archive (`apt-get source <package>`).
- If you're employed, check your employer's policy on publishing personal open-source projects.
