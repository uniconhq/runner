# The clone image, ghcr.io/uniconhq/clone: what the CI's two checkout steps
# run. It is the CI's own clone plugin and one line of system git config,
# lfs.storage = /lfs-cache, which points git-lfs's object store at whatever
# volume the step mounts there. The CI's answer mounts one per org, so a
# dataset is downloaded once per org and machine, and no org's checkout is
# served an object another org's brought. The image names no volume, and runs
# as root as plugin-git does, so a volume Docker makes on first use, which
# belongs to root, needs no preparing. The setting lives in the image and not
# on the step, because a clone step given an environment block stops counting
# as a plugin and is no longer lent the credential it clones with. Nothing
# else changes: this image is trusted by the CI and runs every checkout of
# every grading run.
# Built from the repo root: docker build -f images/clone/Dockerfile .

# woodpeckerci/plugin-git 2.10.1, by the digest of its multi-platform index,
# looked up 2026-09-30 with `docker buildx imagetools inspect`.
FROM woodpeckerci/plugin-git:2.10.1@sha256:550075c844fcec0af41d9d28f9860aa5d8c43e1e9419be4b9266dc02c2f29804

LABEL org.opencontainers.image.title="unicon-clone" \
      org.opencontainers.image.description="The CI's clone plugin with git-lfs objects kept in /lfs-cache." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

RUN git config --system lfs.storage /lfs-cache
